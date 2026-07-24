import json

import pytest

from vigil.plugins.syncthing import Syncthing
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


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
    return make_plugin(Syncthing, BASE_CFG)


def _collect_twice(plugin, run_cycle, config=None, folder_statuses=None, connections=None,
                   watched_folders=None):
    """First cycle discovers folder IDs (config + connections only); second
    cycle fetches per-folder status for the folders discovered in cycle 1 —
    mirrors the one-cycle-lag behavior of the cached-folder-IDs design."""
    cfg = config if config is not None else _CONFIG
    watched = watched_folders if watched_folders is not None else [f["id"] for f in cfg["folders"]]
    fs = folder_statuses or {"docs": _folder_status(), "photos": _folder_status()}
    conn = connections if connections is not None else _connections()

    run_cycle(plugin, lambda c: CmdResult(0, json.dumps(cfg), ""))

    responses = [CmdResult(0, json.dumps(cfg), ""), CmdResult(0, json.dumps(conn), "")]
    responses += [CmdResult(0, json.dumps(fs[folder_id]), "") for folder_id in watched]
    return run_cycle(plugin, lambda c, _it=iter(responses): next(_it))


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
    async def test_all_idle_connected_sets_online(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle)
        assert _latest_status() == "online"

    async def test_folder_error_state_sets_failed(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle, folder_statuses={
            "docs": _folder_status(state="error"),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_idle_with_needed_files_sets_failed(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle, folder_statuses={
            "docs": _folder_status(state="idle", need_files=5, need_bytes=1000),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_pull_errors_set_warning(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle, folder_statuses={
            "docs": _folder_status(pull_errors=2),
            "photos": _folder_status(),
        })
        assert _latest_status() == "warning"

    async def test_disconnected_device_sets_warning(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle, connections=_connections(connected=False))
        assert _latest_status() == "warning"

    async def test_invalid_folder_sets_failed(self, plugin, run_cycle):
        _collect_twice(plugin, run_cycle, folder_statuses={
            "docs": _folder_status(invalid="path missing"),
            "photos": _folder_status(),
        })
        assert _latest_status() == "failed"

    async def test_ssh_failure_on_config_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_first_cycle_discovers_folders(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(0, json.dumps(_CONFIG), ""))
        assert result.status == "warning"
        assert plugin._cached_folder_ids == ["docs", "photos"]

    async def test_folder_filter_excludes_others(self, make_plugin, run_cycle):
        p = make_plugin(Syncthing, {**BASE_CFG, "folders": ["docs"]})
        _collect_twice(p, run_cycle, watched_folders=["docs"], folder_statuses={
            "docs": _folder_status(),
            "photos": _folder_status(state="error"),
        })
        assert _latest_status("test-syncthing") == "online"


class TestSyncthingActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
