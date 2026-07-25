"""Coordination Engine.

The application's central coordinator. It owns the other engines — Settings
(config loader), Database, Connector (SSH today; HTTP/DNS/ICMP being added),
Exporter — and the plugin registry, and drives the per-monitor polling loop.
Plugins stay pure: they declare Commands/ActionPlans/CollectResults and this
engine (and the sub-engines it owns) performs all IO and persistence.

The class is named ``VigilEngine`` for continuity (``core/contracts.py``'s
``EngineLike`` and the test suite reference it); "Coordination Engine" is its
role in the architecture.
"""

import asyncio
import logging
import importlib
import inspect
import random
import sys
import time
from typing import List, Optional, Dict

from peewee import OperationalError

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.ssh.network_orchestrator import NetworkOrchestrator, SSHConnectionPool
from vigil.core.connectors.types import DispatchResult, JobPlan
from vigil.core.database.storage_orchestrator import StorageOrchestrator
from vigil.core.coordination.data_view import PluginDataView
from vigil.core.settings.config_file import ConfigFileManager as VigilConfig
from vigil.core.database.database import DatabaseManager as VigilDatabase
from vigil.core.exporters import ExporterEngine
from vigil.core.connectors import ConnectorEngine

STARTUP_JITTER_SECONDS = 3.0

_PRUNE_CHECK_SECONDS = 60


class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[Plugin] = []
        self.log_retention_days = self.config_loader.log_retention_days
        self._last_prune = 0.0
        self.ssh_pool = SSHConnectionPool()
        self._collecting: Dict[str, bool] = {}
        self._last_collected: Dict[str, float] = {}
        # Engine-owned per-plugin IO/persistence, keyed by plugin id. Pure
        # plugins never hold these; the engine wires them in setup_modules and
        # uses them on the collection/action paths.
        self._net: Dict[str, "NetworkOrchestrator"] = {}
        self._store: Dict[str, "StorageOrchestrator"] = {}
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

        self.exporters = ExporterEngine(self.db, self.config_loader.exporters)
        self.connectors = ConnectorEngine()

    def _wire_plugin(self, plugin: Plugin, plugin_cfg: Dict) -> None:
        """Build the engine-owned IO/persistence for a plugin and hand it the
        read-only data view. Keeps pure plugins free of db/network/storage."""
        net = NetworkOrchestrator(
            plugin_cfg, self.db, plugin.id, plugin.target, plugin.timeout, self.ssh_pool)
        # NetworkOrchestrator resolves the effective SSH target host; keep the
        # plugin's target in sync so its labels/reads match what's collected.
        plugin.target = net.target
        store = StorageOrchestrator(self.db, plugin.target, plugin.name, plugin.id)
        self._net[plugin.id] = net
        self._store[plugin.id] = store
        plugin.bind(PluginDataView(self.db, plugin.id, plugin.target, plugin.name, store=store))

    def _store_for(self, plugin: Plugin):
        # Fall back to a plugin-attached orchestrator (test-wired plugins that
        # weren't registered through setup_modules on this engine instance).
        return self._store.get(plugin.id) or getattr(plugin, 'storage', None)

    def _net_for(self, plugin: Plugin):
        return self._net.get(plugin.id) or getattr(plugin, 'network', None)

    async def _run_io(self, fn):
        """Run a plugin's io_call()/IoActionPlan closure off the event loop
        (or await it if it's a coroutine function). Engine-owned so pure
        plugins never do their own thread offloading."""
        if inspect.iscoroutinefunction(fn):
            return await fn()
        return await asyncio.to_thread(fn)

    def _apply_ssh_defaults(self, plugin_cfg: Dict) -> Dict:
        defaults = self.config_loader.ssh_defaults
        if not defaults or 'ssh_config' not in plugin_cfg:
            return plugin_cfg

        merged = dict(plugin_cfg)
        merged['ssh_config'] = {**defaults, **plugin_cfg['ssh_config']}
        return merged

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None) -> List[Plugin]:
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
                    if issubclass(obj, Plugin) and obj is not Plugin:
                        plugin_instance = obj(name, plugin_cfg)
                        plugin_instance.engine = self
                        self._wire_plugin(plugin_instance, plugin_cfg)

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
            from vigil.plugins.vigil_self import VigilSelfPlugin
        except ImportError as e:
            logging.debug(f"Self-monitoring plugin unavailable: {e}")
            return
        VigilSelfPlugin.engine = self

    def _start_exporters(self):
        self.exporters.start()

    @staticmethod
    def _flatten(plugins: List[Plugin]):
        for p in plugins:
            yield p
            yield from VigilEngine._flatten(p.children)

    async def _run_cycle(self, plugin: Plugin) -> bool:
        """The collection loop: all async/IO lives here; the plugin's
        requests()/parse_results() (and io_call() for the sequential-local-IO
        case) are pure and touched only from here.

        A plugin declares a heterogeneous request list (SSH Commands, HTTP,
        DNS, ICMP); the Connector Engine routes and executes them, and the
        plugin's pure parse_results() turns the results into a CollectResult
        the Database Engine persists. The rare plugin with genuinely
        sequential/conditional local IO uses io_call() instead."""
        io_fn = plugin.io_call()
        if io_fn is not None:
            io_result = await self._run_io(io_fn)
            result = plugin.parse_results([io_result])
        else:
            requests = plugin.requests()
            net = self._net_for(plugin)
            results = await self.connectors.run(net, requests) if requests else []
            result = plugin.parse_results(results)
        self._store_for(plugin).apply(result)
        return True

    async def run_cycle_now(self, plugin: Plugin) -> bool:
        """Single-flight wrapper for out-of-band (dashboard-poll-triggered)
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

    async def dispatch_action(self, plugin: Plugin, action_id: str, **kwargs) -> DispatchResult:
        """Returns (success, metadata). metadata is the applied CollectResult's
        .metadata dict when one was applied (e.g. carrying 'content' for
        read-style dialog actions), else None. Plain bool outcomes (the
        common write/dispatch case) return (bool, None)."""
        from vigil.core.connectors.types import (
            CollectResult, HttpRequest, DnsQuery, PingRequest, IoActionPlan,
        )

        store = self._store_for(plugin)
        net = self._net_for(plugin)

        def _finish(outcome):
            if isinstance(outcome, CollectResult):
                store.apply(outcome)
                return outcome.success, (outcome.metadata or None)
            return bool(outcome), None

        plan = plugin.plan_action(action_id, **kwargs)
        if plan is None:
            return False, None
        if isinstance(plan, CollectResult):
            store.apply(plan)
            return plan.success, (plan.metadata or None)
        if isinstance(plan, JobPlan):
            on_line = plugin.job_on_line(action_id, **kwargs)
            _, status = await net.run_job_plan(plan, on_line=on_line)
            return _finish(plugin.interpret_job(action_id, status, **kwargs))
        if isinstance(plan, IoActionPlan):
            io_result = await self._run_io(plan.call)
            return _finish(plugin.interpret_action(action_id, io_result, **kwargs))
        if isinstance(plan, (HttpRequest, DnsQuery, PingRequest)):
            result = (await self.connectors.run(net, [plan]))[0]
            return _finish(plugin.interpret_action(action_id, result, **kwargs))
        # Default: an ActionPlan — a short SSH command.
        result = await net.execute(plan)
        return _finish(plugin.interpret_action(action_id, result, **kwargs))

    # --- Job-control surface the UI Engine's job panel calls through the
    # engine, since these touch the live JobController (not a pure DB read)
    # and a pure plugin no longer holds self.network. ---
    def job_is_running(self, plugin: Plugin) -> bool:
        return self._net_for(plugin).is_running()

    def job_current_id(self, plugin: Plugin) -> Optional[int]:
        return self._net_for(plugin).current_job_id()

    def job_recent(self, plugin: Plugin, limit: int = 20) -> list:
        return self._net_for(plugin).recent(limit=limit)

    async def job_cancel(self, plugin: Plugin) -> bool:
        return self._net_for(plugin).cancel()

    def set_setting(self, key: str, value: str) -> None:
        """UI-triggered setting write (e.g. a group's expand/collapse state)
        the Database Engine persists. Pure plugins read settings via their
        data view and route the occasional write back through the engine."""
        self.db.set_setting(key, value)

    def apply(self, plugin: Plugin, result) -> None:
        """Persist a CollectResult a plugin produced outside the collection
        cycle (e.g. push.record_push from the REST endpoint). The plugin holds
        no writer of its own; the engine owns the StorageOrchestrator."""
        self._store_for(plugin).apply(result)

    def set_job_progress(self, job_id: int, summary: str) -> None:
        """Streaming job-progress write (borg's per-line progress) the
        Database Engine persists on the plugin's behalf."""
        self.db.set_job_progress(job_id, summary)

    async def _monitor_loop(self, plugin: Plugin):
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

        self.db.insert_event("INFO", "Vigil Engine started polling loop.", "vigil_core")

        self._start_exporters()

        monitors = list(self._flatten(self.plugins))
        logging.info(f"Starting {len(monitors)} independent monitor schedule(s).")
        for plugin in monitors:
            asyncio.create_task(self._monitor_loop(plugin))

        asyncio.create_task(self._prune_loop())

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
