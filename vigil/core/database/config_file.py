import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

class ConfigFileManager:
    def __init__(self, config_path: str):
        self.path = Path(config_path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
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
    def database_settings(self) -> Dict[str, Any]:
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
    def plugins(self) -> List[Dict[str, Any]]:
        return self.data.get('plugins', [])

    @property
    def alert_handlers(self) -> List[Dict[str, Any]]:
        return self.data.get('alerting', [])

    @property
    def theme_settings(self) -> Dict[str, Any]:
        return self.data.get('theme', {})

    @property
    def exporters(self) -> Dict[str, Any]:
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
    def ssh_defaults(self) -> Dict[str, Any]:
        return self.data.get('ssh_defaults', {})

    @property
    def controllers(self) -> List[Dict[str, Any]]:
        return self.data.get('control', [])

    @property
    def auth_settings(self) -> Dict[str, Any]:
        return self.data.get('auth', {})