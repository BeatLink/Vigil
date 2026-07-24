import asyncio
import logging
import importlib
import inspect
import random
import sys
import time
from typing import List, Optional, Dict, Tuple
from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.network_orchestrator import SSHConnectionPool
from vigil.core.connectors.orchestration.types import JobPlan
from vigil.core.database.config_file import ConfigFileManager as VigilConfig
from vigil.core.database.database import DatabaseManager as VigilDatabase
from peewee import OperationalError

DEFAULT_INTERNAL_API_HOST = '127.0.0.1'
DEFAULT_INTERNAL_API_PORT = 8081

STARTUP_JITTER_SECONDS = 3.0

_PRUNE_CHECK_SECONDS = 60


class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[CollectorPlugin] = []
        self.log_retention_days = self.config_loader.log_retention_days
        self._last_prune = 0.0
        self.ssh_pool = SSHConnectionPool()
        self._collecting: Dict[str, bool] = {}
        self._last_collected: Dict[str, float] = {}
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            self.db = VigilDatabase(self.db_path, write_batch_seconds=self.config_loader.write_batch_seconds)
            self.db.insert_event("INFO", "Vigil Engine initialized.", "vigil_core")
            orphaned = self.db.reconcile_orphaned_jobs()
            if orphaned:
                logging.warning(f"Marked {orphaned} orphaned job(s) as failed after restart")
        except OperationalError as e:
            logging.critical(f"Failed to initialize database: {e}. Exiting.")
            sys.exit(1)

    def _apply_ssh_defaults(self, plugin_cfg: Dict) -> Dict:
        defaults = self.config_loader.ssh_defaults
        if not defaults or 'ssh_config' not in plugin_cfg:
            return plugin_cfg

        merged = dict(plugin_cfg)
        merged['ssh_config'] = {**defaults, **plugin_cfg['ssh_config']}
        return merged

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[CollectorPlugin]:
        current_level_plugins = []
        target_cfg = plugins_cfg if plugins_cfg is not None else self.config_loader.plugins

        for plugin_cfg in target_cfg:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            plugin_cfg = self._apply_ssh_defaults(plugin_cfg)
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)

                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, CollectorPlugin) and obj is not CollectorPlugin:
                        plugin_instance = obj(name, plugin_cfg, self.db, self.ssh_pool)
                        
                        if 'children' in plugin_cfg:
                            plugin_instance.children = self.setup_modules(plugin_cfg['children'])
                        
                        current_level_plugins.append(plugin_instance)
                        logging.info(f"Loaded plugin '{name}' of type '{p_type}'")
                        break
            except Exception as e:
                logging.error(f"Failed to load plugin '{name}' ({p_type}): {e}")
        
        if plugins_cfg is None:
            self.plugins = current_level_plugins
            logging.info(f"Plugin registry built with {len(self.plugins)} root-level monitors.")
            self._warn_on_duplicate_ids()
            self._wire_self_monitor()

        return current_level_plugins

    def _warn_on_duplicate_ids(self):
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
        try:
            from vigil.plugins.vigil_self import VigilSelfCollectorPlugin
        except ImportError as e:
            logging.debug(f"Self-monitoring plugin unavailable: {e}")
            return
        VigilSelfCollectorPlugin.engine = self

    def _start_exporters(self):
        exporters_cfg = self.config_loader.exporters or {}
        influx_cfg = exporters_cfg.get('influxdb')
        if influx_cfg and influx_cfg.get('url'):
            try:
                from vigil.core.connectors.exporters.influxdb import InfluxDBExporter
                exporter = InfluxDBExporter(self.db, influx_cfg)
                asyncio.create_task(exporter.run())
                logging.info("InfluxDB exporter task started.")
            except Exception as e:
                logging.error(f"Failed to start InfluxDB exporter: {e}")

    @staticmethod
    def _flatten(plugins: List[CollectorPlugin]):
        for p in plugins:
            yield p
            yield from VigilEngine._flatten(p.children)

    async def _run_cycle(self, plugin: CollectorPlugin) -> bool:
        """The orchestration loop: async/IO lives here, plugin.commands()/
        parse() (or local_call()/parse_local() for non-SSH plugins) are
        pure and never touched directly by anything but this."""
        local_fn = plugin.local_call()
        if local_fn is not None:
            local_result = await plugin.local_io.run(local_fn)
            result = plugin.parse_local(local_result)
        else:
            commands = plugin.commands()
            results = await plugin.network.run(commands) if commands else []
            result = plugin.parse(results)
        plugin.storage.apply(result)
        return True

    async def run_cycle_now(self, plugin: CollectorPlugin) -> bool:
        """Single-flight wrapper for out-of-band (web-poll-triggered)
        collection, sharing the same reentrancy guard as the scheduler."""
        if self._collecting.get(plugin.id):
            logging.debug(f"{plugin.name}: previous collection still running, skipping poll-triggered cycle")
            return False
        self._collecting[plugin.id] = True
        try:
            return await self._run_cycle(plugin)
        finally:
            self._last_collected[plugin.id] = time.monotonic()
            self._collecting[plugin.id] = False

    async def dispatch_action(self, plugin: CollectorPlugin, action_id: str, **kwargs) -> Tuple[bool, Optional[Dict[str, str]]]:
        """Returns (success, metadata). metadata is the applied CollectResult's
        .metadata dict when one was applied (e.g. carrying 'content' for
        read-style dialog actions), else None. Plain bool outcomes (the
        common write/dispatch case) return (bool, None)."""
        from vigil.core.connectors.orchestration.types import CollectResult, LocalActionPlan

        plan = plugin.plan_action(action_id, **kwargs)
        if plan is None:
            return False, None
        if isinstance(plan, CollectResult):
            plugin.storage.apply(plan)
            return plan.success, (plan.metadata or None)
        if isinstance(plan, JobPlan):
            on_line = plugin.job_on_line(action_id, **kwargs)
            _, status = await plugin.network.run_job_plan(plan, on_line=on_line)
            outcome = plugin.interpret_job(action_id, status, **kwargs)
            if isinstance(outcome, CollectResult):
                plugin.storage.apply(outcome)
                return outcome.success, (outcome.metadata or None)
            return bool(outcome), None
        if isinstance(plan, LocalActionPlan):
            local_result = await plugin.local_io.run(plan.call)
            outcome = plugin.interpret_local_action(action_id, local_result, **kwargs)
            if isinstance(outcome, CollectResult):
                plugin.storage.apply(outcome)
                return outcome.success, (outcome.metadata or None)
            return bool(outcome), None
        result = await plugin.network.execute(plan)
        outcome = plugin.interpret_action(action_id, result, **kwargs)
        if isinstance(outcome, CollectResult):
            plugin.storage.apply(outcome)
            return outcome.success, (outcome.metadata or None)
        return bool(outcome), None

    async def _monitor_loop(self, plugin: CollectorPlugin):
        await asyncio.sleep(random.uniform(0, STARTUP_JITTER_SECONDS))
        while True:
            try:
                await self.run_cycle_now(plugin)
            except Exception as e:
                logging.error(f"Plugin execution error ({plugin.name}): {e}")
            await asyncio.sleep(plugin.interval)

    async def _prune_loop(self):
        while True:
            self._maybe_prune_logs()
            await asyncio.sleep(_PRUNE_CHECK_SECONDS)

    async def run(self):
        logging.info("Vigil Engine started...")

        from vigil.core.database.events import bus
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
        from vigil.core.connectors.internal_api import run_internal_api
        api_cfg = self.config_loader.data.get('internal_api', {}) or {}
        host = api_cfg.get('host', DEFAULT_INTERNAL_API_HOST)
        port = int(api_cfg.get('port', DEFAULT_INTERNAL_API_PORT))
        try:
            await run_internal_api(self, host=host, port=port)
        except Exception as e:
            logging.critical(f"Collector internal API failed to start: {e}")

    def _maybe_prune_logs(self, interval: float = 3600.0):
        if self.log_retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_prune < interval:
            return
        self._last_prune = now
        try:
            self.db.prune_logs(self.log_retention_days)
            self.db.prune_jobs(self.log_retention_days)
        except Exception as e:
            logging.error(f"Log retention prune failed: {e}")