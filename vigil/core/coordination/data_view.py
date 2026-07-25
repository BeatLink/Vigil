"""PluginDataView — the read-only projection of the Database Engine that a
pure plugin (and the UI Engine rendering it) is allowed to hold.

A plugin never holds ``db``/``network``; it holds one ``self.data``
(a ``PluginDataView``) for reads. Writes go through the Coordination Engine,
which calls ``db.apply_result(target, id, name, result)`` on the collection/
action path, never through this view. Every method here is a read; there is
deliberately no ``apply``.

The view scopes the plugin-id-keyed Database Engine reads
(``latest_metric``/``latest_snapshot``/``get_setting``) plus the handful of
other reads the UI tables and bespoke plugins need, so the UI depends only on
this read surface rather than reaching through the plugin into the database.
"""

from typing import Any, Dict, Optional


class PluginDataView:
    def __init__(self, db: Any, plugin_id: str, target: str, plugin_name: str):
        self._db = db
        self._id = plugin_id
        self._target = target

    # --- plugin-scoped reads (scoped to this plugin's id) ---
    def latest_metric(self, metric_name: str):
        return self._db.latest_metric_cached(self._id, metric_name)

    def latest_snapshot(self, default: Any = None) -> Any:
        return self._db.latest_snapshot(self._id, default)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)

    # --- direct Database Engine reads the UI tables / bespoke plugins need ---
    def latest_statuses(self, max_age: float = 2.0) -> Dict[str, str]:
        return self._db.latest_statuses(max_age=max_age)

    # These mirror the Database Engine's cached-read signatures exactly so the
    # UI Engine's table/chart readers are drop-in replacements for the former
    # ``plugin.db.*`` calls.
    def latest_status_cached(self, collector_id: str, max_age: float = 1.0):
        return self._db.latest_status_cached(collector_id, max_age=max_age)

    def latest_status_time(self, collector_id: Optional[str] = None, max_age: float = 1.0):
        return self._db.latest_status_time_cached(collector_id or self._id, max_age=max_age)

    def collector_metrics_cached(self, collector: str, limit: int = 15, max_age: float = 1.0):
        return self._db.collector_metrics_cached(collector, limit=limit, max_age=max_age)

    def metric_history_cached(self, collector: str, metric_name: str, limit: int = 30, max_age: float = 1.0):
        return self._db.metric_history_cached(collector, metric_name, limit=limit, max_age=max_age)

    def log_lines_cached(self, target: str, filter_prefix: str = '', limit: int = 15, max_age: float = 1.0):
        return self._db.log_lines_cached(target, filter_prefix, limit=limit, max_age=max_age)

    def plugin_events_cached(self, plugin_id: str = '', prefix: str = '', target: str = '',
                             limit: int = 100, max_age: float = 1.0):
        return self._db.plugin_events_cached(plugin_id, prefix, target, limit=limit, max_age=max_age)

    def job(self, job_id: int):
        return self._db.get_job(job_id)
