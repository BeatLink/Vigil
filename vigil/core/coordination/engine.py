"""Coordination Engine.

The application's central coordinator. It owns the other engines — Settings
(config loader), Database, Connector (agent / SSH / HTTP / DNS / ICMP),
Exporter — and the plugin registry, and drives the per-monitor polling loop.
Plugins stay pure: they declare Commands/ActionPlans/CollectResults and this
engine (and the sub-engines it owns) performs all IO and persistence.

Collection has two paths. The polling loop runs each monitor's declared
requests on its own `interval`, as it always has. Alongside it, monitors on an
agent-backed target can subscribe to event streams the agent watches locally
and pushes the instant they change; those arrive through _on_agent_event and
are persisted the same way, without waiting for the next cycle.

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
from vigil.core.connectors.types import DispatchResult
from vigil.core.coordination.data_view import PluginDataView
from vigil.core.settings.config_file import ConfigFileManager as VigilConfig
from vigil.core.database.database import DatabaseManager as VigilDatabase
from vigil.core.exporters import ExporterEngine
from vigil.core.connectors import ConnectorEngine, ExecContext

STARTUP_JITTER_SECONDS = 3.0

_PRUNE_CHECK_SECONDS = 60


class VigilEngine:
    def __init__(self, config_path: str, db_path_override: Optional[str] = None):
        self.config_loader = VigilConfig(config_path)
        self.config = self.config_loader.data
        self.plugins: List[Plugin] = []
        self.log_retention_days = self.config_loader.log_retention_days
        self.metric_retention_days = self.config_loader.metric_retention_days
        self._last_prune = 0.0
        self._collecting: Dict[str, bool] = {}
        self._last_collected: Dict[str, float] = {}
        # Engine-owned per-plugin IO/persistence, keyed by plugin id. Pure
        # plugins never hold these; the engine wires them in setup_modules and
        # uses them on the collection/action paths. _net holds each plugin's
        # ExecContext handle (the connection itself lives in self.connectors).
        self._net: Dict[str, "ExecContext"] = {}
        # Monitors reachable by a pushed event, keyed by the stream id the
        # agent will send back (which is the plugin's own id).
        self._event_targets: Dict[str, Plugin] = {}
        if db_path_override:
            self.db_path = db_path_override
        else:
            self.db_path = self.config_loader.database_settings.get('path', 'vigil.db')
        try:
            self.db = VigilDatabase(
                self.db_path,
                write_batch_seconds=self.config_loader.write_batch_seconds,
                buffers=self.config_loader.buffer_sizes,
            )
            self.db.insert_event("INFO", "Vigil Engine initialized.", "vigil_core")
            orphaned = self.db.reconcile_orphaned_jobs()
            if orphaned:
                logging.warning(f"Marked {orphaned} orphaned job(s) as failed after restart")
        except OperationalError as e:
            logging.critical(f"Failed to initialize database: {e}. Exiting.")
            sys.exit(1)

        self.exporters = ExporterEngine(self.db, self.config_loader.exporters)
        self.connectors = ConnectorEngine()
        self.connectors.agents.configure(self.config_loader.agents)
        self.connectors.agents.set_event_sink(self._on_agent_event)

    def _wire_plugin(self, plugin: Plugin, plugin_cfg: Dict) -> None:
        """Build the engine-owned IO for a plugin and hand it the read-only data
        view. Keeps pure plugins free of db/network. Persistence needs no
        per-plugin object: the engine holds the plugin's (target, id, name) and
        writes via db.apply_result on the collection/action path."""
        net = self.connectors.exec_context(plugin_cfg, collect_timeout=plugin.timeout)
        # The transport resolves the effective target host; keep the plugin's
        # target in sync so its labels/reads match what's collected.
        plugin.target = net.target
        self._net[plugin.id] = net
        plugin.bind(PluginDataView(self.db, plugin.id, plugin.target, plugin.name))
        if net.is_agent:
            self._wire_subscriptions(plugin, net)

    def _wire_subscriptions(self, plugin: Plugin, net: "ExecContext") -> None:
        """Register a plugin's declared event streams with its agent, so the
        agent starts watching them the moment it connects. Streams are keyed by
        the plugin id, which is how an inbound event finds its way back here."""
        try:
            specs = plugin.subscriptions()
        except Exception as e:
            logging.error(f"{plugin.name}: subscriptions() failed: {e}")
            return
        if not specs:
            return
        for spec in specs:
            net.conn.register_stream(spec)
            self._event_targets[spec.id] = plugin
        logging.info(
            f"{plugin.name}: subscribed to {len(specs)} agent event stream(s) "
            f"on {net.conn.agent_id!r}"
        )

    def _on_agent_event(self, agent_id: str, stream_id: str,
                        timestamp: float, payload: Dict) -> None:
        """Apply one pushed event. Runs on the agent's socket task, off the
        polling schedule entirely — this is the path that makes detection
        latency independent of a monitor's `interval`.

        The plugin's parse_event() is pure and the write is the same batched
        db.apply_result the polling cycle uses, so an event costs one parse and
        one buffered write with no IO of its own."""
        plugin = self._event_targets.get(stream_id)
        if plugin is None:
            logging.debug(f"agent {agent_id!r}: event for unknown stream {stream_id!r}")
            return
        try:
            result = plugin.parse_event(stream_id, payload, timestamp)
        except Exception as e:
            logging.error(f"{plugin.name}: parse_event failed for {stream_id!r}: {e}")
            return
        if result is not None:
            self._apply(plugin, result)

    def _apply(self, plugin: Plugin, result) -> None:
        """Persist a plugin's CollectResult. The engine owns the write path and
        supplies the plugin's identity; pure plugins hold no writer."""
        self.db.apply_result(plugin.target, plugin.id, plugin.name, result)

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

    def setup_modules(self, plugins_cfg: Optional[List[Dict]] = None,
                      inherited_agent: Optional[str] = None) -> List[Plugin]:
        current_level_plugins = []
        target_cfg = plugins_cfg if plugins_cfg is not None else self.config_loader.plugins

        for plugin_cfg in target_cfg:
            name = plugin_cfg.get('name')
            p_type = plugin_cfg.get('type')
            plugin_cfg = self._apply_ssh_defaults(plugin_cfg)
            if inherited_agent and not plugin_cfg.get('agent'):
                plugin_cfg = {**plugin_cfg, 'agent': inherited_agent}
            try:
                module_path = f"vigil.plugins.{p_type}"
                module = importlib.import_module(module_path)

                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, Plugin) and obj is not Plugin:
                        plugin_instance = obj(name, plugin_cfg)
                        plugin_instance.engine = self
                        self._wire_plugin(plugin_instance, plugin_cfg)

                        if 'children' in plugin_cfg:
                            plugin_instance.children = self.setup_modules(
                                plugin_cfg['children'],
                                inherited_agent=plugin_cfg.get('agent'),
                            )

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
        self._apply(plugin, result)
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

        net = self._net_for(plugin)

        def _finish(outcome):
            if isinstance(outcome, CollectResult):
                self._apply(plugin, outcome)
                return outcome.success, (outcome.metadata or None)
            return bool(outcome), None

        plan = plugin.plan_action(action_id, **kwargs)
        if plan is None:
            return False, None
        if isinstance(plan, CollectResult):
            self._apply(plugin, plan)
            return plan.success, (plan.metadata or None)
        if isinstance(plan, IoActionPlan):
            io_result = await self._run_io(plan.call)
            return _finish(plugin.interpret_action(action_id, io_result, **kwargs))
        if isinstance(plan, (HttpRequest, DnsQuery, PingRequest)):
            result = (await self.connectors.run(net, [plan]))[0]
            return _finish(plugin.interpret_action(action_id, result, **kwargs))
        # Default: an ActionPlan — a short SSH command.
        result = await self.connectors.execute(net, plan)
        return _finish(plugin.interpret_action(action_id, result, **kwargs))

    # --- Job-control surface, now entirely DB-backed. A job is a detached
    # command on the target (a Job row) advanced by the owning plugin's poll;
    # "running" is a DB state, not a live coroutine, so these are DB reads plus
    # (for cancel) one ordinary SSH command. ---
    def job_running(self, plugin: Plugin) -> Optional[dict]:
        jobs = self.db.running_jobs(plugin_id=plugin.id)
        return jobs[0] if jobs else None

    def job_is_running(self, plugin: Plugin) -> bool:
        return self.job_running(plugin) is not None

    def job_current_id(self, plugin: Plugin) -> Optional[int]:
        job = self.job_running(plugin)
        return job['id'] if job else None

    def job_recent(self, plugin: Plugin, limit: int = 20) -> list:
        return self.db.recent_jobs(plugin_id=plugin.id, limit=limit)

    async def job_cancel(self, plugin: Plugin) -> bool:
        """Kill the plugin's running detached job on the target (one ordinary
        SSH command) and mark it cancelled. The plugin's next poll would also
        observe the death, but cancelling eagerly gives immediate feedback."""
        from vigil.core.connectors.ssh_connector import cancel_command
        job = self.job_running(plugin)
        if not job or not job.get('pid'):
            return False
        net = self._net_for(plugin)
        if net is not None:
            await self.connectors.execute_raw(net, cancel_command(job['pid']))
        self.db.finish_job(job['id'], 'cancelled', exit_code=130, error='Cancelled by user')
        return True

    def set_setting(self, key: str, value: str) -> None:
        """UI-triggered setting write (e.g. a group's expand/collapse state)
        the Database Engine persists. Pure plugins read settings via their
        data view and route the occasional write back through the engine."""
        self.db.set_setting(key, value)

    def apply(self, plugin: Plugin, result) -> None:
        """Persist a CollectResult a plugin produced outside the collection
        cycle (e.g. push.record_push from the REST endpoint). The plugin holds
        no writer of its own; the engine owns the write path."""
        self._apply(plugin, result)

    async def http_fetch(self, request):
        """Run one HttpRequest through the shared HttpConnector. For the rare
        plugin whose HTTP is genuinely sequential/dependent (a login POST for a
        session token, then a data GET using it) — expressed as an async
        io_call() closure that awaits this. The flat requests() list can't
        express request-to-request dependencies; this keeps that IO on the
        engine-owned connector instead of a plugin opening its own session."""
        return await self.connectors.http.fetch(request)

    # --- Job persistence, called by a plugin from its poll (parse_results) to
    # advance its detached job's Job/JobOutput rows through the engine. ---
    def create_job(self, plugin: Plugin, kind: str, command: str, workdir: str) -> int:
        return self.db.create_job(plugin_id=plugin.id, target=plugin.target,
                                  kind=kind, command=command, workdir=workdir)

    def set_job_pid(self, job_id: int, pid: int) -> None:
        self.db.set_job_pid(job_id, pid)

    def set_job_progress(self, job_id: int, summary: str) -> None:
        self.db.set_job_progress(job_id, summary)

    def append_job_output(self, job_id: int, lines: list) -> None:
        self.db.append_job_output(job_id, lines)

    def bump_job_output_seq(self, job_id: int, new_seq: int) -> None:
        self.db.bump_job_output_seq(job_id, new_seq)

    def finish_job(self, job_id: int, state: str, exit_code=None, error=None) -> None:
        self.db.finish_job(job_id, state, exit_code=exit_code, error=error)

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
        # Nothing to prune if both retention windows are disabled (0/negative).
        if self.log_retention_days <= 0 and self.metric_retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_prune < interval:
            return
        self._last_prune = now
        try:
            # Logs and jobs share the log-retention window; metrics and status
            # history use their own (metric_retention_days, which defaults to
            # log_retention_days when unset). Each prune_* is a no-op when its
            # window is <= 0, so the two schedules are independent.
            self.db.prune_logs(self.log_retention_days)
            self.db.prune_jobs(self.log_retention_days)
            self.db.prune_metrics(self.metric_retention_days)
            self.db.prune_status(self.metric_retention_days)
        except Exception as e:
            logging.error(f"Retention prune failed: {e}")
