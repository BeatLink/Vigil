import asyncio
import logging
import importlib
import inspect
import sys
import time
from typing import List, Optional, Dict
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.config_file import ConfigFileManager as VigilConfig
from vigil.core.data.database import DatabaseManager as VigilDatabase
from peewee import OperationalError

# How often the engine wakes to check which monitors are due.
#
# This is the scheduler's resolution, not a polling interval: each monitor
# collects only once its own `interval` has elapsed (BasePlugin.run_cycle).
# It must therefore be no larger than the shortest interval in use, or that
# interval is silently rounded up to the tick — a 60s tick made every
# `interval: 30s` monitor poll at 60s instead. 10s keeps the common 30s
# setting honest while leaving the loop nearly idle when nothing is due.
TICK_SECONDS = 10


class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[BasePlugin] = []
        self.log_retention_days = self.config_loader.log_retention_days
        self._last_prune = 0.0  # monotonic time of the last retention prune
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            self.db = VigilDatabase(self.db_path)
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

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[BasePlugin]:
        """
        Dynamically instantiates plugins and injects internal modules.
        Supports recursive loading for nested group structures.
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
                
                # Find class inheriting from BasePlugin
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BasePlugin) and obj is not BasePlugin:
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
            from vigil.plugins.vigil_self import VigilSelfPlugin
        except ImportError as e:
            logging.debug(f"Self-monitoring plugin unavailable: {e}")
            return
        VigilSelfPlugin.engine = self

    def _start_exporters(self):
        """Launch configured push exporters (e.g. InfluxDB) as background tasks.

        Pull exporters (Prometheus) need no task — they're served on demand by
        the REST API's /metrics endpoint. Only push exporters run a loop here.
        """
        exporters_cfg = self.config_loader.exporters or {}
        influx_cfg = exporters_cfg.get('influxdb')
        if influx_cfg and influx_cfg.get('url'):
            try:
                from vigil.core.modules.exporters.influxdb import InfluxDBExporter
                exporter = InfluxDBExporter(self.db, influx_cfg)
                asyncio.create_task(exporter.run())
                logging.info("InfluxDB exporter task started.")
            except Exception as e:
                logging.error(f"Failed to start InfluxDB exporter: {e}")

    async def run(self):
        logging.info("Vigil Engine started...")
        self.db.insert_event("INFO", "Vigil Engine started polling loop.", "vigil_core")

        self._start_exporters()

        while True:
            # Build levels via BFS, then run bottom-up so group plugins always
            # aggregate after their children have written fresh status to the DB.
            levels = []
            current_level = list(self.plugins)
            while current_level:
                levels.append(current_level)
                next_level = []
                for p in current_level:
                    next_level.extend(p.children)
                current_level = next_level

            total = 0
            for level in reversed(levels):
                tasks = [p.run_cycle() for p in level]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        logging.error(f"Plugin execution error: {res}")
                # run_cycle returns True only when it actually collected; the
                # rest were skipped because their interval had not elapsed.
                total += sum(1 for r in results if r is True)

            if total:
                logging.info(f"Engine Cycle: Collected {total} monitors.")
            else:
                logging.debug("Engine Cycle: nothing due.")

            self._maybe_prune_logs()

            await asyncio.sleep(TICK_SECONDS)

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