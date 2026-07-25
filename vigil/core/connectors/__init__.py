"""Connector package. The engine itself lives in ``connectors/engine.py``;
this re-export keeps ``from vigil.core.connectors import ConnectorEngine``
working."""

from vigil.core.connectors.engine import ConnectorEngine, SSHContext

__all__ = ["ConnectorEngine", "SSHContext"]
