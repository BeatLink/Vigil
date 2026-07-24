import pytest
from vigil.core.database.database import DatabaseManager, db
from vigil.core.connectors.exporters import prometheus, influxdb


@pytest.fixture
def mgr(tmp_path):
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.insert_metric("host1", "cpu-mon", "cpu_pct", 42.5)
    manager.insert_metric("host1", "cpu-mon", "cpu_pct", 55.0)
    manager.insert_metric("host2", "mem-mon", "memory_pct", 30.0)
    manager.insert_status("cpu-mon", "online")
    manager.insert_status("mem-mon", "warning")
    manager.flush()
    yield manager
    if not db.is_closed():
        db.close()


class TestLatestMetrics:
    def test_dedupes_to_newest(self, mgr):
        metrics = mgr.latest_metrics()
        cpu = [m for m in metrics if m['metric_name'] == 'cpu_pct']
        assert len(cpu) == 1
        assert cpu[0]['value'] == pytest.approx(55.0)

    def test_includes_all_series(self, mgr):
        names = {m['metric_name'] for m in mgr.latest_metrics()}
        assert names == {'cpu_pct', 'memory_pct'}


class TestRecentEvents:
    def test_filter_by_level(self, mgr):
        mgr.insert_event("ERROR", "boom", "host1")
        mgr.insert_event("INFO", "fine", "host1")
        mgr.flush()
        errors = mgr.recent_events(level="ERROR")
        assert all(e['level'] == 'ERROR' for e in errors)
        assert any('boom' in e['message'] for e in errors)

    def test_search_substring(self, mgr):
        mgr.insert_event("INFO", "unique-token-xyz", "host1")
        mgr.flush()
        hits = mgr.recent_events(search="unique-token")
        assert len(hits) == 1


class TestPrometheusExporter:
    def test_renders_status_and_metrics(self, mgr):
        text = prometheus.render(mgr)
        assert 'vigil_up{monitor="cpu-mon",state="online"} 1.0' in text
        assert 'vigil_up{monitor="mem-mon",state="warning"} 0.5' in text
        assert 'vigil_metric{monitor="cpu-mon"' in text
        assert 'metric="cpu_pct"} 55.0' in text
        assert text.endswith('\n')

    def test_sanitizes_and_escapes(self, mgr):
        mgr.insert_status('weird"id', 'failed')
        mgr.flush()
        text = prometheus.render(mgr)
        assert 'weird\\"id' in text


class TestInfluxExporter:
    def test_line_protocol_payload(self, mgr):
        payload = influxdb.build_payload(mgr)
        lines = payload.splitlines()
        assert any(l.startswith('vigil_metric,') and 'value=55.0' in l for l in lines)
        assert any(l.startswith('vigil_up,') for l in lines)
        for l in lines:
            assert l.rsplit(' ', 1)[1].isdigit()

    def test_v2_endpoint(self, mgr):
        exp = influxdb.InfluxDBExporter(mgr, {
            'url': 'http://influx:8086', 'org': 'o', 'bucket': 'b', 'token': 't'})
        assert exp._endpoint() == 'http://influx:8086/api/v2/write?org=o&bucket=b&precision=ns'
        assert exp._headers()['Authorization'] == 'Token t'

    def test_v1_endpoint(self, mgr):
        exp = influxdb.InfluxDBExporter(mgr, {'url': 'http://influx:8086', 'database': 'mydb'})
        assert exp._endpoint() == 'http://influx:8086/write?db=mydb&precision=ns'
