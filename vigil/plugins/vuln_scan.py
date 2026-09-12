"""Network vulnerability exposure: one `nmap --script vuln` sweep of a host per
cycle. The scan runs wherever this monitor's agent (or SSH target) lives and is
pointed at `scan_host`, so it sees the host the way a network peer does —
through its firewall — rather than over loopback, where a host's own firewall
lets everything through. Every finding the NSE vuln scripts report is one row:
a definite `VULNERABLE` state fails the monitor, a `LIKELY VULNERABLE` one
warns, and a `vulners` version match is graded by its CVSS score against
`warning_cvss` / `threshold_cvss`. Open ports and their detected services are
kept alongside so a new exposure is visible even when no script objects to it.
Config: scan_host, ports, scripts, script_args, service_detection,
discovery_ports, nmap_args, warning_cvss, threshold_cvss, likely_status,
max_findings, require_sudo, nmap_bin, timeout."""

import shlex
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, Command, CollectResult, Status
from vigil.plugins.base.plugin_helpers import (
    KILL_GRACE_SECONDS, StatusAccumulator, deadline_prefix, format_age, format_duration,
)


_DEFAULT_LAYOUT = [
    ['host_card', 'vulnerable_card', 'suspected_card', 'noted_card'],
    ['last_scan_card', 'duration_card', 'open_ports_card'],
    ['findings'],
    ['ports'],
    ['chart'],
    ['events'],
]

# The NSE vulns library's states: VULNERABLE, VULNERABLE (Exploitable) and VULNERABLE (DoS) are definite, LIKELY VULNERABLE is a suspicion.
_LIKELY_STATE = 'LIKELY VULNERABLE'

# Finding grades, named after the status each one escalates the monitor to.
_FAILED, _WARNING, _NOTED = 'failed', 'warning', 'online'
_GRADE_LABEL = {_FAILED: 'VULNERABLE', _WARNING: 'LIKELY', _NOTED: 'NOTE'}

# The output a script leaves when it found nothing; a plain-text result that reads like this is not a finding.
_NEGATIVE_PHRASES = ("couldn't find", 'could not find', 'no vulnerabilities', 'not vulnerable',
                     'no exploitable', 'nothing found')


def _severity(value, default: str) -> str:
    """Read a configured status name, falling back on anything unrecognised."""
    try:
        return str(Status(str(value)))
    except ValueError:
        return default


def _elem_text(table: ET.Element, key: str) -> Optional[str]:
    """The text of the `<elem key=...>` directly under a table, if present."""
    elem = table.find(f"elem[@key='{key}']")
    return elem.text.strip() if elem is not None and elem.text else None


def _table_items(table: ET.Element, key: str) -> List[str]:
    """The texts listed in a `<table key=...>` under a table."""
    inner = table.find(f"table[@key='{key}']")
    if inner is None:
        return []
    return [e.text.strip() for e in inner.findall('elem') if e.text]


def _cvss_of(table: ET.Element) -> Optional[float]:
    """The CVSS score a vulns-library table carries under `scores`."""
    scores = table.find("table[@key='scores']")
    if scores is None:
        return None
    for key in ('CVSS', 'CVSSv3', 'CVSSv2'):
        value = _elem_text(scores, key)
        if value:
            try:
                return float(value.split()[0])
            except ValueError:
                continue
    return None


def _grade_state(state: str, likely_status: str) -> Optional[str]:
    """The grade a vulns-library state earns, or None for one that says nothing."""
    if state.startswith('VULNERABLE'):
        return _FAILED
    if state == _LIKELY_STATE:
        return likely_status
    return None


def _grade_cvss(cvss: Optional[float], warning: float, threshold: float) -> str:
    """The grade a version-matched advisory earns from its CVSS score."""
    if cvss is None:
        return _NOTED
    if cvss >= threshold:
        return _FAILED
    if cvss >= warning:
        return _WARNING
    return _NOTED


def _looks_vulnerable(text: str) -> bool:
    """Whether a script's plain output declares a vulnerability outright."""
    return 'VULNERABLE' in text and 'NOT VULNERABLE' not in text


def _looks_negative(text: str) -> bool:
    """Whether a script's plain output is one of the stock nothing-found lines."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in _NEGATIVE_PHRASES)


class VulnScan(Plugin):
    DEFAULT_TIMEOUT = '20m'

    def __init__(self, name: str, config):
        config = {'timeout': self.DEFAULT_TIMEOUT, **config}
        super().__init__(name, config)
        self.scan_host = str(config.get('scan_host') or config.get('target_host') or 'localhost')
        self.ports = str(config['ports']) if config.get('ports') else None
        self.scripts = str(config.get('scripts', 'vuln'))
        self.script_args = str(config['script_args']) if config.get('script_args') else None
        self.service_detection = bool(config.get('service_detection', True))
        self.discovery_ports = [int(p) for p in config.get('discovery_ports', [22, 80, 443])]
        self.nmap_args = [str(a) for a in config.get('nmap_args', [])]
        self.warning_cvss = float(config.get('warning_cvss', 4.0))
        self.threshold_cvss = float(config.get('threshold_cvss', 7.0))
        self.likely_status = _severity(config.get('likely_status'), 'warning')
        self.max_findings = int(config.get('max_findings', 200))
        self.require_sudo = bool(config.get('require_sudo', False))
        self.nmap_bin = str(config.get('nmap_bin', 'nmap'))

        from vigil.core.ui.spec import register_item_color_rule
        self._grade_color = f'vuln_scan_grade_{self.id}'
        register_item_color_rule(self._grade_color)(lambda row: row.get('grade'))

    # --- collection ---

    def _scan_command(self) -> str:
        """The nmap invocation, bounded by a deadline placed inside any sudo."""
        argv = [self.nmap_bin, '-n', '-oX', '-']
        if self.discovery_ports:
            argv.append('-PS' + ','.join(str(p) for p in self.discovery_ports))
        else:
            argv.append('-Pn')
        if self.service_detection:
            argv.append('-sV')
        argv += ['--script', self.scripts]
        if self.script_args:
            argv += ['--script-args', self.script_args]
        if self.ports:
            argv += ['-p', self.ports]
        argv += self.nmap_args
        argv.append(self.scan_host)
        prefix = ['sudo', '-n'] if self.require_sudo else []
        return ' '.join(prefix + deadline_prefix(int(self.timeout)) + [shlex.quote(a) for a in argv])

    def commands(self) -> List[Command]:
        # The agent's own deadline sits past the inner one, so a scan that overruns reports as such rather than as a lost reply.
        return [Command(self._scan_command(), timeout=self.timeout + KILL_GRACE_SECONDS + 10)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not results:
            return CollectResult.failed('No scan result for this cycle', status='offline')
        result = results[0]
        stdout = result.stdout or ''
        if '<nmaprun' not in stdout:
            reason = (result.stderr or stdout).strip()[:200] or f'exit {result.exit_code}'
            return CollectResult.failed(f"nmap did not run: {reason}", status='offline')
        try:
            root = ET.fromstring(stdout)
        except ET.ParseError:
            return CollectResult.failed(
                f"The scan of {self.scan_host} did not finish within "
                f"{format_duration(int(self.timeout))}", status='offline')

        host = root.find('host')
        status = host.find('status') if host is not None else None
        if host is None or status is None or status.get('state') != 'up':
            probe = (', '.join(str(p) for p in self.discovery_ports)
                     if self.discovery_ports else 'no discovery')
            return CollectResult(
                metrics={'host_up': 0.0},
                logs=[(f"{self.scan_host} did not answer on ports {probe}; nothing scanned",
                       'WARNING')],
                status='offline')

        return self._assemble(root, host)

    def _assemble(self, root: ET.Element, host: ET.Element) -> CollectResult:
        """Turn a completed scan into metrics, one row per open port and per
        finding, and the worst grade any finding earned."""
        acc = StatusAccumulator()
        open_ports = self._open_ports(host)
        findings = self._findings(host)
        for finding in findings:
            acc.escalate(finding['grade'])

        counts = {grade: sum(1 for f in findings if f['grade'] == grade)
                  for grade in (_FAILED, _WARNING, _NOTED)}
        finished = root.find('runstats/finished')
        elapsed = float(finished.get('elapsed') or 0) if finished is not None else 0.0
        ended = host.get('endtime') or (finished.get('time') if finished is not None else None)
        scanned_at = int(ended) if ended and ended.isdigit() else int(time.time())
        address = host.find('address')
        addr = address.get('addr') if address is not None else self.scan_host

        summary = (f"{self.scan_host} ({addr}): {len(open_ports)} open ports, "
                   f"{counts[_FAILED]} vulnerable, {counts[_WARNING]} likely, "
                   f"{counts[_NOTED]} noted, scanned in {format_duration(int(elapsed))}")
        logs: List[Tuple[str, str]] = [(summary, acc.log_level)]
        for finding in findings:
            if finding['grade'] == _NOTED:
                continue
            logs.append((
                f"{finding['port']} {finding['service']}: {finding['title']} "
                f"[{finding['script']}] {finding['state']}",
                Status(finding['grade']).log_level))

        metrics = {
            'vulnerable': float(counts[_FAILED]),
            'suspected': float(counts[_WARNING]),
            'noted': float(counts[_NOTED]),
            'open_ports': float(len(open_ports)),
            'scan_seconds': elapsed,
            'host_up': 1.0,
            'last_scan_epoch': float(scanned_at),
        }
        return CollectResult(
            metrics=metrics, logs=logs, status=str(acc.status),
            snapshot={'host': self.scan_host, 'address': addr, 'scanned_at': scanned_at,
                      'elapsed': elapsed, 'nmap_version': root.get('version'),
                      'open_ports': open_ports, 'findings': findings[:self.max_findings]},
        )

    @staticmethod
    def _open_ports(host: ET.Element) -> List[Dict[str, Any]]:
        """One row per open port, with whatever service detection learned."""
        rows = []
        for port in host.findall('ports/port'):
            state = port.find('state')
            if state is None or state.get('state') != 'open':
                continue
            service = port.find('service')
            svc = service.attrib if service is not None else {}
            rows.append({
                'port': f"{port.get('portid')}/{port.get('protocol')}",
                'service': svc.get('name', '') + ('/ssl' if svc.get('tunnel') == 'ssl' else ''),
                'product': svc.get('product', ''),
                'version': ' '.join(v for v in (svc.get('version'), svc.get('extrainfo')) if v),
            })
        return rows

    def _findings(self, host: ET.Element) -> List[Dict[str, Any]]:
        """Every finding the scripts reported, on ports and against the host."""
        findings: List[Dict[str, Any]] = []
        for port in host.findall('ports/port'):
            service = port.find('service')
            label = f"{port.get('portid')}/{port.get('protocol')}"
            name = service.get('name', '') if service is not None else ''
            for script in port.findall('script'):
                findings += self._script_findings(script, label, name)
        for script in host.findall('hostscript/script'):
            findings += self._script_findings(script, 'host', '')
        order = {_FAILED: 0, _WARNING: 1, _NOTED: 2}
        findings.sort(key=lambda f: (order[f['grade']], f['port'], f['script'], f['id']))
        return findings

    def _script_findings(self, script: ET.Element, port: str, service: str) -> List[Dict[str, Any]]:
        """The findings one script's result holds: vulns-library tables, a
        vulners advisory list, or a plain-text verdict."""
        script_id = script.get('id', '')
        base = {'port': port, 'service': service, 'script': script_id}
        if script_id == 'vulners':
            return self._vulners_findings(script, base)

        findings = []
        for table in script.findall('table'):
            state = _elem_text(table, 'state')
            if state is None:
                continue
            grade = _grade_state(state, self.likely_status)
            if grade is None:
                continue
            ids = _table_items(table, 'ids')
            cvss = _cvss_of(table)
            findings.append({
                **base,
                'id': (ids[0].split(':', 1)[-1] if ids else table.get('key', '')),
                'title': _elem_text(table, 'title') or table.get('key', script_id),
                'state': state,
                'cvss': cvss,
                'grade': grade,
            })
        if findings:
            return findings

        text = (script.get('output') or '').strip()
        if not text or _looks_negative(text):
            return []
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), script_id)
        if _looks_vulnerable(text):
            return [{**base, 'id': script_id, 'title': first_line[:120], 'state': 'VULNERABLE',
                     'cvss': None, 'grade': _FAILED}]
        if script.find('table') is not None or script.find('elem') is not None:
            return [{**base, 'id': script_id, 'title': first_line[:120], 'state': 'reported',
                     'cvss': None, 'grade': _NOTED}]
        return []

    def _vulners_findings(self, script: ET.Element, base: Dict[str, str]) -> List[Dict[str, Any]]:
        """One finding per advisory vulners matched to a detected version."""
        findings = []
        for cpe_table in script.findall('table'):
            for entry in cpe_table.findall('table'):
                advisory = _elem_text(entry, 'id')
                if not advisory:
                    continue
                cvss_text = _elem_text(entry, 'cvss')
                try:
                    cvss = float(cvss_text) if cvss_text else None
                except ValueError:
                    cvss = None
                exploit = (_elem_text(entry, 'is_exploit') or '').lower() == 'true'
                findings.append({
                    **base,
                    'id': advisory,
                    'title': f"{cpe_table.get('key', '')} matches {advisory}",
                    'state': (f"CVSS {cvss:.1f}" if cvss is not None else 'unscored')
                             + (' · exploit' if exploit else ''),
                    'cvss': cvss,
                    'grade': _grade_cvss(cvss, self.warning_cvss, self.threshold_cvss),
                })
        return findings

    # --- UI ---

    def _metric(self, name: str) -> Optional[float]:
        metric = self.data.latest_metric(name)
        return metric.value if metric is not None else None

    def _last_scan_pair(self) -> Tuple[str, Optional[str]]:
        epoch = self._metric('last_scan_epoch')
        if not epoch:
            return 'NEVER', 'offline'
        age = int(time.time()) - int(epoch)
        # A second interval passing without a fresh scan means the sweeps have stopped landing.
        return format_age(age), 'warning' if age > 2 * self.interval else 'online'

    @property
    def _last_scan_text(self) -> str:
        return self._last_scan_pair()[0]

    @property
    def _last_scan_color(self) -> Optional[str]:
        return self._last_scan_pair()[1]

    @property
    def _duration_text(self) -> str:
        seconds = self._metric('scan_seconds')
        return '--' if seconds is None else format_duration(int(seconds))

    @property
    def _finding_rows(self) -> List[Dict[str, str]]:
        snapshot = self.data.latest_snapshot(default={}) or {}
        rows = []
        for index, finding in enumerate(snapshot.get('findings') or []):
            cvss = finding.get('cvss')
            rows.append({
                'key': f"{index}-{finding.get('id', '')}",
                'severity': _GRADE_LABEL.get(finding.get('grade'), 'NOTE'),
                'grade': finding.get('grade', _NOTED),
                'port': finding.get('port', ''),
                'service': finding.get('service', ''),
                'script': finding.get('script', ''),
                'id': finding.get('id', ''),
                'title': finding.get('title', ''),
                'cvss': f"{cvss:.1f}" if isinstance(cvss, (int, float)) else '--',
                'state': finding.get('state', ''),
            })
        return rows

    @property
    def _port_rows(self) -> List[Dict[str, str]]:
        snapshot = self.data.latest_snapshot(default={}) or {}
        return [{
            'port': row.get('port', ''),
            'service': row.get('service', ''),
            'product': row.get('product', ''),
            'version': row.get('version', ''),
        } for row in snapshot.get('open_ports') or []]

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'vulnerable_card': {'metric': 'vulnerable', 'title': 'VULNERABLE',
                                    'format': 'int', 'color': 'nonzero_failed'},
                'suspected_card': {'metric': 'suspected', 'title': 'LIKELY VULNERABLE',
                                   'format': 'int', 'color': 'nonzero_warning'},
                'noted_card': {'metric': 'noted', 'title': 'NOTED', 'format': 'int'},
                'last_scan_card': {'title': 'LAST SCAN', 'value_attr': '_last_scan_text',
                                   'color_attr': '_last_scan_color', 'refresh': True},
                'duration_card': {'title': 'SCAN TIME', 'value_attr': '_duration_text',
                                  'refresh': True},
                'open_ports_card': {'metric': 'open_ports', 'title': 'OPEN PORTS',
                                    'format': 'int'},
            },
            'tables': {
                'findings': {
                    'row_key': 'key',
                    'rows_attr': '_finding_rows',
                    'columns': [
                        {'name': 'severity', 'label': 'Severity', 'field': 'severity',
                         'align': 'left', 'cell_color_by': self._grade_color},
                        {'name': 'port', 'label': 'Port', 'field': 'port', 'align': 'left'},
                        {'name': 'service', 'label': 'Service', 'field': 'service', 'align': 'left'},
                        {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'left'},
                        {'name': 'title', 'label': 'Finding', 'field': 'title', 'align': 'left'},
                        {'name': 'cvss', 'label': 'CVSS', 'field': 'cvss', 'align': 'right'},
                        {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                        {'name': 'script', 'label': 'Script', 'field': 'script', 'align': 'left'},
                    ],
                },
                'ports': {
                    'row_key': 'port',
                    'rows_attr': '_port_rows',
                    'columns': [
                        {'name': 'port', 'label': 'Port', 'field': 'port', 'align': 'left'},
                        {'name': 'service', 'label': 'Service', 'field': 'service', 'align': 'left'},
                        {'name': 'product', 'label': 'Product', 'field': 'product', 'align': 'left'},
                        {'name': 'version', 'label': 'Version', 'field': 'version', 'align': 'left'},
                    ],
                },
            },
            'chart': {'metric': 'vulnerable', 'title': f'VULNERABILITIES — {self.scan_host}'},
            'events': {'title': 'EVENTS', 'limit': 100, 'full_height': True},
        }
