# Development Notes

Architectural decisions and non-obvious rationale for Vigil, organized by topic. Code comments stay short (≤1 sentence); anything longer belongs here.

## Process split

`init_gui` serves the dashboard for a `VigilWebEngine` and does not start a polling loop. Monitor polling happens entirely in the separate collector process (`VigilEngine.run()`, `core/main.py`); the web process only reads the shared database and proxies actions to the collector's internal API.

No writer thread runs in the web process for monitor data (only for UI preferences, see `VigilWebEngine.__init__`), so `DataBus.emit()` never fires there for status/metric/event/log_line writes. `on_data_event` (`components.py`) polls instead, once `bus.polling_mode` is set — this must happen before any page is built, since `render_ui()` calls `on_data_event` during construction.

## Threading and the event loop

`offload()` (`components.py`) wraps a blocking DB read so it runs in the default thread pool executor instead of inline on NiceGUI's single asyncio event loop. Every dashboard widget refresh used to run its SQLite query directly inside a `ui.timer` callback on the event loop thread. NiceGUI's websocket heartbeat (ping/pong, driven by `reconnect_timeout`, default 3s/2s) also runs on that same loop — with 47 plugin pages and several always-on overview widgets each polling every ~1s, enough blocking reads landing close together stalls the loop long enough to miss a pong, and the browser sees it as a dropped connection. That's the dashboard's "lags and disconnects" symptom, not (only) a caching problem.

`read_fn` passed to `offload()` must be pure I/O with no NiceGUI element access, since it runs off the event loop; only the *result* of awaiting the wrapper is safe to hand to a widget back on the loop. Element updates like `.rows = ...` / `.update()` are not thread-safe (they touch plain dicts and an `asyncio.Event` without `call_soon_threadsafe`), so callers must apply the result after awaiting, never inside `read_fn` itself.

## Redundant websocket pushes

Every row-based widget (`metric_table`, `log_table`, `event_table`) reruns its (cached) query and used to unconditionally call `.update()` on every tick — one websocket push per widget per second regardless of whether the data changed. A group with a dozen expanded children, each with a couple of these tables, turned into dozens of redundant pushes a second even while every reading was stable.

`refresh_rows()` (`components.py`) and the equivalent inline checks in `history_chart`, the sidebar tree (`refresh_tree` in `main_dashboard.py`), and `update_charts` all compare new data against what's currently shown and skip the push when nothing changed. For the sidebar tree specifically: `build_tree_nodes` walks every monitor, not just whichever group/page is open — for a real deployment with 100+ nodes, and the sidebar mounted for the whole session (outside `main_container`), this ran continuously on every open tab. `update_charts` has the same issue scoped to the overview tab; `latest_statuses()` is cached for 2s at the DB layer, but the callback still ran every ~1s tick before the equality check was added.

## Per-client navigation state

Navigation state (`_navigation_state`, `state` dict in `index_page`) is created per client inside `index_page()`. `init_gui` runs once at startup, but each browser connection builds its own element tree — sharing one dict at module scope meant a new tab overwrote the previous tab's render callback, so navigating in an older tab called `.clear()` on a disconnected client and raised "The client this element belongs to has been deleted." `navigate_to()` (used by plugins) drives the most recently connected client; rebinding `_navigation_state['switch_func']` per client keeps it pointing at a live element tree.

## NiceGUI routing quirk

The dashboard is built as an explicit `@ui.page('/')` route rather than relying on NiceGUI's auto-index page. In NiceGUI 3.x the auto-index re-executes the main script via `runpy.run_path(sys.argv[0])`; under the Nix wrapper, `sys.argv[0]` is a shell script, so that parse fails with a `SyntaxError` and every request 500s.

## Bindings

Vigil never uses NiceGUI's reactive `.bind_*()` — every widget update goes through `on_data_event`/`safe_timer` setting `.text`/`.rows`/`.options` directly, then calling `.update()` explicitly. The binding-refresh loop (default: check every 0.1s) has nothing to do here, so `binding_refresh_interval` is slowed way down (2.0s) rather than disabled outright (`None`), in case a future widget starts using bindings.

## Safe timers and teardown

NiceGUI resolves a `ui.timer`'s context *outside* the callback, in code that raises "The parent slot of the element has been deleted." as soon as the client disconnects or the page re-renders. Because that raise happens in NiceGUI's own task, a try/except around the callback never sees it — the error reaches `app.handle_exception` and floods the log every tick. `_SafeTimer` (`components.py`) overrides `_should_stop` (the hook NiceGUI checks each iteration) so detachment becomes an ordinary stop condition instead of a raise.

`_detached()` deliberately doesn't use `parent_slot` as its signal: after a delete it still returns the (now orphaned) `Slot` object, and only raises later once the slot's own parent is gone — the very raise this class exists to avoid. It checks `is_deleted` and `client.elements` instead, which NiceGUI updates at delete time.

`safe_timer()`'s `defer_first=True` skips `ui.timer`'s immediate first call (which otherwise runs inline during widget construction, before the page has painted) and fires it on the next event-loop tick instead, so navigation/clicks aren't stuck behind that first DB query. Callbacks may be sync or async — NiceGUI's `Timer._invoke_callback` already awaits anything awaitable.

`on_data_event()` re-runs a callback when `DataBus` fires an event, instead of polling on a fixed interval. `event` accepts an iterable because some widgets read more than one data type per callback (e.g. a card showing both a Setting and a Metric) and need one refresh per firing, not one per event type. Unlike a timer, a DataBus subscription has no natural next tick to detect its own detachment on — the event might not fire again for a long time (or ever) after the widget is gone, and until it does, the callback and its closure stay registered, leaking. Detachment is therefore checked from two directions: each firing checks whether `element` has since been detached and unsubscribes if so (handles same-client navigation via `main_container.clear()`), and `client.on_disconnect()` unsubscribes immediately on a full browser disconnect, which otherwise might never trigger another event.

## Binding vs. explicit refresh

`render_status_card`'s label text uses real NiceGUI binding (`label.text` is a `BindableProperty` with an on-change hook that pushes to the browser) against `page.model.metrics[metric_name]`. Its color has no such bindable hook on a plain label, so it's still set via an explicit `page.on_refresh()` callback, same as the row-based widgets. More generally, `ui.table.rows` and `ui.echart.options` have no on-change hook at all (verified against NiceGUI's source), so every table/chart in `components.py` refreshes through a shared per-page timer (`page.on_refresh()`) rather than binding.

## Auth middleware ordering

HTTP Basic Auth (`register_auth`) is registered before the routes it protects are defined. Starlette middleware wraps the whole app regardless of registration order relative to routes, but registering it early keeps intent obvious: everything that follows is meant to be gated by it.
