import json
import shlex
import subprocess
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.qbittorrent import (
    Qbittorrent,
    _AUTH_FAILED,
    _SEP,
    _build_action_script,
    _build_fetch_script,
    _format_rate,
    _parse_response,
)
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-qbittorrent",
    "id":   "test-qbittorrent",
    "api_url": "http://127.0.0.1:9050",
    "stalled_warning":   3,
    "stalled_threshold": 10,
    "error_threshold":   1,
    "min_downloading":   1,
    "ssh_config": {"host": "test.host"},
}


def _transfer(connection="connected", dl_speed=1_500_000, up_speed=250_000):
    return {
        "connection_status": connection,
        "dl_info_speed": dl_speed,
        "dl_info_data": 91_000_000_000,
        "up_info_speed": up_speed,
        "up_info_data": 44_000_000_000,
        "dht_nodes": 342,
    }


def _torrents(downloading=2, stalled=0, errored=0, seeding=5):
    out = []
    for i in range(downloading):
        out.append({"name": f"dl-{i}", "state": "downloading", "progress": 0.4})
    for i in range(stalled):
        out.append({"name": f"stalled-{i}", "state": "stalledDL", "progress": 0.1})
    for i in range(errored):
        out.append({"name": f"err-{i}", "state": "error", "progress": 0.9})
    for i in range(seeding):
        out.append({"name": f"seed-{i}", "state": "uploading", "progress": 1.0})
    return out


def _response(transfer=None, torrents=None):
    t = json.dumps(transfer if transfer is not None else _transfer())
    l = json.dumps(torrents if torrents is not None else _torrents())
    return f"{t}\n{_SEP}\n{l}"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Qbittorrent, BASE_CFG)


def _respond(plugin, run_cycle, transfer=None, torrents=None):
    return run_cycle(plugin, lambda c: CmdResult(0, _response(transfer, torrents), ""))


def _latest_status(plugin_id: str = "test-qbittorrent") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-qbittorrent") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestFormatRate:
    def test_bytes(self):
        assert _format_rate(512) == "512 B/s"

    def test_kib(self):
        assert _format_rate(2048) == "2.0 KiB/s"

    def test_mib(self):
        assert _format_rate(1_500_000) == "1.4 MiB/s"

    def test_zero(self):
        assert _format_rate(0) == "0 B/s"


class TestBuildFetchScript:
    def test_no_auth_omits_login(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, None, None)
        assert "auth/login" not in script
        assert "/api/v2/transfer/info" in script
        assert "/api/v2/torrents/info" in script
        assert _SEP in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_fetch_script(
            "http://127.0.0.1:9050", 10,
            "cat /run/secrets/qbittorrent_api", "admin", None)
        assert "cat /run/secrets/qbittorrent_api" in script
        assert "auth/login" in script

    def test_inline_password_is_quoted(self):
        import subprocess
        script = _build_fetch_script(
            "http://127.0.0.1:9050", 10, None, "admin", "p'wd$(x)")
        assign = script.splitlines()[1]
        assert assign.startswith("__pw=")
        out = subprocess.run(
            ["bash", "-c", f'{assign}\nprintf "%s" "$__pw"'],
            capture_output=True, text=True,
        )
        assert out.stdout == "p'wd$(x)"
        assert "auth/login" in script

    def test_username_without_password_skips_auth(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", None)
        assert "auth/login" not in script

    def test_password_without_username_skips_auth(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, None, "pw")
        assert "auth/login" not in script

    def test_trailing_slash_does_not_double_up(self):
        script = _build_fetch_script("http://127.0.0.1:9050/", 10, None, None, None)
        assert "//api/v2" not in script

    def test_login_failure_is_detected(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "pw")
        assert _AUTH_FAILED in script
        assert "Ok." in script

    def test_cookie_jar_is_cleaned_up(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "pw")
        assert "mktemp" in script
        assert 'rm -f "$__jar"' in script

    def test_password_command_preferred_over_inline(self):
        script = _build_fetch_script(
            "http://127.0.0.1:9050", 10, "cat /run/secrets/qb", "admin", "inline-pw")
        assert "cat /run/secrets/qb" in script
        assert "inline-pw" not in script


class TestAuthScriptBehaviour:
    def _run(self, script: str, curl_body: str):
        stub = f"curl() {{ printf '%s' {shlex.quote(curl_body)}; }}\n{script}"
        return subprocess.run(["bash", "-c", stub], capture_output=True, text=True)

    def test_rejected_login_exits_nonzero(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "wrong")
        out = self._run(script, "Fails.")
        assert out.returncode != 0
        assert _AUTH_FAILED in out.stderr

    def test_accepted_login_proceeds(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "right")
        out = self._run(script, "Ok.")
        assert out.returncode == 0
        assert _AUTH_FAILED not in out.stderr

    def test_no_auth_configured_runs_clean(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, None, None)
        out = self._run(script, "{}")
        assert out.returncode == 0


class TestBuildActionScript:
    def test_includes_referer_header(self):
        script = _build_action_script(
            "http://127.0.0.1:9050", 10, None, None, None,
            "/api/v2/torrents/start", {"hashes": "all"})
        assert "Referer: http://127.0.0.1:9050" in script

    def test_uses_fail_flag(self):
        script = _build_action_script(
            "http://127.0.0.1:9050", 10, None, None, None,
            "/api/v2/torrents/start", {"hashes": "all"})
        action_line = script.splitlines()[-1]
        assert "/api/v2/torrents/start" in action_line
        assert " -f " in action_line

    def test_encodes_params(self):
        script = _build_action_script(
            "http://127.0.0.1:9050", 10, None, None, None,
            "/api/v2/torrents/recheck", {"hashes": "abc|def"})
        assert "hashes=abc|def" in script

    def test_authenticates_when_configured(self):
        script = _build_action_script(
            "http://127.0.0.1:9050", 10, "cat /run/secrets/qb", "admin", None,
            "/api/v2/torrents/start", {"hashes": "all"})
        assert "auth/login" in script
        assert "cat /run/secrets/qb" in script


class TestParseResponse:
    def test_parses_both_payloads(self):
        transfer, torrents = _parse_response(_response())
        assert transfer["connection_status"] == "connected"
        assert len(torrents) == 7

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError, match="unexpected API response"):
            _parse_response('{"connection_status": "connected"}')

    def test_forbidden_gives_actionable_message(self):
        with pytest.raises(ValueError, match="Forbidden"):
            _parse_response(f"Forbidden\n{_SEP}\nForbidden")

    def test_malformed_transfer_json_raises(self):
        with pytest.raises(ValueError, match="transfer info was not JSON"):
            _parse_response(f"not json\n{_SEP}\n[]")

    def test_malformed_torrents_json_raises(self):
        with pytest.raises(ValueError, match="torrent list was not JSON"):
            _parse_response(f'{{"connection_status": "connected"}}\n{_SEP}\nnot json')

    def test_transfer_missing_connection_status_raises(self):
        with pytest.raises(ValueError, match="missing 'connection_status'"):
            _parse_response(f'{{"dl_info_speed": 0}}\n{_SEP}\n[]')

    def test_torrents_not_a_list_raises(self):
        with pytest.raises(ValueError, match="was not a list"):
            _parse_response(f'{{"connection_status": "connected"}}\n{_SEP}\n{{}}')


class TestCollect:
    async def test_healthy_is_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_status() == "online"

    async def test_records_metrics(self, plugin, run_cycle):
        _respond(plugin, run_cycle)
        assert _latest_metric("dl_speed_bytes") == 1_500_000
        assert _latest_metric("up_speed_bytes") == 250_000
        assert _latest_metric("torrents_total") == 7
        assert _latest_metric("torrents_downloading") == 2
        assert _latest_metric("torrents_stalled") == 0
        assert _latest_metric("torrents_errored") == 0
        assert _latest_metric("connected") == 1.0

    async def test_ssh_failure_is_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_unparseable_response_is_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "garbage", ""))
        assert _latest_status() == "failed"

    async def test_disconnected_is_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, transfer=_transfer(connection="disconnected",
                                                        dl_speed=0, up_speed=0))
        assert _latest_status() == "failed"
        assert _latest_metric("connected") == 0.0

    async def test_firewalled_is_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, transfer=_transfer(connection="firewalled"))
        assert _latest_status() == "warning"

    async def test_firewalled_ignored_when_disabled(self, make_plugin, run_cycle):
        p = make_plugin(Qbittorrent, {**BASE_CFG, "firewalled_warning": False})
        _respond(p, run_cycle, transfer=_transfer(connection="firewalled"))
        assert _latest_status("test-qbittorrent") == "online"

    async def test_stalled_above_warning_is_warning(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=_torrents(downloading=1, stalled=3))
        assert _latest_status() == "warning"

    async def test_stalled_above_threshold_is_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=_torrents(downloading=1, stalled=10))
        assert _latest_status() == "failed"

    async def test_few_stalled_stays_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=_torrents(downloading=4, stalled=2))
        assert _latest_status() == "online"

    async def test_errored_torrent_is_failed(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=_torrents(downloading=1, errored=1))
        assert _latest_status() == "failed"

    async def test_idle_client_with_no_downloads_is_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=_torrents(downloading=0, stalled=0, seeding=8))
        assert _latest_status() == "online"

    async def test_empty_torrent_list_is_online(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=[])
        assert _latest_status() == "online"
        assert _latest_metric("torrents_total") == 0

    async def test_torrent_without_state_key_does_not_crash(self, plugin, run_cycle):
        _respond(plugin, run_cycle, torrents=[{"name": "partial"}])
        assert _latest_status() == "online"

    async def test_worst_condition_wins(self, plugin, run_cycle):
        _respond(plugin, run_cycle,
                 transfer=_transfer(connection="firewalled"),
                 torrents=_torrents(downloading=1, errored=1))
        assert _latest_status() == "failed"

    async def test_metadata_stall_counts_as_stalled(self, plugin, run_cycle):
        torrents = [{"name": f"meta-{i}", "state": "metaDL"} for i in range(10)]
        _respond(plugin, run_cycle, torrents=torrents)
        assert _latest_status() == "failed"

    async def test_auth_failure_is_reported_distinctly(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", f"{_AUTH_FAILED}: Fails."))
        assert _latest_status() == "failed"


class TestActions:
    def test_exposes_expected_actions(self, plugin):
        ids = {a["action_id"] for a in plugin.get_actions()}
        assert ids == {"resume_all", "recheck_errored", "pause_all"}

    def test_no_destructive_action_is_offered(self, plugin):
        blob = json.dumps(plugin.get_actions()).lower()
        assert "delete" not in blob
        assert "remove" not in blob

    def test_pause_is_marked_danger(self, plugin):
        pause = next(a for a in plugin.get_actions() if a["action_id"] == "pause_all")
        assert pause["variant"] == "danger"

    async def test_unknown_action_returns_false(self, plugin):
        assert plugin.plan_action("nope") is None

    async def test_resume_all_uses_modern_endpoint_with_legacy_fallback(self, plugin):
        plan = plugin.plan_action("resume_all")
        assert "/api/v2/torrents/start" in plan.command
        assert "/api/v2/torrents/resume" in plan.command
        assert "hashes=all" in plan.command
        outcome = plugin.interpret_action("resume_all", CmdResult(0, "", ""))
        assert outcome.success is True

    async def test_resume_all_fails_when_both_endpoints_fail(self, plugin):
        plan = plugin.plan_action("resume_all")
        outcome = plugin.interpret_action("resume_all", CmdResult(22, "", "403"))
        assert outcome.success is False

    async def test_pause_all_uses_modern_endpoint(self, plugin):
        plan = plugin.plan_action("pause_all")
        assert "/api/v2/torrents/stop" in plan.command
        outcome = plugin.interpret_action("pause_all", CmdResult(0, "", ""))
        assert outcome.success is True

    async def test_recheck_targets_only_errored_torrents(self, plugin):
        plan = plugin.plan_action("recheck_errored")
        assert "torrents/info" in plan.command
        assert "torrents/recheck" in plan.command

    async def test_recheck_with_no_errored_torrents_is_a_noop(self, plugin):
        outcome = plugin.interpret_action(
            "recheck_errored", CmdResult(0, "HASHES:", ""))
        assert outcome.success is True

    async def test_recheck_fails_when_queue_unreadable(self, plugin):
        outcome = plugin.interpret_action(
            "recheck_errored", CmdResult(1, "", "boom"))
        assert outcome.success is False

    async def test_recheck_reports_count_from_hashes_line(self, plugin):
        outcome = plugin.interpret_action(
            "recheck_errored", CmdResult(0, "HASHES:bbb|ccc", ""))
        assert outcome.success is True
        assert any("2" in m for m, _ in outcome.logs)
