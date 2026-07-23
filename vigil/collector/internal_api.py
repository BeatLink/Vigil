"""
Collector-side internal API.

Vigil runs as two OS processes: a collector (polls targets, owns live plugin
instances) and a web process (serves the dashboard, reads the shared SQLite
DB directly). Almost everything the web process needs is a plain DB read —
but a few things only make sense against a live SSH connection, which exists
only here: triggering an action (restart a service, kill a process), an
ad-hoc SSH control command a plugin builds internally (e.g. service_list.py's
"view unit file"), job control (borg backup start/cancel), "Poll Now", and
recording a push-monitor heartbeat.

This module is that seam: a small FastAPI app, bound to loopback only, that
the web process's remote_proxy client calls. It is not a public API — it has
no auth of its own and must never be reachable from anywhere but the web
process on the same host (see register_internal_api's host binding note).
Vigil's existing REST API (core/api.py) is the public, read-only, optionally
Basic-Auth-gated surface; this one is private plumbing between Vigil's own
two processes.
"""
import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _tokens_match(given: str, expected: str) -> bool:
    """Constant-time token comparison, so a wrong guess can't be timed."""
    return hmac.compare_digest(given, expected)


def _flatten(plugins):
    """Yield every plugin instance in the tree (groups and leaves)."""
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
    """
    Build the collector's internal API app.

    Kept as a separate FastAPI instance (not mounted on the same app as the
    public REST API) so it can be bound to its own loopback-only port —
    accidentally exposing it alongside the public API on a public bind
    address would let anyone who reaches Vigil run arbitrary commands on
    every monitored host.
    """
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
            success = await plugin.on_action(req.action_id, **req.kwargs)
        except Exception as e:
            logging.error(f"internal_api: action {req.action_id!r} on {monitor_id!r} failed: {e}")
            return JSONResponse({'error': str(e)}, status_code=500)
        return JSONResponse({'success': bool(success)})

    @app.post('/internal/poll/{monitor_id}')
    async def poll(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        collected = await plugin.run_cycle()
        return JSONResponse({'collected': bool(collected)})

    @app.post('/internal/ssh/{monitor_id}')
    async def ssh_execute(monitor_id: str, req: SSHCommandRequest):
        """
        Run a pre-built command through a plugin's SSHController.

        `command` is always constructed server-side by trusted plugin code
        (e.g. service_list.py's "view unit file" button), the same as when
        ssh_controller.execute_action was called in-process — this endpoint
        does not change that trust model, it only relocates where the call
        happens. It must stay unreachable from anywhere but the web process
        (see register_internal_api).
        """
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        status, stdout, stderr = await plugin.ssh_controller.execute_action(
            req.command, timeout=req.timeout,
        )
        return JSONResponse({'status': status, 'stdout': stdout, 'stderr': stderr})

    @app.get('/internal/job/{monitor_id}/running')
    def job_is_running(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({
            'running': plugin.job_controller.is_running(),
            'job_id': plugin.job_controller.current_job_id(),
        })

    @app.post('/internal/job/{monitor_id}/start')
    async def job_start(monitor_id: str, req: JobStartRequest):
        from vigil.collector.controllers.job_controller import JobRejected
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        try:
            job_id, exit_code = await plugin.job_controller.run_job(
                req.kind, req.command, redacted=req.redacted, timeout=req.timeout,
            )
        except JobRejected as e:
            return JSONResponse({'error': str(e)}, status_code=409)
        return JSONResponse({'job_id': job_id, 'exit_code': exit_code})

    @app.post('/internal/job/{monitor_id}/cancel')
    def job_cancel(monitor_id: str):
        plugin = _find(monitor_id)
        if plugin is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({'cancelled': plugin.job_controller.cancel()})

    @app.post('/internal/push/{monitor_id}')
    def push(monitor_id: str, req: PushRequest):
        """
        Record a push-monitor heartbeat.

        The token check happens here, not in the web process's /api/push
        route: `plugin.token` is config the collector-side PushCollectorPlugin
        holds (see push.py), and the web process's UIPlugin never has it —
        forwarding the caller's token here for the collector to verify
        against the real one, rather than the web process trying to check it
        itself, means the secret is compared in exactly one place.
        """
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
    """
    Serve the internal API as an asyncio task on the collector's own event
    loop, alongside VigilEngine.run()'s monitor scheduler.

    Bound to `host` (loopback by default — see the module docstring on why
    this must never be a public bind address) rather than reusing the
    dashboard's NiceGUI/FastAPI app, which listens on the public port.
    """
    import uvicorn
    app = create_internal_app(engine)
    config = uvicorn.Config(app, host=host, port=port, log_level='warning')
    server = uvicorn.Server(config)
    logging.info(f"Collector internal API listening on {host}:{port}")
    await server.serve()
