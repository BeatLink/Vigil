import json

import pytest

from vigil.plugins.syncthing import Syncthing
from vigil.core.connectors.types import HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-syncthing",
    "id":   "test-syncthing",
    "api_url": "http://syncthing.test:8384",
    "api_key": "testkey",
    "ssh_config": {"host": "test.host"},
}


def _ok(payload):
    return HttpResult(status_code=200, text=json.dumps(payload))

_CONFIG = {
    "folders": [{"id": "docs"}, {"id": "photos"}],
    "devices": [{"deviceID": "DEV1", "name": "Odin"}, {"deviceID": "DEV2", "name": "Thor"}],
}


def _folder_status(state="idle", need_files=0, need_bytes=0, pull_errors=0, invalid=""):
    return {"state": state, "needFiles": need_files, "needBytes": need_bytes,
            "pullErrors": pull_errors, "invalid": invalid}


def _connections(connected=True):
    return {"connections": {
        "DEV1": {"connected": connected},
        "DEV2": {"connected": connected},
    }}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Syncthing, BASE_CFG)


def _collect_twice(plugin, run_requests, config=None, folder_statuses=None, connections=None,
                   watched_folders=None):
    """First cycle discovers folder IDs (config + connections only); second
    cycle fetches per-folder status for the folders discovered in cycle 1 —
    mirrors the one-cycle-lag behavior of the cached-folder-IDs design."""
    cfg = config if config is not None else _CONFIG
    watched = watched_folders if watched_folders is not None else [f["id"] for f in cfg["folders"]]
    fs = folder_statuses or {"docs": _folder_status(), "photos": _folder_status()}
    conn = connections if connections is not None else _connections()

    # Cycle 1: only config + connections requested (no cached folder IDs yet).
    responses1 = [_ok(cfg), _ok(conn)]
    run_requests(plugin, lambda r, _it=iter(responses1): next(_it))

    # Cycle 2: config + connections + one status per discovered folder.
    responses2 = [_ok(cfg), _ok(conn)]
    responses2 += [_ok(fs[folder_id]) for folder_id in watched]
    return run_requests(plugin, lambda r, _it=iter(responses2): next(_it))


def _latest_status(plugin_id: str = "test-syncthing") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-syncthing") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestRequests:
    def test_config_and_connections_use_api_key_header(self, plugin):
        reqs = plugin.requests()
        assert len(reqs) == 2  # no cached folder IDs on first cycle
        assert all(isinstance(r, HttpRequest) for r in reqs)
        assert reqs[0].url == "http://syncthing.test:8384/rest/system/config"
        assert reqs[1].url == "http://syncthing.test:8384/rest/system/connections"
        assert reqs[0].headers == {"X-API-Key": "testkey"}

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(Syncthing, {"name": "s", "id": "s",
                                    "ssh_config": {"host": "h"}})
        assert p.requests() == []

    def test_folder_requests_added_after_discovery(self, plugin):
        plugin._cached_folder_ids = ["docs", "photos"]
        reqs = plugin.requests()
        assert len(reqs) == 4
        assert reqs[2].url.endswith("/rest/db/status?folder=docs")
        assert reqs[3].url.endswith("/rest/db/status?folder=photos")


class TestSyncthingCollection:
    async def test_all_idle_connected_sets_online(self, plugin, run_requests):
        _collect_twice(plugin, run_requests)
        assert _latest_status() == "online"

    async def test_folder_error_state_sets_failed(self, plugin, run_requests):
        _collect_twice(plugin, run_requests, folder_statuses={
            "docs": _folder_status(state="error"),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_idle_with_needed_files_sets_failed(self, plugin, run_requests):
        _collect_twice(plugin, run_requests, folder_statuses={
            "docs": _folder_status(state="idle", need_files=5, need_bytes=1000),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_pull_errors_set_warning(self, plugin, run_requests):
        _collect_twice(plugin, run_requests, folder_statuses={
            "docs": _folder_status(pull_errors=2),
            "photos": _folder_status(),
        })
        assert _latest_status() == "warning"

    async def test_local_device_is_not_expected(self, plugin, run_requests):
        cfg = {**_CONFIG, "devices": _CONFIG["devices"] + [{"deviceID": "SELF", "name": "Heimdall"}]}
        _collect_twice(plugin, run_requests, config=cfg)
        assert _latest_status() == "online"
        assert _latest_metric("devices_expected") == 2
        assert _latest_metric("devices_disconnected") == 0

    async def test_disconnected_device_sets_warning(self, plugin, run_requests):
        _collect_twice(plugin, run_requests, connections=_connections(connected=False))
        assert _latest_status() == "warning"

    async def test_invalid_folder_sets_failed(self, plugin, run_requests):
        _collect_twice(plugin, run_requests, folder_statuses={
            "docs": _folder_status(invalid="path missing"),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_http_error_on_config_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r, _it=iter([
            HttpResult(status_code=None, text="", error="connection refused"),
            _ok(_connections()),
        ]): next(_it))
        assert _latest_status() == "failed"

    async def test_bad_api_key_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r, _it=iter([
            HttpResult(status_code=403, text="CSRF"),
            _ok(_connections()),
        ]): next(_it))
        assert _latest_status() == "failed"

    async def test_first_cycle_discovers_folders(self, plugin, run_requests):
        result = run_requests(plugin, lambda r, _it=iter([
            _ok(_CONFIG), _ok(_connections())]): next(_it))
        assert result.status == "warning"
        assert plugin._cached_folder_ids == ["docs", "photos"]

    async def test_folder_filter_excludes_others(self, make_plugin, run_requests):
        p = make_plugin(Syncthing, {**BASE_CFG, "folders": ["docs"]})
        _collect_twice(p, run_requests, watched_folders=["docs"], folder_statuses={
            "docs": _folder_status(),
            "photos": _folder_status(state="error"),
        })
        assert _latest_status("test-syncthing") == "online"


class TestSyncthingActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
