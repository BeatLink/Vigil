"""
InfluxDB exporter (push).

Periodically pushes Vigil's latest metrics to an InfluxDB instance using the
line protocol over HTTP. Supports both InfluxDB 1.x (`/write?db=`) and 2.x
(`/api/v2/write?org=&bucket=` with a token). Configured under an `exporters:`
block in config.yaml; started as a background asyncio task by the engine.

Line protocol: measurement,tag=val field=val timestamp_ns
We emit one measurement `vigil_metric` with tags {monitor,target,metric} and a
single `value` field, plus `vigil_up` for per-monitor status.
"""
import asyncio
import logging
import time
from typing import Any, Dict

import requests

_STATUS_VALUE = {'online': 1.0, 'warning': 0.5, 'failed': 0.0, 'offline': -1.0}


def _escape_tag(value: str) -> str:
    # Tag keys/values escape commas, spaces, and equals signs.
    return value.replace('\\', '\\\\').replace(',', '\\,').replace(' ', '\\ ').replace('=', '\\=')


def _line(measurement: str, tags: Dict[str, str], value: float, ts_ns: int) -> str:
    tag_str = ''.join(f',{_escape_tag(k)}={_escape_tag(v)}' for k, v in tags.items() if v != '')
    return f'{measurement}{tag_str} value={value} {ts_ns}'


def build_payload(db: Any) -> str:
    """Render the current state as an InfluxDB line-protocol payload."""
    ts_ns = int(time.time() * 1e9)
    lines = []
    for m in db.latest_metrics():
        lines.append(_line('vigil_metric', {
            'monitor': m['collector'], 'target': m['target'], 'metric': m['metric_name'],
        }, m['value'], ts_ns))
    for collector_id, state in db.latest_statuses().items():
        lines.append(_line('vigil_up', {'monitor': collector_id, 'state': state},
                           _STATUS_VALUE.get(state, -1.0), ts_ns))
    return '\n'.join(lines)


class InfluxDBExporter:
    """
    Background pusher. Config keys (under exporters.influxdb):
      url        Base InfluxDB URL, e.g. http://localhost:8086   (required)
      interval   Push interval in seconds (default: 30)
      # InfluxDB 2.x:
      org, bucket, token
      # InfluxDB 1.x:
      database   (used when org/bucket not given)
    """

    def __init__(self, db: Any, config: Dict[str, Any]):
        self.db = db
        self.url = config['url'].rstrip('/')
        self.interval = int(config.get('interval', 30))
        self.org = config.get('org')
        self.bucket = config.get('bucket')
        self.token = config.get('token')
        self.database = config.get('database')
        self._session = requests.Session()

    def _endpoint(self) -> str:
        if self.bucket and self.org:  # v2
            return f'{self.url}/api/v2/write?org={self.org}&bucket={self.bucket}&precision=ns'
        return f'{self.url}/write?db={self.database or "vigil"}&precision=ns'  # v1

    def _headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'text/plain; charset=utf-8'}
        if self.token:
            headers['Authorization'] = f'Token {self.token}'
        return headers

    def _push_once(self) -> None:
        payload = build_payload(self.db)
        if not payload:
            return
        resp = self._session.post(self._endpoint(), data=payload.encode('utf-8'),
                                  headers=self._headers(), timeout=10)
        if resp.status_code >= 300:
            logging.warning(f"InfluxDB push failed ({resp.status_code}): {resp.text[:200]}")

    async def run(self) -> None:
        logging.info(f"InfluxDB exporter started -> {self.url} every {self.interval}s")
        while True:
            try:
                # requests is blocking; run it off the event loop.
                await asyncio.to_thread(self._push_once)
            except Exception as e:
                logging.warning(f"InfluxDB push error: {e}")
            await asyncio.sleep(self.interval)
