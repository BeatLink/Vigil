# Development Notes

Architectural decisions and non-obvious rationale for Vigil, organized by topic.
Code comments stay short (≤1 sentence); anything that needs more explanation
lives here. This documents the architecture as it *is* — read it before changing
the engine, the plugin contract, or the UI polling model.

## Single process, one event loop

Vigil runs as one OS process. `main()` (`vigil/__main__.py`) builds one
`VigilEngine` (the Coordination Engine, `core/coordination/engine.py`), calls
`setup_modules()` to load plugins, then hands the engine to `init_gui()`
(`core/ui/main_dashboard.py`). The per-monitor polling loop and the NiceGUI web
dashboard share the same asyncio event loop and the same live `Plugin`
instances — there is no IPC between "collecting" and "displaying". A plugin's
`render_ui()` reads through `self.data` (its read-only `PluginDataView`), which
is the same projection of the database the polling loop just wrote to.

## Engine model

`core/` is organized as named engines the Coordination Engine (`VigilEngine`)
owns and wires together:

- **Settings Engine** (`core/settings/`) — `ConfigFileManager` loads and types
  `config.yaml` (`config_schema.py`).
- **Database Engine** (`core/database/`) — `DatabaseManager` is the single
  reader/writer over SQLite. Models live in `models.py`, read-result dict shapes
  in `rowtypes.py`.
- **Connector Engine** (`core/connectors/`) — all external IO. One
  `ConnectorEngine` routes a plugin's heterogeneous request list
  (`Command` / `HttpRequest` / `DnsQuery` / `PingRequest`) to the right
  sub-connector (`ssh_connector` / `http_connector` / `dns_connector` /
  `icmp_connector`).
- **UI Engine** (`core/ui/`) — the NiceGUI dashboard. Reads only through
  `plugin.data` or the Database Engine; it never reaches into a plugin's IO.
- **Exporter Engine** (`core/exporters/`) — Prometheus pull render + InfluxDB
  push task.

`EngineLike` (`core/contracts.py`) is the narrow slice of `VigilEngine` that the
UI and plugins actually call, so those modules — and test doubles — depend on a
small contract rather than the concrete class.

## Pure plugins

A `Plugin` exposes **only pure functions and data** — no IO, no persistence
handles. It is constructed with just `(name, config)`. The engine's
`_wire_plugin()` builds the plugin's engine-owned IO (an `SSHContext` for its
target, kept in `engine._net[id]`) and injects a read-only `PluginDataView` as
`plugin.data`. The plugin holds neither a database handle nor a connection.

Collection is:

- `requests() -> List[Request]` then `parse_results(results) -> CollectResult`
  — the declarative path (99% of plugins). Both are pure; the engine executes
  the requests through the Connector Engine and hands the positionally-matched
  results back.
- `commands()` / `parse()` are the SSH-only shorthand `requests()` /
  `parse_results()` default to, so an SSH plugin overrides only those.
- `io_call()` is the escape hatch for genuinely sequential/conditional local IO
  the request list can't express (e.g. `ddns_updater`: fetch public IP → resolve
  DNS → compare → maybe push). It returns a zero-arg closure the engine runs off
  the event loop.

A plugin never persists anything itself. Its `parse_results()` returns one
`CollectResult` describing everything to write (metrics, logs, log lines,
status, a snapshot, settings); the engine persists it. Out-of-cycle writes (a
push heartbeat from the REST endpoint, a group's expand/collapse state) and
job-row updates route back through the engine surface — `engine.apply`,
`engine.set_setting`, `engine.create_job`/`finish_job`/etc.

## The write path: `db.apply_result`

There is no per-plugin writer object. The engine holds each plugin's identity
`(target, id, name)` and persists a `CollectResult` with a single call:

```
self.db.apply_result(plugin.target, plugin.id, plugin.name, result)
```

(via the engine's one-line `_apply()` helper). `DatabaseManager.apply_result`
is the *only* place that translates the plugin-facing `CollectResult` contract
into table-level writes — it fans the result out to `insert_metric`,
`write_event` (which prefixes `[plugin_name]` for the events feed),
`insert_log_line`, `insert_status`, `set_snapshot`, and `set_setting`.

`PluginDataView` (`core/coordination/data_view.py`) is the read half: it scopes
the plugin-id-keyed reads (`latest_metric`, `latest_snapshot`, `get_setting`)
plus the handful of other reads the UI tables need, calling `DatabaseManager`
directly. Every method on it is a read — there is deliberately no `apply`. This
read/write split is what keeps plugins and the UI unable to reach the write path
except through the engine.

## Interface contracts

Most seams here were originally duck-typed: caller and callee agreed on a dict
shape or callback signature by convention. Where a contract crosses more than one
module it's now named once — as a `Protocol`, `TypedDict`, or type alias — rather
than reimplemented per call site. This is documentation for readers and type
checkers, **not** runtime validation: YAML config and `UI_SPEC` dicts are still
unchecked at the boundary (bad data fails at first use, not at load).

`core/contracts.py` holds the cross-subsystem contracts:
- `RefreshCallback` — "a sync function, or one returning an awaitable", the
  signature `PluginPage._tick`, `safe_timer`, and `on_data_event` all share.
- `MetricsSource` — the two-method read-only slice of `DatabaseManager`
  (`latest_metrics`, `latest_statuses`) the exporters and REST API need.
- `PushablePlugin` — a `runtime_checkable` Protocol for the `token`/`record_push`
  surface `plugins/push.py` adds; `api.py` narrows via `isinstance` instead of
  importing the concrete class.
- `EngineLike` — the narrow engine surface the UI and plugins call.
- `ActionButtonSpec` — one entry in `Plugin.get_actions()`, the header action
  buttons rendered regardless of `UI_SPEC`.

Other typed boundaries: `core/ui/spec_types.py` (`UISpec` and its nested card /
table / dialog / job-panel shapes that `generic_render` interprets),
`core/settings/config_schema.py` (`config.yaml`'s shape — intentionally not
exhaustive per plugin type), `core/database/rowtypes.py` (`DatabaseManager`'s
dict-returning reads), and `core/connectors/types.py` (the
`plan_action`/`dispatch_action` discriminated union the engine walks with
`isinstance`).

## Polling loop

Each monitor sleeps its own `interval` between cycles — a 30s monitor is never
rounded up to a slower one's schedule — with a random startup stagger
(`STARTUP_JITTER_SECONDS`) so they don't all fire in the same event-loop
iteration at boot. Exceptions are caught per-iteration, so one crashing monitor
never stops its own future polls or anyone else's. Group plugins get a loop too;
they re-read live child status from the DB each cycle.

`run_cycle_now` is single-flight per plugin (`_collecting[id]`), shared by the
scheduler and any out-of-band (dashboard-triggered) collection, so a slow cycle
never overlaps itself.

Two monitors resolving to the same effective `id` (falls back to display name
when config omits it) would silently overwrite each other's status/metrics/logs
every cycle, since everything is keyed by `id`. Startup detects and logs this
once, loudly.

## SSH transport

`core/connectors/ssh_connector.py` uses asyncssh rather than shelling out to the
system `ssh` client: one native connection per physical target, each command a
channel on it rather than a forked process. `ConnectorEngine` pools one
`SSHConnection` per `(host, port, username, key_path)`; a plugin's per-target
handle is a small `SSHContext` value, so the engine itself stays a stateless
singleton.

Three behaviors of the old subprocess design are reproduced deliberately, each
verified against a real sshd:

1. **Killing a remote process.** asyncssh's `process.close()` does not reliably
   terminate a remote command, so every timeout path explicitly `terminate()`s
   then `kill()`s.
2. **Host-key trust.** `known_hosts=[]` (not `None`) makes
   `_TofuClient.validate_host_public_key` fire, reproducing
   `StrictHostKeyChecking=accept-new`: trust and persist a host's key on first
   use, reject any later mismatch.
3. **Per-host channel limits.** Vigil bounds its own per-host concurrency
   (`_MAX_CONCURRENT_PER_HOST = 8`, a semaphore) below sshd's default
   `MaxSessions` of 10.

## Job control

A long-running job (a borg backup) is **not** a live SSH channel held open for
hours. It is launched *detached on the target* with one ordinary command
(`setsid sh -c '…' &`), writing its output to `<workdir>/out` and its exit code
to `<workdir>/exit`, and is then advanced by ordinary polling commands on the
owning plugin's normal monitor cycle. The builders/parsers in `ssh_connector.py`
(`launch_command`/`parse_launch`, `poll_command`/`parse_poll`, `cancel_command`,
`split_lines`) are pure — no runtime, no coroutine, no IO. One poll round-trip
returns output size, exit code (empty if running), liveness (`kill -0`), and any
output past the byte offset the last poll consumed (`Job.output_seq`).

Because the job lives on the target it **survives a Vigil restart**:
`reconcile_orphaned_jobs` only fails rows with no `pid` (crashed before launch);
any row with a pid is re-adopted by the owning plugin's next poll (pid alive →
resume; pid/exit gone → finalize).

## SQLite: writer, reader, caching, retention

**One background writer thread** (`_AsyncWriter`) owns all writes. The polling
loop and UI run on the asyncio event loop; committing to SQLite fsyncs, which can
block noticeably (especially on ZFS) and would stall the async server if done
inline. Writes are enqueued (non-blocking for the caller) and batched: the thread
commits whatever arrives within `batch_window` seconds as one transaction. This
is a durability trade-off — a crash can lose the in-memory batch — and the UI
observes writes on its own poll cadence, not on commit.

**Reads run on executor threads** (`offload()` in `core/ui/components.py`) so a
blocking query never runs inline on the loop. The read path uses the `_reader()`
context manager, **not** peewee's `db.connection_context()`: the latter's
`__exit__` unconditionally `close()`s, so every read on a recycled executor
thread would re-`connect()`. `_reader()` opens the thread-local connection if
closed but leaves it open, so successive reads on the same thread reuse the warm
connection. (Pooled threads live for the process, so the open connections are
freed at exit — nothing to close explicitly. Writes on the caller thread keep
`connection_context()`.)

`read_fn` handed to `offload()` must be pure IO with no NiceGUI element access
(it runs off the loop); element updates (`.rows = …`, `.update()`) are not
thread-safe and must be applied after awaiting, back on the loop.

**Short read caches.** `_cached()` memoizes read-heavy queries
(`latest_metric_cached`, `metric_history_cached`, `recent_events_cached`, …) for
~1s per unique key, because the overview, every plugin page, and every expanded
group child poll roughly once a second and frequently want the identical rows.
Cache `max_age` should not drop below the writer's batch window — polling faster
than a write can land surfaces nothing fresher. `recent_events()` itself is left
uncached (the REST API shares it and expects a live read); the Events page caches
around it at the call site.

**Indexing.** The hot metric read path filters on `(collector, metric_name)` and
orders by `timestamp DESC`. `Metric` carries a composite index
`(collector, metric_name, timestamp)` for exactly that (the leading `collector`
prefix also serves the collector-only query). Fresh DBs get it from
`create_tables`; existing DBs get it from `_migrate`.

**Retention.** Metrics and a status row are written on every poll of every
plugin, so both tables grow unbounded without pruning. `prune_metrics` /
`prune_status` (metric-retention window) run alongside `prune_logs` /
`prune_jobs` (log-retention window) on the periodic prune loop; `prune_status`
always keeps the newest row per collector so a plugin's current state is never
pruned away. Retention windows are independent — see `logging.retention_days`
and `logging.metric_retention_days` in `config.yaml` (the latter defaults to the
former when unset). `0` disables a window (keep forever).

**Migrations.** `create_tables` only creates missing tables, never alters an
existing one, so a column or index added to a model appears on fresh installs
but not upgraded databases (where queued inserts would then fail silently).
`_migrate` backfills each additive, idempotent change on every start, with no
version bookkeeping.

**Snapshots.** `PluginSnapshot` exists because a `Metric` row carries one named
number — wrong for a process list or systemd unit list, where every row matters.
A plugin with row-level data returns a `snapshot` in its `CollectResult` (written
via `set_snapshot`) once per cycle and reads it back with `latest_snapshot`.

**Log-line dedup** uses a `UNIQUE dedup_hash` (target/source/log_time/message)
with `on_conflict_ignore`, since collectors re-fetch the same trailing lines
every cycle — dedup is enforced by the DB with no read on the hot path.

## UI refresh: polling, not push

The UI polls the Database Engine; it does **not** subscribe to write
notifications. There is no event bus — the writer commits batches and does
nothing else. Every widget refreshes by re-reading (cached) DB state on its tick
and applying the result only when it changed (`refresh_rows` and the inline
equality checks in `history_chart`, the sidebar tree, `update_charts`). This
trades sub-second push latency for a much simpler model with no cross-thread
subscription lifecycle to leak; the ~1s read-cache TTLs mean polling faster than
a write can land surfaces nothing fresher anyway.

### One timer per client

`_PageScheduler` (`core/ui/model.py`) drives every *tickable* registered for a
NiceGUI client from a **single** `safe_timer`, ticking at the fastest interval
any tickable asked for. A tickable is anything with `_tick()` and `_detached()`:

- A `PluginPage`, which refreshes a plugin detail page's cards/charts/tables.
- A `_CallbackTick`, wrapping a bare refresh callback registered via
  `on_data_event` / `schedule_callback` — used by the overview and events pages.

Both ride the one timer, so the overview's sidebar/events/charts refreshes and a
plugin page's widgets are all coalesced. This is what keeps a group's refresh
cost from scaling with how many children are expanded — plugins have no idea
whether they're standalone or one of many expanded children.

`PluginPage.start()` does one synchronous refresh immediately before
registering, so a freshly loaded page shows real data rather than its
constructed defaults (`--`, empty tables) until the first tick. `run_now=False`
on a callback defers only its first tick.

## Safe timers and teardown

NiceGUI resolves a `ui.timer`'s context *outside* the callback and raises "The
parent slot of the element has been deleted." as soon as the client disconnects
or the page re-renders — in NiceGUI's own task, so a try/except around the
callback never sees it, and it floods the log every tick. `_SafeTimer`
(`core/ui/components.py`) overrides `_should_stop` so detachment is an ordinary
stop condition. `_detached()` checks `is_deleted` / `client.elements` rather than
`parent_slot`, which only raises later — the very raise this class avoids.

`safe_timer`'s `defer_first=True` skips `ui.timer`'s inline first call (which
otherwise runs during widget construction, before the page paints) and fires on
the next loop tick instead.

## Bindings

Vigil uses NiceGUI's reactive `.bind_*()` only for `render_status_card`'s label
text (`label.text` is a real `BindableProperty` with an on-change push hook).
Everything else — `ui.table.rows`, `ui.echart.options`, label colors — has no
such hook (verified against NiceGUI's source), so it refreshes through the shared
per-page timer via `page.on_refresh()`, setting values and calling `.update()`
explicitly. `binding_refresh_interval` is slowed to 2s rather than disabled, in
case a future widget starts binding.

## NiceGUI routing quirk

The dashboard is an explicit `@ui.page('/')` route, not NiceGUI's auto-index. In
NiceGUI 3.x the auto-index re-executes the main script via
`runpy.run_path(sys.argv[0])`; under the Nix wrapper `sys.argv[0]` is a shell
script, so that parse fails with a `SyntaxError` and every request 500s.

## Per-client navigation state

Navigation state is created per client inside `index_page()`. `init_gui` runs
once at startup, but each browser connection builds its own element tree —
sharing one dict at module scope meant a new tab overwrote the previous tab's
render callback, raising "the client this element belongs to has been deleted."
Rebinding `_navigation_state['switch_func']` per client keeps `navigate_to`
pointing at a live element tree (the most recently connected client).

## Auth middleware ordering

HTTP Basic Auth (`register_auth`) is registered before the routes it protects.
Starlette middleware wraps the whole app regardless of registration order, but
registering it early keeps intent obvious: everything that follows is gated.

## Declarative UI spec

Most plugins reduce to the same shape: a few metric cards with a formatter, a
layout grid, a chart, an events table. A plugin declares that shape as a
`UI_SPEC` dict and calls `spec.generic_render()` from `render_ui()`, instead of
hand-writing widgets. Only genuinely bespoke plugins (`group`,
`systemd_service`) keep a full hand-written `render_ui()`.

Format/color/predicate functions are referenced by name from module registries
(`FORMATTERS`, `COLOR_RULES`, `ITEM_FORMATTERS`, `ITEM_COLOR_RULES`,
`ENABLED_PREDICATES`) rather than inlined, so a spec stays pure, serializable
data. A plugin needing a one-off transform registers it under its own key
(`register_formatter` / `register_color_rule` / …).

`core/ui/layout.py` lets `config.yaml` override a plugin's default widget
arrangement two ways: replacing the row structure entirely, or per-widget
overrides (visibility, height, flex) merged onto the default rows — the same
list-or-dict pattern `UI_SPEC['events']` / `['logs']` (`bool | dict`) uses.

## Plugin config & helpers

`plugins/base/plugin_helpers.py` holds `PluginConfigMixin` (the `id` / `target` /
`interval` derivation every plugin needs, via `_init_config`) plus shared pure
helpers (`parse_duration`, `format_duration`, `format_bytes`, `level_for`), split
out so they're reusable without dragging in the rest of `plugin_base.py`.
`parse_duration` accepts plain numbers or strings like `'1w'`, `'7d'`,
`'2h30m'`, `'30s'`, including compound forms like `'1d12h'`.

## Testing

`pytest` (via `nix develop` — there is no bare `python3` on the target dev
environment). Plugin tests exercise the pure `commands()`/`parse()` or
`requests()`/`parse_results()` directly through the `run_cycle` / `run_requests`
/ `run_io_cycle` fixtures in `tests/conftest.py`, which drive a plugin with a
fake connector and apply the resulting `CollectResult` — mirroring
`VigilEngine._run_cycle` without a real event loop. `_FakeEngine` stands in for
the Coordination Engine, and `plugin.storage` is a thin test-only per-plugin
adapter over `db.apply_result` (production has no such object).
