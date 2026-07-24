from typing import Dict, Any, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_MD = "169.254.169.254"

_AWS_CMD = (
    f"T=$(curl -s -m 3 -X PUT 'http://{_MD}/latest/api/token' "
    f"-H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); "
    f"[ -z \"$T\" ] && exit 7; "
    f"h(){{ curl -s -m 3 -H \"X-aws-ec2-metadata-token: $T\" \"http://{_MD}/latest/meta-data/$1\"; }}; "
    "echo \"provider=aws\"; "
    "echo \"instance_id=$(h instance-id)\"; "
    "echo \"instance_type=$(h instance-type)\"; "
    "echo \"region=$(h placement/region)\"; "
    "echo \"az=$(h placement/availability-zone)\""
)

_GCP_CMD = (
    f"h(){{ curl -s -m 3 -H 'Metadata-Flavor: Google' \"http://{_MD}/computeMetadata/v1/$1\"; }}; "
    "ID=$(h instance/id); [ -z \"$ID\" ] && exit 7; "
    "echo \"provider=gcp\"; "
    "echo \"instance_id=$ID\"; "
    "echo \"instance_type=$(h instance/machine-type | awk -F/ '{print $NF}')\"; "
    "echo \"zone=$(h instance/zone | awk -F/ '{print $NF}')\""
)

_AZURE_CMD = (
    f"J=$(curl -s -m 3 -H 'Metadata:true' "
    f"'http://{_MD}/metadata/instance?api-version=2021-02-01'); "
    "[ -z \"$J\" ] && exit 7; "
    "echo \"provider=azure\"; "
    "echo \"raw=$J\""
)

_DEFAULT_LAYOUT = [
    ['host_card', 'provider_card', 'type_card'],
    ['detail'],
    ['events'],
]


class CloudCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.provider = str(config.get('provider', 'auto')).lower()

    def _cmds(self):
        table = {'aws': _AWS_CMD, 'gcp': _GCP_CMD, 'azure': _AZURE_CMD}
        if self.provider in table:
            return [(self.provider, table[self.provider])]
        return list(table.items())

    async def on_collect(self):
        for name, cmd in self._cmds():
            ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)
            if ret == 7 or (ret != 0 and not stdout.strip()):
                continue

            fields = _parse_kv(stdout)
            if not fields.get('provider'):
                continue

            for key, value in fields.items():
                if value:
                    self.db_logger.write(f"{key}={value}", level="INFO")
            self.db_metrics.metric('on_cloud', 1.0)
            self._store_fields(fields)
            self.db_logger.write(
                f"Detected {fields.get('provider')} instance "
                f"{fields.get('instance_id', '?')} ({fields.get('instance_type', '?')})",
                level="INFO"
            )
            self.set_status('online')
            return

        self.db_metrics.metric('on_cloud', 0.0)
        self.db_logger.write("No cloud metadata endpoint responded — not a recognized cloud host", level="INFO")
        self.set_status('offline')

    def _store_fields(self, fields: Dict[str, str]):
        import json
        try:
            self.db.set_setting(f"cloud:{self.id}", json.dumps(fields))
        except Exception:
            pass

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class CloudUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import json
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page()

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('provider_card'):
            provider_label = info_card('PROVIDER', '--')
        with layout.cell('type_card'):
            type_label = info_card('INSTANCE TYPE', '--')
        with layout.cell('detail'):
            detail_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update():
            raw = self.db.get_setting(f"cloud:{self.id}")
            if not raw:
                provider_label.text = 'NONE'
                return
            try:
                fields = json.loads(raw)
            except Exception:
                return
            provider_label.text = str(fields.get('provider', '--')).upper()
            type_label.text = str(fields.get('instance_type', '--'))
            detail_container.clear()
            with detail_container:
                for key in ('instance_id', 'region', 'az', 'zone'):
                    if fields.get(key):
                        info_card(key.replace('_', ' ').upper(), str(fields[key]))

        page.on_refresh(update)
        update()
        page.start()


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out
