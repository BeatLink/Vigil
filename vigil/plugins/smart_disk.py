from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult

_SMART_SCRIPT = (
    "command -v smartctl >/dev/null 2>&1 || { echo 'ERROR smartctl not found'; exit 1; }; "
    "disks=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print \"/dev/\"$1}'); "
    "[ -z \"$disks\" ] && exit 0; "
    "for d in $disks; do "
    "  transport=$(lsblk -no TRAN \"$d\" 2>/dev/null || echo ''); "
    "  if [ \"$transport\" = 'usb' ]; then "
    "    result=$(sudo smartctl -H -d sat \"$d\" 2>&1 || true); "
    "  else "
    "    result=$(sudo smartctl -H \"$d\" 2>&1 || true); "
    "  fi; "
    "  if echo \"$result\" | grep -iq 'FAIL'; then echo \"FAIL $d\"; "
    "  else echo \"PASS $d\"; fi; "
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

        passed, failed = 0, 0
        logs = []
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0] not in ('PASS', 'FAIL'):
                continue
            result, disk = parts
            if result == 'FAIL':
                failed += 1
                logs.append((f"SMART failure detected on {disk}", "ERROR"))
            else:
                passed += 1
                logs.append((f"SMART OK on {disk}", "INFO"))

        total = passed + failed
        if total == 0:
            return CollectResult.failed(
                "No physical disks found", level="WARNING", status='offline')

        return CollectResult(
            metrics={"disks_total": total, "disks_ok": passed, "disks_failed": failed},
            logs=logs,
            status='failed' if failed > 0 else 'online',
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
