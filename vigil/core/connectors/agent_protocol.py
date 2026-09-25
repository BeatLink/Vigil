"""Re-export of the agent wire protocol.

The definitions live in ``vigil_agent.protocol`` so the agent package stays
installable on a monitored host without the server's dependencies; this alias
keeps the server-side import path alongside the rest of the transport code.
"""

from vigil_agent.protocol import (  # noqa: F401
    DISMISS, EVENT, EXEC, HELLO, NOTIFY, PING, PONG, PROTOCOL_VERSION, RESULT, SUBSCRIBE,
    WELCOME, StreamSpec, decode, dismiss, encode, event, exec_request, exec_result,
    hello, notify, subscribe, welcome,
)
