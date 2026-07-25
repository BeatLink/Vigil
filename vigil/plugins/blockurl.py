import json
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, Command, CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret


def _parse_response(stdout: str) -> list:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"domains response was not JSON ({e}): {stdout[:200]!r}"
        ) from e
    if not isinstance(data, list):
        raise ValueError(f"domains response was not a list: {stdout[:200]!r}")
    return data


_DEFAULT_LAYOUT = [
    ["host_card", "domains_card", "urls_card"],
    ["chart"],
    ["events"],
]


class Blockurl(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.api_url = config.get("api_url")
        # Resolve the API key once, on the Vigil host, so requests() stays pure.
        self.api_key = resolve_secret(
            config.get("api_key"),
            config.get("api_key_command", "cut -d= -f2- /run/secrets/blockurl_api_key"),
        )
        self.min_domains = int(config.get("min_domains", 1))
        self.api_timeout = int(config.get("api_timeout", 10))
        from vigil.core.ui.spec import register_color_rule

        self._color_rule_name = f"blockurl_min_domains_{self.id}"

        @register_color_rule(self._color_rule_name)
        def _domains_color(v, _min_domains=self.min_domains):
            if v is None:
                return None
            return "warning" if v < _min_domains else "online"

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        base = self.api_url.rstrip("/")
        return [HttpRequest(
            url=f"{base}/urls/domains", timeout=self.api_timeout,
            headers={"X-API-Key": self.api_key or ""},
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'api_url' configured")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult.failed(
                f"Failed to query BlockURL API: {result.error}"
            )
        if result.status_code != 200:
            return CollectResult.failed(
                f"BlockURL API returned HTTP {result.status_code}"
            )

        try:
            data = _parse_response(result.text)
        except ValueError as e:
            return CollectResult.failed(str(e))

        domain_count = len(data)
        url_total = sum(
            int(entry[1])
            for entry in data
            if isinstance(entry, list) and len(entry) == 2
        )

        metrics = {"domains_total": float(domain_count), "urls_total": float(url_total)}

        if domain_count < self.min_domains:
            return CollectResult(
                metrics=metrics,
                logs=[
                    (
                        f"Only {domain_count} domain(s) in the blocklist "
                        f"(< {self.min_domains}) — database may be empty or wiped",
                        "WARNING",
                    )
                ],
                status="warning",
            )

        return CollectResult(
            metrics=metrics,
            logs=[(f"{domain_count} domain(s), {url_total} blocked URL(s)", "INFO")],
            status="online",
        )

    @property
    def UI_SPEC(self):
        return {
            "layout": _DEFAULT_LAYOUT,
            "cards": {
                "domains_card": {
                    "metric": "domains_total",
                    "title": "DOMAINS",
                    "format": "int",
                    "color": self._color_rule_name,
                },
                "urls_card": {
                    "metric": "urls_total",
                    "title": "BLOCKED URLS",
                    "format": "count_comma",
                },
            },
            "chart": {"metric": "urls_total", "title": "BLOCKED URLS"},
            "events": True,
        }

    def render_ui(self, context: str = "page"):
        from vigil.core.ui.spec import generic_render

        generic_render(self, context)
