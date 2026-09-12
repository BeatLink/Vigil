import time

import pytest

from vigil.plugins.vuln_scan import VulnScan
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name":       "test-vuln-scan",
    "id":         "test-vuln-scan",
    "interval":   86400,
    "scan_host":  "web-01.example.com",
    "ssh_config": {"host": "scanner.host"},
}


def _latest_status(plugin_id: str = "test-vuln-scan"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, plugin_id: str = "test-vuln-scan"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == plugin_id) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


# The vulns-library table nmap writes for a definite finding, as ssl-poodle renders it.
POODLE = """<script id="ssl-poodle" output="&#xa;  VULNERABLE:&#xa;  SSL POODLE information leak"><table key="CVE-2014-3566">
<elem key="title">SSL POODLE information leak</elem>
<elem key="state">VULNERABLE</elem>
<table key="ids"><elem>CVE:CVE-2014-3566</elem><elem>BID:70574</elem></table>
<table key="scores"><elem key="CVSS">4.3</elem></table>
<table key="description"><elem>The SSL protocol 3.0 uses nondeterministic CBC padding.</elem></table>
<table key="refs"><elem>https://www.openssl.org/~bodo/ssl-poodle.pdf</elem></table>
</table></script>"""

LIKELY = """<script id="http-slowloris-check" output="&#xa;  LIKELY VULNERABLE"><table key="CVE-2007-6750">
<elem key="title">Slowloris DOS attack</elem>
<elem key="state">LIKELY VULNERABLE</elem>
<table key="ids"><elem>CVE:CVE-2007-6750</elem></table>
</table></script>"""

NEGATIVE = """<script id="http-csrf" output="Couldn&apos;t find any CSRF vulnerabilities."></script>"""

PLAIN_POSITIVE = """<script id="ftp-vsftpd-backdoor" output="&#xa;  VULNERABLE:&#xa;  vsFTPd version 2.3.4 backdoor"></script>"""

VULNERS = """<script id="vulners" output="&#xa;  cpe:/a:openbsd:openssh:7.4: &#xa;    CVE-2018-15473 5.0"><table key="cpe:/a:openbsd:openssh:7.4">
<table><elem key="id">CVE-2018-15473</elem><elem key="type">cve</elem><elem key="is_exploit">false</elem><elem key="cvss">5.0</elem></table>
<table><elem key="id">CVE-2016-6515</elem><elem key="type">cve</elem><elem key="is_exploit">false</elem><elem key="cvss">7.8</elem></table>
<table><elem key="id">EDB-ID:45233</elem><elem key="type">exploitdb</elem><elem key="is_exploit">true</elem><elem key="cvss">0.0</elem></table>
</table></script>"""

HOSTSCRIPT = """<hostscript><script id="smb-vuln-ms17-010" output="VULNERABLE"><table key="CVE-2017-0143">
<elem key="title">Remote Code Execution vulnerability in Microsoft SMBv1 servers (ms17-010)</elem>
<elem key="state">VULNERABLE</elem>
<table key="ids"><elem>CVE:CVE-2017-0143</elem></table>
</table></script></hostscript>"""


def _port(portid, name, scripts="", product=None, version=None, tunnel=None, state="open"):
    service = f'<service name="{name}"'
    if product:
        service += f' product="{product}"'
    if version:
        service += f' version="{version}"'
    if tunnel:
        service += f' tunnel="{tunnel}"'
    service += ' method="probed" conf="10"/>'
    return (f'<port protocol="tcp" portid="{portid}"><state state="{state}" reason="syn-ack" '
            f'reason_ttl="0"/>{service}{scripts}</port>')


def _scan(ports="", hostscript="", up=True, elapsed=42.5, ended=None) -> CmdResult:
    """The XML nmap writes to stdout with -oX - for a one-host scan."""
    ended = ended if ended is not None else int(time.time())
    if up:
        host = (f'<host starttime="{ended - 40}" endtime="{ended}"><status state="up" reason="syn-ack" '
                f'reason_ttl="0"/><address addr="192.0.2.10" addrtype="ipv4"/><hostnames>'
                f'</hostnames><ports>{ports}</ports>{hostscript}</host>')
        hosts = '<hosts up="1" down="0" total="1"/>'
    else:
        host = ('<host><status state="down" reason="no-response" reason_ttl="0"/>'
                '<address addr="192.0.2.10" addrtype="ipv4"/></host>')
        hosts = '<hosts up="0" down="1" total="1"/>'
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><nmaprun scanner="nmap" version="7.99" '
           f'start="{ended - 45}">{host}<runstats><finished time="{ended}" elapsed="{elapsed}" '
           f'exit="success"/>{hosts}</runstats></nmaprun>')
    return CmdResult(0, xml, "")


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(VulnScan, BASE_CFG)


def _collect(plugin, result):
    plugin.commands()
    parsed = plugin.parse([result])
    plugin.storage.apply(parsed)
    return parsed


class TestCommand:
    async def test_scan_runs_nmap_against_scan_host_under_a_deadline(self, plugin):
        [cmd] = plugin.commands()
        assert cmd.text.startswith("timeout -k 5 1200 nmap -n -oX - -PS22,80,443 -sV --script vuln ")
        assert cmd.text.endswith(" web-01.example.com")
        assert cmd.timeout == 1200 + 5 + 10

    async def test_options_reach_the_command_line(self, make_plugin):
        p = make_plugin(VulnScan, {**BASE_CFG, "ports": "1-1024", "scripts": "vuln and not http-slowloris-check",
                                   "script_args": "vulns.showall=1", "nmap_args": ["-T4"],
                                   "discovery_ports": [], "service_detection": False,
                                   "require_sudo": True, "timeout": "5m"})
        [cmd] = p.commands()
        assert cmd.text.startswith("sudo -n timeout -k 5 300 nmap -n -oX - -Pn --script ")
        assert "'vuln and not http-slowloris-check'" in cmd.text
        assert "--script-args vulns.showall=1" in cmd.text
        assert "-p 1-1024" in cmd.text
        assert "-T4 web-01.example.com" in cmd.text
        assert "-sV" not in cmd.text

    async def test_monitor_is_labelled_with_the_scanned_host(self, plugin):
        assert plugin.display_target == "web-01.example.com"
        assert plugin.target == "web-01.example.com"
        assert plugin.scanner == "scanner.host"

    async def test_scanner_names_the_agent_when_one_is_set(self, make_plugin):
        p = make_plugin(VulnScan, {**BASE_CFG, "agent": "monitor"})
        assert p.scanner == "monitor"

    async def test_scan_host_falls_back_to_target_host(self, make_plugin):
        cfg = {k: v for k, v in BASE_CFG.items() if k not in ("scan_host", "ssh_config")}
        p = make_plugin(VulnScan, {**cfg, "target_host": "db-01.example.com"})
        assert p.scan_host == "db-01.example.com"


class TestGrading:
    async def test_clean_host_is_online(self, plugin):
        _collect(plugin, _scan(_port(22, "ssh", product="OpenSSH", version="9.9") + _port(443, "http", NEGATIVE)))
        assert _latest_status() == "online"
        assert _latest_metric("vulnerable") == 0
        assert _latest_metric("open_ports") == 2
        assert _latest_metric("host_up") == 1
        assert _latest_metric("scan_seconds") == 42.5

    async def test_a_definite_finding_fails(self, plugin):
        _collect(plugin, _scan(_port(443, "https", POODLE)))
        assert _latest_status() == "failed"
        assert _latest_metric("vulnerable") == 1
        rows = plugin._finding_rows
        assert rows[0]["id"] == "CVE-2014-3566"
        assert rows[0]["title"] == "SSL POODLE information leak"
        assert rows[0]["cvss"] == "4.3"
        assert rows[0]["severity"] == "VULNERABLE"
        assert rows[0]["port"] == "443/tcp"

    async def test_a_likely_finding_warns(self, plugin):
        _collect(plugin, _scan(_port(80, "http", LIKELY)))
        assert _latest_status() == "warning"
        assert _latest_metric("suspected") == 1
        assert _latest_metric("vulnerable") == 0

    async def test_likely_status_is_configurable(self, make_plugin):
        p = make_plugin(VulnScan, {**BASE_CFG, "likely_status": "online"})
        _collect(p, _scan(_port(80, "http", LIKELY)))
        assert _latest_status() == "online"

    async def test_a_host_script_finding_counts(self, plugin):
        _collect(plugin, _scan(_port(445, "microsoft-ds"), hostscript=HOSTSCRIPT))
        assert _latest_status() == "failed"
        assert plugin._finding_rows[0]["port"] == "host"

    async def test_plain_text_verdict_is_a_finding(self, plugin):
        _collect(plugin, _scan(_port(21, "ftp", PLAIN_POSITIVE)))
        assert _latest_status() == "failed"
        assert plugin._finding_rows[0]["title"] == "VULNERABLE:"

    async def test_nothing_found_text_is_ignored(self, plugin):
        _collect(plugin, _scan(_port(80, "http", NEGATIVE)))
        assert _latest_status() == "online"
        assert plugin._finding_rows == []

    async def test_vulners_advisories_grade_by_cvss(self, plugin):
        _collect(plugin, _scan(_port(22, "ssh", VULNERS, product="OpenSSH", version="7.4")))
        assert _latest_status() == "failed"
        assert _latest_metric("vulnerable") == 1
        assert _latest_metric("suspected") == 1
        assert _latest_metric("noted") == 1
        rows = plugin._finding_rows
        assert [r["id"] for r in rows] == ["CVE-2016-6515", "CVE-2018-15473", "EDB-ID:45233"]
        assert rows[2]["state"] == "CVSS 0.0 · exploit"

    async def test_cvss_thresholds_are_configurable(self, make_plugin):
        p = make_plugin(VulnScan, {**BASE_CFG, "warning_cvss": 9.0, "threshold_cvss": 9.5})
        _collect(p, _scan(_port(22, "ssh", VULNERS)))
        assert _latest_status() == "online"
        assert _latest_metric("noted") == 3

    async def test_findings_table_is_capped_but_counts_are_not(self, make_plugin):
        p = make_plugin(VulnScan, {**BASE_CFG, "max_findings": 1})
        _collect(p, _scan(_port(22, "ssh", VULNERS)))
        assert _latest_metric("noted") == 1
        assert _latest_metric("vulnerable") == 1
        assert len(p._finding_rows) == 1


class TestPorts:
    async def test_open_ports_are_listed_with_their_service(self, plugin):
        _collect(plugin, _scan(_port(22, "ssh", product="OpenSSH", version="10.5")
                               + _port(22000, "snapenetio", tunnel="ssl")
                               + _port(80, "http", state="closed")))
        rows = plugin._port_rows
        assert [r["port"] for r in rows] == ["22/tcp", "22000/tcp"]
        assert rows[0]["product"] == "OpenSSH" and rows[0]["version"] == "10.5"
        assert rows[1]["service"] == "snapenetio/ssl"


class TestFailures:
    async def test_host_down_is_offline(self, plugin):
        _collect(plugin, _scan(up=False))
        assert _latest_status() == "offline"
        assert _latest_metric("host_up") == 0

    async def test_nmap_missing_is_offline(self, plugin):
        _collect(plugin, CmdResult(127, "", "sh: nmap: command not found"))
        assert _latest_status() == "offline"

    async def test_truncated_output_is_offline(self, plugin):
        _collect(plugin, CmdResult(124, '<?xml version="1.0"?><nmaprun><host>', ""))
        assert _latest_status() == "offline"

    async def test_last_scan_card_reads_the_scan_time(self, plugin):
        assert plugin._last_scan_text == "NEVER"
        _collect(plugin, _scan(_port(22, "ssh"), ended=int(time.time()) - 3600))
        assert plugin._last_scan_text.endswith(" ago")
        assert plugin._last_scan_color == "online"
