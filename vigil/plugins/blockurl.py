"""Health of a BlockURL blocklist service, checked over HTTP from the Vigil
host with one X-API-Key-authenticated GET of its /urls/domains endpoint, plus
a write probe run on the agent that blocks, checks and unblocks a reserved
URL.
Config: api_url (required, Vigil-reachable), api_key / api_key_command,
min_domains, api_timeout, write_probe, probe_url. It counts the domains and
blocked URLs in the response; fewer than min_domains domains is warning (the
database may be empty or wiped), while a non-200 reply or a write that does
not round-trip is failed. An unreachable API, a malformed response or a probe
that could not run at all (agent not connected) is unavailable, not failed.

Reading alone cannot see a database whose every write fails, which is how a
corrupt index once held this monitor green while nothing could be blocked."""

import json
from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, CollectResult, Command, HttpRequest, HttpResult, Request, Result,
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


AGENT_UNAVAILABLE = "agent-unavailable"
"""Sentinel: the probe could not run at all, which is not evidence that writes fail."""


def _write_probe_error(result) -> str:
    """Return why the block/check/unblock round-trip failed, or an empty string when it worked."""
    if result is None:
        return AGENT_UNAVAILABLE
    if not isinstance(result, CmdResult):
        return f"unexpected probe result type {type(result).__name__}"
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip()[:200]
        if "is not connected" in detail:
            return AGENT_UNAVAILABLE
        return f"probe command exited {result.exit_code}: {detail!r}"

    fields = dict(
        part.split("=", 1)
        for part in result.stdout.strip().split(" ", 2)
        if "=" in part
    )
    for step in ("block", "unblock"):
        if fields.get(step) != "200":
            return f"{step} returned HTTP {fields.get(step) or '?'}"
    try:
        checked = json.loads(fields.get("check", ""))
    except json.JSONDecodeError:
        return f"check response was not JSON: {fields.get('check', '')[:200]!r}"
    if not (isinstance(checked, dict) and any(checked.values())):
        return "a URL blocked moments earlier did not read back as blocked"
    return ""


_DEFAULT_LAYOUT = [
    ["host_card", "domains_card", "urls_card", "write_card"],
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
        self.write_probe = bool(config.get("write_probe", True))
        # Reserved and unreachable, so a probe row left behind by a failed cycle blocks nothing real.
        self.probe_url = config.get(
            "probe_url", "https://vigil-write-probe.invalid/blockurl"
        )
        # Re-read on the agent rather than interpolated, to keep the key out of the command text.
        self.api_key_command = config.get(
            "api_key_command", "cut -d= -f2- /run/secrets/blockurl_api_key"
        )
        from vigil.core.ui.spec import register_color_rule

        self._color_rule_name = f"blockurl_min_domains_{self.id}"

        @register_color_rule(self._color_rule_name)
        def _domains_color(v, _min_domains=self.min_domains):
            if v is None:
                return None
            return "warning" if v < _min_domains else "online"

        self._write_rule_name = f"blockurl_write_ok_{self.id}"

        @register_color_rule(self._write_rule_name)
        def _write_color(v):
            if v is None:
                return None
            return "online" if v else "failed"

    def _probe_command(self) -> str:
        """Block, check and unblock the reserved URL in that order, printing each step's outcome."""
        base = self.api_url.rstrip("/")
        body = json.dumps({"urls": [self.probe_url]})
        call = (
            f"curl -sS -m {self.api_timeout} -X POST "
            '-H "X-API-Key: $KEY" -H "Content-Type: application/json" '
            f"-d '{body}'"
        )
        return (
            f"KEY=$({self.api_key_command}); "
            f"B=$({call} -o /dev/null -w '%{{http_code}}' {base}/urls/block); "
            f"C=$({call} {base}/urls/check); "
            f"D=$({call} -o /dev/null -w '%{{http_code}}' {base}/urls/unblock); "
            'echo "block=$B unblock=$D check=$C"'
        )

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        base = self.api_url.rstrip("/")
        requests: List[Request] = [HttpRequest(
            url=f"{base}/urls/domains", timeout=self.api_timeout,
            headers={"X-API-Key": self.api_key or ""},
        )]
        if self.write_probe:
            requests.append(Command(self._probe_command()))
        return requests

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'api_url' configured")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult.unavailable(
                f"Failed to query BlockURL API: {result.error}"
            )
        if result.status_code != 200:
            return CollectResult.failed(
                f"BlockURL API returned HTTP {result.status_code}"
            )

        try:
            data = _parse_response(result.text)
        except ValueError as e:
            return CollectResult.unavailable(str(e))

        domain_count = len(data)
        url_total = sum(
            int(entry[1])
            for entry in data
            if isinstance(entry, list) and len(entry) == 2
        )

        metrics = {"domains_total": float(domain_count), "urls_total": float(url_total)}

        write_error = None
        if self.write_probe:
            write_error = _write_probe_error(results[1] if len(results) > 1 else None)
            # An unrun probe measured nothing, so it neither passes nor fails the write metric.
            if write_error != AGENT_UNAVAILABLE:
                metrics["write_ok"] = 0.0 if write_error else 1.0

        if write_error == AGENT_UNAVAILABLE:
            return CollectResult(
                metrics=metrics,
                logs=[("Write probe did not run: agent not connected", "WARNING")],
                status="unavailable",
            )

        if write_error:
            return CollectResult(
                metrics=metrics,
                logs=[(f"Write probe failed: {write_error}", "ERROR")],
                status="failed",
            )

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
                "write_card": {
                    "metric": "write_ok",
                    "title": "WRITES",
                    "format": "int",
                    "color": self._write_rule_name,
                },
            },
            "chart": {"metric": "urls_total", "title": "BLOCKED URLS"},
            "events": True,
        }

