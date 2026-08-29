"""SSH transport: pooled asyncssh connections and the detached-job shell scripts."""
import asyncio
import logging
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional

import asyncssh

from vigil.core.settings.config_schema import PluginConfig

_STATE_DIR = Path(os.environ.get("VIGIL_SSH_CONTROL_DIR",
                                 Path(tempfile.gettempdir()) / "vigil-ssh"))

_KILL_GRACE_SECONDS = 5.0
_CONNECT_TIMEOUT = 5.0
_MAX_CONCURRENT_PER_HOST = 8

COLLECT_TIMEOUT = 30.0
CONTROL_TIMEOUT = 60.0

asyncssh.set_log_level(logging.WARNING)


def _known_hosts_path() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / "known_hosts"


class _TofuClient(asyncssh.SSHClient):
    """Trust-on-first-use host-key check: the first key seen for a target is
    stored; every later connection must present that same key or is refused."""

    def __init__(self, host: str, host_key_alias: str):
        self._host = host
        self._alias = host_key_alias

    def _stored_fingerprints(self) -> set:
        path = _known_hosts_path()
        if not path.exists():
            return set()
        # asyncssh raises assorted errors on a corrupt known_hosts; treat it as no stored key.
        try:
            exact = asyncssh.read_known_hosts(str(path)).match(self._alias, '', 0)[0]
            return {k.get_fingerprint() for k in exact}
        except Exception as e:
            logging.warning(f"ssh: could not read known_hosts for {self._alias}: {e}")
            return set()

    def validate_host_public_key(self, host: str, addr: str, port: int,
                                 key: "asyncssh.SSHKey") -> bool:
        known = self._stored_fingerprints()
        if known:
            if key.get_fingerprint() in known:
                return True
            logging.error(
                f"ssh: host key for {self._alias} does NOT match the stored "
                f"key — refusing to connect (possible MITM or reinstalled host)"
            )
            return False

        try:
            line = f"{self._alias} {key.export_public_key().decode().strip()}\n"
            with open(_known_hosts_path(), 'a') as f:
                f.write(line)
            logging.info(f"ssh: trusting and storing new host key for {self._alias}")
            return True
        except OSError as e:
            logging.error(f"ssh: could not persist host key for {self._alias}: {e}")
            return False


def resolve_host(config: PluginConfig) -> str:
    """The effective host a plugin config points at; the single definition the
    connection pool key and the dialled connection both use."""
    ssh_cfg = config.get('ssh_config', {})
    return ssh_cfg.get('host', config.get('target_host', 'localhost'))


class SSHConnection:
    """One lazily-opened, reused asyncssh connection to a single target. Its
    only public method is execute(); the connection is dialled on first use and
    re-dialled if it drops."""

    @classmethod
    def from_config(cls, config: PluginConfig) -> "SSHConnection":
        ssh_cfg = config.get('ssh_config', {})
        return cls(
            host=resolve_host(config),
            username=ssh_cfg.get('username'),
            key_path=ssh_cfg.get('key_path'),
            port=ssh_cfg.get('port'),
        )

    def __init__(self, host: str, username: Optional[str] = None,
                 key_path: Optional[str] = None, port: Optional[int] = 22):
        self.host = host
        self.username = username
        self.key_path = key_path
        self.port = port if port is not None else 22
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._connect_lock = asyncio.Lock()
        self._channel_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PER_HOST)

    def _host_key_alias(self) -> str:
        user = self.username or os.environ.get("USER", "")
        return f"{user}@{self.host}:{self.port}"

    async def _get_connection(self) -> asyncssh.SSHClientConnection:
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
                connect_timeout=_CONNECT_TIMEOUT,
                keepalive_interval=5,
                keepalive_count_max=2,
            )
            if self.key_path:
                options['client_keys'] = [self.key_path]
                options['agent_path'] = None
            self._conn = await asyncssh.connect(**options)
            return self._conn

    async def execute(self, command: str, timeout: float = COLLECT_TIMEOUT) -> Tuple[int, str, str]:
        """Run one command, returning (exit_code, stdout, stderr). Any transport
        failure or timeout maps to (-1, "", message); a timed-out remote process
        is explicitly killed rather than left running."""
        proc = None
        try:
            conn = await self._get_connection()
            async with self._channel_semaphore:
                proc = await conn.create_process(command)
                try:
                    result = await proc.wait(timeout=timeout)
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
        """Best-effort terminate → force-kill of a still-running remote process."""
        if proc.exit_status is not None or proc.is_closing():
            return
        for stop in (proc.terminate, proc.kill):
            try:
                stop()
                await asyncio.wait_for(proc.wait_closed(), timeout=_KILL_GRACE_SECONDS)
                return
            except (asyncio.TimeoutError, OSError):
                continue

    def close(self):
        if self._conn is not None:
            # Closing an already-broken connection must not fail teardown.
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# --- Detached jobs ---------------------------------------------------------
#
# A long-running job (e.g. a borg backup) is not a live SSH streaming channel
# held open for hours. It is launched *detached on the target* with one ordinary
# SSH command, then advanced by ordinary polling commands (the same execute()
# path collection uses) on the owning plugin's normal monitor cycle. Nothing
# lives in a Vigil-side coroutine, so a job survives a Vigil restart and is
# re-adopted by polling.
#
# These are pure shell-command builders and result parsers — no IO of their
# own; the engine runs the strings they build through SSHConnection.execute,
# exactly like any other command.

# Marker lines separating the sections of a single poll command's output, so
# one round-trip returns size + exit code + liveness + new output.
_SIZE = "===VIGIL_SIZE==="
_EXIT = "===VIGIL_EXIT==="
_ALIVE = "===VIGIL_ALIVE==="
_OUT = "===VIGIL_OUT==="


def workdir_for(token: str) -> str:
    """Per-job working dir on the target, keyed by an opaque token the plugin
    picks (it names the dir before the Job row's id exists)."""
    safe = "".join(c for c in token if c.isalnum() or c in "-_.")
    return f"$HOME/.cache/vigil/jobs/{safe}"


def launch_command(command: str, workdir: str) -> str:
    """Build the one detached command whose stdout is the remote PID.

    The job's stdout+stderr go to `out`; its exit status is written to `exit`
    on completion. `setsid`/`&` detach it from this SSH channel so it keeps
    running after the channel closes. `command` is embedded verbatim (it is
    already a fully-built, quoted shell command from the plugin)."""
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    inner = f'{{ {command}; }} > "$d/out" 2>&1; echo $? > "$d/exit"'
    return (
        f'd={d}; mkdir -p "$d"; : > "$d/out"; rm -f "$d/exit"; '
        f'setsid sh -c {shlex.quote(inner)} < /dev/null > /dev/null 2>&1 & '
        f'echo $!'
    )


def parse_launch(stdout: str) -> Optional[int]:
    """Extract the remote PID from launch_command()'s output."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    try:
        return int(line)
    except ValueError:
        return None


def poll_command(workdir: str, pid: int, offset: int) -> str:
    """One command returning the job's output size, exit code (empty if still
    running), liveness, and any output beyond `offset` bytes."""
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    start = offset + 1  # tail -c is 1-indexed
    return (
        f'd={d}; '
        f'echo {_SIZE}; wc -c < "$d/out" 2>/dev/null || echo 0; '
        f'echo {_EXIT}; cat "$d/exit" 2>/dev/null; '
        f'echo {_ALIVE}; kill -0 {int(pid)} 2>/dev/null && echo 1 || echo 0; '
        f'echo {_OUT}; tail -c +{start} "$d/out" 2>/dev/null'
    )


@dataclass(frozen=True)
class PollResult:
    size: int
    exit_code: Optional[int]
    alive: bool
    new_output: str            # bytes of the output file past the poll offset


def parse_poll(stdout: str) -> PollResult:
    """Split a detached-job poll's sectioned stdout into a PollResult."""
    sections = {'size': [], 'exit': [], 'alive': [], 'out': []}
    current = None
    for line in stdout.split("\n"):
        if line == _SIZE:
            current = 'size'; continue
        if line == _EXIT:
            current = 'exit'; continue
        if line == _ALIVE:
            current = 'alive'; continue
        if line == _OUT:
            current = 'out'; continue
        if current is None:
            continue
        sections[current].append(line)

    def _int(lines: list) -> Optional[int]:
        s = "".join(lines).strip()
        return int(s) if s.lstrip("-").isdigit() else None

    # Rejoin the output section with \n so a trailing partial line (no newline
    # on the target yet) stays partial — split_lines then leaves it unconsumed.
    return PollResult(
        size=_int(sections['size']) or 0,
        exit_code=_int(sections['exit']),
        alive=("".join(sections['alive']).strip() == '1'),
        new_output="\n".join(sections['out']),
    )


def cancel_command(pid: int) -> str:
    """Terminate the detached job, then force-kill after a short grace. Best
    effort — mirrors the terminate()->kill() escalation."""
    p = int(pid)
    return f'kill {p} 2>/dev/null; sleep 2; kill -9 {p} 2>/dev/null; true'


def cleanup_command(workdir: str) -> str:
    """Build the shell command that removes a finished job's working directory."""
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    return f'rm -rf {d}'


def split_lines(new_output: str) -> Tuple[List[str], int]:
    """Split a poll's new bytes into whole lines to append as JobOutput,
    returning (lines, consumed_byte_count). A trailing partial line (no
    newline yet) is left unconsumed so the next poll completes it."""
    if not new_output:
        return [], 0
    consumed = new_output.rfind("\n")
    if consumed == -1:
        return [], 0
    complete = new_output[:consumed]
    lines = complete.split("\n")
    return lines, consumed + 1
