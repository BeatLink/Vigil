# Development Notes

Architectural decisions and non-obvious rationale for Vigil, organized by topic.
Code comments stay short (≤1 sentence); anything that needs more explanation
lives here. This documents the architecture as it *is* — read it before changing
the engine, the plugin contract, or the UI refresh model.

## Single process, one event loop

Vigil runs as one OS process. `main()` (`vigil/__main__.py`) builds one
`VigilEngine` (the Coordination Engine, `core/coordination/engine.py`), calls
`setup_modules()` to load plugins, then hands the engine to `init_gui()`
(`core/ui/main_dashboard.py`). The per-monitor polling loop and the NiceGUI web
dashboard share the same asyncio event loop and the same live `Plugin`
instances — there is no IPC between "collecting" and "displaying". A plugin's
`render_ui()` reads through `self.data` (its read-only `PluginDataView`), which
projects the same in-memory state the polling loop just wrote to.

**State lives in memory.** Collectors write into a `StateStore`
(`core/state/`) of plain Python objects and the UI reads from it directly;
SQLite is a persistence sink behind that, not a read path. Nothing queries the
database while the process runs — it is written to (asynchronously, off the
read path) and read exactly once, at startup, to restore the store.

## Engine model

`core/` is organized as named engines the Coordination Engine (`VigilEngine`)
owns and wires together:

- **Settings Engine** (`core/settings/`) — `ConfigFileManager` loads and types
  `config.yaml` (`config_schema.py`).
- **State Engine** (`core/state/`) — `StateStore` holds the live system of
  record in memory: current state (statuses, snapshots, settings, jobs) whole,
  and history (metrics, events, log lines, job output) in bounded per-stream
  ring buffers sized by `memory:` in `config.yaml`. Record types are in
  `records.py`.
- **Database Engine** (`core/database/`) — `DatabaseManager` fronts the store:
  every read is served from memory, every write updates the store and is then
  mirrored to SQLite on a background thread. Models live in `models.py`,
  read-result dict shapes in `rowtypes.py`.
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
`_wire_plugin()` builds the plugin's engine-owned IO (an `ExecContext` for its
target, kept in `engine._net[id]`) and injects a read-only `PluginDataView` as
`plugin.data`. The plugin holds neither a database handle nor a connection, and
never learns whether its `ExecContext` wraps an agent or an SSH connection.

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
- `subscriptions() -> List[StreamSpec]` then `parse_event(stream_id, payload,
  timestamp) -> Optional[CollectResult]` — the push path, for targets reached by
  an agent. Also both pure. See [Agent transport](#agent-transport).

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
  signature `PluginPage._tick`, `_CallbackTick._tick` and `on_data_event` all share.
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

A modular monitor subdivides its own cycle: a module in the `modules` block may
carry an `interval` of its own, and the plugin issues the commands of only the
modules due that cycle. The monitor's `interval` is therefore the floor, not the
schedule — a module asking for less than it gets collected every cycle. Because a
resting module contributes no result, the plugin holds its last status and folds
that into the worst-status roll-up, so an hourly `smart` check does not read as
online for the 59 minutes between probes. Metrics are deliberately *not* carried:
`latest_metric` already returns the last value written, and forging samples would
put points on a chart that were never measured.

`run_cycle_now` is single-flight per plugin (`_collecting[id]`), shared by the
scheduler and any out-of-band (dashboard-triggered) collection, so a slow cycle
never overlaps itself.

Two monitors resolving to the same effective `id` (falls back to display name
when config omits it) would silently overwrite each other's status/metrics/logs
every cycle, since everything is keyed by `id`. Startup detects and logs this
once, loudly.

## Agent transport

`core/connectors/agent_connector.py` is the server half of the agent link; the
daemon itself is the separate `vigil_agent` package.

The whole design rests on one decision: `AgentConnection.execute()` has the same
signature and the same `(exit_code, stdout, stderr)` failure mapping as
`SSHConnection.execute()`. That is why moving a host onto an agent is a config
key and not a plugin rewrite — `ExecContext` holds either object, and no plugin
is transport-aware.

**Direction.** The agent dials the server, never the reverse. The monitored host
opens no listening port and needs no certificate of its own, and a host behind
NAT works untouched. The cost is that the server cannot reach an agent that has
not dialled in; a monitor on an absent agent fails with an explicit message,
which is the same shape as a refused SSH dial.

**Why WebSocket and not MQTT or SNMP.** SNMP and REST are both pull, so neither
removes the polling floor that motivated the agent. MQTT would add a broker as a
new single point of failure inside the monitoring path, and its pub/sub model
means hand-building request/response correlation for the exec RPC that makes
existing plugins work. A WebSocket rides the dashboard's existing port, auth and
TLS, and costs no new server dependency — NiceGUI already brings FastAPI and
uvicorn. MQTT remains the right answer for *exporting* to Home Assistant, which
is a separate concern from the transport.

**Concurrency.** Commands are frames multiplexed on one socket, so there is no
analogue of `MaxSessions` and no semaphore. The agent dispatches each `exec`
into its own task, so a 30-second `borg` check cannot delay a 1-second sample on
the same connection.

**Process groups.** `vigil_agent/executor.py` runs each command with
`start_new_session=True` and kills the whole process group on timeout. This
closes a real gap in the SSH path, where killing the remote command left
anything it had spawned running on the target.

**Events.** A plugin's `subscriptions()` returns `StreamSpec`s keyed by its own
plugin id; the engine registers them on the agent's connection and the endpoint
sends the full set in the welcome frame (full set, never a delta, so a
reconnecting agent converges without the server tracking what it knows). The
agent runs one supervised coroutine per stream from `vigil_agent/watchers.py`
and pushes an `event` frame the moment something happens.
`VigilEngine._on_agent_event` routes it back by stream id to the plugin's pure
`parse_event()` and persists the result through the same batched
`db.apply_result` the polling cycle uses — no IO on the event path.

When an agent finishes its handshake the engine collects every monitor bound to
it immediately (`_on_agent_connected`). Monitors start their schedule when Vigil
does, but the agent takes a moment to dial in, so a monitor's first cycle
normally runs before its transport exists and records a failure. For a 30s
monitor that is invisible; for the hourly SMART and ZFS checks it meant an hour
of reporting failed after every restart, which is long enough to be mistaken for
a real fault.

Two conventions matter here:

1. **The poll keeps owning status.** Both plugins that stream today (`oom`,
   `systemd_service`) return log lines from `parse_event()` and leave `status`
   to `parse_results()`. Otherwise one noisy journal line could flip a healthy
   monitor, and a disconnected agent would look like a healthy one.
2. **A raising plugin must not take down the socket.** `_on_agent_event`
   catches, logs and continues, because it runs on the agent's receive task.

**Protocol location.** `vigil_agent/protocol.py` is canonical and
`vigil/core/connectors/agent_protocol.py` re-exports it. It lives in the agent
package because the agent is the constrained side: a monitored host installs
`vigil-agent` and must not pull in nicegui, peewee and dnspython to do it. One
definition, not two copies to keep in step.

## SSH transport

`core/connectors/ssh_connector.py` uses asyncssh rather than shelling out to the
system `ssh` client: one native connection per physical target, each command a
channel on it rather than a forked process. `ConnectorEngine` pools one
`SSHConnection` per `(host, port, username, key_path)`; a plugin's per-target
handle is a small `ExecContext` value, so the engine itself stays a stateless
singleton. (`SSHContext` remains as an alias for that type.)

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
   `MaxSessions` of 10. This ceiling is intrinsic to SSH — a host with many
   monitors either raises `MaxSessions` or moves to the agent transport, which
   has no equivalent limit.

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
`reconcile_orphaned_jobs` only fails jobs with no `pid` (crashed before launch);
any job with a pid is re-adopted by the owning plugin's next poll (pid alive →
resume; pid/exit gone → finalize).

Jobs live in the store like everything else, so **job ids are assigned by the
store**, not by SQLite's autoincrement — `create_job` returns an id
immediately, with the row persisted asynchronously under that same id. A
`JobRecord` is the one mutable record type (successive polls advance `pid`,
`progress`, `state`, `output_seq` in place); readers take a rendered dict via
`as_dict()`, so a half-applied update is never observed. Note the durability
edge this buys: if Vigil dies between launching a job and the next flush, the
job is lost from both memory and disk, and the detached process on the target
has nothing left to re-adopt it.

## SQLite: persistence, hydration, retention

SQLite is write-mostly. It is read exactly once per process, at startup; every
runtime read is served from the state store.

**One background writer thread** (`_AsyncWriter`) owns all writes. The polling
loop and UI run on the asyncio event loop; committing to SQLite fsyncs, which can
block noticeably (especially on ZFS) and would stall the async server if done
inline. Writes are enqueued (non-blocking for the caller) and batched: the thread
commits whatever arrives within `batch_window` seconds as one transaction.

Because the store — not the database — is what the UI reads, a slow disk can no
longer make a read wait, and nothing in the running system blocks on the
writer. The durability trade-off is the flip side: a crash loses the unflushed
batch, and since jobs are also memory-first, a job launched and lost within
that window leaves an unreconciled remote process (see **Jobs** below).
`flush()` waits for the queue and exists for tests and shutdown.

**Hydration** (`DatabaseManager.hydrate()`) restores the store at startup:
latest status per collector, the recent tail of each metric series, recent
events and log lines, all snapshots and settings, and recent/unfinished jobs.
Each history stream loads only as deep as its buffer, so startup cost is
bounded by `memory:` rather than by how large the database has grown. A
failure here is logged, not fatal — the store starts empty and collectors
refill it.

**Reads still run through `offload()`** in the UI (`core/ui/components.py`),
which is now cheap insurance rather than a necessity: a store read is a dict
lookup, but keeping the call off the loop costs nothing and preserves the rule
that `read_fn` is pure with no NiceGUI element access. Element updates
(`.rows = …`, `.update()`) are not thread-safe and must be applied after
awaiting, back on the loop.

**Indexing.** `Metric` carries a composite index
`(collector, metric_name, timestamp)`. No live read uses it; it serves
hydration (which loads the recent tail of each series in timestamp order) and
the retention prune. Fresh DBs get it from `create_tables`; existing DBs get it
from `_migrate`.

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

`set_snapshot` takes the **decoded object**, not a JSON string: the store holds
it as-is and serialises it only on the way to disk, so a reader never pays a
decode. (It previously took pre-serialised JSON, since the value went straight
into a `TEXT` column.)

**Log-line dedup** is enforced by the store, which keeps a hash set per target
alongside the buffer, since collectors re-fetch the same trailing lines every
cycle. The set ages out with the buffer (an evicted line's hash is discarded)
so it cannot grow without bound. The DB's `UNIQUE dedup_hash` remains as a
persistence-side backstop.

**Retention vs. buffers** are separate knobs for separate resources.
`logging.retention_days` / `logging.metric_retention_days` prune the *database
file*; `memory:` bounds the *in-memory* buffers. The buffers are self-limiting
(a deque drops its oldest entry on append), so the prunes never touch the store.

**Memory is bounded by config, not by uptime.** Every stream has a ceiling, so
resident state reaches a steady state and stays there — measured flat across
12k poll cycles (~40 MB RSS for 50 monitors x 5 metrics with chatty jobs).
Roughly: `monitors x metrics x metric_history` metric points, `event_history`
events, `log_history` lines per target.

Jobs are the exception to "just use a deque", because a job owns an output
buffer rather than being one fixed-size entry, and they get two bounds:
- `jobs_per_plugin` evicts a plugin's oldest **finished** jobs (running ones
  are never evicted — their plugin is still advancing them).
- `finished_job_output` trims a job's output to a short tail once it ends. A
  running job keeps the full `job_output` buffer, since a plugin tails it live
  to parse progress; afterwards the complete log lives on disk, and a caller
  wanting an old job's full output must read it from there.

Without those two, `monitors x jobs x output_lines` would stay resident to
serve a view that is only opened on demand.

## UI refresh: push, not polling

The UI subscribes to write notifications; it does **not** poll. Every semantic
write on the Database Engine publishes one change to the bus in
`core/state/changes.py`, tagged with its kind (`STATUS`, `METRIC`, `EVENT`,
`LOG`, `JOB`, `SNAPSHOT`, `SETTING`) and the monitor it belongs to. An idle
system publishes nothing and so costs nothing, and a status flip reaches the
screen as soon as it is written rather than at the next tick of a timer.

Publishing is synchronous, on whichever thread performed the write — a
collector's worker thread, the agent's socket task, the UI's own loop. A
subscriber therefore does the minimum possible inline and marshals the real work
onto its own event loop; nothing in the bus awaits, holds a lock for long, or
lets a subscriber's exception escape into the write path.

Widgets still apply a refresh only when the value changed (`refresh_rows` and
the inline equality checks in `history_chart`, the sidebar tree,
`update_charts`), so a wake-up that finds nothing new is cheap.

Reads are dict lookups and slices over live objects — sub-microsecond — so
there is no cache and no TTL anywhere: a read sees what the last collector
wrote, immediately. (Reads formerly went to SQLite behind a 1s TTL cache, which
is what made polling expensive enough to need caching in the first place.)
Widgets still read through `offload()` where they did before, since a NiceGUI
callback should not block the loop, but the work it wraps is now trivial.

**Thread-safety.** Collectors run on asyncio worker threads while the UI reads
from its own pool, so the store is touched from several threads. Its flat
dicts hold immutable records and need no lock (a single dict get/set is atomic
under the GIL); the bounded buffers take an `RLock`, because appending and
reading them are read-modify-write sequences. Buffer readers copy out under
that lock and return lists, so a caller never iterates a deque a collector is
appending to.

### One change subscription per client

`_PageScheduler` (`core/ui/model.py`) drives every *tickable* registered for a
NiceGUI client from a **single** subscription to the change bus. A tickable is
anything with `_tick()` and `_detached()`:

- A `PluginPage`, which refreshes a plugin detail page's cards/charts/tables.
- A `_CallbackTick`, wrapping a bare refresh callback registered via
  `on_data_event` / `schedule_callback` — used by the overview and events pages.

Both ride the one subscription, so the overview's sidebar/events/charts
refreshes and a plugin page's widgets are all coalesced. This is what keeps a
group's refresh cost from scaling with how many children are expanded — plugins
have no idea whether they're standalone or one of many expanded children.

A change wakes *every* tickable on the client, not only those interested in the
changed monitor. Refresh callbacks are registered by plugin render code and may
read any monitor's data — a group page renders its children — so there is no
reliable interest set to filter on.

**Debouncing is leading-edge.** The first change refreshes immediately and
further changes arriving during `COOLDOWN_SECONDS` collapse into one trailing
refresh. That keeps latency at zero for an isolated event while bounding the
refresh rate on a busy system, where one collection cycle publishes a change per
metric, log line and status it writes.

An `IDLE_REFRESH_SECONDS` sweep runs alongside it as a backstop, for the two
things a change notification cannot cover: values that age on their own (a
running job's duration, "last seen" times) and reaping a client that closed
while the system had nothing to publish.

`PluginPage.start()` does one synchronous refresh immediately before
registering, so a freshly loaded page shows real data rather than its
constructed defaults (`--`, empty tables) until the first tick. `run_now=False`
on a callback defers only its first tick.

## Teardown

NiceGUI resolves a `ui.timer`'s context *outside* the callback and raises "The
parent slot of the element has been deleted." as soon as the client disconnects
or the page re-renders — in NiceGUI's own task, so a try/except around the
callback never sees it, and it floods the log every tick. Driving refreshes off
the change bus rather than `ui.timer` removes that failure mode: the scheduler
owns its own tasks and checks liveness itself.

Each tick drops the tickables whose `_detached()` is true, and a scheduler with
none left unsubscribes from the bus, cancels its idle sweep and removes itself
from `_schedulers`. `_CallbackTick._detached()` tests the client it was
registered under against `Client.instances`, giving it the same lifetime as a
`PluginPage`. If the event loop is already gone when a change arrives,
`_on_change` drops the subscription and returns — cancelling the tasks is the
loop's job, not the publishing thread's.


## Bindings

Vigil uses NiceGUI's reactive `.bind_*()` only for `render_status_card`'s label
text (`label.text` is a real `BindableProperty` with an on-change push hook).
Everything else — `ui.table.rows`, `ui.echart.options`, label colors — has no
such hook (verified against NiceGUI's source), so it refreshes through the
client's shared change subscription via `page.on_refresh()`, setting values and
calling `.update()` explicitly. `binding_refresh_interval` is left at NiceGUI's
default: the binding propagation loop only has the one label to walk, and a
refresh model that no longer runs on a timer has no reason to slow it.

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

## Theme: two CSS layers, no color in Python

The dashboard wears the Halon theme, split the way Halon's design guide
requires. `core/ui/static/halon-tokens.css` is Layer 1 — the only place a
literal color exists, in a light block and a dark block that re-declares
twenty-two of them; `core/ui/static/halon.css` is Layer 2, the Quasar/NiceGUI
component rules, where every value is a `var(--token)`. `core/ui/theme.py`
loads both once per process, maps Quasar's palette variables onto the tokens,
and exposes the token names to Python as `var(--…)` strings — so an inline
style written from Python (`STATUS_COLORS['failed']`) resolves per scheme in
the browser and needs no Python branch for dark mode.

Two hazards are worth knowing before editing either sheet:

- **Quasar's utility classes (`bg-primary`, `text-white`) sit in an
  `!important` cascade layer.** Important declarations in a layer outrank
  unlayered author styles regardless of specificity, so no rule of ours can
  recolor them. The fix is to stop them being emitted — `ui.button(color=None)`
  — or, where the framework insists (toast fills), to give them a token that
  does not flip (`--fill-*`).
- **ECharts paints to a canvas and cannot read custom properties.** Charts
  register with `theme.on_scheme_change()`, which hands them the literals of
  the scheme the client reported and repaints them when it flips. Those
  literals are parsed out of the token sheet at import, so they are not a
  second copy of the palette.

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

`plugins/base/module_plugin.py` holds the scaffolding the modular monitors
(`system_stats`, `network`, `disks`) share: the `Module` contract, the `modules`
block resolver, the severity ordering, and the `ModularPlugin` base that
concatenates due modules' commands, slices the results back out positionally, and
assembles the composite `UI_SPEC`. A modular plugin declares `MODULE_TYPES` and
`MODULE_LABEL` and adds only what is genuinely its own.

## Testing

`pytest` (via `nix develop` — there is no bare `python3` on the target dev
environment). Plugin tests exercise the pure `commands()`/`parse()` or
`requests()`/`parse_results()` directly through the `run_cycle` / `run_requests`
/ `run_io_cycle` fixtures in `tests/conftest.py`, which drive a plugin with a
fake connector and apply the resulting `CollectResult` — mirroring
`VigilEngine._run_cycle` without a real event loop. `_FakeEngine` stands in for
the Coordination Engine, and `plugin.storage` is a thin test-only per-plugin
adapter over `db.apply_result` (production has no such object).
