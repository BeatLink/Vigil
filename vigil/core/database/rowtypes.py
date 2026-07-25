"""TypedDict shapes for DatabaseManager's dict-returning read methods.

Two different sources produce these dicts and their fields diverge
slightly as a result:
  - `_job_to_dict` and `job_output`/`plugin_events_cached`/`recent_events*`
    build a dict by hand with a fixed, deliberate key set (safe to type
    exactly).
  - `collector_metrics_cached`/`log_lines_cached`/`recent_metrics_raw_cached`
    /`recent_events_raw_cached` return peewee's `Model.__data__` directly —
    every column on that model, verbatim, including ones no caller reads
    (e.g. Metric.metadata). These types document the columns a caller can
    rely on; peewee may include more.
"""

from typing import Any, List, Optional, Tuple, TypedDict


class JobDict(TypedDict):
    """Returned by DatabaseManager.get_job/recent_jobs/running_jobs."""
    id: int
    plugin_id: str
    target: str
    kind: str
    state: str
    command: str
    started: str            # isoformat(sep=' ', timespec='seconds')
    finished: Optional[str]
    duration: int            # seconds; computed against now() while running
    exit_code: Optional[int]
    progress: Optional[str]
    error: Optional[str]
    running: bool
    pid: Optional[int]              # remote PID of the detached job
    workdir: Optional[str]          # per-job dir on the target
    output_seq: int                 # bytes/lines of the output file consumed so far


class JobOutputDict(TypedDict):
    """Returned by DatabaseManager.job_output."""
    seq: int
    timestamp: str
    stream: str
    message: str


class EventDict(TypedDict):
    """Returned by DatabaseManager.recent_events/recent_events_cached."""
    timestamp: str
    level: str
    target: str
    message: str


class PluginEventDict(TypedDict):
    """Returned by DatabaseManager.plugin_events_cached — a narrower shape
    than EventDict (no `target`; `message` has the plugin-name prefix
    stripped)."""
    timestamp: str
    level: str
    message: str


class MetricRowDict(TypedDict):
    """Returned by DatabaseManager.latest_metrics — hand-built, not a
    peewee __data__ dump, so its keys are exact."""
    target: str
    collector: str
    metric_name: str
    value: float
    timestamp: str


class MetricModelDict(TypedDict):
    """peewee Metric.__data__ as returned by collector_metrics_cached/
    recent_metrics_raw_cached — includes every Metric column, unlike
    MetricRowDict's hand-picked subset."""
    id: int
    timestamp: Any           # datetime, not yet isoformat()'d
    target: str
    collector: str
    metric_name: str
    value: float
    metadata: Optional[str]


class LogLineModelDict(TypedDict):
    """peewee LogLine.__data__ as returned by log_lines_cached."""
    id: int
    timestamp: Any
    target: str
    source: str
    level: str
    message: str
    dedup_hash: str


class EventModelDict(TypedDict):
    """peewee Event.__data__ as returned by recent_events_raw_cached."""
    id: int
    timestamp: Any
    level: str
    message: str
    target: Optional[str]
    source_id: Optional[str]


# ssh_connector.py's (exit_code, stdout, stderr) and
# (exit_code, output) command-result shapes are already-adequate plain
# tuples, not dicts — no TypedDict needed there. Kept here only as a
# pointer for anyone looking for "the rest of the DB-adjacent row types":
# CmdResult (connectors/types.py) is the typed equivalent for command
# results; nothing analogous wraps these SSH-layer tuples since they never
# cross a module boundary un-parsed.
CmdResultTuple = Tuple[int, str, str]
