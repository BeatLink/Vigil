"""
Push monitor: the inverse of every other plugin.

Every other plugin reaches out to a target on its own schedule. A push monitor
has no target to reach — the thing being watched (a cron job, a script on a
machine with no SSH access, a task with no fixed host) instead calls Vigil's
REST API to say "I'm alive" whenever it runs. Vigil's job is just to notice
when those calls stop arriving.

This inverts run_cycle()'s usual meaning: on_collect() does not collect
anything from a target. It only checks how long it has been since the last
heartbeat (record_push(), invoked by the API route) and reports failed once
that exceeds max_age. Because heartbeats are typically far less frequent than
the engine's own tick, `interval` here controls how often that staleness check
runs, not how often data is expected to arrive — `max_age` controls that.

Config options:
  max_age      Seconds since the last heartbeat before reporting failed
               (default: interval * 2 — tolerates one missed beat before
               alarming, matching how oneshot systemd_service treats max_age)
  token        Shared secret the caller must present to record a heartbeat.
               Required — without one, anyone who can reach the API could
               mark this monitor healthy. Generate with e.g. `openssl rand
               -hex 20`.
"""
import time
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.time_utils import format_age, format_duration

_DEFAULT_LAYOUT = [
    ['status_card', 'lastbeat_card', 'maxage_card'],
    ['events'],
]

_VALID_PUSH_STATUSES = {'up', 'down'}


class PushCollectorPlugin(CollectorPlugin):
    """
    Monitors an external heartbeat pushed to Vigil's REST API rather than
    collected from a target. Reports failed once max_age elapses with no
    heartbeat.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.max_age = int(config.get('max_age', self.interval * 2))
        self.token = config.get('token')
        # Nothing is polled, so there is no remote host this monitor is "about".
        self.target = config.get('target_host', self.name)

    # -------------------------------------------------------------------------
    # Collection — staleness check only. Heartbeats arrive via record_push(),
    # called from the API route, entirely outside this method and the engine's
    # tick. on_collect() never itself indicates the target is healthy: it can
    # only notice that a heartbeat did or did not show up in time.
    # -------------------------------------------------------------------------

    async def on_collect(self):
        last = self.latest_metric('last_push_epoch')

        if last is None:
            self.db_logger.write("No heartbeat received yet", level="WARNING")
            self.set_status('failed')
            return

        age = time.time() - last.value
        if age > self.max_age:
            self.db_logger.write(
                f"No heartbeat for {format_age(int(age))}, exceeds max_age of "
                f"{format_duration(self.max_age)}",
                level="ERROR"
            )
            self.set_status('failed')
            return

        # A heartbeat can report its own failure (status=down) even though it
        # arrived on time — the caller ran, but the thing it checked failed.
        last_reported = self.latest_metric('reported_up')
        if last_reported is not None and last_reported.value == 0.0:
            self.set_status('failed')
        else:
            self.set_status('online')

    def record_push(self, status: str = 'up', msg: Optional[str] = None,
                    value: Optional[float] = None) -> bool:
        """
        Record an incoming heartbeat. Called from the REST API route, not from
        the polling loop — this is what makes it a push rather than a pull.

        `status` is the caller's own assessment ('up' or 'down'): a script can
        run successfully enough to phone home while reporting that the thing
        it checked is actually failing. Returns False if `status` isn't
        recognised, so the API route can reject the request with a 400 rather
        than silently accepting garbage.
        """
        if status not in _VALID_PUSH_STATUSES:
            return False

        now = time.time()
        is_up = status == 'up'
        self.db_metrics.metric('last_push_epoch', now)
        self.db_metrics.metric('reported_up', 1.0 if is_up else 0.0)
        if value is not None:
            self.db_metrics.metric('value', float(value))

        log_level = "INFO" if is_up else "ERROR"
        detail = f": {msg}" if msg else ""
        self.db_logger.write(f"Heartbeat received (status={status}){detail}", level=log_level)
        self.set_status('online' if is_up else 'failed')
        return True

    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Push monitors report what they're told; there is nothing to remediate."""
        return False


class PushUIPlugin(UIPlugin):
    """Dashboard rendering for the push monitor. See PushCollectorPlugin for
    collection/heartbeat logic."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        # UIPlugin has no max_age attribute (that's collector-side state) — the
        # same default as PushCollectorPlugin.__init__ is re-derived here from
        # config, matching what the collector actually configured itself with.
        max_age = int(self.config.get('max_age', self.interval * 2))

        page = self.page()

        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                page,
                metric_name='reported_up',
                title='LAST REPORTED STATUS',
                on_text='UP',
                off_text='DOWN'
            )
        with layout.cell('lastbeat_card'):
            lastbeat_label = info_card('LAST HEARTBEAT', 'Never')
        with layout.cell('maxage_card'):
            info_card('MAX AGE', format_duration(max_age))
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update():
            last = self.latest_metric('last_push_epoch')
            if last is not None:
                age = int(time.time() - last.value)
                lastbeat_label.text = format_age(age)

        page.on_refresh(update)
        update()
        page.start()
