import logging
from typing import Any, Dict, Optional, Tuple

import httpx


class CollectorClient:
    def __init__(self, base_url: str = 'http://127.0.0.1:8081', timeout: float = 65.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def actions(self, monitor_id: str) -> list:
        try:
            resp = await self._client.get(f'/internal/actions/{monitor_id}')
            resp.raise_for_status()
            return resp.json().get('actions', [])
        except httpx.HTTPError as e:
            logging.error(f"collector actions lookup for {monitor_id!r} failed: {e}")
            return []

    async def action(self, monitor_id: str, action_id: str, kwargs: Dict[str, Any]) -> bool:
        try:
            resp = await self._client.post(
                f'/internal/action/{monitor_id}', json={'action_id': action_id, 'kwargs': kwargs},
            )
            resp.raise_for_status()
            return bool(resp.json().get('success'))
        except httpx.HTTPError as e:
            logging.error(f"collector action {action_id!r} on {monitor_id!r} failed: {e}")
            return False

    async def poll(self, monitor_id: str) -> bool:
        try:
            resp = await self._client.post(f'/internal/poll/{monitor_id}')
            resp.raise_for_status()
            return bool(resp.json().get('collected'))
        except httpx.HTTPError as e:
            logging.error(f"collector poll of {monitor_id!r} failed: {e}")
            return False

    async def ssh_execute(self, monitor_id: str, command: str,
                          timeout: Optional[float] = None) -> Tuple[int, str, str]:
        try:
            resp = await self._client.post(
                f'/internal/ssh/{monitor_id}', json={'command': command, 'timeout': timeout},
            )
            resp.raise_for_status()
            data = resp.json()
            return data['status'], data['stdout'], data['stderr']
        except httpx.HTTPError as e:
            logging.error(f"collector ssh_execute on {monitor_id!r} failed: {e}")
            return -1, "", str(e)

    async def job_start(self, monitor_id: str, kind: str, command: str,
                        redacted: Optional[str] = None,
                        timeout: Optional[float] = None) -> Tuple[Optional[int], Optional[int]]:
        try:
            resp = await self._client.post(
                f'/internal/job/{monitor_id}/start',
                json={'kind': kind, 'command': command, 'redacted': redacted, 'timeout': timeout},
            )
            if resp.status_code == 409:
                raise JobRejectedRemote(resp.json().get('error', 'A job is already running'))
            resp.raise_for_status()
            data = resp.json()
            return data['job_id'], data['exit_code']
        except httpx.HTTPError as e:
            logging.error(f"collector job_start on {monitor_id!r} failed: {e}")
            return None, None

    async def job_cancel(self, monitor_id: str) -> bool:
        try:
            resp = await self._client.post(f'/internal/job/{monitor_id}/cancel')
            resp.raise_for_status()
            return bool(resp.json().get('cancelled'))
        except httpx.HTTPError as e:
            logging.error(f"collector job_cancel on {monitor_id!r} failed: {e}")
            return False

    async def push(self, monitor_id: str, token: str, status: str = 'up',
                   msg: Optional[str] = None, value: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
        try:
            resp = await self._client.post(
                f'/internal/push/{monitor_id}',
                json={'token': token, 'status': status, 'msg': msg, 'value': value},
            )
            return resp.status_code, resp.json()
        except httpx.HTTPError as e:
            logging.error(f"collector push to {monitor_id!r} failed: {e}")
            return 502, {'error': 'collector unreachable'}

    async def aclose(self):
        await self._client.aclose()


class JobRejectedRemote(Exception):
    pass


class RemoteSSHController:
    def __init__(self, client: CollectorClient, monitor_id: str):
        self._client = client
        self._monitor_id = monitor_id

    async def execute_action(self, command: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
        return await self._client.ssh_execute(self._monitor_id, command, timeout=timeout)


class RemoteJobController:
    def __init__(self, client: CollectorClient, monitor_id: str, db: Any):
        self._client = client
        self._monitor_id = monitor_id
        self._db = db

    def is_running(self) -> bool:
        running = self._db.running_jobs(plugin_id=self._monitor_id)
        return bool(running)

    def current_job_id(self) -> Optional[int]:
        running = self._db.running_jobs(plugin_id=self._monitor_id)
        return running[0]['id'] if running else None

    async def run_job(self, kind: str, command: str, redacted: Optional[str] = None,
                      on_line=None, timeout: Optional[float] = None) -> Tuple[int, int]:
        job_id, exit_code = await self._client.job_start(
            self._monitor_id, kind, command, redacted=redacted, timeout=timeout,
        )
        if job_id is None:
            raise JobRejectedRemote(f"Could not start {kind} job (collector unreachable or rejected)")
        return job_id, exit_code

    async def cancel(self) -> bool:
        return await self._client.job_cancel(self._monitor_id)

    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        return self._db.recent_jobs(plugin_id=self._monitor_id, limit=limit, kind=kind)

    def output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        return self._db.job_output(job_id, after_seq=after_seq, limit=limit)
