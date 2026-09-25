"""Notification Engine.

Watches every monitor's status writes on the change bus and sends a
notification when a monitor's rule says the change matters: a new problem,
a reminder while it lasts, or its recovery. See docs/notifications.md.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Set
from urllib.parse import quote

from vigil.core.notifications.channels import Channel, DesktopChannel, Message
from vigil.core.notifications.http import NtfyChannel, WebhookChannel
from vigil.core.notifications.rules import PROBLEM, RECOVERED, Alert, Tracker, resolve_rules
from vigil.core.state import changes
from vigil.core.state.changes import CHANGES
from vigil.plugins.base.plugin_helpers import format_duration

RETRY_DELAYS = (5, 30)
"""Seconds to wait before each retry of a failed delivery."""


CHANNEL_TYPES = {cls.TYPE: cls for cls in (DesktopChannel, WebhookChannel, NtfyChannel)}


def build_channels(entries: List[Dict[str, Any]], agents: Any) -> Dict[str, Channel]:
    """The configured channels by id. A bad entry is logged and skipped."""
    channels: Dict[str, Channel] = {}
    for entry in entries or []:
        channel_id = str(entry.get('id') or entry.get('type') or '')
        if not channel_id or channel_id in channels:
            logging.error(f"notifications: channel needs a unique `id`, skipping {entry!r}")
            continue
        channel_type = CHANNEL_TYPES.get(entry.get('type'))
        if channel_type is None:
            logging.error(f"notifications: channel {channel_id!r} has unknown type {entry.get('type')!r}")
            continue
        # A broken channel must not stop the others from working.
        try:
            channels[channel_id] = channel_type(channel_id, entry, agents)
        except ValueError as e:
            logging.error(f"notifications: channel {channel_id!r} disabled: {e}")
    return channels


def monitor_url(base_url: Optional[str], monitor_id: str) -> Optional[str]:
    """The dashboard link to one monitor's page, when the dashboard's address is configured."""
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/monitor/{quote(monitor_id, safe='')}"


class NotificationEngine:
    """Owns the notification channels and decides when to use them."""

    def __init__(self, db: Any, settings: Optional[Mapping[str, Any]], agents: Any):
        self._db = db
        settings = settings or {}
        self.base_url = settings.get('base_url')
        self.channels: Dict[str, Channel] = build_channels(settings.get('channels') or [], agents)
        self._defaults = settings.get('defaults') or {}
        self._monitors: Dict[str, Any] = {}
        self._tracker = Tracker({})
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._unsubscribe = None
        self._tasks: Set[asyncio.Task] = set()

    def start(self, roots: List[Any]) -> None:
        """Resolve each monitor's rule and start watching status writes. Needs the running loop."""
        if not self.channels:
            return
        rules = resolve_rules(roots, self._defaults, list(self.channels))
        self._tracker = Tracker(rules)
        stack = list(roots)
        while stack:
            plugin = stack.pop()
            stack.extend(plugin.children)
            self._monitors[plugin.id] = plugin

        now = time.time()
        for plugin_id, record in self._db.store.statuses.items():
            self._tracker.seed(plugin_id, record.state, record.timestamp.timestamp(), now)

        self._loop = asyncio.get_running_loop()
        self._unsubscribe = CHANGES.subscribe(self._on_change)
        logging.info(
            f"Notifications: {len(self.channels)} channel(s), {len(rules)} monitor(s) notify"
        )

    def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._tasks):
            task.cancel()

    def _on_change(self, kind: str, plugin_id: Optional[str]) -> None:
        """Change-bus subscriber: runs on the writer's thread, so it only hands off."""
        if kind != changes.STATUS or plugin_id not in self._tracker.rules:
            return
        status = self._db.latest_status(plugin_id)
        try:
            self._loop.call_soon_threadsafe(self._observe, plugin_id, status, time.time())
        except RuntimeError:
            pass  # The loop has closed during shutdown.

    def _observe(self, plugin_id: str, status: str, now: float) -> None:
        alert = self._tracker.observe(plugin_id, status, now)
        if alert is None:
            return
        message = self.message(plugin_id, alert, now)
        for channel_id in self._tracker.rules[plugin_id].channels:
            self._spawn(self._deliver(self.channels[channel_id], message))

    def message(self, plugin_id: str, alert: Alert, now: float) -> Message:
        """The notification for one alert on one monitor."""
        plugin = self._monitors.get(plugin_id)
        name = getattr(plugin, 'name', plugin_id)
        if alert.kind == RECOVERED:
            title = f"{name} recovered" if alert.status == 'online' else f"{name} is {alert.status}"
            detail = f"Was down for {format_duration(int(now - alert.since))}"
        else:
            title = f"{name} is {alert.status}" if alert.kind == PROBLEM else f"{name} is still {alert.status}"
            detail = self._reason(plugin_id, name)
        target = getattr(plugin, 'target', '') or ''
        body = "\n".join(line for line in (detail, f"Host: {target}" if target else "") if line)
        return Message(title, body, alert.status, alert.kind, plugin_id,
                       monitor_url(self.base_url, plugin_id), name, target, now)

    def _reason(self, plugin_id: str, name: str) -> str:
        """The monitor's latest event, which says why its status changed."""
        events = self._db.store.plugin_events(plugin_id=plugin_id, limit=1)
        if not events:
            return ""
        return events[0].message.removeprefix(f"[{name}] ")

    async def send_test(self, channel_id: str) -> None:
        """Send a test notification through one channel, raising if it fails."""
        channel = self.channels.get(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        await channel.send(Message(
            "Vigil test notification", f"Sent through the {channel_id!r} channel.",
            "online", "test", url=self.base_url, timestamp=time.time(),
        ))

    async def _deliver(self, channel: Channel, message: Message) -> None:
        for attempt, delay in enumerate((0, *RETRY_DELAYS)):
            await asyncio.sleep(delay)
            # Any failure is retried, then reported; a broken channel must not stop the others.
            try:
                await channel.send(message)
                logging.info(f"Notified {channel.id!r}: {message.title}")
                return
            except Exception as e:
                error = e
                logging.warning(f"Notification to {channel.id!r} failed (try {attempt + 1}): {e}")
        self._db.insert_event(
            "WARNING",
            f"[notifications] Could not notify {channel.id!r} that {message.title}: {error}",
            "vigil_core", plugin_id=message.monitor_id or None,
        )

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
