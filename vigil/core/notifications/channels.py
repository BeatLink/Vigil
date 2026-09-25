"""Where notifications go.

Each channel type turns one Message into a delivery and raises if the
delivery fails, so the engine can retry it and report the failure.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Message:
    """One notification, in the form every channel sends."""
    title: str
    body: str
    status: str
    kind: str
    monitor_id: str = ""
    url: Optional[str] = None

    @property
    def key(self) -> str:
        """What a per-status channel setting is looked up by: the status, or 'recovered'."""
        return "recovered" if self.kind == "recovered" else self.status


class Channel:
    """A configured destination for notifications."""
    TYPE = ""

    def __init__(self, channel_id: str, config: Dict[str, Any]):
        self.id = channel_id
        self.config = config

    async def send(self, message: Message) -> None:
        raise NotImplementedError


STATUS_KEYS = ('failed', 'warning', 'unavailable', 'recovered')
"""The keys a per-status setting may use."""


def per_status(value: Any, defaults: Dict[str, str], name: str) -> Dict[str, str]:
    """A setting given once for all statuses, or per status with defaults for the rest."""
    if value is None:
        return dict(defaults)
    if isinstance(value, dict):
        unknown = set(value) - set(STATUS_KEYS)
        if unknown:
            raise ValueError(f"`{name}` keys must be {', '.join(STATUS_KEYS)}, not {sorted(unknown)}")
        return {**defaults, **{k: str(v) for k, v in value.items()}}
    return {key: str(value) for key in defaults}


class DesktopChannel(Channel):
    """A desktop notification shown by an agent running in the user's graphical session."""
    TYPE = "desktop"

    URGENCIES = ('low', 'normal', 'critical')
    DEFAULT_URGENCY = {'failed': 'critical', 'warning': 'normal', 'unavailable': 'normal',
                       'recovered': 'low'}
    DEFAULT_ICON = {key: 'vigil' for key in STATUS_KEYS}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any):
        super().__init__(channel_id, config)
        self.agent_id = str(config.get('agent') or '')
        if not self.agent_id:
            raise ValueError("a desktop channel needs `agent:`, the id of the agent to show it")
        if agents.get(self.agent_id) is None:
            raise ValueError(f"agent {self.agent_id!r} is not declared in `agents:`")
        self._agents = agents
        self.urgency = per_status(config.get('urgency'), self.DEFAULT_URGENCY, 'urgency')
        bad = set(self.urgency.values()) - set(self.URGENCIES)
        if bad:
            raise ValueError(f"`urgency` must be low, normal or critical, not {sorted(bad)}")
        self.icon = per_status(config.get('icon'), self.DEFAULT_ICON, 'icon')

    async def send(self, message: Message) -> None:
        await self._agents.get(self.agent_id).notify(
            message.title, message.body,
            self.urgency.get(message.key, 'normal'), message.url,
            self.icon.get(message.key, self.DEFAULT_ICON['failed']),
        )


def build_channels(entries: List[Dict[str, Any]], agents: Any) -> Dict[str, Channel]:
    """The configured channels by id. A bad entry is logged and skipped."""
    channels: Dict[str, Channel] = {}
    for entry in entries or []:
        channel_id = str(entry.get('id') or entry.get('type') or '')
        kind = entry.get('type')
        if not channel_id or channel_id in channels:
            logging.error(f"notifications: channel needs a unique `id`, skipping {entry!r}")
            continue
        # A broken channel must not stop the others from working.
        try:
            if kind == DesktopChannel.TYPE:
                channels[channel_id] = DesktopChannel(channel_id, entry, agents)
            else:
                raise ValueError(f"unknown channel type {kind!r}")
        except ValueError as e:
            logging.error(f"notifications: channel {channel_id!r} disabled: {e}")
    return channels
