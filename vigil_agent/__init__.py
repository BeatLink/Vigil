"""Vigil agent — the companion daemon that runs on a monitored host.

The agent dials outward to the Vigil server over a WebSocket and keeps that
one connection open. Two things travel on it: shell commands the server asks
it to run (the same command strings the SSH transport used to carry), and
events the agent observes locally and pushes the moment they happen.

It needs no inbound port, no listening socket, and no privilege beyond what
the commands it is asked to run require.
"""

__version__ = "0.1.0"
