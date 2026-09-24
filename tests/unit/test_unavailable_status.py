from dataclasses import dataclass

from vigil.core.connectors.types import (
    CmdResult, CollectResult, DnsResult, HttpResult, PingResult, Status, unreachable,
)
from vigil.core.coordination.engine import _settle


@dataclass
class _Monitor:
    AVAILABILITY: bool = False


class TestStatusLadder:
    def test_unavailable_ranks_between_online_and_warning(self):
        assert Status.worst(['online', 'unavailable']) == Status.UNAVAILABLE
        assert Status.worst(['unavailable', 'warning']) == Status.WARNING

    def test_the_old_offline_name_reads_as_unavailable(self):
        assert Status('offline') is Status.UNAVAILABLE
        assert Status.worst(['online', 'offline']) == Status.UNAVAILABLE

    def test_an_unknown_value_ranks_as_unavailable(self):
        assert Status.worst(['online', 'bogus']) == Status.UNAVAILABLE

    def test_the_unavailable_result_carries_the_status(self):
        result = CollectResult.unavailable("no answer")
        assert result.status == 'unavailable' and result.logs == [("no answer", 'WARNING')]


class TestUnreachable:
    def test_each_transport_failure_shape(self):
        assert unreachable(CmdResult(-1, "", "Agent 'x' is not connected"))
        assert unreachable(HttpResult(status_code=None, text="", error="refused"))
        assert unreachable(DnsResult(kind='timeout'))
        assert unreachable(PingResult(exception="boom", returncode=None))

    def test_a_measured_answer_is_not_unreachable(self):
        assert not unreachable(CmdResult(1, "", "command failed"))
        assert not unreachable(HttpResult(status_code=503, text=""))
        assert not unreachable(DnsResult(kind='nxdomain'))
        assert not unreachable(PingResult(exception=None, returncode=1))
        assert not unreachable("an io_call value")


class TestSettle:
    def test_a_cycle_that_never_reached_the_host_is_unavailable(self):
        result = _settle(_Monitor(), [CmdResult(-1, "", "not connected")], CollectResult.failed("x"))
        assert result.status == 'unavailable' and result.logs == [("x", 'ERROR')]

    def test_a_partly_reached_cycle_keeps_the_plugin_verdict(self):
        results = [CmdResult(-1, "", ""), CmdResult(0, "ok", "")]
        assert _settle(_Monitor(), results, CollectResult.failed("x")).status == 'failed'

    def test_an_availability_monitor_keeps_failed(self):
        result = _settle(_Monitor(AVAILABILITY=True), [PingResult("boom", None)], CollectResult.failed("x"))
        assert result.status == 'failed'

    def test_no_requests_means_nothing_to_settle(self):
        assert _settle(_Monitor(), [], CollectResult.failed("x")).status == 'failed'
