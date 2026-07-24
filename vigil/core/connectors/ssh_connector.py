import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, Callable

import asyncssh

_STATE_DIR = Path(os.environ.get("VIGIL_SSH_CONTROL_DIR",
                                 Path(tempfile.gettempdir()) / "vigil-ssh"))

_KILL_GRACE_SECONDS = 5.0

_MAX_CONCURRENT_PER_HOST = 8
_MAX_CONCURRENT_JOBS_PER_HOST = 2

asyncssh.set_log_level(logging.WARNING)


def _known_hosts_path() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / "known_hosts"


class _TofuClient(asyncssh.SSHClient):
    def __init__(self, host: str, host_key_alias: str):
        self._host = host
        self._alias = host_key_alias

    def _load_known_fingerprints(self) -> set:
        path = _known_hosts_path()
        if not path.exists():
            return set()
        try:
            exact = asyncssh.read_known_hosts(str(path)).match(self._alias, '', 0)[0]
            return {k.get_fingerprint() for k in exact}
        except Exception as e:
            logging.warning(f"ssh: could not read known_hosts for {self._alias}: {e}")
            return set()

    def validate_host_public_key(self, host: str, addr: str, port: int,
                                 key: "asyncssh.SSHKey") -> bool:
        known_fps = self._load_known_fingerprints()
        if known_fps:
            matches = key.get_fingerprint() in known_fps
            if not matches:
                logging.error(
                    f"ssh: host key for {self._alias} does NOT match the stored "
                    f"key — refusing to connect (possible MITM or reinstalled host)"
                )
            return matches

        try:
            line = f"{self._alias} {key.export_public_key().decode().strip()}\n"
            with open(_known_hosts_path(), 'a') as f:
                f.write(line)
            logging.info(f"ssh: trusting and storing new host key for {self._alias}")
        except OSError as e:
            logging.error(f"ssh: could not persist host key for {self._alias}: {e}")
            return False
        return True


class SSHConnection:
    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "SSHConnection":
        ssh_cfg = config.get('ssh_config', {})
        return cls(
            host=ssh_cfg.get('host', config.get('target_host', 'localhost')),
            username=ssh_cfg.get('username'),
            key_path=ssh_cfg.get('key_path'),
            password=ssh_cfg.get('password'),
            port=ssh_cfg.get('port'),
        )

    def __init__(self, host: str, username: Optional[str] = None, key_path: Optional[str] = None,
                 password: Optional[str] = None, port: Optional[int] = 22):
        self.host = host
        self.username = username
        self.key_path = key_path
        self.password = password
        self.port = port if port is not None else 22
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._connect_lock = asyncio.Lock()
        self._channel_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PER_HOST)
        self._job_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_JOBS_PER_HOST)

    def _host_key_alias(self) -> str:
        user = self.username or os.environ.get("USER", "")
        return f"{user}@{self.host}:{self.port}"

    async def _get_connection(self, connect_timeout: float) -> asyncssh.SSHClientConnection:
        if self._conn is not None and not self._conn.is_closed():
            return self._conn

        async with self._connect_lock:
            if self._conn is not None and not self._conn.is_closed():
                return self._conn

            alias = self._host_key_alias()
            options = dict(
                host=self.host,
                port=self.port,
                username=self.username,
                known_hosts=[],
                client_factory=lambda: _TofuClient(self.host, alias),
                host_key_alias=alias,
                connect_timeout=connect_timeout,
                keepalive_interval=5,
                keepalive_count_max=2,
            )
            if self.key_path:
                options['client_keys'] = [self.key_path]
                options['agent_path'] = None
            self._conn = await asyncssh.connect(**options)
            return self._conn

    async def execute(self, command: str, timeout: float = 30.0,
                      connect_timeout: float = 5.0) -> Tuple[int, str, str]:
        proc = None
        try:
            conn = await asyncio.wait_for(
                self._get_connection(connect_timeout), timeout=connect_timeout + 5
            )
            async with self._channel_semaphore:
                proc = await conn.create_process(command)
                try:
                    result = await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._kill_process(proc)
                    return -1, "", f"Timed out after {timeout}s"

                return (
                    result.exit_status if result.exit_status is not None else -1,
                    result.stdout.strip(),
                    result.stderr.strip(),
                )
        except (asyncssh.Error, OSError) as e:
            logging.error(f"SSH execution failed on {self.host}: {e}")
            if proc is not None:
                await self._kill_process(proc)
            return -1, "", str(e)

    @staticmethod
    async def _kill_process(proc: "asyncssh.SSHClientProcess") -> None:
        if proc.exit_status is not None or proc.is_closing():
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait_closed(), timeout=_KILL_GRACE_SECONDS)
            return
        except (asyncio.TimeoutError, OSError):
            pass
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait_closed(), timeout=_KILL_GRACE_SECONDS)
        except (asyncio.TimeoutError, OSError):
            pass

    async def execute_streaming(self, command: str, on_line: Optional[Callable[[str, str], None]] = None,
                                connect_timeout: float = 5.0, timeout: Optional[float] = None,
                                should_cancel: Optional[Callable[[], bool]] = None) -> Tuple[int, str]:
        async with self._job_semaphore:
            return await self._execute_streaming_body(
                command, on_line, connect_timeout, timeout, should_cancel,
            )

    async def _execute_streaming_body(self, command: str, on_line: Optional[Callable[[str, str], None]],
                                       connect_timeout: float, timeout: Optional[float],
                                       should_cancel: Optional[Callable[[], bool]]) -> Tuple[int, str]:
        proc = None
        try:
            conn = await asyncio.wait_for(
                self._get_connection(connect_timeout), timeout=connect_timeout + 5
            )
            proc = await conn.create_process(
                command, term_type='xterm-vigil', stderr=asyncssh.STDOUT,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
            logging.error(f"SSH streaming start failed on {self.host}: {e}")
            return -1, str(e)

        start = asyncio.get_event_loop().time()
        cancelled = False
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    line = None

                if line:
                    if on_line is not None:
                        try:
                            on_line("stdout", line.rstrip("\r\n"))
                        except Exception as e:
                            logging.error(f"Job output handler failed: {e}")
                elif proc.stdout.at_eof():
                    break

                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                if timeout is not None and (asyncio.get_event_loop().time() - start) > timeout:
                    logging.error(f"SSH streaming timed out after {timeout}s on {self.host}")
                    await self._kill_process(proc)
                    return -1, f"Timed out after {timeout}s"

            if cancelled:
                await self._kill_process(proc)
                return 130, "Cancelled"

            await proc.wait()
            return proc.exit_status if proc.exit_status is not None else -1, ""
        except (asyncssh.Error, OSError) as e:
            logging.error(f"SSH streaming failed on {self.host}: {e}")
            await self._kill_process(proc)
            return -1, str(e)

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
