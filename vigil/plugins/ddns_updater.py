"""
Dynamic DNS updater: keeps a DNS record pointed at this network's current
public IP, and reports the record's health while doing it.

Every cycle: discover the current public IP, resolve what `domain` currently
answers publicly (bypassing any local resolver override — see `resolver`),
and push an update to the provider only when the two differ. A provider
update is a real side effect against sonmeone else's infrastructure and one
most providers rate-limit or ban accounts for firing too often, so this
plugin only ever calls it on detected drift — never on a fixed schedule
regardless of whether anything changed.

Currently supports FreeDNS's (afraid.org / *.mooo.com and its other free
subdomains) per-host dynamic update URL — a plain HTTPS GET to a secret,
account-specific URL that updates the record to the caller's apparent IP.
That URL already encodes which record it updates, so this plugin never
needs the provider's REST API or credentials beyond the URL itself.

Config options:
  domain          Domain name whose public record is kept current (required)
  update_url      The provider's per-host dynamic update URL, containing its
                  own secret token (required). Prefer `update_url_file` /
                  `update_url_command` to keep the token out of config.yaml.
  update_url_file       Path to a file containing the update URL.
  update_url_command    Shell command whose stdout is the update URL.
  resolver        Resolver IP to check the current public record against
                  (default: "8.8.8.8" — a local resolver may have a hosts-file
                  override for this exact domain, which would mask real DNS
                  drift by always answering with the LAN IP instead)
  record_type     Record type being kept current (default: "A")
  timeout         Timeout in seconds for both IP lookup and the update request
                  (default: 10)
  min_interval    Minimum seconds between update attempts, regardless of how
                  often `interval` ticks (default: 300) — a second guard
                  against hammering the provider if drift is detected on
                  every cycle for some reason (e.g. a provider that silently
                  fails to apply the update).

Precedence when more than one is set: `update_url` > `update_url_file` >
`update_url_command`.
"""
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

# Plain-text IP echo services, tried in order. Each returns the caller's
# public IP as the entire response body — no JSON parsing, no API key. Several
# are configured so one endpoint being down or rate-limiting us doesn't stall
# every cycle.
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
    """
    Detects public-IP drift against a DNS record and pushes an update to the
    provider (FreeDNS-style update URL) when they differ.
    """
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
        """
        Resolve the provider update URL per the documented precedence.
        Re-read every time rather than cached at __init__, matching how
        borg.py treats its passphrase_command — so a rotated secret file
        takes effect without restarting Vigil.
        """
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
        """Try each IP-echo service in turn, returning the first usable answer."""
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
        """Current public answer for `domain`, via `resolver` (blocking)."""
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
        """Call the provider's update URL. Returns True on a successful update."""
        try:
            resp = self._session.get(update_url, timeout=self.timeout)
        except requests.RequestException as e:
            self.db_logger.write(f"Update request failed: {e}", level="ERROR")
            return False

        body = resp.text.strip()
        # FreeDNS's own convention: "good <ip>" / "nochg <ip>" on success,
        # anything else (a login page, an error string) is a failure. Other
        # per-host-URL providers follow the same convention closely enough
        # that this stays a reasonable default; a 2xx with a body starting
        # "good"/"nochg" is accepted regardless of provider.
        ok = resp.status_code == 200 and body.lower().startswith(('good', 'nochg'))
        if ok:
            self.db_logger.write(f"Update accepted: {body}", level="INFO")
        else:
            self.db_logger.write(
                f"Update rejected (HTTP {resp.status_code}): {body[:200]}", level="ERROR"
            )
        return ok

    def _collect_sync(self) -> Dict[str, Any]:
        """
        The blocking half of a cycle — public IP lookup, DNS check, and
        (if needed) the update call — run together in one thread so the
        event loop is blocked for one hop instead of three.
        """
        result: Dict[str, Any] = {
            'public_ip': self._fetch_public_ip(),
            'dns_ip': None,
            'updated': None,  # None = not attempted, True/False = outcome
        }
        if not self.domain:
            return result

        result['dns_ip'] = self._resolve_public_record()

        if result['public_ip'] and result['public_ip'] != result['dns_ip']:
            now = time.monotonic()
            if now - self._last_update_attempt < self.min_interval:
                result['updated'] = None  # throttled, not attempted
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
            # The provider's authoritative record just changed but public
            # resolvers/caches will not reflect it until this record's TTL
            # elapses, so re-reading it this same cycle would still show the
            # old value. Report online: the update succeeded, which is the
            # thing this monitor can actually verify synchronously.
            self.set_status('online')
        elif updated is False:
            self.db_logger.write(
                f"{self.domain} is {dns_ip}, should be {public_ip}, update failed",
                level="ERROR"
            )
            self.set_status('failed')
        else:
            # Drift detected but throttled by min_interval, or push not
            # attempted this cycle. Not yet a failure — give the next
            # eligible attempt a chance before alarming.
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
    """Dashboard rendering for the ddns_updater monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
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
            self.internal_modules['ui']['events_table']()

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

        on_data_event(('setting', 'metric'), public_ip_label, update)
