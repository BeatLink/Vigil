import json
import time

import pytest

from vigil.plugins.nix_gc import NixGc
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name":       "test-nix-gc",
    "id":         "test-nix-gc",
    "interval":   3600,
    "ssh_config": {"host": "test.host"},
}

GB = 1024 ** 3


def _latest_status(plugin_id: str = "test-nix-gc"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, plugin_id: str = "test-nix-gc"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == plugin_id) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


# The two lines systemd logs when a run ends, as journalctl -o short-unix renders them.
_OUTCOMES = {
    "finished": "systemd[1]: Finished Nix Garbage Collector.",
    "failed":   "systemd[1]: nix-gc.service: Failed with result 'exit-code'.",
}


def _probe(size=200 * GB, used=80 * GB, paths=111121, roots=118,
           svc_state="inactive", svc_result="success", svc_exit="",
           timer_load="loaded", timer_state="active", timer_next=None,
           last_gc=None, freed="44.4 GiB", deleted=21482, outcome="finished",
           summary_at=None, error=None, error_at=None,
           profiles=(("/nix/var/nix/profiles/system", 236, 59, None),)) -> CmdResult:
    """The stdout the probe script produces on a NixOS target."""
    now = int(time.time())
    last_gc = now - 86400 if last_gc is None else last_gc
    timer_next = now + 3 * 86400 if timer_next is None else timer_next
    summary_at = last_gc if summary_at is None else summary_at
    error_at = last_gc if error_at is None else error_at
    summary = (f"{summary_at}.204758 host nix-gc-start[2166397]: "
               f"{deleted} store paths deleted, {freed} freed") if freed else ""
    lines = [
        "hostname=host",
        f"df={size} {used} {size - used} /nix/store",
        f"paths={paths}",
        f"roots={roots}",
        f"svc_state={svc_state}",
        f"svc_result={svc_result}",
        f"svc_exit={svc_exit}",
        f"timer_load={timer_load}",
        f"timer_state={timer_state}",
        f"timer_next=@{timer_next}" if timer_next else "timer_next=",
        f"summary={summary}",
        f"outcome={last_gc}.225681 host {_OUTCOMES[outcome]}" if outcome else "outcome=",
        f"error={error_at}.100000 host nix-gc-start[2166397]: {error}" if error else "error=",
    ]
    for path, generation, count, oldest in profiles:
        oldest = now - 30 * 86400 if oldest is None else oldest
        lines.append(f"profile={path}|{generation}|{count}|{oldest}")
    return CmdResult(0, "\n".join(lines) + "\n", "")


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(NixGc, BASE_CFG)


def _collect(plugin, probe=None):
    """Drive one cycle over a probe result and persist what it produced."""
    plugin.commands()
    result = plugin.parse([probe if probe is not None else _probe()])
    plugin.storage.apply(result)
    return result


def _state(plugin):
    return json.loads(plugin.data.latest_metric("last_gc_epoch").metadata)


class TestFreshness:
    async def test_recent_collection_is_online(self, plugin):
        _collect(plugin)
        assert _latest_status() == "online"
        assert _latest_metric("last_gc_success") == 1.0

    async def test_collection_older_than_max_age_fails(self, plugin):
        _collect(plugin, _probe(last_gc=int(time.time()) - 21 * 86400))
        assert _latest_status() == "failed"

    async def test_stale_status_is_configurable(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "stale_status": "warning"})
        _collect(p, _probe(last_gc=int(time.time()) - 21 * 86400))
        assert _latest_status() == "warning"

    async def test_unrecognised_stale_status_falls_back_to_failed(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "stale_status": "catastrophe"})
        assert p.stale_status == "failed"

    async def test_max_age_is_configurable(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "max_age": "1d"})
        _collect(p, _probe(last_gc=int(time.time()) - 2 * 86400))
        assert _latest_status() == "failed"

    async def test_a_running_collection_is_never_stale(self, plugin):
        _collect(plugin, _probe(last_gc=int(time.time()) - 21 * 86400, svc_state="active"))
        assert _latest_status() == "online"

    async def test_failed_last_run_is_failed(self, plugin):
        _collect(plugin, _probe(outcome="failed"))
        assert _latest_status() == "failed"
        assert _latest_metric("last_gc_success") == 0.0

    async def test_a_journal_failure_beats_a_reset_unit_result(self, plugin):
        # systemd resets Result to success on a daemon re-exec, which is what a NixOS switch
        # does, so a run the journal says failed must still read as failed.
        result = _collect(plugin, _probe(outcome="failed", svc_result="success"))
        assert _latest_status() == "failed"
        assert not any("result success" in message for message, _ in result.logs)

    async def test_a_stale_unit_result_does_not_fail_a_finished_run(self, plugin):
        _collect(plugin, _probe(outcome="finished", svc_result="exit-code"))
        assert _latest_status() == "online"

    async def test_a_failure_is_reported_with_the_journal_reason(self, plugin):
        result = _collect(plugin, _probe(outcome="failed", error="error: interrupted by the user"))
        message = next(m for m, level in result.logs if level == "ERROR")
        assert message == "The last collection failed: error: interrupted by the user"

    async def test_an_error_from_an_earlier_run_is_not_this_run_reason(self, plugin):
        result = _collect(plugin, _probe(outcome="failed", error="error: out of disk",
                                         error_at=int(time.time()) - 30 * 86400))
        assert next(m for m, level in result.logs if level == "ERROR") == \
            "The last collection failed"

    async def test_a_succeeded_run_reports_no_reason(self, plugin):
        result = _collect(plugin, _probe(error="error: interrupted by the user"))
        assert _latest_status() == "online"
        assert not any(level == "ERROR" for _, level in result.logs)

    async def test_the_unit_result_still_decides_when_the_journal_has_no_outcome(self, plugin):
        _collect(plugin, _probe(outcome=None, svc_result="exit-code"))
        assert _latest_status() == "failed"

    async def test_no_record_of_any_run_is_offline(self, plugin):
        _collect(plugin, _probe(freed=None, outcome=None))
        assert _latest_status() == "offline"
        assert _latest_metric("last_gc_epoch") == 0.0


class TestYield:
    async def test_summary_line_records_what_was_freed(self, plugin):
        _collect(plugin, _probe(freed="44.4 GiB", deleted=21482))
        assert _latest_metric("last_gc_freed_gb") == pytest.approx(44.4)
        assert _latest_metric("last_gc_deleted") == 21482.0

    @pytest.mark.parametrize("rendered,gb", [
        ("512.0 MiB", 0.5), ("2.0 TiB", 2048.0), ("100 bytes", 100 / GB),
    ])
    async def test_every_rendered_unit_is_understood(self, plugin, rendered, gb):
        _collect(plugin, _probe(freed=rendered))
        assert _latest_metric("last_gc_freed_gb") == pytest.approx(gb)

    async def test_an_unparseable_unit_records_no_yield(self, plugin):
        _collect(plugin, _probe(freed="44.4 parsecs"))
        assert _latest_metric("last_gc_freed_gb") is None
        assert _latest_status() == "online"

    async def test_a_rotated_journal_keeps_the_last_known_run(self, plugin):
        stamp = int(time.time()) - 86400
        _collect(plugin, _probe(last_gc=stamp))
        _collect(plugin, _probe(freed=None, outcome=None))
        assert _latest_metric("last_gc_epoch") == float(stamp)
        assert _latest_metric("last_gc_freed_gb") == pytest.approx(44.4)
        assert _latest_status() == "online"

    async def test_a_failed_run_keeps_the_yield_it_managed(self, plugin):
        # nix prints its summary a moment before systemd logs the failure; both are one run.
        stamp = int(time.time()) - 3600
        _collect(plugin, _probe(outcome="failed", last_gc=stamp, summary_at=stamp - 1,
                                freed="2.8 GiB", deleted=2048))
        assert _latest_status() == "failed"
        assert _latest_metric("last_gc_freed_gb") == pytest.approx(2.8)
        assert _latest_metric("last_gc_deleted") == 2048.0

    async def test_a_summary_from_an_older_run_is_not_this_run_yield(self, plugin):
        _collect(plugin, _probe(summary_at=int(time.time()) - 30 * 86400))
        assert _latest_metric("last_gc_freed_gb") is None

    async def test_a_newer_run_without_a_summary_clears_the_old_yield(self, plugin):
        _collect(plugin, _probe(last_gc=int(time.time()) - 86400))
        _collect(plugin, _probe(last_gc=int(time.time()) - 60, freed=None))
        assert _state(plugin)["freed_bytes"] is None


class TestSchedule:
    async def test_an_inactive_timer_warns(self, plugin):
        _collect(plugin, _probe(timer_state="inactive"))
        assert _latest_status() == "warning"
        assert _latest_metric("timer_active") == 0.0

    async def test_a_missing_timer_says_so(self, plugin):
        result = _collect(plugin, _probe(timer_load="not-found", timer_state="inactive"))
        assert any("not installed" in message for message, _ in result.logs)

    async def test_timer_status_is_configurable(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "timer_status": "failed"})
        _collect(p, _probe(timer_state="inactive"))
        assert _latest_status() == "failed"

    async def test_next_elapse_is_recorded(self, plugin):
        due = int(time.time()) + 3 * 86400
        _collect(plugin, _probe(timer_next=due))
        assert _latest_metric("next_gc_epoch") == float(due)


class TestStore:
    async def test_usage_is_measured_from_df(self, plugin):
        _collect(plugin, _probe(size=200 * GB, used=100 * GB))
        assert _latest_metric("store_used_pct") == pytest.approx(50.0)
        assert _latest_metric("store_size_gb") == pytest.approx(200.0)
        assert _latest_metric("store_avail_gb") == pytest.approx(100.0)
        assert _latest_metric("store_paths") == 111121.0
        assert _latest_metric("gc_roots") == 118.0

    async def test_usage_over_the_warning_warns(self, plugin):
        _collect(plugin, _probe(size=100 * GB, used=85 * GB))
        assert _latest_status() == "warning"

    async def test_usage_over_the_threshold_fails(self, plugin):
        _collect(plugin, _probe(size=100 * GB, used=95 * GB))
        assert _latest_status() == "failed"

    async def test_unreadable_store_is_offline(self, plugin):
        _collect(plugin, CmdResult(0, "hostname=host\ndf=\n", ""))
        assert _latest_status() == "offline"

    async def test_unreachable_target_is_offline(self, plugin):
        _collect(plugin, CmdResult(255, "", "ssh: connect failed"))
        assert _latest_status() == "offline"


class TestProfiles:
    PROFILES = (
        ("/nix/var/nix/profiles/system", 236, 59, None),
        ("/nix/var/nix/profiles/per-user/root/profile", 29, 11, None),
    )

    async def test_generations_are_counted_across_profiles(self, plugin):
        _collect(plugin, _probe(profiles=self.PROFILES))
        assert _latest_metric("profiles") == 2.0
        assert _latest_metric("generations") == 70.0

    async def test_oldest_generation_is_the_oldest_of_any_profile(self, plugin):
        old = int(time.time()) - 90 * 86400
        _collect(plugin, _probe(profiles=(
            ("/nix/var/nix/profiles/system", 236, 59, old),
            ("/nix/var/nix/profiles/per-user/root/profile", 29, 11, old + 86400),
        )))
        assert _latest_metric("oldest_generation_epoch") == float(old)

    async def test_a_generation_older_than_the_limit_warns(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "max_generation_age": "7d"})
        _collect(p, _probe(profiles=(
            ("/nix/var/nix/profiles/system", 236, 59, int(time.time()) - 30 * 86400),
        )))
        assert _latest_status() == "warning"

    async def test_too_many_generations_warns(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "max_generations": 20})
        result = _collect(p, _probe(profiles=self.PROFILES))
        assert _latest_status() == "warning"
        assert any("59" in message for message, _ in result.logs)

    async def test_profile_rows_render(self, plugin):
        _collect(plugin, _probe(profiles=self.PROFILES))
        rows = plugin._profile_rows
        assert [row["path"] for row in rows] == [path for path, *_ in self.PROFILES]
        assert rows[0]["generation"] == "236" and rows[0]["count"] == "59"


async def _launch(plugin, pid=4242):
    """Drive plan_action -> ActionPlan (launch) -> interpret_action, mirroring
    VigilEngine.dispatch_action for a launched detached job."""
    plan = plugin.plan_action("collect")
    if plan is None:
        return None
    if hasattr(plan, "success"):        # CollectResult (refused) — no launch
        plugin.storage.apply(plan)
        return plan
    outcome = plugin.interpret_action("collect", CmdResult(0, f"{pid}\n", ""))
    plugin.storage.apply(outcome)
    return outcome


def _poll(size, exit_code, alive, out):
    """Build the raw stdout one poll_command produces on the target."""
    return CmdResult(0, (
        f"===VIGIL_SIZE===\n{size}\n"
        f"===VIGIL_EXIT===\n{exit_code if exit_code is not None else ''}\n"
        f"===VIGIL_ALIVE===\n{1 if alive else 0}\n"
        f"===VIGIL_OUT===\n{out}"
    ), "")


def _poll_once(plugin, poll_result):
    commands = plugin.commands()
    assert len(commands) == 1            # a running job polls with one command
    result = plugin.parse([poll_result])
    plugin.storage.apply(result)
    return result


class TestAction:
    async def test_the_collect_action_is_exposed(self, plugin):
        assert [a["action_id"] for a in plugin.get_actions()] == ["collect"]

    async def test_the_collect_command_carries_the_configured_arguments(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "gc_args": ["--delete-older-than", "14d"]})
        assert p._collect_command() == "sudo -n nix-collect-garbage --delete-older-than 14d"

    async def test_sudo_can_be_turned_off(self, make_plugin):
        p = make_plugin(NixGc, {**BASE_CFG, "require_sudo": False})
        assert p._collect_command().startswith("nix-collect-garbage ")

    async def test_launching_records_a_running_job(self, plugin):
        outcome = await _launch(plugin, pid=1234)
        assert outcome.success is True
        job = plugin.jobs.running()
        assert job["kind"] == "collection" and job["pid"] == 1234
        assert "nix-collect-garbage" in job["command"]

    async def test_a_failed_launch_records_no_job(self, plugin):
        plugin.plan_action("collect")
        outcome = plugin.interpret_action("collect", CmdResult(1, "", "sudo: a password is required"))
        plugin.storage.apply(outcome)
        assert outcome.success is False
        assert plugin.jobs.running() is None

    async def test_a_second_collection_is_refused_while_one_runs(self, plugin):
        await _launch(plugin)
        refused = plugin.plan_action("collect")
        assert any("already running" in message for message, _ in refused.logs)

    async def test_an_unknown_action_is_not_handled(self, plugin):
        assert plugin.plan_action("nope") is None


class TestJobPolling:
    async def test_a_running_job_is_polled_instead_of_probed(self, plugin):
        await _launch(plugin)
        assert "4242" in plugin.commands()[0].text

    async def test_poll_while_running_keeps_the_job_running(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(10, None, True, "deleting unused links...\n"))
        assert plugin.jobs.running()["progress"] == "deleting unused links..."

    async def test_completion_marks_succeeded(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(20, 0, False, "21482 store paths deleted, 44.4 GiB freed\n"))
        assert plugin.jobs.running() is None
        assert plugin.jobs.recent()[0]["state"] == "succeeded"

    async def test_nonzero_exit_is_failure(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(5, 1, False, ""))
        assert plugin.jobs.recent()[0]["state"] == "failed"

    async def test_vanished_process_is_failure(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(5, None, False, ""))
        assert plugin.jobs.recent()[0]["state"] == "failed"

    async def test_unanswered_poll_leaves_the_job_running(self, plugin):
        await _launch(plugin)
        result = _poll_once(plugin, CmdResult(-1, "", "Agent 'x' is not connected"))
        assert plugin.jobs.running() is not None
        assert any("Could not poll" in message for message, _ in result.logs)

    async def test_a_job_started_mid_cycle_does_not_swallow_the_probe(self, plugin):
        assert len(plugin.commands()) == 1                  # asked for a probe: no job yet
        await _launch(plugin)                               # the button lands before parse
        result = plugin.parse([_probe()])
        plugin.storage.apply(result)
        assert plugin.jobs.running() is not None            # the job was not judged by probe output
        assert result.metrics["store_paths"] == 111121.0    # and the probe was still read as a probe


class TestUiSpec:
    def test_every_laid_out_widget_has_a_definition(self, plugin):
        spec = plugin.UI_SPEC
        defined = set(spec['cards']) | set(spec['tables']) | {'host_card', 'chart', 'events'}
        defined.add(spec['job_panel']['widget'])
        laid_out = {cell for row in spec['layout'] for cell in row}
        assert laid_out - defined == set()

    def test_every_defined_widget_is_laid_out(self, plugin):
        spec = plugin.UI_SPEC
        laid_out = {cell for row in spec['layout'] for cell in row}
        assert set(spec['cards']) | set(spec['tables']) <= laid_out

    def test_every_bound_metric_is_one_the_plugin_records(self, plugin):
        _collect(plugin)
        recorded = set(_collect(plugin).metrics)
        bound = {card['metric'] for card in plugin.UI_SPEC['cards'].values() if 'metric' in card}
        bound.add(plugin.UI_SPEC['chart']['metric'])
        assert bound <= recorded

    def test_the_last_yield_card_reads_the_stored_record(self, plugin):
        _collect(plugin, _probe(freed="512.0 MiB", deleted=99))
        assert plugin._freed_text == "512 MB · 99 paths"

    def test_an_unscheduled_timer_shows_on_the_next_collection_card(self, plugin):
        _collect(plugin, _probe(timer_state="inactive"))
        assert plugin._next_gc_text == "NOT SCHEDULED"
        assert plugin._next_gc_color == "warning"

    def test_a_stale_collection_colors_its_card(self, plugin):
        _collect(plugin, _probe(last_gc=int(time.time()) - 21 * 86400))
        assert plugin._last_gc_color == "failed"
