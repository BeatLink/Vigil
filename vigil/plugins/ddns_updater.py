import asyncio
import subprocess
import time
from typing import Any, Dict, List, Optional

import dns.exception
import dns.resolver
import requests

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.time_utils import format_age

_IP_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)

_DEFAULT_LAYOUT = [
    ['status_card', 'public_ip_card', 'dns_ip_card'],
    ['lastupdate_card'],
    ['events'],
]


class DdnsUpdaterCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.domain = config.get('domain')
        self.record_type = str(config.get('record_type', 'A')).upper()
        self.resolver_addr = config.get('resolver', '8.8.8.8')
        self.timeout = float(config.get('timeout', 10))
        self.min_interval = float(config.get('min_interval', 300))
        self._update_url = config.get('update_url')
        self._update_url_file = config.get('update_url_file')
        self._update_url_command = config.get('update_url_command')
        self.target = self.domain or self.name
        self._last_update_attempt = 0.0
        self._session = requests.Session()

    def _resolve_update_url(self) -> Optional[str]:
        if self._update_url:
            return self._update_url
        if self._update_url_file:
            try:
                with open(self._update_url_file) as fh:
                    return fh.read().strip()
            except OSError as e:
                self.db_logger.write(f"Could not read update_url_file: {e}", level="ERROR")
                return None
        if self._update_url_command:
            try:
                result = subprocess.run(
                    self._update_url_command, shell=True, capture_output=True,
                    text=True, timeout=10,
                )
                if result.returncode != 0:
                    self.db_logger.write(
                        f"update_url_command failed: {result.stderr.strip()}", level="ERROR"
                    )
                    return None
                return result.stdout.strip()
            except Exception as e:
                self.db_logger.write(f"update_url_command failed: {e}", level="ERROR")
                return None
        return None

    def _fetch_public_ip(self) -> Optional[str]:
        for url in _IP_ECHO_SERVICES:
            try:
                resp = self._session.get(url, timeout=self.timeout)
                ip = resp.text.strip()
                if resp.status_code == 200 and ip:
                    return ip
            except requests.RequestException:
                continue
        return None

    def _resolve_public_record(self) -> Optional[str]:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [self.resolver_addr]
        resolver.timeout = self.timeout
        resolver.lifetime = self.timeout
        try:
            answer = resolver.resolve(self.domain, self.record_type)
            return str(next(iter(answer))).rstrip('.')
        except dns.exception.DNSException:
            return None

    def _push_update(self, update_url: str) -> bool:
        try:
            resp = self._session.get(update_url, timeout=self.timeout)
        except requests.RequestException as e:
            self.db_logger.write(f"Update request failed: {e}", level="ERROR")
            return False

        body = resp.text.strip()
        ok = resp.status_code == 200 and body.lower().startswith(('good', 'nochg'))
        if ok:
            self.db_logger.write(f"Update accepted: {body}", level="INFO")
        else:
            self.db_logger.write(
                f"Update rejected (HTTP {resp.status_code}): {body[:200]}", level="ERROR"
            )
        return ok

    def _collect_sync(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'public_ip': self._fetch_public_ip(),
            'dns_ip': None,
            'updated': None,
        }
        if not self.domain:
            return result

        result['dns_ip'] = self._resolve_public_record()

        if result['public_ip'] and result['public_ip'] != result['dns_ip']:
            now = time.monotonic()
            if now - self._last_update_attempt < self.min_interval:
                result['updated'] = None
                return result
            self._last_update_attempt = now

            update_url = self._resolve_update_url()
            if not update_url:
                self.db_logger.write(
                    "Drift detected but no update_url/update_url_file/"
                    "update_url_command configured", level="ERROR"
                )
                result['updated'] = False
                return result

            result['updated'] = self._push_update(update_url)

        return result

    async def on_collect(self):
        if not self.domain:
            self.db_logger.write("No 'domain' configured", level="ERROR")
            self.set_status('failed')
            return

        r = await asyncio.to_thread(self._collect_sync)
        public_ip, dns_ip, updated = r['public_ip'], r['dns_ip'], r['updated']

        if public_ip is None:
            self.db_logger.write("Could not determine public IP (all IP services failed)", level="ERROR")
            self.set_status('failed')
            return

        self.db.set_setting(f"ddns:{self.id}:public_ip", public_ip)
        if dns_ip:
            self.db.set_setting(f"ddns:{self.id}:dns_ip", dns_ip)

        in_sync = public_ip == dns_ip
        self.db_metrics.metric('in_sync', 1.0 if in_sync else 0.0)

        if in_sync:
            self.db_logger.write(f"{self.domain} -> {dns_ip} (in sync)", level="INFO")
            self.set_status('online')
            return

        if updated is True:
            self.db_metrics.metric('last_update_epoch', time.time())
            self.db_logger.write(
                f"{self.domain} was {dns_ip}, updated to {public_ip}", level="INFO"
            )
            self.set_status('online')
        elif updated is False:
            self.db_logger.write(
                f"{self.domain} is {dns_ip}, should be {public_ip}, update failed",
                level="ERROR"
            )
            self.set_status('failed')
        else:
            self.db_logger.write(
                f"{self.domain} is {dns_ip}, should be {public_ip}, "
                f"update throttled ({format_age(int(time.monotonic() - self._last_update_attempt))} since last attempt)",
                level="WARNING"
            )
            self.set_status('warning')

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Force Update', 'action_id': 'force_update', 'variant': 'primary', 'icon': 'sync'},
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id != 'force_update':
            return False

        update_url = self._resolve_update_url()
        if not update_url:
            self.db_logger.write("Force Update: no update_url configured", level="ERROR")
            return False

        self._last_update_attempt = time.monotonic()
        ok = await asyncio.to_thread(self._push_update, update_url)
        if ok:
            self.db_metrics.metric('last_update_epoch', time.time())
        return ok


class DdnsUpdaterUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=[])

        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                page,
                metric_name='in_sync',
                title='DDNS STATUS',
                on_text='IN SYNC',
                off_text='OUT OF SYNC'
            )
        with layout.cell('public_ip_card'):
            public_ip_label = info_card('PUBLIC IP', '--')
        with layout.cell('dns_ip_card'):
            dns_ip_label = info_card('DNS RECORD', '--')
        with layout.cell('lastupdate_card'):
            lastupdate_label = info_card('LAST UPDATE PUSHED', 'Never')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update():
            public_ip = self.db.get_setting(f"ddns:{self.id}:public_ip")
            dns_ip = self.db.get_setting(f"ddns:{self.id}:dns_ip")
            if public_ip:
                public_ip_label.text = public_ip
            if dns_ip:
                in_sync = dns_ip == public_ip
                dns_ip_label.text = dns_ip
                dns_ip_label.style(f"color: {STATUS_COLORS['online' if in_sync else 'failed']}")

            last_update = self.latest_metric('last_update_epoch')
            if last_update is not None:
                age = int(time.time() - last_update.value)
                lastupdate_label.text = format_age(age)

        page.on_refresh(update)
        update()
        page.start()
