"""
CollectorPlugin: the collector-process half of Vigil's plugin split.

Vigil runs as two OS processes sharing one SQLite database: a collector that
polls targets and writes state, and a web process (vigil.web.plugin_base.UIPlugin)
that serves the dashboard by reading that state. CollectorPlugin holds a real
SSHConnection and is the only thing in Vigil that talks to a monitored host —
only the collector process ever constructs one (see
vigil.collector.main.VigilEngine.setup_modules).

Deliberately a separate class (and module) from UIPlugin rather than one
BasePlugin with a mode flag: a plugin author calling self.ssh_controller from
inside render_ui() should be calling a real, local SSHController here, and a
genuinely different (network-proxying) object in the web process — making
that a different class, not a runtime branch inside one class, means the
process boundary is visible in the type system. It also means importing this
module (module-level ssh_collector/ssh_controller/job_controller imports)
never happens in the web process, which has no business constructing any of
that collector-only machinery.
"""
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from vigil.core.common.plugin_config import PluginConfigMixin
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.common.time_utils import parse_duration
from vigil.collector.collectors.ssh_collector import SSHCollector, TIMEOUT as SSH_TIMEOUT
from vigil.collector.controllers.ssh_controller import SSHController
from vigil.collector.controllers.job_controller import JobController


class CollectorPlugin(PluginConfigMixin, ABC):
    """
    Collector-side plugin: gathers data from a target and can act on it.

    Encapsulates collection, alerting, and control logic for a specific
    domain. Only the collector process ever constructs these — they hold a
    real SSHConnection and are the only thing in Vigil that talks to a
    monitored host.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        self._init_config(name, config)
        self.db = db
        # True while on_collect is in flight; guards against overlapping polls
        # of the same monitor (see run_cycle).
        self._collecting = False
        # Monotonic time of the last collection, for the interval check.
        self._last_collected = 0.0

        # Initialize SSH infrastructure via the common library
        # The settings are passed down to allow the library to handle its own setup
        self.ssh_conn = SSHConnection.from_config(config)
        self.target = getattr(self.ssh_conn, 'host', config.get('target_host', 'localhost'))

        # Per-monitor command deadline. Defaults to the collector's own, which
        # suits quick reads; monitors whose commands are legitimately slow
        # (e.g. borg against a busy repository) raise it in config rather than
        # everyone inheriting a long timeout that would hide a dead host.
        self.timeout = parse_duration(config.get('timeout', SSH_TIMEOUT))

        self.internal_modules = {
            'collectors': {'ssh': SSHCollector(self.ssh_conn, timeout=self.timeout)},
            'controllers': {
                'ssh': SSHController(self.ssh_conn),
                # Long-running, cancellable, DB-tracked commands. Distinct from
                # 'ssh', which is capped at 30s and returns only a boolean.
                'job': JobController(self.ssh_conn, db, self.id, self.target),
            },
            'loggers': {
                # Both loggers carry the display name (for readable event
                # prefixes) and the unique id (what rows are keyed by). Names
                # are only unique within a group, so nesting monitors —
                # "Odin > Borgmatic > On Disk" and "Heimdall > Borgmatic >
                # On Disk" — makes several share a name, and anything keyed on
                # the name alone silently mixes their data together.
                'db_logs': db.get_logger(self.target, self.name, self.id),
                'db_metrics': db.get_logger(self.target, self.name, self.id)
            },
        }

        # Convenience aliases — available in every plugin without repetitive __init__ boilerplate
        self.ssh_collector  = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.job_controller = self.internal_modules['controllers'].get('job')
        self.db_logger      = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics     = self.internal_modules['loggers'].get('db_metrics')

    def set_status(self, state: str):
        """Sets the current state of the plugin (online, warning, failed, offline)."""
        self.db.insert_status(self.id, state)

    @abstractmethod
    async def on_collect(self):
        """Triggered during the polling cycle to gather and log data."""
        pass

    def get_actions(self) -> List[Dict[str, str]]:
        """Returns a list of available control actions for this plugin."""
        return []

    @abstractmethod
    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Executes a specific control action logic."""
        pass

    def present(self) -> Dict[str, Any]:
        """Formats data for the UI/Dashboard."""
        return {
            "name": self.name,
            "target": self.target,
            "actions": self.get_actions()
        }

    async def run_cycle(self) -> bool:
        """
        Main execution entry point for the plugin's polling interval.

        Returns True if this call actually collected, False if it was skipped —
        the engine's per-monitor scheduler sleeps this plugin's own interval
        between calls, so this is mainly a safety net (e.g. a caller invoking
        run_cycle early, like the web process's "Poll Now" proxy calling in
        over the internal API).

        Skips this tick when the previous collection for this monitor has not
        finished. A poll against a busy target can outlast its own interval; if
        the engine kept starting new ones regardless, each would hold an SSH
        session and a remote process, and they would accumulate until the
        target was saturated — the monitor DoSing the thing it monitors. One
        in-flight collection per monitor is always enough: the next tick picks
        up whatever the last one missed.
        """
        if self._collecting:
            logging.debug(
                f"{self.name}: previous collection still running, skipping this tick"
            )
            return False

        now = time.monotonic()
        if self._last_collected and (now - self._last_collected) < self.interval:
            return False

        self._collecting = True
        try:
            await self.on_collect()
            return True
        finally:
            self._last_collected = time.monotonic()
            self._collecting = False

    def latest_metric(self, metric_name: str):
        """
        Return the most recent Metric row for this plugin, or None.

        Scoped by `id` rather than `name`: display names repeat across groups
        (several monitors are called "On Disk"), so filtering by name returns
        whichever of them wrote last — one monitor's page showing another's
        readings.
        """
        from vigil.core.data.database import Metric
        return (
            Metric.select()
            .where((Metric.collector == self.id) & (Metric.metric_name == metric_name))
            .order_by(Metric.timestamp.desc())
            .first()
        )
