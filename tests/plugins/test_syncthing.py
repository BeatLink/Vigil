import json
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.syncthing import SyncthingCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-syncthing",
    "id":   "test-syncthing",
    "api_key": "testkey",
    "ssh_config": {"host": "test.host"},
}

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
    return make_plugin(SyncthingCollectorPlugin, BASE_CFG)


def _mock_calls(plugin, config=None, folder_statuses=None, connections=None, watched_folders=None):
    cfg = config if config is not None else _CONFIG
    watched = watched_folders if watched_folders is not None else [f["id"] for f in cfg["folders"]]
    responses = [json.dumps(cfg)]
    fs = folder_statuses or {"docs": _folder_status(), "photos": _folder_status()}
    for folder_id in watched:
        responses.append(json.dumps(fs[folder_id]))
    responses.append(json.dumps(connections if connections is not None else _connections()))

    calls = iter(responses)

    async def fake_fetch(script):
        return (0, next(calls), "")

    plugin.ssh_collector.fetch_output = AsyncMock(side_effect=fake_fetch)


def _latest_status(plugin_id: str = "test-syncthing") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-syncthing") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestSyncthingCollection:
    async def test_all_idle_connected_sets_online(self, plugin):
        _mock_calls(plugin)
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_folder_error_state_sets_failed(self, plugin):
        _mock_calls(plugin, folder_statuses={
            "docs": _folder_status(state="error"),
            "photos": _folder_status(),
        })
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_idle_with_needed_files_sets_failed(self, plugin):
        _mock_calls(plugin, folder_statuses={
            "docs": _folder_status(state="idle", need_files=5, need_bytes=1000),
            "photos": _folder_status(),
        })
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_pull_errors_set_warning(self, plugin):
        _mock_calls(plugin, folder_statuses={
            "docs": _folder_status(pull_errors=2),
            "photos": _folder_status(),
        })
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_disconnected_device_sets_warning(self, plugin):
        _mock_calls(plugin, connections=_connections(connected=False))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_invalid_folder_sets_failed(self, plugin):
        _mock_calls(plugin, folder_statuses={
            "docs": _folder_status(invalid="path missing"),
            "photos": _folder_status(),
        })
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ssh_failure_on_config_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "connection refused"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_folder_filter_excludes_others(self, make_plugin):
        p = make_plugin(SyncthingCollectorPlugin, {**BASE_CFG, "folders": ["docs"]})
        _mock_calls(p, watched_folders=["docs"], folder_statuses={
            "docs": _folder_status(),
            "photos": _folder_status(state="error"),
        })
        await p.on_collect()
        assert _latest_status("test-syncthing") == "online"


class TestSyncthingActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
