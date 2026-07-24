import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import dns.exception
import dns.resolver
import requests

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult, LocalActionPlan
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
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
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

    def _resolve_update_url(self) -> Tuple[Optional[str], Optional[str]]:
        """Returns (url, error_message)."""
        if self._update_url:
            return self._update_url, None
        if self._update_url_file:
            try:
                with open(self._update_url_file) as fh:
                    return fh.read().strip(), None
            except OSError as e:
                return None, f"Could not read update_url_file: {e}"
        if self._update_url_command:
            try:
                result = subprocess.run(
                    self._update_url_command, shell=True, capture_output=True,
                    text=True, timeout=10,
                )
                if result.returncode != 0:
                    return None, f"update_url_command failed: {result.stderr.strip()}"
                return result.stdout.strip(), None
            except Exception as e:
                return None, f"update_url_command failed: {e}"
        return None, None

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

    def _push_update(self, update_url: str) -> Tuple[bool, str]:
        """Returns (ok, log_message)."""
        try:
            resp = self._session.get(update_url, timeout=self.timeout)
        except requests.RequestException as e:
            return False, f"Update request failed: {e}"

        body = resp.text.strip()
        ok = resp.status_code == 200 and body.lower().startswith(('good', 'nochg'))
        if ok:
            return True, f"Update accepted: {body}"
        return False, f"Update rejected (HTTP {resp.status_code}): {body[:200]}"

    def _collect_sync(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'public_ip': self._fetch_public_ip(),
            'dns_ip': None,
            'updated': None,
            'update_log': None,
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

            update_url, url_error = self._resolve_update_url()
            if not update_url:
                result['updated'] = False
                result['update_log'] = url_error or (
                    "Drift detected but no update_url/update_url_file/"
                    "update_url_command configured"
                )
                return result

            ok, log_message = self._push_update(update_url)
            result['updated'] = ok
            result['update_log'] = log_message

        return result

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def local_call(self) -> Optional[Callable[[], Any]]:
        if not self.domain:
            return lambda: {'no_domain': True}
        return self._collect_sync

    def parse_local(self, result: Any) -> CollectResult:
        if result.get('no_domain'):
            return CollectResult.failed("No 'domain' configured")

        public_ip, dns_ip, updated = result['public_ip'], result['dns_ip'], result['updated']

        if public_ip is None:
            return CollectResult.failed("Could not determine public IP (all IP services failed)")

        settings = {f"ddns:{self.id}:public_ip": public_ip}
        if dns_ip:
            settings[f"ddns:{self.id}:dns_ip"] = dns_ip

        in_sync = public_ip == dns_ip
        metrics = {'in_sync': 1.0 if in_sync else 0.0}

        if in_sync:
            return CollectResult(
                metrics=metrics, settings=settings,
                logs=[(f"{self.domain} -> {dns_ip} (in sync)", "INFO")],
                status='online',
            )

        if updated is True:
            metrics['last_update_epoch'] = time.time()
            return CollectResult(
                metrics=metrics, settings=settings,
                logs=[(f"{self.domain} was {dns_ip}, updated to {public_ip}", "INFO")],
                status='online',
            )
        if updated is False:
            return CollectResult(
                metrics=metrics, settings=settings,
                logs=[(f"{self.domain} is {dns_ip}, should be {public_ip}, update failed"
                       + (f": {result['update_log']}" if result.get('update_log') else ""), "ERROR")],
                status='failed',
            )
        return CollectResult(
            metrics=metrics, settings=settings,
            logs=[(
                f"{self.domain} is {dns_ip}, should be {public_ip}, "
                f"update throttled ({format_age(int(time.monotonic() - self._last_update_attempt))} since last attempt)",
                "WARNING",
            )],
            status='warning',
        )

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Force Update', 'action_id': 'force_update', 'variant': 'primary', 'icon': 'sync'},
        ]

    def plan_action(self, action_id: str, **kwargs):
        if action_id != 'force_update':
            return None

        def _do_force_update():
            update_url, url_error = self._resolve_update_url()
            if not update_url:
                return {'ok': False, 'log': url_error or 'Force Update: no update_url configured'}
            self._last_update_attempt = time.monotonic()
            ok, log_message = self._push_update(update_url)
            return {'ok': ok, 'log': log_message}

        return LocalActionPlan(_do_force_update)

    def interpret_local_action(self, action_id: str, result: Any, **kwargs):
        if not result['ok']:
            return CollectResult.failed(result['log'])
        return CollectResult(
            metrics={'last_update_epoch': time.time()},
            logs=[(result['log'], "INFO")],
            success=True,
        )


class DdnsUpdaterUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page(metric_names=[])

        with layout.cell('status_card'):
            self.ui.status_card(
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
            self.ui.events_table(page)

        def update():
            public_ip = self.storage.get_setting(f"ddns:{self.id}:public_ip")
            dns_ip = self.storage.get_setting(f"ddns:{self.id}:dns_ip")
            if public_ip:
                public_ip_label.text = public_ip
            if dns_ip:
                in_sync = dns_ip == public_ip
                dns_ip_label.text = dns_ip
                dns_ip_label.style(f"color: {STATUS_COLORS['online' if in_sync else 'failed']}")

            last_update = self.storage.latest_metric('last_update_epoch')
            if last_update is not None:
                age = int(time.time() - last_update.value)
                lastupdate_label.text = format_age(age)

        page.on_refresh(update)
        update()
        page.start()
