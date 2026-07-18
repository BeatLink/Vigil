import logging
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

# Directory holding OpenSSH ControlMaster sockets. One master connection is
# established per target host on first use and reused (multiplexed) by every
# subsequent command, so ~90 monitors against 3 hosts open 3 real SSH
# connections instead of one per monitor per cycle — which is what tripped
# sshd's MaxStartups before. OpenSSH handles the master lifecycle, keepalive
# and reconnect, so there is no pooling/locking to maintain here.
_CONTROL_DIR = Path(os.environ.get("VIGIL_SSH_CONTROL_DIR",
                                   Path(tempfile.gettempdir()) / "vigil-ssh"))


class SSHConnection:
    """
    Runs commands on remote nodes via the system `ssh` client with connection
    multiplexing (ControlMaster/ControlPersist).

    Unlike a library client, this holds no long-lived object state: each
    ``execute`` invokes ``ssh``, and OpenSSH transparently reuses the shared
    master socket for the host. Multiple commands to the same host therefore run
    concurrently over one connection (separate channels), rather than being
    serialized. The public surface (from_config, host, username, execute) is
    unchanged so collectors/controllers need no changes.
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
        # Password auth is not supported over the multiplexed ssh client (it
        # cannot prompt non-interactively). Key/agent auth only.
        self.password = password
        self.port = port if port is not None else 22

    def _control_path(self) -> str:
        # One socket per (user@host:port). %C would also work, but an explicit
        # name keeps sockets identifiable and lets us reason about reuse.
        user = self.username or os.environ.get("USER", "")
        safe = f"{user}@{self.host}:{self.port}".replace("/", "_")
        return str(_CONTROL_DIR / safe)

    def _ssh_base(self, connect_timeout: int) -> list:
        """Common ssh argv: multiplexing, non-interactive, key/host options."""
        _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        argv = [
            "ssh",
            # Multiplexing: reuse a shared master, spawning one automatically if
            # none exists, and keep it alive 60s past the last use for reuse
            # across polling cycles.
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path()}",
            "-o", "ControlPersist=60",
            # Non-interactive, key-only. Never block on prompts.
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            # Keep known_hosts inside our control dir — the service runs with
            # ProtectHome and may have no writable ~/.ssh.
            "-o", f"UserKnownHostsFile={_CONTROL_DIR / 'known_hosts'}",
            "-o", f"ConnectTimeout={connect_timeout}",
            # Detect a dead master reasonably fast rather than hanging.
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-p", str(self.port),
        ]
        if self.key_path:
            argv += ["-o", "IdentitiesOnly=yes", "-i", self.key_path]
        return argv

    def execute(self, command: str, timeout: float = 30.0,
                connect_timeout: int = 5) -> Tuple[int, str, str]:
        """
        Execute a command on the target and return (exit_status, stdout, stderr).

        Runs the system ssh client; the first call to a host establishes the
        master connection and later calls reuse it. A wall-clock ``timeout``
        bounds the whole call so a stuck host is abandoned, not hung on.
        """
        target = f"{self.username}@{self.host}" if self.username else self.host
        argv = self._ssh_base(connect_timeout) + [target, command]
        proc = None
        try:
            # start_new_session puts ssh in its own process group so a timeout
            # can signal the whole tree at once. subprocess.run's own timeout
            # kills only the direct child: the remote command keeps running, and
            # locally the `ssh` process can survive wedged in uninterruptible
            # I/O. That leaked one process per poll against a busy repo until
            # the host was saturated — the failure this guards against.
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            return (
                proc.returncode,
                stdout.decode(errors="replace").strip(),
                stderr.decode(errors="replace").strip(),
            )
        except subprocess.TimeoutExpired:
            logging.error(f"SSH command timed out after {timeout}s on {self.host}: {command!r}")
            self._kill_group(proc)
            return -1, "", f"Timed out after {timeout}s"
        except Exception as e:
            logging.error(f"SSH execution failed on {self.host}: {e}")
            self._kill_group(proc)
            return -1, "", str(e)

    @staticmethod
    def _kill_group(proc: Optional["subprocess.Popen"]) -> None:
        """
        Terminate a timed-out ssh invocation and everything it spawned.

        Signals the process group (negative pid) rather than the single child,
        because `ssh` may have forked helpers and, more importantly, so a
        wedged process cannot outlive the call that owns it. SIGTERM first so
        ssh can close its channel cleanly, then SIGKILL for anything stuck in
        uninterruptible I/O, which SIGTERM alone cannot clear.

        Killing the local ssh also tears down the remote command: sshd sees the
        channel close and signals the process it started.
        """
        if proc is None or proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
        # Reap whatever is left so it does not linger as a zombie.
        try:
            proc.poll()
        except Exception:
            pass

    def execute_streaming(self, command: str, on_line=None, connect_timeout: int = 5,
                          timeout: Optional[float] = None,
                          should_cancel=None) -> Tuple[int, str]:
        """
        Execute a long-running command, delivering output line-by-line as it
        arrives. Returns (exit_status, error_message).

        ``execute`` cannot serve this case: subprocess.run buffers everything
        until the process exits, so a backup running for an hour yields nothing
        until it is over, and its 30s-style timeout would kill it outright.
        Here output is read incrementally and handed to ``on_line(stream, text)``
        as each line completes, so the caller can persist progress live.

        stderr is merged into stdout rather than read on a second pipe. borg
        writes progress to stderr and results to stdout; reading two pipes from
        one thread risks deadlocking when one fills while we block on the other,
        and interleaved order is what makes the output readable anyway. The
        caller tags the stream.

        ``should_cancel`` is polled between lines; when it returns True the
        remote process is terminated and the status is 130 (SIGINT convention).
        ``timeout`` is an optional overall wall-clock bound — None means run to
        completion, which is the norm for jobs.
        """
        target = f"{self.username}@{self.host}" if self.username else self.host
        # -tt forces a PTY so that killing the local ssh client also signals the
        # remote process. Without it, terminating ssh leaves borg running on the
        # far end, holding the repo lock with nothing able to stop it.
        argv = self._ssh_base(connect_timeout) + ["-tt", target, command]

        proc = None
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                universal_newlines=True,
                errors="replace",
                # Own process group, so _terminate can signal the whole tree
                # rather than just ssh — see _kill_group.
                start_new_session=True,
            )
        except Exception as e:
            logging.error(f"SSH streaming start failed on {self.host}: {e}")
            return -1, str(e)

        start = time.monotonic()
        cancelled = False
        try:
            for line in proc.stdout:
                if on_line is not None:
                    try:
                        on_line("stdout", line.rstrip("\r\n"))
                    except Exception as e:
                        # A failing consumer must not abort the remote job.
                        logging.error(f"Job output handler failed: {e}")
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                if timeout is not None and (time.monotonic() - start) > timeout:
                    logging.error(f"SSH streaming timed out after {timeout}s on {self.host}")
                    self._terminate(proc)
                    return -1, f"Timed out after {timeout}s"

            if cancelled:
                self._terminate(proc)
                return 130, "Cancelled"

            return proc.wait(), ""
        except Exception as e:
            logging.error(f"SSH streaming failed on {self.host}: {e}")
            self._terminate(proc)
            return -1, str(e)
        finally:
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

    @staticmethod
    def _terminate(proc: "subprocess.Popen") -> None:
        """
        Stop a streaming process, escalating to SIGKILL if it ignores SIGTERM.

        borg traps SIGTERM to release its repository lock cleanly, so it is
        given a grace period before being killed — a hard kill can leave a stale
        lock that blocks the next backup.

        Signals the process group, not just ssh, so no part of the tree is left
        behind holding a connection.
        """
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:
                pass
        except Exception:
            pass

    def close(self):
        """Tear down the shared master connection for this host, if any."""
        try:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self._control_path()}",
                 (f"{self.username}@{self.host}" if self.username else self.host)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
