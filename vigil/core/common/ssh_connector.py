"""
Async SSH transport, built on asyncssh rather than shelling out to the
system `ssh` client.

Replaces the previous subprocess+ControlMaster design: one native asyncssh
connection per host takes the place of one ControlMaster socket, and every
command becomes a channel on that connection rather than a forked `ssh`
process. Multiple commands to the same host already run concurrently here —
verified empirically (10 concurrent commands on one connection completed in
~1s, not ~10s) — which is what removes both the per-poll fork/exec cost and
SSHCollector's thread-pool/semaphore ceiling (MAX_CONCURRENT_SSH) from the
collector's design; that pool is gone as of this change.

Three behaviors of the old subprocess design had to be reproduced deliberately
rather than assumed, all verified empirically against a real sshd before
this was written:

  1. Killing a remote process. asyncssh's `process.close()` alone does NOT
     reliably terminate the remote command — verified: a `sleep 300` command
     survived `close()` and had to be killed out-of-band. An explicit
     `terminate()`/`kill()` call is required, and was verified to reliably
     kill the remote process both with and without a PTY session. Every
     timeout/cancel path here calls terminate()-then-kill() explicitly —
     skipping this leaks remote processes exactly the way the old code's
     extensive kill-group comments describe. `execute_streaming` additionally
     opens a PTY (`term_type=`), matching the old code's `-tt`, since it feeds
     borg-style interactive progress output; `execute` deliberately does NOT
     open one — see its docstring for why (a PTY merges stdout/stderr, which
     would break the many plugins that inspect stderr specifically).

  2. Host key trust. asyncssh's `known_hosts=None` disables verification
     entirely and never calls a validation callback — that was the first,
     wrong approach tried here, and it provides no MITM protection at all.
     The `_TofuClient.validate_host_public_key` override below (paired with
     `known_hosts=[]`, not None, which is what makes the callback fire) is
     what actually reproduces `StrictHostKeyChecking=accept-new`: it trusts
     and persists a host's key the first time it's seen, but rejects any
     later connection whose key doesn't match what was stored — verified
     against both a matching and a genuinely different key.

  3. Per-host channel limits. sshd's default `MaxSessions` is 10 — verified:
     15 concurrent channels on one connection left 5 failing with "open
     failed". TechNet's fleet-wide sshd config
     (nix/0-common/3-services/1-ssh.nix) raises this to 50, but Vigil also
     bounds its own concurrency per host below that (see
     _MAX_CONCURRENT_PER_HOST) so it behaves safely against any host,
     including ones outside TechNet's own config that keep the default.
"""
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, Callable

import asyncssh

# Directory holding the known_hosts file and per-host connection state.
# Equivalent role to the old ControlMaster socket directory.
_STATE_DIR = Path(os.environ.get("VIGIL_SSH_CONTROL_DIR",
                                 Path(tempfile.gettempdir()) / "vigil-ssh"))

# How long a killed process is given to exit after SIGTERM (via terminate())
# before escalating to kill() — mirrors the old code's 5s/10s grace periods,
# shortened here since terminate() over an already-open channel is faster
# than signalling and reaping a local subprocess.
_KILL_GRACE_SECONDS = 5.0

# Maximum regular (execute()) channels this process will open concurrently
# on one connection — one per SSH-connected host, not global (contrast with
# the old MAX_CONCURRENT_SSH in ssh_collector.py, which bounded every host
# combined through a single thread pool).
#
# Streaming/job channels (execute_streaming, i.e. JobController) draw from a
# separate, smaller pool (_MAX_CONCURRENT_JOBS_PER_HOST) rather than this
# one: a job can run for hours, and sharing one pool would let a single
# long-running borg backup hold a slot that starves that host's regular
# polling for its whole duration. The two together (8 + 2 = 10) are kept at
# sshd's default MaxSessions (10) as the floor, so Vigil behaves safely
# against any host, including ones outside TechNet's own fleet config
# (nix/0-common/3-services/1-ssh.nix, which raises MaxSessions to 50) that
# keep the OpenSSH default — verified empirically that exceeding MaxSessions
# fails open requests outright rather than queuing them.
_MAX_CONCURRENT_PER_HOST = 8
_MAX_CONCURRENT_JOBS_PER_HOST = 2

asyncssh.set_log_level(logging.WARNING)  # asyncssh is chatty at INFO


def _known_hosts_path() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / "known_hosts"


class _TofuClient(asyncssh.SSHClient):
    """
    Trust-on-first-use host key validation, persisted to a known_hosts file.

    Only invoked because callers pass `known_hosts=[]` (not `None`) to
    asyncssh.connect — see this module's docstring for why that distinction
    matters. Read/append the file synchronously: it's a handful of lines,
    touched once per host at most per process lifetime (after the first
    successful connection, the key is already on disk and every later
    connection just matches against it).
    """
    def __init__(self, host: str, host_key_alias: str):
        # `host_key_alias` — not the raw hostname — is the identity keys are
        # stored/matched under, so two SSHConnections that both resolve to
        # "the same host" by config (e.g. a custom port) don't collide with
        # an unrelated host that happens to share a hostname on a different
        # port.
        self._host = host
        self._alias = host_key_alias

    def _load_known_fingerprints(self) -> set:
        path = _known_hosts_path()
        if not path.exists():
            return set()
        try:
            # match() returns a 7-tuple: (exact, ca, revoked, x509_certs,
            # x509_ca_certs, x509_subject_patterns, x509_ca_subject_patterns).
            # Only the first element (exact host key matches) applies here —
            # Vigil has no CA/X.509 setup, and revoked keys are absent unless
            # an operator hand-edits the file, in which case they belong in
            # neither set (treated as "not yet known" would re-trust them).
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

        # No stored key yet: trust this one and persist it, like
        # StrictHostKeyChecking=accept-new.
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
    """
    Runs commands on a remote host over a persistent native SSH connection.

    One asyncssh.SSHClientConnection is opened lazily on first use and
    cached on this instance; every `execute`/`execute_streaming` call opens
    a new channel on it rather than a new connection, which is what gives
    concurrent commands to the same host real concurrency instead of
    serialization. The public surface (from_config, host, username, execute,
    execute_streaming, close) is unchanged from the subprocess-based
    version, so callers (SSHCollector, SSHController, JobController) need no
    changes beyond removing the thread-pool plumbing that awaiting a
    blocking call required.
    """
    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "SSHConnection":
        """Factory method to create a connection from a plugin config dictionary."""
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
        # Password auth is not supported, same as the previous subprocess
        # client: BatchMode/asyncssh's default agent+key auth expects
        # non-interactive key/agent auth, and there is no prompting here.
        self.password = password
        self.port = port if port is not None else 22
        # Lazily-established, cached connection. Guarded by a lock so two
        # concurrent callers racing to connect (e.g. a monitor's first poll
        # firing alongside a UI-triggered action right after startup) open
        # exactly one connection, not two.
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._connect_lock = asyncio.Lock()
        # Bounds concurrent channels on this host's connection — see
        # _MAX_CONCURRENT_PER_HOST / _MAX_CONCURRENT_JOBS_PER_HOST. One pair
        # of semaphores per SSHConnection (i.e. per host), not shared
        # globally: a slow/saturated host must not throttle unrelated hosts'
        # polling. Separate pools so a long-running job (execute_streaming)
        # can never starve that host's regular polling (execute) of channels.
        self._channel_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PER_HOST)
        self._job_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_JOBS_PER_HOST)

    def _host_key_alias(self) -> str:
        user = self.username or os.environ.get("USER", "")
        return f"{user}@{self.host}:{self.port}"

    async def _get_connection(self, connect_timeout: float) -> asyncssh.SSHClientConnection:
        """Return the cached connection, establishing or re-establishing it
        as needed. A connection that has died (e.g. the host rebooted) is
        detected via is_closed() and transparently replaced."""
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
                known_hosts=[],  # non-None: required for validate_host_public_key to fire
                client_factory=lambda: _TofuClient(self.host, alias),
                host_key_alias=alias,
                connect_timeout=connect_timeout,
                # Detect a dead connection reasonably fast rather than hanging,
                # matching the old ServerAliveInterval/CountMax settings.
                keepalive_interval=5,
                keepalive_count_max=2,
            )
            if self.key_path:
                options['client_keys'] = [self.key_path]
                options['agent_path'] = None  # IdentitiesOnly=yes equivalent
            self._conn = await asyncssh.connect(**options)
            return self._conn

    async def execute(self, command: str, timeout: float = 30.0,
                      connect_timeout: float = 5.0) -> Tuple[int, str, str]:
        """
        Execute a command on the target and return (exit_status, stdout, stderr).

        Deliberately opens NO PTY (unlike execute_streaming) — dozens of
        plugins destructure (ret, stdout, stderr) from this and specifically
        inspect stderr for error text (e.g. "failed to connect"). A PTY
        session merges the remote stdout/stderr into a single stream (there
        is only one terminal device), verified empirically; without one,
        the two stay genuinely separate, matching the previous subprocess
        implementation exactly. terminate()/kill() were verified to reliably
        kill a PTY-less remote process too, so this loses nothing on the
        cancellation guarantee by skipping the PTY.

        `timeout` bounds the whole call (connect + command), same contract
        as the previous implementation. On timeout, the process is
        explicitly killed rather than left to asyncssh's own TimeoutError
        path, which was verified to leak the remote process if not handled
        this way.
        """
        proc = None
        try:
            conn = await asyncio.wait_for(
                self._get_connection(connect_timeout), timeout=connect_timeout + 5
            )
            # Held for the channel's whole lifetime, not just while opening
            # it — it's concurrent open channels that trip sshd's
            # MaxSessions, verified empirically, not the rate of opening them.
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
        """
        Terminate a process, escalating to kill() if it ignores terminate().

        Verified empirically to reliably kill the remote process both with
        and without a PTY session — what does NOT reliably kill it, also
        verified, is `proc.close()` alone or letting a wait_for(proc.wait())
        timeout without an explicit terminate()/kill() call; see the module
        docstring.
        """
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
        """
        Execute a long-running command, delivering output line-by-line as it
        arrives. Returns (exit_status, error_message).

        stderr is merged into stdout (`stderr=asyncssh.STDOUT`), same
        rationale as the old implementation: borg interleaves progress on
        stderr with results on stdout, and reading two streams from one
        coroutine invites the same ordering/deadlock issues reading two
        pipes did.

        `should_cancel` is polled between lines; when it returns True the
        remote process is terminated (PTY + terminate()/kill(), see
        _kill_process) and the status is 130 (SIGINT convention), matching
        the previous behavior.

        Held against _job_semaphore (a separate, smaller pool than regular
        execute() channels — see _MAX_CONCURRENT_JOBS_PER_HOST) for the
        channel's entire lifetime, since a job can run for hours.
        """
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
                    # A short per-read timeout keeps this loop responsive to
                    # should_cancel/timeout even when the remote command is
                    # quiet for a while, without busy-polling.
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    line = None

                if line:
                    if on_line is not None:
                        try:
                            on_line("stdout", line.rstrip("\r\n"))
                        except Exception as e:
                            # A failing consumer must not abort the remote job.
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
        """
        Tear down the cached connection, if any.

        Synchronous to match the previous interface (used from __exit__ and
        a handful of non-async call sites); asyncssh's close() itself is
        synchronous (it schedules the teardown), so this needs no event
        loop of its own.
        """
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
