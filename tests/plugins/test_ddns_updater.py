from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.ddns_updater import DdnsUpdaterCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(pid, name):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == pid) & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {
        "name": "test-ddns", "id": "test-ddns",
        "domain": "bltechnet.mooo.com",
        "update_url": "https://freedns.example/update?token=secret",
    }
    base.update(extra)
    return base


class TestInSync:
    async def test_matching_ip_sets_online(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_fetch_public_ip', return_value="1.2.3.4"), \
             patch.object(p, '_resolve_public_record', return_value="1.2.3.4"), \
             patch.object(p, '_push_update') as push:
            await p.on_collect()
        assert _latest_status("test-ddns") == "online"
        assert _latest_metric("test-ddns", "in_sync") == pytest.approx(1.0)
        push.assert_not_called()


class TestDriftAndUpdate:
    async def test_drift_triggers_update(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_fetch_public_ip', return_value="5.6.7.8"), \
             patch.object(p, '_resolve_public_record', return_value="1.2.3.4"), \
             patch.object(p, '_push_update', return_value=True) as push:
            await p.on_collect()
        push.assert_called_once_with("https://freedns.example/update?token=secret")
        assert _latest_status("test-ddns") == "online"
        assert _latest_metric("test-ddns", "in_sync") == pytest.approx(0.0)

    async def test_failed_update_sets_failed(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_fetch_public_ip', return_value="5.6.7.8"), \
             patch.object(p, '_resolve_public_record', return_value="1.2.3.4"), \
             patch.object(p, '_push_update', return_value=False):
            await p.on_collect()
        assert _latest_status("test-ddns") == "failed"

    async def test_throttled_update_sets_warning_not_failed(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(min_interval=99999))
        push = MagicMock()
        with patch.object(p, '_fetch_public_ip', return_value="5.6.7.8"), \
             patch.object(p, '_resolve_public_record', return_value="1.2.3.4"), \
             patch.object(p, '_push_update', push):
            p._last_update_attempt = __import__('time').monotonic()
            await p.on_collect()
        push.assert_not_called()
        assert _latest_status("test-ddns") == "warning"

    async def test_missing_update_url_sets_failed(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(update_url=None))
        with patch.object(p, '_fetch_public_ip', return_value="5.6.7.8"), \
             patch.object(p, '_resolve_public_record', return_value="1.2.3.4"):
            await p.on_collect()
        assert _latest_status("test-ddns") == "failed"


class TestFailureModes:
    async def test_public_ip_lookup_failure_sets_failed(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_fetch_public_ip', return_value=None):
            await p.on_collect()
        assert _latest_status("test-ddns") == "failed"

    async def test_missing_domain_sets_failed(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(domain=None))
        await p.on_collect()
        assert _latest_status("test-ddns") == "failed"

    async def test_dns_lookup_failure_still_triggers_update(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_fetch_public_ip', return_value="5.6.7.8"), \
             patch.object(p, '_resolve_public_record', return_value=None), \
             patch.object(p, '_push_update', return_value=True) as push:
            await p.on_collect()
        push.assert_called_once()


class TestUpdateUrlResolution:
    async def test_update_url_file_is_read(self, make_plugin, tmp_path):
        secret_file = tmp_path / "url.txt"
        secret_file.write_text("https://freedns.example/update?token=fromfile\n")
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(update_url=None, update_url_file=str(secret_file)))
        assert p._resolve_update_url() == "https://freedns.example/update?token=fromfile"

    async def test_update_url_command_is_run(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(
            update_url=None, update_url_command="echo https://freedns.example/update?token=fromcmd"
        ))
        assert p._resolve_update_url() == "https://freedns.example/update?token=fromcmd"

    async def test_direct_update_url_takes_precedence(self, make_plugin, tmp_path):
        secret_file = tmp_path / "url.txt"
        secret_file.write_text("https://freedns.example/update?token=fromfile")
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(update_url_file=str(secret_file)))
        assert p._resolve_update_url() == "https://freedns.example/update?token=secret"

    async def test_no_source_configured_returns_none(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(update_url=None))
        assert p._resolve_update_url() is None


class TestPushUpdate:
    def _mock_response(self, status_code=200, text="good 5.6.7.8"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        return resp

    async def test_good_response_is_success(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        p._session = MagicMock(get=MagicMock(return_value=self._mock_response(text="good 5.6.7.8")))
        assert p._push_update("https://freedns.example/update?token=secret") is True

    async def test_nochg_response_is_success(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        p._session = MagicMock(get=MagicMock(return_value=self._mock_response(text="nochg 5.6.7.8")))
        assert p._push_update("https://freedns.example/update?token=secret") is True

    async def test_error_body_is_failure(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        p._session = MagicMock(get=MagicMock(return_value=self._mock_response(text="ERROR: bad token")))
        assert p._push_update("https://freedns.example/update?token=secret") is False

    async def test_non_200_is_failure(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        p._session = MagicMock(get=MagicMock(return_value=self._mock_response(status_code=500, text="good 5.6.7.8")))
        assert p._push_update("https://freedns.example/update?token=secret") is False

    async def test_request_exception_is_failure(self, make_plugin):
        import requests
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        p._session = MagicMock(get=MagicMock(side_effect=requests.RequestException("boom")))
        assert p._push_update("https://freedns.example/update?token=secret") is False


class TestForceUpdateAction:
    async def test_force_update_calls_push(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        with patch.object(p, '_push_update', return_value=True) as push:
            result = await p.on_action('force_update')
        assert result is True
        push.assert_called_once_with("https://freedns.example/update?token=secret")

    async def test_force_update_without_url_returns_false(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg(update_url=None))
        assert await p.on_action('force_update') is False

    async def test_unknown_action_returns_false(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        assert await p.on_action('anything') is False

    async def test_get_actions_lists_force_update(self, make_plugin):
        p = make_plugin(DdnsUpdaterCollectorPlugin, _cfg())
        action_ids = [a['action_id'] for a in p.get_actions()]
        assert 'force_update' in action_ids
