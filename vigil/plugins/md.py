"""mdadm array health, read from /proc/mdstat."""

import re

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig


_ARRAY_RE = re.compile(r'^(md\d+)\s*:\s*(\S+)\s+(\S+)', re.MULTILINE)


_STATE_RE = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')


_RECOVERY_RE = re.compile(r'(recovery|resync|reshape|check)\s*=\s*([\d.]+)%')


class Md(SignalPlugin):
    """Linux software RAID health from /proc/mdstat — the mdadm sibling of the
    zfs monitor, counting arrays that are clean, degraded or rebuilding."""

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command("cat /proc/mdstat 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"Failed to read /proc/mdstat: {stderr}")

        ok = degraded = 0
        recovering = False
        logs = []

        for m in _ARRAY_RE.finditer(stdout):
            dev = m.group(1)
            block = stdout[m.end():]
            next_blank = block.find('\n\n')
            block = block if next_blank < 0 else block[:next_blank]

            state = _STATE_RE.search(block)
            recov = _RECOVERY_RE.search(block)

            if state:
                expected, active, flags = int(state.group(1)), int(state.group(2)), state.group(3)
                if flags.count('_') > 0 or active < expected:
                    degraded += 1
                    logs.append((f"{dev}: DEGRADED [{active}/{expected}] [{flags}]", "ERROR"))
                    continue

            if recov:
                recovering = True
                logs.append((f"{dev}: {recov.group(1)} {recov.group(2)}% in progress", "WARNING"))
                ok += 1
                continue

            ok += 1
            logs.append((f"{dev}: clean", "INFO"))

        if ok + degraded == 0:
            return CollectResult(
                logs=[("No RAID arrays found in /proc/mdstat", "WARNING")], status='offline')

        metrics = {
            'arrays_total': float(ok + degraded),
            'arrays_ok': float(ok),
            'arrays_degraded': float(degraded),
        }
        if degraded:
            status = 'failed'
        elif recovering:
            status = 'warning'
        else:
            status = 'online'
        return CollectResult(metrics=metrics, logs=logs, status=status)

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'md_total_card': {'metric': 'arrays_total', 'title': 'ARRAYS', 'format': 'int'},
            'md_ok_card': {
                'metric': 'arrays_ok', 'title': 'CLEAN', 'format': 'int',
                'color': 'always_online',
            },
            'md_degraded_card': {
                'metric': 'arrays_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'nonzero_failed',
            },
        }

    def card_row(self) -> List[str]:
        return ['md_total_card', 'md_ok_card', 'md_degraded_card']
