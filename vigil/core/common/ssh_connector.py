import logging
import os
import shlex
import subprocess
import tempfile
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
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            return (
                proc.returncode,
                proc.stdout.decode(errors="replace").strip(),
                proc.stderr.decode(errors="replace").strip(),
            )
        except subprocess.TimeoutExpired:
            logging.error(f"SSH command timed out after {timeout}s on {self.host}: {command!r}")
            return -1, "", f"Timed out after {timeout}s"
        except Exception as e:
            logging.error(f"SSH execution failed on {self.host}: {e}")
            return -1, "", str(e)

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
