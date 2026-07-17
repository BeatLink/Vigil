import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.cloud import CloudPlugin, _parse_kv
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
    async def test_aws_detected_online(self, make_plugin):
        p = make_plugin(CloudPlugin, _cfg(provider="aws"))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _AWS_OUT, ""))
        await p.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("on_cloud") == pytest.approx(1.0)

    async def test_not_cloud_offline(self, make_plugin):
        p = make_plugin(CloudPlugin, _cfg(provider="aws"))
        # exit 7 is the "endpoint didn't answer" sentinel
        p.ssh_collector.fetch_output = AsyncMock(return_value=(7, "", ""))
        await p.on_collect()
        assert _latest_status() == "offline"
        assert _latest_metric("on_cloud") == pytest.approx(0.0)

    async def test_auto_falls_through_providers(self, make_plugin):
        p = make_plugin(CloudPlugin, _cfg(provider="auto"))
        # aws fails, gcp fails, azure succeeds
        p.ssh_collector.fetch_output = AsyncMock(side_effect=[
            (7, "", ""),
            (7, "", ""),
            (0, "provider=azure\nraw={}\n", ""),
        ])
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_auto_none_respond_offline(self, make_plugin):
        p = make_plugin(CloudPlugin, _cfg(provider="auto"))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(7, "", ""))
        await p.on_collect()
        assert _latest_status() == "offline"


class TestCloudActions:
    async def test_on_action_returns_false(self, make_plugin):
        p = make_plugin(CloudPlugin, _cfg())
        assert await p.on_action("anything") is False
