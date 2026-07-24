from typing import Any, Optional

from fastapi.responses import JSONResponse, PlainTextResponse

from vigil.collector.exporters import prometheus


def _flatten(plugins):
    for p in plugins:
        yield p
        yield from _flatten(p.children)


def register_api(app: Any, engine: Any) -> None:
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
        http_status, body = await engine.collector_client.push(
            monitor_id, token, status=status, msg=msg, value=value,
        )
        return JSONResponse(body, status_code=http_status)

    @app.get('/api/push/{monitor_id}/{token}')
    @app.post('/api/push/{monitor_id}/{token}')
    async def push(monitor_id: str, token: str, status: str = 'up',
                   msg: Optional[str] = None, value: Optional[float] = None):
        return await _handle_push(monitor_id, token, status, msg, value)
