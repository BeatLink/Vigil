from typing import Dict, Any, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult

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


class Cloud(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.provider = str(config.get('provider', 'auto')).lower()

    def _cmds(self):
        table = {'aws': _AWS_CMD, 'gcp': _GCP_CMD, 'azure': _AZURE_CMD}
        if self.provider in table:
            return [(self.provider, table[self.provider])]
        return list(table.items())

    def commands(self) -> List[Command]:
        return [Command(cmd) for _, cmd in self._cmds()]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        import json

        for result in results:
            if result.exit_code == 7 or (result.exit_code != 0 and not result.stdout.strip()):
                continue

            fields = _parse_kv(result.stdout)
            if not fields.get('provider'):
                continue

            logs = [(f"{key}={value}", "INFO") for key, value in fields.items() if value]
            logs.append((
                f"Detected {fields.get('provider')} instance "
                f"{fields.get('instance_id', '?')} ({fields.get('instance_type', '?')})",
                "INFO",
            ))
            return CollectResult(
                metrics={'on_cloud': 1.0},
                logs=logs,
                status='online',
                settings={f"cloud:{self.id}": json.dumps(fields)},
            )

        return CollectResult(
            metrics={'on_cloud': 0.0},
            logs=[("No cloud metadata endpoint responded — not a recognized cloud host", "INFO")],
            status='offline',
        )

    def _cloud_fields(self) -> Dict[str, str]:
        import json
        raw = self.storage.get_setting(f"cloud:{self.id}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    @property
    def _provider_text(self) -> str:
        fields = self._cloud_fields()
        if not fields:
            return 'NONE'
        return str(fields.get('provider', '--')).upper()

    @property
    def _instance_type_text(self) -> str:
        return str(self._cloud_fields().get('instance_type', '--'))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'provider_card': {'title': 'PROVIDER', 'value_attr': '_provider_text'},
                'type_card': {'title': 'INSTANCE TYPE', 'value_attr': '_instance_type_text'},
                'detail': {
                    'repeat': {
                        'source': 'setting',
                        'setting_key': 'cloud:{plugin_id}',
                        'dict_fields': ['instance_id', 'region', 'az', 'zone'],
                        'container': 'cards',
                        'empty_text': 'No cloud metadata',
                    },
                },
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out
