"""Agent configuration.

A deliberately small YAML file — the agent's job is to connect and obey, so
everything about *what* to watch comes from the server's subscription frames
rather than from local config. That keeps a fleet of agents identical and
means adding a monitor never requires touching the monitored host.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_PATH = "/etc/vigil-agent.yaml"


def _read_token(token_file: str) -> Optional[str]:
    """Read a token from a file, trimming the trailing newline an editor or a
    secret manager will have added. An unreadable file is a hard failure rather
    than a silent fall-through to no token, which would otherwise show up as a
    confusing authentication rejection at the server."""
    try:
        return Path(token_file).read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SystemExit(f"vigil-agent: could not read token_file {token_file}: {e}")


@dataclass
class AgentConfig:
    url: str
    """The server's agent endpoint, e.g. ws://vigil.lan:8080/api/agent/ws
    (or wss:// when the dashboard is behind TLS)."""
    agent_id: str
    """Must match an `id` in the server's `agents:` list."""
    token: str
    hostname: Optional[str] = None

    @staticmethod
    def load(path: Optional[str] = None) -> "AgentConfig":
        """Read the config file, letting VIGIL_AGENT_* environment variables
        override any field. The env path exists so a container or a systemd
        unit can supply the token without it being written to disk."""
        import yaml

        data = {}
        config_path = Path(path or os.environ.get('VIGIL_AGENT_CONFIG', DEFAULT_PATH))
        if config_path.exists():
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding='utf-8'))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, yaml.YAMLError) as e:
                logging.error(f"could not read {config_path}: {e}")
        elif path:
            raise SystemExit(f"vigil-agent: config file not found: {config_path}")

        url = os.environ.get('VIGIL_AGENT_URL') or data.get('url')
        agent_id = os.environ.get('VIGIL_AGENT_ID') or data.get('id')
        token = os.environ.get('VIGIL_AGENT_TOKEN') or data.get('token')
        hostname = os.environ.get('VIGIL_AGENT_HOSTNAME') or data.get('hostname')

        # token_file is the deployment-friendly form: the token stays in a file
        # the secret manager owns, so it never enters a config file, a unit's
        # environment, or (under NixOS) the world-readable Nix store.
        token_file = os.environ.get('VIGIL_AGENT_TOKEN_FILE') or data.get('token_file')
        if not token and token_file:
            token = _read_token(token_file)

        missing = [n for n, v in (('url', url), ('id', agent_id), ('token', token)) if not v]
        if missing:
            raise SystemExit(
                f"vigil-agent: missing required setting(s): {', '.join(missing)} "
                f"(set them in {config_path} or as VIGIL_AGENT_* environment variables)"
            )
        return AgentConfig(str(url), str(agent_id), str(token),
                           str(hostname) if hostname else None)
