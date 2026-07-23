import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

class ConfigFileManager:
    """
    Handles loading and processing of the Vigil YAML configuration file.
    """
    def __init__(self, config_path: str):
        self.path = Path(config_path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        """Loads YAML content from the config path with basic error handling."""
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
        """Returns the database section or default path."""
        return self.data.get('database', {'path': 'vigil.db'})

    # Default window over which queued DB writes (metrics, status history,
    # events, log lines) are batched into one commit. Larger values mean
    # fewer disk commits/fsyncs under load, at the cost of losing up to this
    # many seconds of unwritten data on a crash.
    DEFAULT_WRITE_BATCH_SECONDS = 5.0

    @property
    def write_batch_seconds(self) -> float:
        """
        Seconds the background DB writer accumulates queued writes before
        committing them as one transaction.

        Read from `database.write_batch_seconds`; defaults to
        DEFAULT_WRITE_BATCH_SECONDS. A value <= 0 disables batching (each
        write commits immediately, as before).
        """
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
        """Returns the list of plugin configurations."""
        return self.data.get('plugins', [])

    @property
    def alert_handlers(self) -> List[Dict[str, Any]]:
        """Returns the list of alerting configurations."""
        return self.data.get('alerting', [])

    @property
    def theme_settings(self) -> Dict[str, Any]:
        """Returns the theme overrides section (may be empty)."""
        return self.data.get('theme', {})

    @property
    def exporters(self) -> Dict[str, Any]:
        """Returns the exporters section (e.g. influxdb push config). May be empty."""
        return self.data.get('exporters', {})

    # Default log retention in days when not configured. Chosen to keep a
    # useful history without letting the SQLite file grow without bound.
    DEFAULT_LOG_RETENTION_DAYS = 30

    @property
    def logging_settings(self) -> Dict[str, Any]:
        """Returns the logging section (may be empty)."""
        return self.data.get('logging', {})

    @property
    def log_retention_days(self) -> int:
        """
        Number of days to keep collected log lines before pruning.

        Read from `logging.retention_days`; defaults to
        DEFAULT_LOG_RETENTION_DAYS. A value <= 0 disables pruning (logs kept
        indefinitely).
        """
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
        """
        Returns global SSH connection defaults (may be empty).

        These are merged into every plugin's ``ssh_config`` unless the plugin
        overrides a given key, so a single ``username``/``key_path`` can apply
        across all monitors instead of being repeated on each one.
        """
        return self.data.get('ssh_defaults', {})

    @property
    def controllers(self) -> List[Dict[str, Any]]:
        """Returns the list of control/remediation configurations."""
        return self.data.get('control', [])

    @property
    def auth_settings(self) -> Dict[str, Any]:
        """
        Returns the auth section (may be empty).

        Expected keys: `username`, and either `password` or `password_file`
        (a path read at startup, so the secret need not enter the YAML
        config or the Nix store). When neither is set, auth is disabled and
        the dashboard/API are unauthenticated — as they always were.
        """
        return self.data.get('auth', {})