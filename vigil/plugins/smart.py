"""SMART health of every physical disk, via smartctl."""

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig


# Classification is by positive assertion: any output other than an explicit
# PASSED/FAILED verdict means the check did not run, and a blind check must not
# read as a healthy disk.
_SMART_SCRIPT = (
    "command -v smartctl >/dev/null 2>&1 || { echo 'ERROR smartctl not found'; exit 1; }; "
    # zram, zvols, loop/md/device-mapper nodes are TYPE=disk to lsblk but have no SMART.
    "disks=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print $1}' "
    "  | grep -Ev '^(zram|zd|loop|md|dm-|sr|fd|ram)' | sed 's|^|/dev/|'); "
    "[ -z \"$disks\" ] && exit 0; "
    "for d in $disks; do "
    "  transport=$(lsblk -no TRAN \"$d\" 2>/dev/null || echo ''); "
    "  if [ \"$transport\" = 'usb' ]; then "
    "    result=$(sudo smartctl -H -d sat \"$d\" 2>&1 || true); "
    "  else "
    "    result=$(sudo smartctl -H \"$d\" 2>&1 || true); "
    "  fi; "
    "  if echo \"$result\" | grep -iq 'test result: *PASSED'; then echo \"PASS $d\"; "
    "  elif echo \"$result\" | grep -iqE 'test result: *FAILED|SMART Health Status: *FAIL'; then echo \"FAIL $d\"; "
    "  elif echo \"$result\" | grep -iqE 'does not support SMART|Unable to detect device type|Operation not supported'; then "
    "    echo \"SKIP $d\"; "
    "  else echo \"UNKNOWN $d $(echo \"$result\" | tr '\\n' ' ' | cut -c1-160)\"; fi; "
    "done"
)


class Smart(SignalPlugin):
    """Per-disk SMART overall-health verdicts from smartctl, counted into
    healthy/failed/unreadable. A disk whose health could not be read counts as
    failed, not healthy: "I cannot tell" and "it is fine" must not look alike."""

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command(_SMART_SCRIPT)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"SMART check script failed: {stdout or stderr}")

        passed, failed, unknown = 0, 0, 0
        logs = []
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0] not in ('PASS', 'FAIL', 'UNKNOWN', 'SKIP'):
                continue
            result, rest = parts
            if result == 'SKIP':
                continue
            if result == 'FAIL':
                failed += 1
                logs.append((f"SMART failure detected on {rest}", "ERROR"))
            elif result == 'UNKNOWN':
                unknown += 1
                disk, _, detail = rest.partition(' ')
                logs.append((
                    f"Could not read SMART health for {disk}: {detail or 'no usable output'}",
                    "ERROR",
                ))
            else:
                passed += 1
                logs.append((f"SMART OK on {rest}", "INFO"))

        total = passed + failed + unknown
        if total == 0:
            return CollectResult(logs=[("No physical disks found", "WARNING")], status='offline')

        return CollectResult(
            metrics={
                'disks_total': total,
                'disks_ok': passed,
                'disks_failed': failed,
                'disks_unknown': unknown,
            },
            logs=logs,
            status='failed' if (failed > 0 or unknown > 0) else 'online',
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'smart_total_card': {'metric': 'disks_total', 'title': 'DISKS', 'format': 'int'},
            'smart_ok_card': {
                'metric': 'disks_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'always_online',
            },
            'smart_failed_card': {
                'metric': 'disks_failed', 'title': 'FAILED', 'format': 'int',
                'color': 'nonzero_failed',
            },
        }
