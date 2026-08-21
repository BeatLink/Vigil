from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, Command, CollectResult

# Classification is by positive assertion, deliberately. smartctl prints
# "SMART overall-health self-assessment test result: PASSED" (or FAILED); any
# other output means the check did not run — no sudo rights, an unsupported
# controller, a device that vanished. Matching only on the absence of "FAIL"
# would report every one of those as a healthy disk, which is the one answer a
# disk-health monitor must never give when it is blind.
_SMART_SCRIPT = (
    "command -v smartctl >/dev/null 2>&1 || { echo 'ERROR smartctl not found'; exit 1; }; "
    # lsblk calls zram devices, ZFS zvols, loop/md/device-mapper nodes "disk"
    # too, and none of them have SMART. They are skipped here rather than
    # probed: a virtual device that cannot answer is not a disk in unknown
    # health, and counting it as one turns a healthy host permanently red.
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


_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'failed_card'],
    ['events'],
]


class SmartDisk(Plugin):
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
            if parts[0] == 'SKIP':
                # Reported SMART as unsupported: a virtual or pass-through
                # device, not a disk whose health is in doubt.
                continue
            result, rest = parts
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
            return CollectResult.failed(
                "No physical disks found", level="WARNING", status='offline')

        # A disk whose health could not be read is reported as failed, not as
        # healthy: "I cannot tell" and "it is fine" must not look the same.
        return CollectResult(
            metrics={
                "disks_total": total,
                "disks_ok": passed,
                "disks_failed": failed,
                "disks_unknown": unknown,
            },
            logs=logs,
            status='failed' if (failed > 0 or unknown > 0) else 'online',
        )

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'disks_total', 'title': 'DISKS', 'format': 'int'},
            'ok_card': {
                'metric': 'disks_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'smart_disk_always_online',
            },
            'failed_card': {
                'metric': 'disks_failed', 'title': 'FAILED', 'format': 'int',
                'color': 'smart_disk_nonzero_failed',
            },
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.spec import register_color_rule


@register_color_rule('smart_disk_always_online')
def _smart_disk_ok_color(v):
    return None if v is None else 'online'


@register_color_rule('smart_disk_nonzero_failed')
def _smart_disk_failed_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
