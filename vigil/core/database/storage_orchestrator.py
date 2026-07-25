import json
from typing import Any, Optional

from vigil.core.connectors.types import CollectResult


class StorageOrchestrator:
    """The per-plugin write surface. One instance is bound to a plugin's
    (target, name, id) at construction, so callers persist a whole
    ``CollectResult`` with a single ``apply()`` instead of re-supplying that
    identity on every table-level DatabaseManager call.

    It also exposes the four scoped single-writes (write/metric/log_line/
    snapshot) that ``apply()`` fans out to, plus the plugin-scoped reads the
    ``PluginDataView`` delegates to. This is the only place that knows how to
    translate the plugin-facing CollectResult contract into DatabaseManager's
    table-level calls."""

    def __init__(self, db: Any, target: str, plugin_name: str, plugin_id: str):
        self._db = db
        self.target = target
        self.plugin_name = plugin_name
        self.plugin_id = plugin_id or plugin_name

    # --- scoped single-writes (identity baked in from __init__) ---
    def write(self, message: str, level: str = "INFO") -> None:
        self._db.insert_event(level, f"[{self.plugin_name}] {message}",
                              self.target, source_id=self.plugin_id)

    def metric(self, name: str, value: float, metadata: Optional[str] = None) -> None:
        self._db.insert_metric(self.target, self.plugin_id, name, value, metadata)

    def log_line(self, message: str, level: str = "INFO",
                 log_time: Optional[str] = None) -> None:
        self._db.insert_log_line(self.target, self.plugin_id, level, message, log_time)

    def snapshot(self, rows: Any) -> None:
        self._db.set_snapshot(self.plugin_id, json.dumps(rows))

    # --- the CollectResult fan-out ---
    def apply(self, result: CollectResult) -> None:
        for name, value in result.metrics.items():
            self.metric(name, value, result.metadata.get(name))
        for message, level in result.logs:
            self.write(message, level=level)
        for message, level, log_time in result.log_lines:
            self.log_line(message, level=level, log_time=log_time)
        if result.status is not None:
            self._db.insert_status(self.plugin_id, result.status)
        if result.snapshot is not None:
            self.snapshot(result.snapshot)
        for key, value in result.settings.items():
            self._db.set_setting(key, value)

    # --- plugin-scoped reads (delegated to by PluginDataView) ---
    def latest_metric(self, metric_name: str):
        """1s-TTL cached read — shared by polling logic (freshness within a
        single interval is irrelevant) and dashboard re-render ticks (where
        the cache avoids re-querying SQLite on every timer firing)."""
        return self._db.latest_metric_cached(self.plugin_id, metric_name)

    def latest_snapshot(self, default: Any = None) -> Any:
        raw = self._db.get_snapshot(self.plugin_id)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)
