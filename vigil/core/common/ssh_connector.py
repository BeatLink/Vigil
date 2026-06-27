import paramiko
import logging
from typing import Tuple, Optional, Dict, Any

class SSHConnection:
    """
    A core utility for managing SSH connections to remote nodes.
    Supports both command execution and metric retrieval.
    """
    @classmethod
    def from_config(cls, config: Dict[str, Any]):
        """Factory method to create a connection from a plugin configuration dictionary."""
        ssh_cfg = config.get('ssh_config', {})
        return cls(
            host=ssh_cfg.get('host', config.get('target_host', 'localhost')),
            username=ssh_cfg.get('username'),
            key_path=ssh_cfg.get('key_path'),
            password=ssh_cfg.get('password'),
            port=ssh_cfg.get('port')
        )

    def __init__(self, host: str, username: Optional[str] = None, key_path: Optional[str] = None, password: Optional[str] = None, port: Optional[int] = 22):
        self.host = host
        self.username = username
        self.key_path = key_path
        self.password = password
        self.port = port if port is not None else 22
        self.client = None

    def connect(self):
        """Establishes the SSH connection using keys or password."""
        if self.client:
            return

        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            connect_kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": 15,
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