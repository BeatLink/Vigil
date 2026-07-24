import json
from typing import Any, Optional

from vigil.core.connectors.orchestration.types import CollectResult


class StorageOrchestrator:
    def __init__(self, db: Any, target: str, plugin_name: str, plugin_id: str):
        self._db = db
        self._logger = db.get_logger(target, plugin_name, plugin_id)
        self._plugin_id = plugin_id

    def apply(self, result: CollectResult) -> None:
        for name, value in result.metrics.items():
            self._logger.metric(name, value, result.metadata.get(name))
        for message, level in result.logs:
            self._logger.write(message, level=level)
        for message, level, log_time in result.log_lines:
            self._logger.log_line(message, level=level, log_time=log_time)
        if result.status is not None:
            self._db.insert_status(self._plugin_id, result.status)
        if result.snapshot is not None:
            self._logger.snapshot(result.snapshot)
        for key, value in result.settings.items():
            self._db.set_setting(key, value)

    def latest_metric(self, metric_name: str):
        """1s-TTL cached read — shared by polling logic (freshness within a
        single interval is irrelevant) and dashboard re-render ticks (where
        the cache avoids re-querying SQLite on every timer firing)."""
        return self._db.latest_metric_cached(self._plugin_id, metric_name)

    def latest_snapshot(self, default: Any = None) -> Any:
        raw = self._db.get_snapshot(self._plugin_id)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)
