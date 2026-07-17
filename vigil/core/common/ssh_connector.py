import paramiko
import logging
import threading
from typing import Tuple, Optional, Dict, Any

# One shared SSHConnection per (host, username, port). Every monitor that
# targets the same host reuses the same connection instead of opening its own.
# This collapses the ~one-connection-per-monitor storm (which trips sshd's
# MaxStartups and resets connections) down to a single connection per host.
_connection_pool: Dict[tuple, "SSHConnection"] = {}
_pool_lock = threading.Lock()


class SSHConnection:
    """
    A core utility for managing SSH connections to remote nodes.
    Supports both command execution and metric retrieval.

    Instances are shared per target via from_config()/get_shared(), and each
    instance serializes its own command execution with a lock so concurrent
    monitors on the shared thread pool don't use one paramiko transport at once.
    """
    @classmethod
    def from_config(cls, config: Dict[str, Any]):
        """Factory method to get a shared connection from a plugin config dictionary."""
        ssh_cfg = config.get('ssh_config', {})
        return cls.get_shared(
            host=ssh_cfg.get('host', config.get('target_host', 'localhost')),
            username=ssh_cfg.get('username'),
            key_path=ssh_cfg.get('key_path'),
            password=ssh_cfg.get('password'),
            port=ssh_cfg.get('port')
        )

    @classmethod
    def get_shared(cls, host: str, username: Optional[str] = None, key_path: Optional[str] = None,
                   password: Optional[str] = None, port: Optional[int] = None) -> "SSHConnection":
        """Return the pooled connection for this target, creating it on first use."""
        key = (host, username, port if port is not None else 22)
        with _pool_lock:
            conn = _connection_pool.get(key)
            if conn is None:
                conn = cls(host, username, key_path, password, port)
                _connection_pool[key] = conn
            return conn

    def __init__(self, host: str, username: Optional[str] = None, key_path: Optional[str] = None, password: Optional[str] = None, port: Optional[int] = 22):
        self.host = host
        self.username = username
        self.key_path = key_path
        self.password = password
        self.port = port if port is not None else 22
        self.client = None
        # Serializes connect()/execute() so a single paramiko transport is not
        # driven by multiple worker threads targeting this host at once.
        self._lock = threading.Lock()

    def connect(self):
        """Establishes the SSH connection using keys or password."""
        if self.client:
            return

        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            connect_kwargs = {
                "hostname": self.host,
                # TCP connect + banner timeout. Kept short so unreachable hosts
                # (e.g. flaky LAN/IoT devices) fail fast instead of tying up an
                # SSH worker for the full command timeout on every cycle.
                "port": self.port,
                "username": self.username,
                "timeout": 5,
                "banner_timeout": 5,
                "auth_timeout": 5,
                "allow_agent": True
            }
            
            if self.key_path:
                connect_kwargs["key_filename"] = self.key_path
            if self.password:
                connect_kwargs["password"] = self.password
                
            self.client.connect(**connect_kwargs)
            logging.debug(f"SSH connection established to {self.host}")
        except Exception as e:
            logging.error(f"SSH connection failed to {self.host}: {e}")
            self.client = None
            raise

    def execute(self, command: str, timeout: float = 30.0) -> Tuple[int, str, str]:
        """Executes a command and returns (exit_status, stdout, stderr)."""
        # Serialize per-host: the shared connection has one transport, and
        # concurrent monitors run on the same thread pool. The lock also means
        # only the first caller pays the connect cost; the rest reuse it.
        with self._lock:
            if not self.client:
                self.connect()

            try:
                _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
                exit_status = stdout.channel.recv_exit_status()
                return exit_status, stdout.read().decode().strip(), stderr.read().decode().strip()
            except Exception as e:
                logging.error(f"Command execution failed on {self.host}: {e}")
                self.client = None  # Force reconnect on the next call
                raise

    def close(self):
        """Safely closes the SSH client."""
        if self.client:
            self.client.close()
            self.client = None

    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()