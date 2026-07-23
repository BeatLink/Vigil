"""
REST API for Vigil.

Registers read-only JSON endpoints (plus a Prometheus /metrics endpoint) on the
NiceGUI FastAPI app so external tools can consume Vigil's state without scraping
the dashboard HTML. Mounted from init_gui() via register_api(app, engine).

Runs in the web process. Everything here is a plain read against the shared
database (`engine.db`) — the one exception, /api/push, proxies to the
collector's internal API (see modules/internal_api.py) because recording a
heartbeat means calling a live PushCollectorPlugin instance, which only
exists there.

Endpoints:
  GET /api/health                 -> {"status": "ok"}
  GET /api/monitors               -> [{id, name, type, target, status}, ...]
  GET /api/monitors/{id}          -> single monitor + its latest metrics
  GET /api/metrics                -> latest value of every metric
  GET /api/events                 -> recent events (?level=&target=&search=&limit=)
  GET /metrics                    -> Prometheus exposition format (text/plain)

  GET/POST /api/push/{id}/{token} -> record a heartbeat for a push monitor
                                      (?status=up|down&msg=&value=)
"""
from typing import Any, Optional

from fastapi.responses import JSONResponse, PlainTextResponse

from vigil.collector.exporters import prometheus


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
        # Match on id: metrics are keyed by it, and display names repeat across
        # groups, so filtering by name returns other monitors' readings too.
        metrics = [m for m in db.latest_metrics() if m['collector'] == target.id]
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

    async def _handle_push(monitor_id: str, token: str, status: str, msg: Optional[str],
                           value: Optional[float]):
        # Deliberately bypasses Basic Auth (see register_auth): a push monitor
        # is checked in by external scripts/cron jobs that have no reason to
        # hold the dashboard's admin credentials. The per-monitor token is
        # this endpoint's own credential instead — verified collector-side
        # (see CollectorClient.push), since the real token lives on the
        # collector's PushCollectorPlugin instance, not anything this
        # process holds.
        http_status, body = await engine.collector_client.push(
            monitor_id, token, status=status, msg=msg, value=value,
        )
        return JSONResponse(body, status_code=http_status)

    @app.get('/api/push/{monitor_id}/{token}')
    @app.post('/api/push/{monitor_id}/{token}')
    async def push(monitor_id: str, token: str, status: str = 'up',
                   msg: Optional[str] = None, value: Optional[float] = None):
        return await _handle_push(monitor_id, token, status, msg, value)
