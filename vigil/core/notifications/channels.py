"""Where notifications go.

Each channel type turns one Message into a delivery and raises if the
delivery fails, so the engine can retry it and report the failure.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Message:
    """One notification, in the form every channel sends."""
    title: str
    body: str
    status: str
    kind: str
    monitor_id: str = ""
    url: Optional[str] = None
    monitor_name: str = ""
    host: str = ""
    timestamp: float = 0.0

    @property
    def key(self) -> str:
        """What a per-status channel setting is looked up by: the status, or 'recovered'."""
        return "recovered" if self.kind == "recovered" else self.status


class Channel:
    """A configured destination for notifications."""
    TYPE = ""

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        self.id = channel_id
        self.config = config

    async def send(self, message: Message) -> None:
        raise NotImplementedError


def secret(config: Dict[str, Any], key: str) -> Optional[str]:
    """A setting given inline as `key`, or as `key_file`, a file holding it that is read once."""
    path = config.get(f'{key}_file')
    if path:
        try:
            return Path(str(path)).read_text(encoding='utf-8').strip() or None
        except OSError as e:
            raise ValueError(f"could not read {key}_file {path}: {e}") from e
    value = config.get(key)
    return str(value) if value else None


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
        return {**defaults, **value}
    return {key: value for key in defaults}


class DesktopChannel(Channel):
    """A desktop notification shown by an agent running in the user's graphical session."""
    TYPE = "desktop"

    URGENCIES = ('low', 'normal', 'critical')
    DEFAULT_URGENCY = {'failed': 'critical', 'warning': 'normal', 'unavailable': 'normal',
                       'recovered': 'low'}
    DEFAULT_ICON = {key: 'vigil' for key in STATUS_KEYS}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.agent_id = str(config.get('agent') or '')
        if not self.agent_id:
            raise ValueError("a desktop channel needs `agent:`, the id of the agent to show it")
        if agents is None or agents.get(self.agent_id) is None:
            raise ValueError(f"agent {self.agent_id!r} is not declared in `agents:`")
        self._agents = agents
        self.urgency = per_status(config.get('urgency'), self.DEFAULT_URGENCY, 'urgency')
        bad = {str(v) for v in self.urgency.values()} - set(self.URGENCIES)
        if bad:
            raise ValueError(f"`urgency` must be low, normal or critical, not {sorted(bad)}")
        self.icon = per_status(config.get('icon'), self.DEFAULT_ICON, 'icon')

    async def send(self, message: Message) -> None:
        await self._agents.get(self.agent_id).notify(
            message.title, message.body,
            self.urgency.get(message.key, 'normal'), message.url,
            str(self.icon.get(message.key, self.DEFAULT_ICON['failed'])),
        )
