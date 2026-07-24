from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Command:
    text: str
    timeout: Optional[float] = None
    action: bool = False


@dataclass(frozen=True)
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ActionPlan:
    command: str
    timeout: Optional[float] = None


@dataclass(frozen=True)
class JobPlan:
    kind: str
    command: str
    redacted: Optional[str] = None
    timeout: Optional[float] = None


@dataclass(frozen=True)
class LocalActionPlan:
    """Like ActionPlan, but for actions whose work is local blocking IO
    (e.g. an outbound HTTP request) rather than an SSH command. The engine
    runs `call` via LocalIOOrchestrator and passes its return value to
    interpret_local_action() instead of interpret_action()."""
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
