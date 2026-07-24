import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _tokens_match(given: str, expected: str) -> bool:
    return hmac.compare_digest(given, expected)


def _flatten(plugins):
    for p in plugins:
        yield p
        yield from _flatten(p.children)


class ActionRequest(BaseModel):
    action_id: str
    kwargs: Dict[str, Any] = {}


class SSHCommandRequest(BaseModel):
    command: str
    timeout: Optional[float] = None


class JobStartRequest(BaseModel):
    kind: str
    command: str
    redacted: Optional[str] = None
    timeout: Optional[float] = None


class PushRequest(BaseModel):
    token: str
    status: str = 'up'
    msg: Optional[str] = None
    value: Optional[float] = None


def create_internal_app(engine: Any) -> FastAPI:
    app = FastAPI(title="vigil-internal", docs_url=None, redoc_url=None)

    def _find(monitor_id: str):
        return next((p for p in _flatten(engine.plugins) if p.id == monitor_id), None)

    @app.get('/internal/health')
    def health():
        return JSONResponse({'status': 'ok'})

    @app.get('/internal/actions/{monitor_id}')
    def actions(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({'actions': plugin.get_actions()})

    @app.post('/internal/action/{monitor_id}')
    async def action(monitor_id: str, req: ActionRequest):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        try:
            success = await engine.dispatch_action(plugin, req.action_id, **req.kwargs)
        except Exception as e:
            logging.error(f"internal_api: action {req.action_id!r} on {monitor_id!r} failed: {e}")
            return JSONResponse({'error': str(e)}, status_code=500)
        return JSONResponse({'success': bool(success)})

    @app.post('/internal/poll/{monitor_id}')
    async def poll(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        collected = await engine.run_cycle_now(plugin)
        return JSONResponse({'collected': bool(collected)})

    @app.post('/internal/ssh/{monitor_id}')
    async def ssh_execute(monitor_id: str, req: SSHCommandRequest):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        result = await plugin.network.execute_raw(req.command, timeout=req.timeout)
        return JSONResponse({'status': result.exit_code, 'stdout': result.stdout, 'stderr': result.stderr})

    @app.get('/internal/job/{monitor_id}/running')
    def job_is_running(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({
            'running': plugin.network.is_running(),
            'job_id': plugin.network.current_job_id(),
        })

    @app.post('/internal/job/{monitor_id}/start')
    async def job_start(monitor_id: str, req: JobStartRequest):
        from vigil.collector.controllers.job_controller import JobRejected
        from vigil.collector.orchestration.types import JobPlan
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        try:
            job_id, exit_code = await plugin.network.run_job_plan(
                JobPlan(req.kind, req.command, redacted=req.redacted, timeout=req.timeout),
            )
        except JobRejected as e:
            return JSONResponse({'error': str(e)}, status_code=409)
        return JSONResponse({'job_id': job_id, 'exit_code': exit_code})

    @app.post('/internal/job/{monitor_id}/cancel')
    def job_cancel(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({'cancelled': plugin.network.cancel()})

    @app.post('/internal/push/{monitor_id}')
    def push(monitor_id: str, req: PushRequest):
        from vigil.plugins.push import PushCollectorPlugin
        plugin = _find(monitor_id)
        if plugin is None or not isinstance(plugin, PushCollectorPlugin):
            return JSONResponse({'error': 'not found'}, status_code=404)
        if not plugin.token or not _tokens_match(req.token, plugin.token):
            return JSONResponse({'error': 'invalid token'}, status_code=401)
        if req.status not in ('up', 'down'):
            return JSONResponse({'error': "status must be 'up' or 'down'"}, status_code=400)
        ok = plugin.record_push(status=req.status, msg=req.msg, value=req.value)
        return JSONResponse({'success': ok})

    return app


async def run_internal_api(engine: Any, host: str = '127.0.0.1', port: int = 8081):
    import uvicorn
    app = create_internal_app(engine)
    config = uvicorn.Config(app, host=host, port=port, log_level='warning')
    server = uvicorn.Server(config)
    logging.info(f"Collector internal API listening on {host}:{port}")
    await server.serve()
