"""TypedDict shapes for config.yaml, as returned by ConfigFileManager's
properties. Every field is optional (total=False): config.yaml has no
required top-level section (an empty file loads as {}, per
ConfigFileManager._load), and each consumer already defaults missing keys
via .get(). These types document the shape a present key must have — YAML
itself is never validated against them; a malformed config.yaml still
degrades the same way it always has (per-field try/except with a logged
fallback, or a KeyError deep in whichever module reads the bad key).
"""

from typing import Any, Dict, List, Optional, TypedDict


class SSHConfig(TypedDict, total=False):
    """A plugin's `ssh_config` block, or the shared `ssh_defaults` merged
    into it by VigilEngine._apply_ssh_defaults (plugin-level keys win)."""
    host: str
    port: int
    username: str
    key_path: str
    password: str


class DatabaseSettings(TypedDict, total=False):
    path: str
    write_batch_seconds: float


class LoggingSettings(TypedDict, total=False):
    retention_days: int
    metric_retention_days: int


class AuthSettings(TypedDict, total=False):
    username: str
    password_file: str


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
    at that call site since theme keys are a flat CSS-color mapping, not
    individually load-bearing to any other module."""
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
    ssh_config: SSHConfig
    children: List["PluginConfig"]  # group plugins only
    layout: Any                 # List[LayoutRow] | Dict[str, dict] — see spec_types.UISpec['layout']


class VigilConfig(TypedDict, total=False):
    database: DatabaseSettings
    plugins: List[PluginConfig]
    alerting: List[Dict[str, Any]]
    theme: ThemeSettings
    exporters: ExporterSettings
    logging: LoggingSettings
    ssh_defaults: SSHConfig
    control: List[Dict[str, Any]]
    auth: AuthSettings
