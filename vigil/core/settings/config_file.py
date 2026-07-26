import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

from vigil.core.settings.config_schema import (
    AuthSettings, DatabaseSettings, ExporterSettings, PluginConfig, SSHConfig,
    ThemeSettings, VigilConfig,
)
from vigil.core.state import BufferSizes

class ConfigFileManager:
    def __init__(self, config_path: str):
        self.path = Path(config_path)
        self.data: VigilConfig = self._load()

    def _load(self) -> VigilConfig:
        if not self.path.exists():
            logging.warning(f"Configuration file not found at {self.path}. Using empty configuration defaults.")
            return {}

        try:
            with open(self.path, 'r') as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except yaml.YAMLError as e:
            logging.error(f"Failed to parse YAML configuration at {self.path}: {e}")
            return {}

    @property
    def database_settings(self) -> DatabaseSettings:
        return self.data.get('database', {'path': 'vigil.db'})

    DEFAULT_WRITE_BATCH_SECONDS = 1.0

    @property
    def write_batch_seconds(self) -> float:
        value = self.database_settings.get('write_batch_seconds', self.DEFAULT_WRITE_BATCH_SECONDS)
        try:
            return float(value)
        except (TypeError, ValueError):
            logging.warning(
                f"Invalid database.write_batch_seconds={value!r}; "
                f"falling back to {self.DEFAULT_WRITE_BATCH_SECONDS}"
            )
            return self.DEFAULT_WRITE_BATCH_SECONDS

    @property
    def plugins(self) -> List[PluginConfig]:
        return self.data.get('plugins', [])

    @property
    def alert_handlers(self) -> List[Dict[str, Any]]:
        return self.data.get('alerting', [])

    @property
    def theme_settings(self) -> ThemeSettings:
        return self.data.get('theme', {})

    @property
    def exporters(self) -> ExporterSettings:
        return self.data.get('exporters', {})

    DEFAULT_LOG_RETENTION_DAYS = 30

    @property
    def logging_settings(self) -> Dict[str, Any]:
        return self.data.get('logging', {})

    @property
    def log_retention_days(self) -> int:
        value = self.logging_settings.get('retention_days', self.DEFAULT_LOG_RETENTION_DAYS)
        try:
            return int(value)
        except (TypeError, ValueError):
            logging.warning(
                f"Invalid logging.retention_days={value!r}; "
                f"falling back to {self.DEFAULT_LOG_RETENTION_DAYS}"
            )
            return self.DEFAULT_LOG_RETENTION_DAYS

    @property
    def metric_retention_days(self) -> int:
        """How long to keep Metric and StatusHistory rows. Metrics power the
        charts, so this is a separate knob from log retention; when unset it
        defaults to log_retention_days so existing configs get bounded metric
        growth automatically. 0 disables (keep forever)."""
        value = self.logging_settings.get('metric_retention_days')
        if value is None:
            return self.log_retention_days
        try:
            return int(value)
        except (TypeError, ValueError):
            logging.warning(
                f"Invalid logging.metric_retention_days={value!r}; "
                f"falling back to log retention ({self.log_retention_days}d)"
            )
            return self.log_retention_days

    @property
    def memory_settings(self) -> Dict[str, Any]:
        return self.data.get('memory', {})

    @property
    def buffer_sizes(self) -> BufferSizes:
        """How much history the in-memory state store keeps per stream.
        Unset or unparseable values fall back to the BufferSizes defaults,
        which already exceed everything the UI reads."""
        defaults = BufferSizes()
        configured = self.memory_settings
        values: Dict[str, int] = {}
        for field in ('metric_history', 'event_history', 'log_history',
                      'job_output', 'jobs_per_plugin',
                      'finished_job_output'):
            raw = configured.get(field)
            if raw is None:
                continue
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                logging.warning(
                    f"Invalid memory.{field}={raw!r}; falling back to "
                    f"{getattr(defaults, field)}"
                )
                continue
            if parsed <= 0:
                logging.warning(
                    f"memory.{field} must be positive (got {parsed}); falling back "
                    f"to {getattr(defaults, field)}"
                )
                continue
            values[field] = parsed
        return BufferSizes(**values) if values else defaults

    @property
    def ssh_defaults(self) -> SSHConfig:
        return self.data.get('ssh_defaults', {})

    @property
    def controllers(self) -> List[Dict[str, Any]]:
        return self.data.get('control', [])

    @property
    def auth_settings(self) -> AuthSettings:
        return self.data.get('auth', {})