from typing import Dict, Any, Optional, Tuple
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, on_data_event

# The link-local metadata endpoint is the same address on every major provider;
# the paths differ, and AWS IMDSv2 additionally requires a token header.
_MD = "169.254.169.254"

# AWS IMDSv2: fetch a short-lived token, then the metadata items.
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

# GCP: requires the Metadata-Flavor header.
_GCP_CMD = (
    f"h(){{ curl -s -m 3 -H 'Metadata-Flavor: Google' \"http://{_MD}/computeMetadata/v1/$1\"; }}; "
    "ID=$(h instance/id); [ -z \"$ID\" ] && exit 7; "
    "echo \"provider=gcp\"; "
    "echo \"instance_id=$ID\"; "
    "echo \"instance_type=$(h instance/machine-type | awk -F/ '{print $NF}')\"; "
    "echo \"zone=$(h instance/zone | awk -F/ '{print $NF}')\""
)

# Azure IMDS: requires Metadata:true and an api-version.
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


class CloudPlugin(BasePlugin):
    """
    Detects the cloud provider of the target and surfaces its instance metadata
    (instance id, type, region/zone) over SSH via the link-local metadata
    endpoint (169.254.169.254).

    Auto-detects across AWS (IMDSv2), GCP, and Azure by trying each in turn, or
    query a single provider by setting `provider`. Reports online when metadata
    is reachable, offline when the host isn't on a recognized cloud, failed on
    an unexpected error. This is an informational monitor — it has no thresholds.

    Config options:
      provider   One of "aws", "gcp", "azure", or "auto" (default: "auto")
    """

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
            # exit 7 is our sentinel for "this provider's endpoint didn't answer".
            if ret == 7 or (ret != 0 and not stdout.strip()):
                continue

            fields = _parse_kv(stdout)
            if not fields.get('provider'):
                continue

            for key, value in fields.items():
                if value:
                    # Store string metadata as a log line so the UI can display it;
                    # metrics are numeric-only, so we log the descriptive fields.
                    self.db_logger.write(f"{key}={value}", level="INFO")
            # A single presence metric lets the overview/exporters see "on cloud".
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
        """Persist the descriptive fields as a Setting keyed by this monitor id, so
        the UI can render them without re-parsing logs."""
        import json
        try:
            self.db.set_setting(f"cloud:{self.id}", json.dumps(fields))
        except Exception:
            pass

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import json
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

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
            self.internal_modules['ui']['events_table']()

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

        on_data_event('setting', provider_label, update)


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out
