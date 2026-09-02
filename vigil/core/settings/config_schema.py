"""TypedDict shapes for config.yaml, as returned by ConfigFileManager's
properties. Every field is optional (total=False): config.yaml has no
required top-level section (an empty file loads as {}, per
ConfigFileManager._load), and each consumer already defaults missing keys
via .get(). These types document the shape a present key must have — YAML
itself is never validated against them; a malformed config.yaml still
degrades the same way it always has (per-field try/except with a logged
fallback, or a KeyError deep in whichever module reads the bad key).
"""

from typing import Any, Dict, List, TypedDict


class SSHConfig(TypedDict, total=False):
    """A plugin's `ssh_config` block, or the shared `ssh_defaults` merged
    into it by VigilEngine._apply_ssh_defaults (plugin-level keys win)."""
    host: str
    port: int
    username: str
    key_path: str
    password: str


class AgentSettings(TypedDict, total=False):
    """One entry in the top-level `agents:` list. The agent dials into the
    dashboard's port over a WebSocket and authenticates with `token` (or
    `token_file`, which is read once at startup and keeps the secret out of a
    generated config.yaml); `host` is only a display/label value, since the
    server never dials the agent."""
    id: str
    token: str
    token_file: str
    host: str


class DatabaseSettings(TypedDict, total=False):
    path: str
    write_batch_seconds: float


class LoggingSettings(TypedDict, total=False):
    retention_days: int
    metric_retention_days: int


class MemorySettings(TypedDict, total=False):
    """How much history the in-memory state store keeps per stream. State is
    held in Python objects and served to the UI from there, so these bound
    how far back a chart/table can read without touching the database.
    Distinct from `logging.retention_days`, which bounds the database file."""

    metric_history: int
    event_history: int
    log_history: int
    job_output: int
    jobs_per_plugin: int
    finished_job_output: int


class AuthSettings(TypedDict, total=False):
    """The single operator account guarding the dashboard, and how long a
    sign-in lasts. Each secret may be given inline or as a ``*_file`` path read
    once at startup. Without ``session_secret`` a key is generated per start,
    so a restart signs everyone out."""
    username: str
    password: str
    password_file: str
    session_secret: str
    session_secret_file: str
    session_hours: int
    remember_days: int


class InfluxDBExporterSettings(TypedDict, total=False):
    url: str
    interval: int
    org: str
    bucket: str
    token: str
    database: str


class ExporterSettings(TypedDict, total=False):
    influxdb: InfluxDBExporterSettings


class ThemeSettings(TypedDict, total=False):
    """Consumed by core/ui/theme.py's configure(); kept as Dict[str, Any]
    at that call site since theme keys are a flat mapping onto Halon tokens,
    not individually load-bearing to any other module. `scheme` is the one
    non-color key: auto (follow the browser), light, or dark."""
    scheme: str
    primary: str
    background: str


class PluginConfig(TypedDict, total=False):
    """One entry in config.yaml's `plugins` list (or a group's nested
    `children`). `type` selects which vigil.plugins.<type> module
    VigilEngine.setup_modules loads; everything else is plugin-specific
    and read via PluginConfigMixin._init_config or the plugin's own
    __init__/commands()/parse(). Deliberately not exhaustive beyond the
    keys every plugin can rely on — a per-plugin config shape would need
    one TypedDict per plugin type for marginal benefit, since plugins
    already validate their own keys via .get() with sensible defaults."""
    name: str
    type: str
    id: str
    interval: Any               # int seconds, or a duration string like '5m' — see parse_duration
    timeout: Any
    target_host: str
    agent: str                  # id of the agent to reach this target through, instead of SSH
    ssh_config: SSHConfig
    children: List["PluginConfig"]  # group plugins only
    layout: Any                 # List[LayoutRow] | Dict[str, dict] — see spec_types.UISpec['layout']


class VigilConfig(TypedDict, total=False):
    agents: List[AgentSettings]
    database: DatabaseSettings
    plugins: List[PluginConfig]
    alerting: List[Dict[str, Any]]
    theme: ThemeSettings
    exporters: ExporterSettings
    logging: LoggingSettings
    memory: MemorySettings
    ssh_defaults: SSHConfig
    control: List[Dict[str, Any]]
    auth: AuthSettings
