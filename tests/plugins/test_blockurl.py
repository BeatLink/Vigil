import json

import pytest

from vigil.plugins.blockurl import (
    AGENT_UNAVAILABLE, Blockurl, _parse_response, _write_probe_error,
)
from vigil.core.connectors.types import CmdResult, Command, HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-blockurl",
    "id":   "test-blockurl",
    "api_url": "http://blockurl.test:9001",
    "api_key": "testkey",
    "min_domains": 1,
    "ssh_config": {"host": "test.host"},
}

_DOMAINS = [["example.com", 5], ["other.com", 3]]
_PROBE_URL = "https://vigil-write-probe.invalid/blockurl"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Blockurl, BASE_CFG)


def _result(domains=None):
    return HttpResult(status_code=200,
                      text=json.dumps(domains if domains is not None else _DOMAINS))


def _probe(stdout=None, exit_code=0, stderr=""):
    if stdout is None:
        stdout = f'block=200 unblock=200 check={{"{_PROBE_URL}": true}}'
    return CmdResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _respond(http=None, probe=None):
    """Answer the domains GET and the write probe each with their own result type."""
    return lambda r: (probe or _probe()) if isinstance(r, Command) else (http or _result())


def _latest_status(plugin_id: str = "test-blockurl") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-blockurl") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestParseResponse:
    def test_parses_domain_list(self):
        data = _parse_response(json.dumps(_DOMAINS))
        assert len(data) == 2

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="not a list"):
            _parse_response('{"foo": "bar"}')

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            _parse_response("not json")


class TestRequests:
    def test_targets_domains_with_api_key_header(self, plugin):
        reqs = plugin.requests()
        assert isinstance(reqs[0], HttpRequest)
        assert reqs[0].url == "http://blockurl.test:9001/urls/domains"
        assert reqs[0].headers == {"X-API-Key": "testkey"}

    def test_declares_a_write_probe(self, plugin):
        reqs = plugin.requests()
        assert len(reqs) == 2
        assert isinstance(reqs[1], Command)
        for part in ("/urls/block", "/urls/check", "/urls/unblock"):
            assert part in reqs[1].text

    def test_probe_reads_the_key_on_the_agent_not_in_the_text(self, plugin):
        assert "testkey" not in plugin.requests()[1].text
        assert "/run/secrets/blockurl_api_key" in plugin.requests()[1].text

    def test_write_probe_can_be_disabled(self, make_plugin):
        p = make_plugin(Blockurl, {**BASE_CFG, "write_probe": False})
        assert len(p.requests()) == 1

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(Blockurl, {"name": "b", "id": "b",
                                   "ssh_config": {"host": "h"}})
        assert p.requests() == []


class TestBlockurlCollection:
    async def test_populated_list_sets_online(self, plugin, run_requests):
        run_requests(plugin, _respond())
        assert _latest_status() == "online"

    async def test_empty_list_sets_warning(self, plugin, run_requests):
        run_requests(plugin, _respond(http=_result(domains=[])))
        assert _latest_status() == "warning"

    async def test_http_error_sets_unavailable(self, plugin, run_requests):
        run_requests(plugin, _respond(http=HttpResult(
            status_code=None, text="", error="connection refused")))
        assert _latest_status() == "unavailable"

    async def test_malformed_response_sets_unavailable(self, plugin, run_requests):
        run_requests(plugin, _respond(http=HttpResult(status_code=200, text="<html>oops</html>")))
        assert _latest_status() == "unavailable"

    async def test_401_sets_failed(self, plugin, run_requests):
        run_requests(plugin, _respond(http=HttpResult(status_code=401, text="")))
        assert _latest_status() == "failed"

    async def test_records_url_total(self, plugin, run_requests):
        run_requests(plugin, _respond())
        assert _latest_metric("urls_total") == 8.0

    async def test_records_domain_total(self, plugin, run_requests):
        run_requests(plugin, _respond())
        assert _latest_metric("domains_total") == 2.0


class TestBlockurlActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None


class TestWriteProbeParsing:
    def test_clean_round_trip_is_no_error(self):
        assert _write_probe_error(_probe()) == ""

    def test_missing_result_is_unavailable_not_a_failure(self):
        assert _write_probe_error(None) == AGENT_UNAVAILABLE

    def test_disconnected_agent_is_unavailable_not_a_failure(self):
        probe = _probe(exit_code=-1, stderr="Agent 'heimdall' is not connected")
        assert _write_probe_error(probe) == AGENT_UNAVAILABLE

    def test_nonzero_exit_is_an_error(self):
        assert "exited 7" in _write_probe_error(_probe(exit_code=7, stderr="boom"))

    def test_failed_block_is_an_error(self):
        out = 'block=500 unblock=200 check={"x": true}'
        assert "block returned HTTP 500" in _write_probe_error(_probe(stdout=out))

    def test_failed_unblock_is_an_error(self):
        out = 'block=200 unblock=500 check={"x": true}'
        assert "unblock returned HTTP 500" in _write_probe_error(_probe(stdout=out))

    def test_url_not_reading_back_as_blocked_is_an_error(self):
        out = f'block=200 unblock=200 check={{"{_PROBE_URL}": false}}'
        assert "did not read back as blocked" in _write_probe_error(_probe(stdout=out))

    def test_non_json_check_is_an_error(self):
        out = "block=200 unblock=200 check=<html>oops</html>"
        assert "not JSON" in _write_probe_error(_probe(stdout=out))


class TestWriteProbeCollection:
    async def test_healthy_probe_records_write_ok(self, plugin, run_requests):
        run_requests(plugin, _respond())
        assert _latest_metric("write_ok") == 1.0
        assert _latest_status() == "online"

    async def test_a_failing_write_sets_failed_though_reads_are_fine(self, plugin, run_requests):
        """The regression this probe exists for: reads healthy, every write rejected."""
        broken = _probe(exit_code=1, stderr="database disk image is malformed")
        run_requests(plugin, _respond(probe=broken))
        assert _latest_status() == "failed"
        assert _latest_metric("write_ok") == 0.0
        assert _latest_metric("domains_total") == 2.0

    async def test_write_failure_outranks_an_empty_blocklist_warning(self, plugin, run_requests):
        run_requests(plugin, _respond(http=_result(domains=[]), probe=_probe(exit_code=1)))
        assert _latest_status() == "failed"

    async def test_disabled_probe_records_no_write_metric(self, make_plugin, run_requests):
        p = make_plugin(Blockurl, {**BASE_CFG, "id": "bu-noprobe", "name": "bu-noprobe",
                                   "write_probe": False})
        run_requests(p, lambda r: _result())
        assert _latest_status("bu-noprobe") == "online"
        assert _latest_metric("write_ok", "bu-noprobe") is None


class TestProbeUnavailable:
    async def test_disconnected_agent_is_unavailable_not_failed(self, plugin, run_requests):
        """A Vigil restart races the agent connection; that is not a write failure."""
        probe = _probe(exit_code=-1, stderr="Agent 'heimdall' is not connected")
        run_requests(plugin, _respond(probe=probe))
        assert _latest_status() == "unavailable"

    async def test_unrun_probe_records_no_write_metric(self, plugin, run_requests):
        probe = _probe(exit_code=-1, stderr="Agent 'heimdall' is not connected")
        run_requests(plugin, _respond(probe=probe))
        assert _latest_metric("write_ok") is None
        assert _latest_metric("domains_total") == 2.0
