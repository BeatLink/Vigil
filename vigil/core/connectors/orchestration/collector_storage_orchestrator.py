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
        from vigil.core.database.database import Metric
        return (
            Metric.select()
            .where((Metric.collector == self._plugin_id) & (Metric.metric_name == metric_name))
            .order_by(Metric.timestamp.desc())
            .first()
        )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)
