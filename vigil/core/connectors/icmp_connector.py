"""IcmpConnector — the ICMP sub-engine of the Connector Engine.

Runs a declarative ``PingRequest`` via the system ``ping`` as an async
subprocess. Plugins never spawn ping themselves — they declare a
``PingRequest`` and receive a ``PingResult`` (the parse of stdout for latency
stays in the plugin's pure ``parse_results``).
"""

import asyncio
import platform

from vigil.core.connectors.types import PingRequest, PingResult


class IcmpConnector:
    async def ping(self, req: PingRequest) -> PingResult:
        is_windows = platform.system().lower() == 'windows'
        cmd = [
            'ping',
            '-n' if is_windows else '-c', str(req.count),
            '-W', str(int(req.timeout)),
            req.host,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return PingResult(
                exception=None,
                returncode=process.returncode,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
            )
        # Any spawn or read failure becomes a failed PingResult, the one shape parse code handles.
        except Exception as e:
            return PingResult(exception=str(e), returncode=None)
