import json
from typing import Any, Optional


class WebStorageOrchestrator:
    def __init__(self, db: Any, plugin_id: str):
        self._db = db
        self._plugin_id = plugin_id

    def latest_metric(self, metric_name: str):
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
