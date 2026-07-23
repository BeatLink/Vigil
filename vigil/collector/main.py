import asyncio
import logging
import importlib
import inspect
import random
import sys
import time
from typing import List, Optional, Dict
from vigil.collector.plugin_base import CollectorPlugin
from vigil.core.data.config_file import ConfigFileManager as VigilConfig
from vigil.core.data.database import DatabaseManager as VigilDatabase
from peewee import OperationalError

# Default bind address/port for the collector's internal API (see
# vigil.collector.internal_api). Loopback-only: this endpoint lets a
# caller run arbitrary pre-built commands on any monitored host via
# ssh_controller, so it must never be reachable from anywhere but the web
# process on the same machine. Configurable via `internal_api.host`/`port` in
# config.yaml for deployments where the two processes are not on one host
# (e.g. separate containers on a private network) — in that case the operator
# is responsible for firewalling the port themselves.
DEFAULT_INTERNAL_API_HOST = '127.0.0.1'
DEFAULT_INTERNAL_API_PORT = 8081

# Upper bound on the random startup stagger applied to each monitor's first
# poll (see _monitor_loop). Spreads the initial burst — every monitor would
# otherwise fire in the same event-loop iteration at process start — across a
# few seconds so they don't all queue on the SSH semaphore at once. Capped low
# so it never meaningfully delays a monitor's first reading.
STARTUP_JITTER_SECONDS = 3.0

# How often the engine checks whether a log-retention prune is due. Pruning
# has its own hourly throttle (_maybe_prune_logs), so this only needs to be
# frequent enough that the hourly check doesn't drift noticeably; it is no
# longer tied to monitor polling at all (see run()).
_PRUNE_CHECK_SECONDS = 60


class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[CollectorPlugin] = []
        self.log_retention_days = self.config_loader.log_retention_days
        self._last_prune = 0.0  # monotonic time of the last retention prune
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            self.db = VigilDatabase(self.db_path, write_batch_seconds=self.config_loader.write_batch_seconds)
            self.db.insert_event("INFO", "Vigil Engine initialized.", "vigil_core")
            # Jobs are child processes of this one, so any still marked running
            # died with the previous Vigil. Clear them at startup or the UI will
            # present a dead job as live forever.
            orphaned = self.db.reconcile_orphaned_jobs()
            if orphaned:
                logging.warning(f"Marked {orphaned} orphaned job(s) as failed after restart")
        except OperationalError as e:
            logging.critical(f"Failed to initialize database: {e}. Exiting.")
            sys.exit(1)

    def _apply_ssh_defaults(self, plugin_cfg: Dict) -> Dict:
        """
        Return a copy of ``plugin_cfg`` with the global ``ssh_defaults`` merged
        into its ``ssh_config``. Keys already present on the plugin take
        precedence, so a monitor can override the username, key, etc. locally.

        Only applied to plugins that actually use SSH (those with an
        ``ssh_config`` block); leaf plugins that connect by ``target_host``
        alone (e.g. uptime/ICMP) and plain groups are left untouched.
        """
        defaults = self.config_loader.ssh_defaults
        if not defaults or 'ssh_config' not in plugin_cfg:
            return plugin_cfg

        merged = dict(plugin_cfg)
        merged['ssh_config'] = {**defaults, **plugin_cfg['ssh_config']}
        return merged

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[CollectorPlugin]:
        """
        Dynamically instantiates plugins and injects internal modules.
        Supports recursive loading for nested group structures.

        Only the collector process calls this — it is the only place a
        plugin module's *CollectorPlugin class is instantiated with a real
        SSHConnection. Each plugin module also defines a *UIPlugin sibling
        class (constructed instead by the web process; see
        vigil.web.engine.VigilWebEngine.setup_ui_modules), which this loader
        skips: matching CollectorPlugin specifically, not the shared
        UIPlugin also importable from the same module, is what keeps a
        misconfigured web process from ever holding a live SSH connection.
        """
        current_level_plugins = []
        target_cfg = plugins_cfg if plugins_cfg is not None else self.config_loader.plugins

        for plugin_cfg in target_cfg:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            # Merge global SSH defaults (e.g. username, key_path) into this
            # plugin's ssh_config. Per-plugin values always win over defaults.
            plugin_cfg = self._apply_ssh_defaults(plugin_cfg)
            # Dynamically load the plugin class and inject dependencies
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)

                # Find the class inheriting from CollectorPlugin (not UIPlugin
                # — see this method's docstring).
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, CollectorPlugin) and obj is not CollectorPlugin:
                        plugin_instance = obj(name, plugin_cfg, self.db)
                        
                        # Recursively setup children if they exist
                        if 'children' in plugin_cfg:
                            plugin_instance.children = self.setup_modules(plugin_cfg['children'])
                        
                        current_level_plugins.append(plugin_instance)
                        logging.info(f"Loaded plugin '{name}' of type '{p_type}'")
                        break
            except Exception as e:
                logging.error(f"Failed to load plugin '{name}' ({p_type}): {e}")
        
        # If this is the root call, store the root-level plugins in the engine
        if plugins_cfg is None:
            self.plugins = current_level_plugins
            logging.info(f"Plugin registry built with {len(self.plugins)} root-level monitors.")
            self._warn_on_duplicate_ids()
            self._wire_self_monitor()

        return current_level_plugins

    def _warn_on_duplicate_ids(self):
        """
        Report monitors that share an effective id.

        Every per-monitor record — status, metrics, events, log lines, jobs —
        is keyed by `id`, and `id` falls back to the display name when the
        config omits it. Two monitors resolving to the same id therefore write
        to the same rows: their statuses overwrite each other every cycle and
        each one's page shows a mixture of both. Nothing else detects this, so
        it is checked once at startup where it is cheap and loud.
        """
        seen = {}
        duplicates = {}
        stack = list(self.plugins)
        while stack:
            p = stack.pop()
            stack.extend(p.children)
            if p.id in seen:
                duplicates.setdefault(p.id, [seen[p.id]]).append(p.name)
            else:
                seen[p.id] = p.name

        for dup_id, names in duplicates.items():
            logging.error(
                f"Duplicate monitor id {dup_id!r} used by {len(names)} monitors "
                f"({', '.join(sorted(set(names)))}). Their status, metrics and logs "
                f"will overwrite each other — give each an explicit unique `id`."
            )
            self.db.insert_event(
                "ERROR",
                f"[vigil_core] Duplicate monitor id {dup_id!r} used by: "
                f"{', '.join(sorted(set(names)))}",
                "vigil_core",
            )

    def _wire_self_monitor(self):
        """
        Give the self-monitoring plugin a reference to this engine.

        It is the only plugin that inspects the monitor tree rather than a
        remote host — it reports how many monitors are collecting on schedule —
        and plugins are constructed with (name, config, db) alone. Setting the
        class attribute here keeps that constructor signature unchanged instead
        of threading an engine parameter through all twenty-eight plugin types
        for the sake of one.

        Imported lazily so the plugin module is only loaded when it is
        configured; a config without a `vigil_self` monitor pays nothing.
        """
        try:
            from vigil.plugins.vigil_self import VigilSelfCollectorPlugin
        except ImportError as e:
            logging.debug(f"Self-monitoring plugin unavailable: {e}")
            return
        VigilSelfCollectorPlugin.engine = self

    def _start_exporters(self):
        """Launch configured push exporters (e.g. InfluxDB) as background tasks.

        Pull exporters (Prometheus) need no task — they're served on demand by
        the REST API's /metrics endpoint. Only push exporters run a loop here.
        """
        exporters_cfg = self.config_loader.exporters or {}
        influx_cfg = exporters_cfg.get('influxdb')
        if influx_cfg and influx_cfg.get('url'):
            try:
                from vigil.collector.exporters.influxdb import InfluxDBExporter
                exporter = InfluxDBExporter(self.db, influx_cfg)
                asyncio.create_task(exporter.run())
                logging.info("InfluxDB exporter task started.")
            except Exception as e:
                logging.error(f"Failed to start InfluxDB exporter: {e}")

    @staticmethod
    def _flatten(plugins: List[CollectorPlugin]):
        """Yield every plugin instance in the tree (groups and leaves)."""
        for p in plugins:
            yield p
            yield from VigilEngine._flatten(p.children)

    async def _monitor_loop(self, plugin: CollectorPlugin):
        """
        Drive one monitor's polling forever, entirely independent of every
        other monitor.

        There is no shared tick: each monitor sleeps its own `interval`
        between calls, so a 30s monitor is never rounded up to a slower
        monitor's schedule and a 6h monitor never wakes early. Group plugins
        get a loop too — they re-read live child status from the DB on each
        collection (GroupPlugin.on_collect), so they need no ordering
        relative to their children; a group's aggregated status can lag a
        child's by at most one of the group's own intervals, which is
        indistinguishable from the group simply not having polled yet.

        A random startup stagger spreads the first poll of every monitor so
        they don't all fire in the same event-loop iteration when Vigil
        starts. Exceptions are caught per-iteration (mirroring the previous
        `gather(..., return_exceptions=True)`) so one crashing monitor never
        stops its own future polls, let alone anyone else's.
        """
        await asyncio.sleep(random.uniform(0, STARTUP_JITTER_SECONDS))
        while True:
            try:
                await plugin.run_cycle()
            except Exception as e:
                logging.error(f"Plugin execution error ({plugin.name}): {e}")
            await asyncio.sleep(plugin.interval)

    async def _prune_loop(self):
        """Periodically check whether a log-retention prune is due.

        Independent of monitor polling now that there is no shared tick to
        piggyback on; _maybe_prune_logs keeps its own hourly throttle.
        """
        while True:
            self._maybe_prune_logs()
            await asyncio.sleep(_PRUNE_CHECK_SECONDS)

    async def run(self):
        logging.info("Vigil Engine started...")

        # DataBus notifications from the background DB-writer thread need a
        # running event loop to hand off to (call_soon_threadsafe); this is
        # the first point one is guaranteed to exist and be the loop NiceGUI
        # itself is serving requests on.
        from vigil.core.data.events import bus
        bus.bind_loop(asyncio.get_running_loop())

        self.db.insert_event("INFO", "Vigil Engine started polling loop.", "vigil_core")

        self._start_exporters()

        monitors = list(self._flatten(self.plugins))
        logging.info(f"Starting {len(monitors)} independent monitor schedule(s).")
        for plugin in monitors:
            asyncio.create_task(self._monitor_loop(plugin))

        asyncio.create_task(self._run_internal_api())

        await self._prune_loop()

    async def _run_internal_api(self):
        """
        Serve the internal API (action/poll/push/ssh/job proxying — see
        vigil.collector.internal_api) on the collector's own event loop,
        so the web process can reach live plugin instances that only exist
        here. Bound to loopback by default; see DEFAULT_INTERNAL_API_HOST.
        """
        from vigil.collector.internal_api import run_internal_api
        api_cfg = self.config_loader.data.get('internal_api', {}) or {}
        host = api_cfg.get('host', DEFAULT_INTERNAL_API_HOST)
        port = int(api_cfg.get('port', DEFAULT_INTERNAL_API_PORT))
        try:
            await run_internal_api(self, host=host, port=port)
        except Exception as e:
            logging.critical(f"Collector internal API failed to start: {e}")

    def _maybe_prune_logs(self, interval: float = 3600.0):
        """
        Prune expired log lines at most once per `interval` seconds, so the
        retention sweep runs roughly hourly rather than every polling cycle.
        """
        if self.log_retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_prune < interval:
            return
        self._last_prune = now
        try:
            self.db.prune_logs(self.log_retention_days)
            # Job history shares the log retention window: both are operator-
            # facing records of past activity, and a separate knob for it would
            # be one more setting with no distinct reason to differ.
            self.db.prune_jobs(self.log_retention_days)
        except Exception as e:
            logging.error(f"Log retention prune failed: {e}")