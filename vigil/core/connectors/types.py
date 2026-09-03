"""Shared connector datatypes: commands, results, the Status ladder and ExecContext."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union


@dataclass(frozen=True)
class Command:
    text: str
    timeout: Optional[float] = None
    action: bool = False
    # Run on this agent instead of the monitor's own target, e.g. a heavy evaluation offloaded to a bigger host.
    agent: Optional[str] = None


@dataclass(frozen=True)
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str


class Status(str, Enum):
    """Canonical monitor status. A str subclass, so comparisons against stored
    strings, STATUS_COLORS lookups and JSON serialisation all keep working.

    'offline' ranks below 'warning': a monitor that cannot measure is less
    alarming than one measuring a bad number."""

    ONLINE = 'online'
    OFFLINE = 'offline'
    WARNING = 'warning'
    FAILED = 'failed'

    # str() and f-strings must yield the bare value, not 'Status.ONLINE'.
    __str__ = str.__str__
    __format__ = str.__format__

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @property
    def log_level(self) -> str:
        return _LOG_LEVEL[self]

    @classmethod
    def worst(cls, statuses: Iterable[str]) -> "Status":
        """The most severe of the given statuses, defaulting to online; an
        unrecognised value ranks as offline."""
        worst = cls.ONLINE
        for value in statuses:
            try:
                candidate = cls(value)
            except ValueError:
                candidate = cls.OFFLINE
            if candidate.severity > worst.severity:
                worst = candidate
        return worst


_SEVERITY = {Status.ONLINE: 0, Status.OFFLINE: 1, Status.WARNING: 2, Status.FAILED: 3}

_LOG_LEVEL = {Status.ONLINE: 'INFO', Status.OFFLINE: 'WARNING',
              Status.WARNING: 'WARNING', Status.FAILED: 'ERROR'}


@dataclass(frozen=True)
class ActionPlan:
    command: str
    timeout: Optional[float] = None


# --- Declarative connector requests/results ---
# A plugin declares what IO it needs this cycle as a heterogeneous list of
# these frozen request objects (from requests()); the Connector Engine routes
# each to the right sub-connector (SSH / HTTP / DNS / ICMP) and returns a
# positionally-matched list of result objects the plugin's pure parse_results()
# consumes. Command/CmdResult (above) are the SSH members of the same union.

@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    timeout: Optional[float] = None
    auth: Optional[Tuple[str, str]] = None
    """Optional HTTP Basic Auth (username, password), applied by the connector."""
    ok_prefixes: Tuple[str, ...] = ()
    """Optional case-insensitive body-prefix success check (e.g. DDNS
    providers answer 'good'/'nochg'); empty means status_code alone decides."""


@dataclass(frozen=True)
class HttpResult:
    status_code: Optional[int]
    text: str
    error: Optional[str] = None
    elapsed_ms: float = 0.0
    """Wall-clock time the request took, measured by the connector. 0.0 on a
    transport error (nothing completed)."""


@dataclass(frozen=True)
class DnsQuery:
    domain: str
    record_type: str = "A"
    resolver: Optional[str] = None
    port: int = 53
    timeout: float = 5.0


@dataclass(frozen=True)
class DnsResult:
    kind: str
    """One of: 'ok', 'nxdomain', 'no_answer', 'timeout', 'dns_error'."""
    answer: Any = None
    """The raw dnspython Answer on 'ok' (so plugins keep full rdata access,
    e.g. MX preference / TXT strings / rrset TTL); None otherwise."""
    error: Optional[str] = None


@dataclass(frozen=True)
class PingRequest:
    host: str
    count: int = 1
    timeout: float = 2.0


@dataclass(frozen=True)
class PingResult:
    exception: Optional[str]
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""


# The heterogeneous request/result unions the Connector Engine speaks. Command
# and CmdResult are the SSH members, so an all-SSH plugin's existing
# commands()/parse() are just the Command-only case of this contract.
Request = Union[Command, HttpRequest, DnsQuery, PingRequest]
Result = Union[CmdResult, HttpResult, DnsResult, PingResult]


@dataclass(frozen=True)
class IoActionPlan:
    """For actions whose work is genuinely sequential/conditional local IO
    (e.g. ddns_updater's Force Update: resolve the update URL — possibly via a
    file or subprocess — then push it) that a single declarative HttpRequest
    can't express. The Coordination Engine runs `call` off the event loop and
    passes its return value to interpret_action(). Prefer a plain HttpRequest/
    DnsQuery/PingRequest action plan whenever the work is a single request."""
    call: Callable[[], Any]


@dataclass
class CollectResult:
    metrics: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)
    logs: List[Tuple[str, str]] = field(default_factory=list)
    log_lines: List[Tuple[str, str, Optional[str]]] = field(default_factory=list)
    status: Optional[str] = None
    snapshot: Any = None
    settings: Dict[str, str] = field(default_factory=dict)
    success: bool = False
    """Only consulted when this CollectResult is returned from
    plan_action()/interpret_action() to describe an action outcome (write +
    return value in one shape); ignored for collection-cycle CollectResults."""

    @staticmethod
    def failed(message: str, level: str = "ERROR", status: str = "failed") -> "CollectResult":
        return CollectResult(logs=[(message, level)], status=status)


# Plugin.plan_action()'s return type. VigilEngine.dispatch_action
# discriminates this union with isinstance, in this order: CollectResult
# (a write with no command run), IoActionPlan (sequential local IO), a
# declarative connector request (HttpRequest/DnsQuery/PingRequest), ActionPlan
# (the default — a short SSH command, including launching a detached job), or
# None (action_id unhandled). Named here so plan_action's signature and
# dispatch_action's isinstance chain both reference one union.
ActionPlanResult = Union[
    ActionPlan, IoActionPlan, HttpRequest, DnsQuery, PingRequest,
    CollectResult, None,
]

# Plugin.interpret_action()'s return type: a plain success/failure bool, or a
# CollectResult (.success set) to also apply a write alongside the outcome.
ActionOutcome = Union[bool, CollectResult]

# VigilEngine.dispatch_action()'s return: (success, metadata). metadata is
# the applied CollectResult's .metadata dict when one was applied (e.g.
# carrying 'content' for read-style dialog actions), else None.
DispatchResult = Tuple[bool, Optional[Dict[str, str]]]
