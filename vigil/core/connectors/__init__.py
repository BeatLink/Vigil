"""Connector package. The engine itself lives in ``connectors/engine.py``;
these re-exports keep ``from vigil.core.connectors import ConnectorEngine``
working. ``SSHContext`` is the historical name for ``ExecContext``, which
carries whichever transport (SSH or agent) reaches a plugin's target."""

from vigil.core.connectors.engine import ConnectorEngine, ExecContext, SSHContext

__all__ = ["ConnectorEngine", "ExecContext", "SSHContext"]
