import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.cloud import CloudCollectorPlugin, _parse_kv
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid="test-cloud"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-cloud"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-cloud", "id": "test-cloud", "ssh_config": {"host": "test.host"}}
    base.update(extra)
    return base


_AWS_OUT = (
    "provider=aws\n"
    "instance_id=i-0abc123\n"
    "instance_type=t3.micro\n"
    "region=us-east-1\n"
    "az=us-east-1a\n"
)


class TestParseKv:
    def test_basic(self):
        assert _parse_kv("a=1\nb=two") == {"a": "1", "b": "two"}

    def test_value_with_equals(self):
        assert _parse_kv("url=http://x?a=b")["url"] == "http://x?a=b"


class TestCloudCollection:
    async def test_aws_detected_online(self, make_plugin, run_cycle):
        p = make_plugin(CloudCollectorPlugin, _cfg(provider="aws"))
        run_cycle(p, lambda c: CmdResult(0, _AWS_OUT, ""))
        assert _latest_status() == "online"
        assert _latest_metric("on_cloud") == pytest.approx(1.0)

    async def test_not_cloud_offline(self, make_plugin, run_cycle):
        p = make_plugin(CloudCollectorPlugin, _cfg(provider="aws"))
        run_cycle(p, lambda c: CmdResult(7, "", ""))
        assert _latest_status() == "offline"
        assert _latest_metric("on_cloud") == pytest.approx(0.0)

    async def test_auto_falls_through_providers(self, make_plugin, run_cycle):
        p = make_plugin(CloudCollectorPlugin, _cfg(provider="auto"))
        outputs = [
            CmdResult(7, "", ""),
            CmdResult(7, "", ""),
            CmdResult(0, "provider=azure\nraw={}\n", ""),
        ]
        run_cycle(p, lambda c, _it=iter(outputs): next(_it))
        assert _latest_status() == "online"

    async def test_auto_none_respond_offline(self, make_plugin, run_cycle):
        p = make_plugin(CloudCollectorPlugin, _cfg(provider="auto"))
        run_cycle(p, lambda c: CmdResult(7, "", ""))
        assert _latest_status() == "offline"


class TestCloudActions:
    async def test_on_action_returns_false(self, make_plugin):
        p = make_plugin(CloudCollectorPlugin, _cfg())
        assert p.plan_action("anything") is None
