import json
import shlex
import subprocess
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.qbittorrent import (
    QbittorrentCollectorPlugin,
    _AUTH_FAILED,
    _SEP,
    _build_action_script,
    _build_fetch_script,
    _format_rate,
    _parse_response,
)
from vigil.core.data.database import db, StatusHistory, Metric


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
    """A payload shaped like qBittorrent's /api/v2/transfer/info."""
    return {
        "connection_status": connection,
        "dl_info_speed": dl_speed,
        "dl_info_data": 91_000_000_000,
        "up_info_speed": up_speed,
        "up_info_data": 44_000_000_000,
        "dht_nodes": 342,
    }


def _torrents(downloading=2, stalled=0, errored=0, seeding=5):
    """A torrent list shaped like /api/v2/torrents/info."""
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
    """Assemble the two-payload stdout the remote script produces."""
    t = json.dumps(transfer if transfer is not None else _transfer())
    l = json.dumps(torrents if torrents is not None else _torrents())
    return f"{t}\n{_SEP}\n{l}"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(QbittorrentCollectorPlugin, BASE_CFG)


def _respond(plugin, transfer=None, torrents=None):
    plugin.ssh_collector.fetch_output = AsyncMock(
        return_value=(0, _response(transfer, torrents), ""))


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
        # The secret is resolved remotely, so it must appear as a command and
        # never as a literal value in the script.
        assert "cat /run/secrets/qbittorrent_api" in script
        assert "auth/login" in script

    def test_inline_password_is_quoted(self):
        # Shell metacharacters in the password must not become executable. The
        # authoritative check is that a real shell assigns the literal string,
        # rather than substituting the command inside it.
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
        # curl exits 0 on an HTTP 403, so the script must inspect the body.
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "pw")
        assert _AUTH_FAILED in script
        assert "Ok." in script

    def test_cookie_jar_is_cleaned_up(self):
        # The jar holds a live session credential; it must not outlive the run.
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "pw")
        assert "mktemp" in script
        assert 'rm -f "$__jar"' in script

    def test_password_command_preferred_over_inline(self):
        script = _build_fetch_script(
            "http://127.0.0.1:9050", 10, "cat /run/secrets/qb", "admin", "inline-pw")
        assert "cat /run/secrets/qb" in script
        assert "inline-pw" not in script


class TestAuthScriptBehaviour:
    """Exercise the generated auth preamble against a real shell."""

    def _run(self, script: str, curl_body: str):
        """Run the script with `curl` stubbed to emit a fixed body."""
        stub = f"curl() {{ printf '%s' {shlex.quote(curl_body)}; }}\n{script}"
        return subprocess.run(["bash", "-c", stub], capture_output=True, text=True)

    def test_rejected_login_exits_nonzero(self):
        script = _build_fetch_script("http://127.0.0.1:9050", 10, None, "admin", "wrong")
        out = self._run(script, "Fails.")
        assert out.returncode != 0
        assert _AUTH_FAILED in out.stderr

    def test_accepted_login_proceeds(self):
        # "Ok." is what qBittorrent returns on success; the script should then
        # continue on to the data calls rather than aborting.
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
        # qBittorrent rejects state-changing calls without a Referer (CSRF).
        script = _build_action_script(
            "http://127.0.0.1:9050", 10, None, None, None,
            "/api/v2/torrents/start", {"hashes": "all"})
        assert "Referer: http://127.0.0.1:9050" in script

    def test_uses_fail_flag(self):
        # Without --fail, curl exits 0 on a 403 and a rejected action would be
        # reported to the operator as a success. Asserted on the line that
        # performs the action, so the flag cannot be satisfied by an unrelated
        # occurrence elsewhere in the script.
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
    @pytest.mark.asyncio
    async def test_healthy_is_online(self, plugin):
        _respond(plugin)
        await plugin.on_collect()
        assert _latest_status() == "online"

    @pytest.mark.asyncio
    async def test_records_metrics(self, plugin):
        _respond(plugin)
        await plugin.on_collect()
        assert _latest_metric("dl_speed_bytes") == 1_500_000
        assert _latest_metric("up_speed_bytes") == 250_000
        assert _latest_metric("torrents_total") == 7
        assert _latest_metric("torrents_downloading") == 2
        assert _latest_metric("torrents_stalled") == 0
        assert _latest_metric("torrents_errored") == 0
        assert _latest_metric("connected") == 1.0

    @pytest.mark.asyncio
    async def test_ssh_failure_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(1, "", "connection refused"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_unparseable_response_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "garbage", ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_disconnected_is_failed(self, plugin):
        # The tunnel-dropped case: process alive, transfers dead.
        _respond(plugin, transfer=_transfer(connection="disconnected",
                                            dl_speed=0, up_speed=0))
        await plugin.on_collect()
        assert _latest_status() == "failed"
        assert _latest_metric("connected") == 0.0

    @pytest.mark.asyncio
    async def test_firewalled_is_warning(self, plugin):
        _respond(plugin, transfer=_transfer(connection="firewalled"))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    @pytest.mark.asyncio
    async def test_firewalled_ignored_when_disabled(self, make_plugin):
        p = make_plugin(QbittorrentCollectorPlugin, {**BASE_CFG, "firewalled_warning": False})
        _respond(p, transfer=_transfer(connection="firewalled"))
        await p.on_collect()
        assert _latest_status() == "online"

    @pytest.mark.asyncio
    async def test_stalled_above_warning_is_warning(self, plugin):
        _respond(plugin, torrents=_torrents(downloading=1, stalled=3))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    @pytest.mark.asyncio
    async def test_stalled_above_threshold_is_failed(self, plugin):
        _respond(plugin, torrents=_torrents(downloading=1, stalled=10))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_few_stalled_stays_online(self, plugin):
        _respond(plugin, torrents=_torrents(downloading=4, stalled=2))
        await plugin.on_collect()
        assert _latest_status() == "online"

    @pytest.mark.asyncio
    async def test_errored_torrent_is_failed(self, plugin):
        _respond(plugin, torrents=_torrents(downloading=1, errored=1))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_idle_client_with_no_downloads_is_online(self, plugin):
        # An empty download queue is not a stall — nothing is in flight to
        # stall. Only seeding torrents present.
        _respond(plugin, torrents=_torrents(downloading=0, stalled=0, seeding=8))
        await plugin.on_collect()
        assert _latest_status() == "online"

    @pytest.mark.asyncio
    async def test_empty_torrent_list_is_online(self, plugin):
        _respond(plugin, torrents=[])
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("torrents_total") == 0

    @pytest.mark.asyncio
    async def test_torrent_without_state_key_does_not_crash(self, plugin):
        _respond(plugin, torrents=[{"name": "partial"}])
        await plugin.on_collect()
        assert _latest_status() == "online"

    @pytest.mark.asyncio
    async def test_worst_condition_wins(self, plugin):
        # Firewalled (warning) alongside an errored torrent (failed) must
        # report failed, not warning.
        _respond(plugin,
                 transfer=_transfer(connection="firewalled"),
                 torrents=_torrents(downloading=1, errored=1))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_metadata_stall_counts_as_stalled(self, plugin):
        # A magnet that cannot fetch its info dictionary is what a dead tunnel
        # looks like for newly added torrents.
        torrents = [{"name": f"meta-{i}", "state": "metaDL"} for i in range(10)]
        _respond(plugin, torrents=torrents)
        await plugin.on_collect()
        assert _latest_status() == "failed"

    @pytest.mark.asyncio
    async def test_auth_failure_is_reported_distinctly(self, plugin, caplog):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(1, "", f"{_AUTH_FAILED}: Fails."))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestActions:
    def test_exposes_expected_actions(self, plugin):
        ids = {a["action_id"] for a in plugin.get_actions()}
        assert ids == {"resume_all", "recheck_errored", "pause_all"}

    def test_no_destructive_action_is_offered(self, plugin):
        # The dashboard fires actions with no confirmation step, so anything
        # that could destroy data must not be reachable from it.
        blob = json.dumps(plugin.get_actions()).lower()
        assert "delete" not in blob
        assert "remove" not in blob

    def test_pause_is_marked_danger(self, plugin):
        pause = next(a for a in plugin.get_actions() if a["action_id"] == "pause_all")
        assert pause["variant"] == "danger"

    @pytest.mark.asyncio
    async def test_unknown_action_returns_false(self, plugin):
        assert await plugin.on_action("nope") is False

    @pytest.mark.asyncio
    async def test_resume_all_uses_modern_endpoint(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await plugin.on_action("resume_all") is True
        script = plugin.ssh_controller.execute_action.call_args[0][0]
        assert "/api/v2/torrents/start" in script
        assert "hashes=all" in script

    @pytest.mark.asyncio
    async def test_resume_all_falls_back_to_legacy_endpoint(self, plugin):
        # qBittorrent < 5.0 only has /resume; the modern /start 404s there.
        plugin.ssh_controller.execute_action = AsyncMock(
            side_effect=[(22, "", "404"), (0, "", "")])
        assert await plugin.on_action("resume_all") is True
        scripts = [c[0][0] for c in plugin.ssh_controller.execute_action.call_args_list]
        assert "/api/v2/torrents/start" in scripts[0]
        assert "/api/v2/torrents/resume" in scripts[1]

    @pytest.mark.asyncio
    async def test_resume_all_fails_when_both_endpoints_fail(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(22, "", "403"))
        assert await plugin.on_action("resume_all") is False

    @pytest.mark.asyncio
    async def test_pause_all_uses_modern_endpoint(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await plugin.on_action("pause_all") is True
        script = plugin.ssh_controller.execute_action.call_args[0][0]
        assert "/api/v2/torrents/stop" in script

    @pytest.mark.asyncio
    async def test_recheck_targets_only_errored_torrents(self, plugin):
        # A recheck re-reads every piece from disk, so it must never be aimed at
        # the whole library.
        torrents = [
            {"name": "ok", "state": "uploading", "hash": "aaa"},
            {"name": "bad", "state": "error", "hash": "bbb"},
            {"name": "gone", "state": "missingFiles", "hash": "ccc"},
        ]
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _response(torrents=torrents), ""))
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))

        assert await plugin.on_action("recheck_errored") is True
        script = plugin.ssh_controller.execute_action.call_args[0][0]
        assert "bbb|ccc" in script
        assert "aaa" not in script

    @pytest.mark.asyncio
    async def test_recheck_with_no_errored_torrents_is_a_noop(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _response(torrents=_torrents(errored=0)), ""))
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))

        assert await plugin.on_action("recheck_errored") is True
        plugin.ssh_controller.execute_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_recheck_fails_when_queue_unreadable(self, plugin):
        # Acting on a queue we could not read would mean guessing at hashes.
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "boom"))
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))

        assert await plugin.on_action("recheck_errored") is False
        plugin.ssh_controller.execute_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_recheck_skips_torrents_without_a_hash(self, plugin):
        torrents = [
            {"name": "no-hash", "state": "error"},
            {"name": "bad", "state": "error", "hash": "bbb"},
        ]
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _response(torrents=torrents), ""))
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))

        assert await plugin.on_action("recheck_errored") is True
        script = plugin.ssh_controller.execute_action.call_args[0][0]
        assert "hashes=bbb" in script
