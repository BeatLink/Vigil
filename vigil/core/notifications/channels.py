"""Where notifications go.

Each channel type turns one Message into a delivery and raises if the
delivery fails, so the engine can retry it and report the failure.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from vigil.core.connectors.types import Status


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
    def clears(self) -> bool:
        """Whether this ends a problem that was announced, so its notification can be cleared."""
        return self.kind == "recovered"

    @property
    def key(self) -> str:
        """What a per-status channel setting is looked up by: the status, 'recovered' or 'flapping'."""
        if self.clears:
            return "recovered"
        return "flapping" if self.kind == "flapping" else self.status


def digest(messages: List[Message], url: Optional[str]) -> Message:
    """One notification standing for several that arrived together."""
    problems = [m for m in messages if not m.clears]
    counts: Dict[str, int] = {}
    for m in problems:
        label = "flapping" if m.kind == "flapping" else m.status
        counts[label] = counts.get(label, 0) + 1
    parts = [f"{n} {label}" for label, n in counts.items()]
    if len(problems) < len(messages):
        parts.append(f"{len(messages) - len(problems)} recovered")
    worst = Status.worst(m.status for m in problems).value if problems else "online"
    return Message(
        f"Vigil: {', '.join(parts)}", "\n".join(f"• {m.title}" for m in messages), worst,
        "problem" if problems else "recovered", "", url, "Vigil", "",
        max(m.timestamp for m in messages),
    )


class Channel:
    """A configured destination for notifications."""
    TYPE = ""

    dismisses_on_recovery = False
    """Whether a recovery only clears the problem's notification, which is sent even while muted."""

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        self.id = channel_id
        self.config = config

    async def send(self, message: Message) -> None:
        raise NotImplementedError

    async def send_batch(self, messages: List[Message], summary: Message) -> None:
        """Send several notifications that arrived together; most channels send their digest."""
        await self.send(summary)


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


STATUS_KEYS = ('failed', 'warning', 'unavailable', 'recovered', 'flapping')
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
                       'recovered': 'low', 'flapping': 'normal'}
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
        self.dismisses_on_recovery = bool(config.get('dismiss_on_recovery', True))
        self._groups: Dict[str, Any] = {}
        self._member: Dict[str, str] = {}

    async def send(self, message: Message) -> None:
        agent = self._agents.get(self.agent_id)
        in_group = await self._leave_group(message.monitor_id)
        if message.clears and self.dismisses_on_recovery and message.monitor_id:
            if in_group:
                return
            # An agent too old to dismiss shows the recovery instead.
            if "dismiss" in agent.capabilities:
                await agent.dismiss(message.monitor_id)
                return
        await self._show(message, message.monitor_id or None)

    async def send_batch(self, messages: List[Message], summary: Message) -> None:
        """Show problems that arrived together as one notification that shrinks as each recovers."""
        problems = [m for m in messages if not m.clears]
        for message in messages:
            if message.clears or len(problems) == 1:
                await self.send(message)
        if len(problems) < 2:
            return
        for message in problems:
            await self._leave_group(message.monitor_id)
        key = f"group:{problems[0].monitor_id}:{int(summary.timestamp)}"
        self._groups[key] = ({m.monitor_id: m for m in problems}, summary.url)
        for message in problems:
            self._member[message.monitor_id] = key
        await self._show_group(key)

    async def _show(self, message: Message, key: Optional[str]) -> None:
        await self._agents.get(self.agent_id).notify(
            message.title, message.body,
            self.urgency.get(message.key, 'normal'), message.url,
            str(self.icon.get(message.key, self.DEFAULT_ICON['failed'])), key,
        )

    async def _show_group(self, key: str) -> None:
        members, url = self._groups[key]
        await self._show(digest(list(members.values()), url), key)

    async def _leave_group(self, monitor_id: str) -> bool:
        """Take a monitor out of the group notification it is in, updating or closing it."""
        key = self._member.pop(monitor_id, None)
        if key is None:
            return False
        members, _ = self._groups[key]
        members.pop(monitor_id, None)
        if members:
            await self._show_group(key)
        else:
            del self._groups[key]
            await self._agents.get(self.agent_id).dismiss(key)
        return True
