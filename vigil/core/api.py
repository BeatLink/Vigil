"""
REST API for Vigil.

Registers read-only JSON endpoints (plus a Prometheus /metrics endpoint) on the
NiceGUI FastAPI app so external tools can consume Vigil's state without scraping
the dashboard HTML. Mounted from init_gui() via register_api(app, engine).

Endpoints:
  GET /api/health                 -> {"status": "ok"}
  GET /api/monitors               -> [{id, name, type, target, status}, ...]
  GET /api/monitors/{id}          -> single monitor + its latest metrics
  GET /api/metrics                -> latest value of every metric
  GET /api/events                 -> recent events (?level=&target=&search=&limit=)
  GET /metrics                    -> Prometheus exposition format (text/plain)
"""
from typing import Any, Optional

from fastapi import Response
from fastapi.responses import JSONResponse, PlainTextResponse

from vigil.core.modules.exporters import prometheus


def _flatten(plugins):
    """Yield every plugin instance in the tree (groups and leaves)."""
    for p in plugins:
        yield p
        yield from _flatten(p.children)


def register_api(app: Any, engine: Any) -> None:
    """Attach Vigil's REST + Prometheus routes to the given FastAPI app."""
    db = engine.db

    def _monitor_summary(statuses):
        out = []
        for p in _flatten(engine.plugins):
            out.append({
                'id': p.id,
                'name': p.name,
                'type': p.config.get('type'),
                'target': getattr(p, 'target', None),
                'status': statuses.get(p.id, 'offline'),
                'is_group': bool(p.children),
            })
        return out

    @app.get('/api/health')
    def health():
        return JSONResponse({'status': 'ok'})

    @app.get('/api/monitors')
    def monitors():
        return JSONResponse(_monitor_summary(db.latest_statuses()))

    @app.get('/api/monitors/{monitor_id}')
    def monitor_detail(monitor_id: str):
        statuses = db.latest_statuses()
        target = next((p for p in _flatten(engine.plugins) if p.id == monitor_id), None)
        if target is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        metrics = [m for m in db.latest_metrics() if m['collector'] == target.name]
        return JSONResponse({
            'id': target.id,
            'name': target.name,
            'type': target.config.get('type'),
            'target': getattr(target, 'target', None),
            'status': statuses.get(target.id, 'offline'),
            'metrics': metrics,
        })

    @app.get('/api/metrics')
    def metrics():
        return JSONResponse(db.latest_metrics())

    @app.get('/api/events')
    def events(level: Optional[str] = None, target: Optional[str] = None,
               search: Optional[str] = None, limit: int = 200):
        limit = max(1, min(int(limit), 2000))
        return JSONResponse(db.recent_events(limit=limit, level=level, target=target, search=search))

    @app.get('/metrics')
    def prometheus_metrics():
        return PlainTextResponse(prometheus.render(db),
                                 media_type='text/plain; version=0.0.4; charset=utf-8')
