import json
import shlex
from typing import Any, Dict, List, Optional

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.web_plugin_base import UIPlugin


def _build_fetch_script(api_url: str, timeout: int, api_key_command: Optional[str],
                        api_key: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if api_key_command:
        lines.append(f"__key=$({api_key_command})")
        header = '-H "X-API-Key: $__key"'
    else:
        header = f'-H {shlex.quote("X-API-Key: " + (api_key or ""))}'

    lines.append(f'curl -s -m {timeout} {header} {shlex.quote(base + "/urls/domains")}')
    return '\n'.join(lines)


def _parse_response(stdout: str) -> list:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"domains response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(data, list):
        raise ValueError(f"domains response was not a list: {stdout[:200]!r}")
    return data


_DEFAULT_LAYOUT = [
    ['host_card', 'domains_card', 'urls_card'],
    ['chart'],
    ['events'],
]


class BlockurlCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:9001')
        self.api_key = config.get('api_key')
        self.api_key_command = config.get(
            'api_key_command', 'cut -d= -f2- /run/secrets/blockurl_api_key')
        self.min_domains = int(config.get('min_domains', 1))
        self.api_timeout = int(config.get('api_timeout', 10))

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.api_key_command, self.api_key)
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query BlockURL API: {stderr.strip()}")

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        domain_count = len(data)
        url_total = sum(int(entry[1]) for entry in data
                         if isinstance(entry, list) and len(entry) == 2)

        metrics = {'domains_total': float(domain_count), 'urls_total': float(url_total)}

        if domain_count < self.min_domains:
            return CollectResult(
                metrics=metrics,
                logs=[(
                    f"Only {domain_count} domain(s) in the blocklist "
                    f"(< {self.min_domains}) — database may be empty or wiped",
                    "WARNING",
                )],
                status='warning',
            )

        return CollectResult(
            metrics=metrics,
            logs=[(f"{domain_count} domain(s), {url_total} blocked URL(s)", "INFO")],
            status='online',
        )


class BlockurlUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.min_domains = int(config.get('min_domains', 1))

        from vigil.web.ui.spec import register_color_rule
        self._color_rule_name = f'blockurl_min_domains_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _domains_color(v, _min_domains=self.min_domains):
            if v is None:
                return None
            return 'warning' if v < _min_domains else 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'domains_card': {
                    'metric': 'domains_total', 'title': 'DOMAINS',
                    'format': 'int', 'color': self._color_rule_name,
                },
                'urls_card': {'metric': 'urls_total', 'title': 'BLOCKED URLS', 'format': 'count_comma'},
            },
            'chart': {'metric': 'urls_total', 'title': 'BLOCKED URLS'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
