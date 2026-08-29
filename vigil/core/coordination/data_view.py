"""PluginDataView — the read-only projection of the state a pure plugin (and
the UI Engine rendering it) is allowed to hold.

A plugin never holds ``db``/``network``; it holds one ``self.data``
(a ``PluginDataView``) for reads. Writes go through the Coordination Engine,
which calls ``db.apply_result(target, id, name, result)`` on the collection/
action path, never through this view. Every method here is a read; there is
deliberately no ``apply``.

Every read is served from the in-memory state store, so these are dict
lookups and slices over live Python objects rather than queries. There is no
``max_age``/caching parameter anywhere: reads see what the last collector
wrote, immediately, and there is no query to amortise.
"""

from typing import Any, Dict, Optional


class PluginDataView:
    def __init__(self, db: Any, plugin_id: str):
        self._db = db
        self._id = plugin_id

    # --- plugin-scoped reads (scoped to this plugin's id) ---
    def latest_metric(self, metric_name: str):
        return self._db.latest_metric(self._id, metric_name)

    def latest_collector_metrics(self):
        return self._db.latest_collector_metrics(self._id)

    def latest_snapshot(self, default: Any = None) -> Any:
        return self._db.latest_snapshot(self._id, default)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)

    # --- reads the UI tables / bespoke plugins need ---
    def latest_statuses(self) -> Dict[str, str]:
        return self._db.latest_statuses()

    def latest_status(self, plugin_id: str) -> str:
        return self._db.latest_status(plugin_id)

    def latest_status_time(self, plugin_id: Optional[str] = None):
        return self._db.latest_status_time(plugin_id or self._id)

    def collector_metrics(self, plugin_id: str, limit: int = 15):
        return self._db.collector_metrics(plugin_id, limit=limit)

    def metric_history(self, plugin_id: str, metric_name: str, limit: int = 30):
        return self._db.metric_history(plugin_id, metric_name, limit=limit)

    def log_lines(self, target: str, filter_prefix: str = '', limit: int = 15):
        return self._db.log_lines(target, filter_prefix, limit=limit)

    def plugin_events(self, plugin_id: str = '', prefix: str = '', target: str = '',
                      limit: int = 100):
        return self._db.plugin_events(plugin_id, prefix, target, limit=limit)

    def job(self, job_id: int):
        return self._db.get_job(job_id)
