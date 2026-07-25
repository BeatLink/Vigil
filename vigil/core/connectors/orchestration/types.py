from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


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


# Plugin.plan_action()'s return type. VigilEngine.dispatch_action
# discriminates this union with isinstance, in this order: CollectResult
# (a write with no command run), JobPlan (long-running/cancellable),
# LocalActionPlan (local blocking IO, no SSH), ActionPlan (the default —
# a short SSH command), or None (action_id unhandled). Named here so
# plan_action's signature and dispatch_action's isinstance chain both
# reference one union instead of restating it independently.
ActionPlanResult = Union[ActionPlan, JobPlan, LocalActionPlan, CollectResult, None]

# Plugin.interpret_action()/interpret_local_action()/interpret_job()'s
# return type: a plain success/failure bool, or a CollectResult (.success
# set) to also apply a write alongside the outcome.
ActionOutcome = Union[bool, CollectResult]

# VigilEngine.dispatch_action()'s return: (success, metadata). metadata is
# the applied CollectResult's .metadata dict when one was applied (e.g.
# carrying 'content' for read-style dialog actions), else None.
DispatchResult = Tuple[bool, Optional[Dict[str, str]]]
