# Development Notes

Architectural decisions and non-obvious rationale for Vigil, organized by topic. Code comments stay short (≤1 sentence); anything longer belongs here.

## Single process

Vigil runs as one OS process. `main()` (`vigil/__main__.py`) builds one `VigilEngine` (the Coordination Engine, `core/coordination/engine.py`), calls `setup_modules()` to load plugins, then calls `init_gui()` (`core/ui/main_dashboard.py`) — the per-monitor polling loop and the NiceGUI web dashboard share the same asyncio event loop and read/write the same `Plugin` instances directly, with no IPC between "collecting" and "displaying". A plugin's `render_ui()` reads through `self.data` (its read-only `PluginDataView`) because it's the same live projection of the Database Engine the polling loop just wrote to, not a stale copy.

## Engine model

`core/` is organized as named engines the Coordination Engine (`VigilEngine`) owns:
- **Settings Engine** (`core/settings/`) — `ConfigFileManager` loads `config.yaml`.
- **Database Engine** (`core/database/`) — `DatabaseManager` reads/writes SQLite; `StorageOrchestrator` is its per-plugin write façade.
- **UI Engine** (`core/ui/`) — NiceGUI dashboard; reads only through `plugin.data` / the Database Engine, never through a plugin's IO objects.
- **Connector Engine** (`core/connectors/`) — all IO, split into named sub-engines: `ssh/` (asyncssh) and `http/` (HTTP + DNS + ICMP). `ConnectorEngine.run()` routes a plugin's heterogeneous request list (`Command`/`HttpRequest`/`DnsQuery`/`PingRequest`) to the right sub-connector.
- **Exporter Engine** (`core/exporters/`) — Prometheus pull render + InfluxDB push task.

### Pure plugins

Plugins expose **only pure functions and data dicts** — no IO, no persistence handles. A `Plugin` is constructed with just `(name, config)`; the Coordination Engine's `_wire_plugin()` builds the plugin's `NetworkOrchestrator` and `StorageOrchestrator` (keyed by id, engine-owned) and injects a read-only `PluginDataView` as `plugin.data`. Collection is `requests() -> parse_results()` (declarative, the 99% path) or the `io_call()` escape hatch for genuinely sequential/conditional local IO (ddns_updater, vigil_self). All writes and IO scheduling happen in the engine; the rare out-of-cycle write (push heartbeat, borg streaming, group expand state) routes back through `engine.apply` / `engine.set_job_progress` / `engine.set_setting`. `EngineLike` (`core/contracts.py`) is the narrow engine surface plugins and the UI call.

## Interface contracts

Most seams in this codebase were duck-typed: a caller and callee agreed on a dict shape, a callback signature, or a "this is set before that" ordering purely by convention, with no type enforcing it. Where a contract crossed more than one module and was worth naming once instead of reimplementing per call site, it's now formalized as a `Protocol`, `TypedDict`, or type alias, rather than left as an implicit agreement. This does not mean everything is validated — YAML config and UI_SPEC dicts are still unchecked at the boundary (bad data fails at first use, not at load time); the types exist so a reader and a type checker can see the shape a caller commits to, not so malformed input is rejected earlier.

`core/contracts.py` holds contracts that cross subsystem boundaries:
- `RefreshCallback` — the "sync function, or one returning an awaitable" signature reimplemented identically in `DataBus.emit`, `PluginPage._tick`, `safe_timer`, and `on_data_event` before this existed as one name.
- `MetricsSource` — the two-method read-only slice of `DatabaseManager` (`latest_metrics`, `latest_statuses`) that both exporters and the public REST API actually need, instead of typing them against the full manager.
- `PushablePlugin` — a `runtime_checkable` Protocol for the `token`/`record_push` surface `vigil/plugins/push.py`'s `Push` adds beyond the base `Plugin` ABC. `core/ui/api.py`'s `/api/push` route checks `isinstance(plugin, PushablePlugin)` instead of importing the concrete `Push` class, so the route stays agnostic to which plugin(s) implement push semantics.
- `EngineLike` — the narrow slice of `VigilEngine` (`db`, `plugins`, `config_loader`, `run`, `dispatch_action`, `run_cycle_now`) that `main_dashboard.py` and `api.py` actually call, so those modules (and any test double standing in for the engine) don't need the concrete class.
- `ActionButtonSpec` — the dict shape of one entry in `Plugin.get_actions()`'s return list, the "custom action button" system rendered in the plugin detail header regardless of `UI_SPEC`.

`core/ui/spec_types.py` types the `UI_SPEC` property every declaratively-rendered plugin defines and `spec.generic_render()` interprets — `UISpec` and its nested `CardSpec`/`RepeatSpec`/`TableSpec`/`ButtonSpec`/`DialogSpec`/`JobPanelSpec` document the ~15 keys `generic_render` reads and their several mutually-exclusive shapes (a card can be `metric`-driven, `metrics`-combining, `value_attr`-polling, static `value`, or `repeat`), previously discoverable only by reading `generic_render`'s branches. `Plugin.UI_SPEC` is typed `Optional[UISpec] = None` on the base class now, rather than being absent entirely and reached only via `getattr(plugin, 'UI_SPEC', None)`.

`core/database/config_schema.py` types `config.yaml`'s shape (`VigilConfig`, `PluginConfig`, `SSHConfig`, etc.) as returned by `ConfigFileManager`'s properties. `PluginConfig` is intentionally not exhaustive per plugin type — each plugin already validates its own keys via `.get()` with defaults, and one TypedDict per plugin would mostly restate that.

`core/database/rowtypes.py` types `DatabaseManager`'s dict-returning read methods. Two distinct dict shapes exist there: hand-built dicts with an exact, deliberate key set (`JobDict`, `EventDict`, `PluginEventDict`, `MetricRowDict`), and raw peewee `Model.__data__` dumps (`MetricModelDict`, `LogLineModelDict`, `EventModelDict`) that include every column on the backing table, not just the ones any caller currently reads.

`connectors/types.py` names the `plan_action()`/`dispatch_action()` discriminated union that `VigilEngine.dispatch_action` walks with `isinstance` (`ActionPlanResult` for what a plugin can return, `ActionOutcome` for what `interpret_*` can return, `DispatchResult` for what dispatch itself returns) — the dispatch logic and its `isinstance` chain were already correct; only the union itself lacked a shared name before individual plugin overrides could narrow it meaningfully in their own signatures.

`ui/layout.py`'s `PluginLayout` documents (via a class docstring, since the polymorphism is a runtime `isinstance` branch on plain YAML-sourced data, not something a type checker can discriminate) that `config.yaml`'s `layout` key is either a full `List[LayoutRow]` replacing the default row structure, or a `Dict[str, dict]` of per-widget overrides merged onto it — the same list-or-dict pattern `UI_SPEC['events']`/`['logs']` (`bool | dict`) also uses.

## Threading and the event loop

`offload()` (`core/ui/components.py`) wraps a blocking DB read so it runs in the default thread pool executor instead of inline on NiceGUI's single asyncio event loop. Every dashboard widget refresh used to run its SQLite query directly inside a `ui.timer` callback on the event loop thread. NiceGUI's websocket heartbeat (ping/pong, driven by `reconnect_timeout`, default 3s/2s) also runs on that same loop — with 47 plugin pages and several always-on overview widgets each polling every ~1s, enough blocking reads landing close together stalls the loop long enough to miss a pong, and the browser sees it as a dropped connection. That's the dashboard's "lags and disconnects" symptom, not (only) a caching problem.

`read_fn` passed to `offload()` must be pure I/O with no NiceGUI element access, since it runs off the event loop; only the *result* of awaiting the wrapper is safe to hand to a widget back on the loop. Element updates like `.rows = ...` / `.update()` are not thread-safe (they touch plain dicts and an `asyncio.Event` without `call_soon_threadsafe`), so callers must apply the result after awaiting, never inside `read_fn` itself.

## Redundant websocket pushes

Every row-based widget (`metric_table`, `log_table`, `event_table`) reruns its (cached) query and used to unconditionally call `.update()` on every tick — one websocket push per widget per second regardless of whether the data changed. A group with a dozen expanded children, each with a couple of these tables, turned into dozens of redundant pushes a second even while every reading was stable.

`refresh_rows()` (`core/ui/components.py`) and the equivalent inline checks in `history_chart`, the sidebar tree (`refresh_tree` in `main_dashboard.py`), and `update_charts` all compare new data against what's currently shown and skip the push when nothing changed. For the sidebar tree specifically: `build_tree_nodes` walks every monitor, not just whichever group/page is open — for a real deployment with 100+ nodes, and the sidebar mounted for the whole session (outside `main_container`), this ran continuously on every open tab. `update_charts` has the same issue scoped to the overview tab; `latest_statuses()` is cached for 2s at the DB layer, but the callback still ran every ~1s tick before the equality check was added.

## Per-client navigation state

Navigation state (`_navigation_state`, `state` dict in `index_page`) is created per client inside `index_page()`. `init_gui` runs once at startup, but each browser connection builds its own element tree — sharing one dict at module scope meant a new tab overwrote the previous tab's render callback, so navigating in an older tab called `.clear()` on a disconnected client and raised "The client this element belongs to has been deleted." `navigate_to()` (used by plugins) drives the most recently connected client; rebinding `_navigation_state['switch_func']` per client keeps it pointing at a live element tree.

## NiceGUI routing quirk

The dashboard is built as an explicit `@ui.page('/')` route rather than relying on NiceGUI's auto-index page. In NiceGUI 3.x the auto-index re-executes the main script via `runpy.run_path(sys.argv[0])`; under the Nix wrapper, `sys.argv[0]` is a shell script, so that parse fails with a `SyntaxError` and every request 500s.

## Bindings

Vigil never uses NiceGUI's reactive `.bind_*()` — every widget update goes through `on_data_event`/`safe_timer` setting `.text`/`.rows`/`.options` directly, then calling `.update()` explicitly. The binding-refresh loop (default: check every 0.1s) has nothing to do here, so `binding_refresh_interval` is slowed way down (2.0s) rather than disabled outright (`None`), in case a future widget starts using bindings. (`render_status_card`'s label text is the one exception — see "Binding vs. explicit refresh" below.)

## Safe timers and teardown

NiceGUI resolves a `ui.timer`'s context *outside* the callback, in code that raises "The parent slot of the element has been deleted." as soon as the client disconnects or the page re-renders. Because that raise happens in NiceGUI's own task, a try/except around the callback never sees it — the error reaches `app.handle_exception` and floods the log every tick. `_SafeTimer` (`core/ui/components.py`) overrides `_should_stop` (the hook NiceGUI checks each iteration) so detachment becomes an ordinary stop condition instead of a raise.

`_detached()` deliberately doesn't use `parent_slot` as its signal: after a delete it still returns the (now orphaned) `Slot` object, and only raises later once the slot's own parent is gone — the very raise this class exists to avoid. It checks `is_deleted` and `client.elements` instead, which NiceGUI updates at delete time.

`safe_timer()`'s `defer_first=True` skips `ui.timer`'s immediate first call (which otherwise runs inline during widget construction, before the page has painted) and fires it on the next event-loop tick instead, so navigation/clicks aren't stuck behind that first DB query. Callbacks may be sync or async — NiceGUI's `Timer._invoke_callback` already awaits anything awaitable.

`on_data_event()` re-runs a callback when `DataBus` fires an event, instead of polling on a fixed interval. `event` accepts an iterable because some widgets read more than one data type per callback (e.g. a card showing both a Setting and a Metric) and need one refresh per firing, not one per event type. Unlike a timer, a DataBus subscription has no natural next tick to detect its own detachment on — the event might not fire again for a long time (or ever) after the widget is gone, and until it does, the callback and its closure stay registered, leaking. Detachment is therefore checked from two directions: each firing checks whether `element` has since been detached and unsubscribes if so (handles same-client navigation via `main_container.clear()`), and `client.on_disconnect()` unsubscribes immediately on a full browser disconnect, which otherwise might never trigger another event.

`bus.polling_mode` (`core/database/events.py`) is a fallback `on_data_event()` checks to use a `safe_timer` instead of a DataBus subscription; nothing sets it true in the current single-process app, since the one process that writes is also the one process rendering the UI, so `DataBus.emit()` always has a subscriber to reach.

## Binding vs. explicit refresh

`render_status_card`'s label text uses real NiceGUI binding (`label.text` is a `BindableProperty` with an on-change hook that pushes to the browser) against `page.model.metrics[metric_name]`. Its color has no such bindable hook on a plain label, so it's still set via an explicit `page.on_refresh()` callback, same as the row-based widgets. More generally, `ui.table.rows` and `ui.echart.options` have no on-change hook at all (verified against NiceGUI's source), so every table/chart in `core/ui/components.py` refreshes through a shared per-page timer (`page.on_refresh()`) rather than binding.

## Auth middleware ordering

HTTP Basic Auth (`register_auth`) is registered before the routes it protects are defined. Starlette middleware wraps the whole app regardless of registration order relative to routes, but registering it early keeps intent obvious: everything that follows is meant to be gated by it.

## Polling loop

`main.py`'s per-monitor polling has no shared tick: each monitor sleeps its own `interval` between calls (a 30s monitor is never rounded up to a slower one's schedule), with a random startup stagger (`STARTUP_JITTER_SECONDS`) so they don't all fire in the same event-loop iteration at boot. Exceptions are caught per-iteration so one crashing monitor never stops its own future polls or anyone else's. Group plugins get a loop too — they re-read live child status from the DB each cycle rather than needing ordering relative to their children.

Two monitors resolving to the same effective `id` (falls back to display name when config omits it) would silently overwrite each other's status/metrics/events/log lines every cycle, since everything is keyed by `id`. Nothing else detects this, so startup checks it once, where it's cheap and loud.

## SSH transport

`core/connectors/ssh.py` uses asyncssh rather than shelling out to the system `ssh` client — one native connection per host stands in for the old ControlMaster socket, and each command becomes a channel on that connection rather than a forked process. This removed the old thread-pool/semaphore concurrency ceiling entirely (multiple commands to the same host already run concurrently on one asyncssh connection).

Three behaviors of the old subprocess design had to be reproduced deliberately, each verified empirically against a real sshd:

1. **Killing a remote process.** asyncssh's `process.close()` alone does not reliably terminate the remote command (a `sleep 300` survived it). Every timeout/cancel path explicitly calls `terminate()` then `kill()`. `execute_streaming` opens a PTY (for borg-style interactive progress output); `execute` deliberately does not, because a PTY merges stdout/stderr into one stream and dozens of plugins inspect stderr specifically for error text.
2. **Host key trust.** `known_hosts=None` disables verification entirely with no callback ever firing — that was tried first and provides no MITM protection. `known_hosts=[]` (not `None`) is what makes the `_TofuClient.validate_host_public_key` callback fire, reproducing `StrictHostKeyChecking=accept-new`: trust and persist a host's key on first use, reject any later connection with a different key.
3. **Per-host channel limits.** sshd's default `MaxSessions` is 10 (15 concurrent channels left 5 failing with "open failed"). Vigil bounds its own per-host concurrency (`_MAX_CONCURRENT_PER_HOST`) below that so it behaves safely against any host regardless of local sshd config.

`execute()` opens no PTY so stdout/stderr stay genuinely separate for callers that destructure and inspect both. `execute_streaming()` merges stderr into stdout (`stderr=asyncssh.STDOUT`) because borg interleaves progress on stderr with results on stdout. It's held against a separate, smaller `_job_semaphore` than regular `execute()` channels, since a job can run for hours.

## Job control

`core/connectors/job_controller.py`'s `JobController` exists for long-lived, cancellable commands (a borg backup) that a short remediation-command model (30s ceiling, boolean result) can't express. A job is a database row (`Job`) plus streamed output persisted as it arrives — nothing lives only in memory, so the UI reattaches after a reload and a second browser sees the same state. The controller holds one job at a time per plugin, since borg takes an exclusive repository lock anyway; rejecting a concurrent start here (`JobRejected`) gives a clear error instead of a confusing one from borg itself.

Output lines are buffered and flushed on a timer (`FLUSH_INTERVAL` = 0.5s, `FLUSH_LINES` = 50 cap) so a chatty command doesn't become one DB commit per line, while the UI still sees progress promptly. Cancellation sets a flag `execute_streaming`'s read loop checks between lines, then terminates the remote process — observed at the next line of output, which for borg's continuous progress is effectively immediate.

## SQLite writer and caching

A single background thread (`_AsyncWriter` in `core/database/database.py`) owns all DB writes. The polling loop and UI both run on the asyncio event loop; committing to SQLite fsyncs, which can block noticeably (especially on ZFS) and would stall the whole async server if done inline. Writes are enqueued (non-blocking for the caller) and batched: the writer thread accumulates whatever arrives within `batch_window` seconds and commits as one transaction. That batch's event-type tags are what `DataBus` notifies subscribed widgets with once the commit lands — making `batch_window` a latency floor for the UI as well as a durability trade-off (a crash can lose the in-memory batch).

Read-heavy queries (`latest_statuses`, `latest_metric_cached`, `metric_history_cached`, `recent_events_cached`) are cached for a few seconds per unique key, because the dashboard's overview page, every plugin detail page, and every expanded group child each poll roughly once a second and frequently want the exact same rows (two tabs open on one monitor, a metric shown in both a card and a chart). The cache's `max_age` should not be set below the writer's batch window — polling faster than a write can land doesn't surface fresher data anyway. `recent_events()` itself is deliberately left uncached since the REST API shares it and expects a live read; the dashboard's Events page caches around it at the call site instead.

`PluginSnapshot` exists because `Metric` can only carry one named number per row — wrong for a process list or systemd unit list, where every row (PID, state, "enabled" string) matters and needs a per-row UI. A plugin with row-level data writes a snapshot (`InternalDatabaseLogger.snapshot`) once per collection cycle, and its UI reads it back with `get_snapshot`/`storage.latest_snapshot`.

`LogLine` dedup uses a `UNIQUE dedup_hash` (derived from target/source/log_time/message) with `on_conflict_ignore`, since collectors re-fetch the same trailing lines every cycle — dedup is enforced by the DB itself with no read needed on the hot path.

`_migrate` exists because `create_tables` only creates missing tables, never alters an existing one — a column added to a model appears on fresh installs but not upgraded databases, where inserts then fail silently (writes are queued, so the failure only shows as a log line while data is dropped). Each migration step is additive and idempotent, so it's safe to run on every start with no version bookkeeping.

On startup, jobs still marked `running` from a previous process are force-failed (`reconcile_orphaned_jobs`) — they're child processes of Vigil, so they die with it, but their row would otherwise still claim to be live.

## DataBus

`core/database/events.py`'s `DataBus` fires per data type (`status`, `metric`, `event`, `log_line`, `setting`, `snapshot`), not per monitor id — a widget re-checks on any write of that type and filters client-side, avoiding per-id subscribe/unsubscribe churn as widgets mount and unmount. `emit()` is called from the writer thread right after a batch *commits*, not at insert time, since the row isn't queryable until then. It hands off to the UI event loop via `call_soon_threadsafe` once `bind_loop()` has registered the running loop; before that (early in tests, or before the UI starts) `emit()` is a no-op. `Subscribe()` (`bus.on`) returns an `off()` function callers must call once their widget is gone, or the callback (and its closure) leaks for the process's life — see "Safe timers and teardown" above for how widgets actually do this.

## Plugin config sharing

`plugins/base/plugin_helpers.py`'s `PluginConfigMixin` holds the config-parsing logic every `Plugin` needs (`id`/`target`/`interval` derivation via `_init_config`), split into its own module so it's reusable without dragging in the rest of `plugin_base.py`.

## Declarative UI spec

Most plugins reduced, after a binding-model migration, to the same shape: a handful of metric cards with a formatter, a layout grid, a chart, an events table. `core/ui/spec.py`'s `UI_SPEC` dict lets a plugin declare that shape instead of hand-writing `render_ui()`; `generic_render()` interprets it using the same `PluginPage`/`PluginModel` binding machinery underneath. Plugins with genuinely bespoke widgets (processes.py's per-row kill buttons, borg.py's job panel, service_list.py's unit-file editor) keep a real `render_ui()` and can still call `generic_render()` for their page's standard parts.

Format/color functions are referenced by name from `FORMATTERS`/`COLOR_RULES` rather than inlined as lambdas, so a spec dict stays pure, serializable data reusable across plugins. A plugin needing a one-off transform registers it under its own key (`register_formatter`/`register_color_rule`) instead of bending a shared name to fit.

`core/ui/layout.py`'s flex-row layout lets `config.yaml` override a plugin's default widget arrangement two ways: replacing the row structure entirely, or per-widget property overrides (visibility, height, flex) that keep the default rows.

## Bindable page model

`core/ui/model.py` replaces per-widget polling timers with one shared timer per page (`PluginPage.model`, driven by `_PageScheduler`). NiceGUI's binding only auto-pushes to the browser for some properties, verified against its source before this was built: scalar fields (`label.text`) are a real `BindableProperty` with an on-change hook, so `label.bind_text_from(model, 'status')` needs no plugin code to reach the browser — including per-metric values via nested-key paths like `('metrics', 'vms_total')`. Row-based widgets (`ui.table.rows`, `ui.echart.options`) have no such hook at all, so they stay on an explicit `page.on_refresh()` callback, still riding the same shared timer.

One `_PageScheduler` per NiceGUI client drives every `PluginPage` on it from a single `safe_timer`, ticking at the fastest interval any registered page asked for — this is what keeps a group's refresh cost from scaling with how many children are expanded, since plugins have no idea whether they're standalone or one of many expanded children. `PluginPage.start()` does one synchronous refresh immediately before registering with the scheduler: without it, a freshly loaded page shows its constructed defaults ('--', empty tables) until the first tick, and a single HTTP response is fully serialized before any deferred timer for that request can run — "defer to next tick" would mean "never" for that page load.

## Duration parsing

`plugins/base/plugin_helpers.py`'s `parse_duration` accepts plain numbers or strings like `'1w'`, `'7d'`, `'2h30m'`, `'30s'`, including compound forms like `'1d12h'`.
