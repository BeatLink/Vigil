import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from vigil.plugins.nixos_upgrade import NixosUpgrade
from vigil.core.connectors.types import CmdResult, CollectResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name":       "test-nixos",
    "id":         "test-nixos",
    "interval":   300,
    "flake":      "/etc/nixos",
    "ssh_config": {"host": "test.host"},
}

CURRENT = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nixos-system-host-26.11"
TARGET = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-nixos-system-host-26.11"
KERNEL = "/nix/store/cccccccccccccccccccccccccccccccc-linux-6.18.46/bzImage"
NEW_KERNEL = "/nix/store/dddddddddddddddddddddddddddddddd-linux-6.19.1/bzImage"


def _latest_status(plugin_id: str):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_id: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == plugin_id) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _probe(current=CURRENT, booted=None, switched=None, generation=210,
           version="26.11.20260826.9fbb54b", kernels=(KERNEL, KERNEL)) -> CmdResult:
    """The stdout the probe script produces on a NixOS target."""
    booted_kernel, current_kernel = kernels
    switched = int(time.time()) - 3600 if switched is None else switched
    lines = [
        f"current={current}",
        f"booted={booted or current}",
        f"switched={switched}",
        f"profile=system-{generation}-link",
        f"version={version}",
        f'version_json={{"nixosVersion":"{version}"}}',
    ]
    for part in ("initrd", "kernel", "kernel-modules", "systemd"):
        suffix = f"/{part}"
        lines.append(f"booted_part={booted_kernel}{suffix}")
        lines.append(f"current_part={current_kernel}{suffix}")
    return CmdResult(0, "\n".join(lines) + "\n", "")


def _metadata(revision="f" * 40, last_modified=None, inputs=None, dirty=False) -> CmdResult:
    """The stdout `nix flake metadata --json` produces."""
    last_modified = int(time.time()) - 7200 if last_modified is None else last_modified
    inputs = {"nixpkgs": int(time.time()) - 86400} if inputs is None else inputs
    nodes = {"root": {"inputs": {name: name for name in inputs}}}
    for name, stamp in inputs.items():
        nodes[name] = {"locked": {"lastModified": stamp, "rev": "0" * 40}}
    rev_key = "dirtyRev" if dirty else "rev"
    return CmdResult(0, json.dumps({
        "lastModified": last_modified,
        "locked": {rev_key: revision, "lastModified": last_modified},
        "locks": {"nodes": nodes, "root": "root", "version": 7},
    }), "")


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(NixosUpgrade, BASE_CFG)


def _collect(plugin, probe=None, eval_result=None, metadata=None):
    """Drive one cycle, feeding whichever commands the plugin asked for."""
    outputs = [probe if probe is not None else _probe()]
    if len(plugin.commands()) > 1:
        outputs.append(eval_result if eval_result is not None
                       else CmdResult(0, TARGET + "\n", ""))
        outputs.append(metadata if metadata is not None else _metadata())
    result = plugin.parse(outputs)
    plugin.storage.apply(result)
    return result


def _rewind_eval(plugin, seconds: int) -> None:
    """Age the stored evaluation by `seconds`, as if that much time had passed."""
    state = json.loads(plugin.data.latest_metric("flake_eval_epoch").metadata)
    state["evaluated_epoch"] = int(time.time()) - seconds
    plugin.storage.apply(CollectResult(
        metrics={"flake_eval_epoch": float(state["evaluated_epoch"])},
        metadata={"flake_eval_epoch": json.dumps(state)},
    ))


class TestDrift:
    async def test_matching_closure_is_online(self, plugin):
        _collect(plugin, eval_result=CmdResult(0, CURRENT + "\n", ""))
        assert _latest_status("test-nixos") == "online"
        assert _latest_metric("test-nixos", "up_to_date") == 1.0

    async def test_differing_closure_warns(self, plugin):
        _collect(plugin)
        assert _latest_status("test-nixos") == "warning"
        assert _latest_metric("test-nixos", "up_to_date") == 0.0

    async def test_drift_status_is_configurable(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "drift_status": "failed"})
        _collect(p)
        assert _latest_status("test-nixos") == "failed"

    async def test_unrecognised_drift_status_falls_back_to_warning(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "drift_status": "catastrophe"})
        assert p.drift_status == "warning"

    async def test_drift_logs_both_closures(self, plugin):
        result = _collect(plugin)
        message = next(m for m, _ in result.logs if "out of date" in m)
        assert TARGET in message and CURRENT in message

    async def test_evaluation_failure_is_failed(self, plugin):
        _collect(plugin, eval_result=CmdResult(1, "", "error: attribute 'nope' missing"))
        assert _latest_status("test-nixos") == "failed"
        assert _latest_metric("test-nixos", "up_to_date") is None

    async def test_non_nixos_host_is_offline(self, plugin):
        _collect(plugin, probe=CmdResult(0, "current=\nbooted=\n", ""))
        assert _latest_status("test-nixos") == "offline"

    async def test_unreachable_target_is_offline(self, plugin):
        _collect(plugin, probe=CmdResult(255, "", "ssh: connect failed"))
        assert _latest_status("test-nixos") == "offline"


class TestReboot:
    async def test_same_kernel_needs_no_reboot(self, plugin):
        _collect(plugin, eval_result=CmdResult(0, CURRENT + "\n", ""))
        assert _latest_metric("test-nixos", "reboot_required") == 0.0
        assert _latest_status("test-nixos") == "online"

    async def test_changed_kernel_requires_reboot(self, plugin):
        _collect(plugin, probe=_probe(kernels=(KERNEL, NEW_KERNEL)),
                 eval_result=CmdResult(0, CURRENT + "\n", ""))
        assert _latest_metric("test-nixos", "reboot_required") == 1.0
        assert _latest_status("test-nixos") == "warning"

    async def test_reboot_status_is_configurable(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "reboot_status": "failed"})
        _collect(p, probe=_probe(kernels=(KERNEL, NEW_KERNEL)),
                 eval_result=CmdResult(0, CURRENT + "\n", ""))
        assert _latest_status("test-nixos") == "failed"


class TestFlakeMetadata:
    async def test_revision_and_inputs_recorded(self, plugin):
        modified = int(time.time()) - 7200
        oldest = int(time.time()) - 86400
        _collect(plugin, metadata=_metadata(revision="a" * 40, last_modified=modified,
                                            inputs={"nixpkgs": oldest, "flake-utils": oldest + 60}))
        assert _latest_metric("test-nixos", "flake_last_modified_epoch") == float(modified)
        assert _latest_metric("test-nixos", "inputs_last_modified_epoch") == float(oldest)
        assert _latest_metric("test-nixos", "flake_reachable") == 1.0
        state = json.loads(plugin.data.latest_metric("flake_eval_epoch").metadata)
        assert state["flake_revision"] == "a" * 40
        assert state["oldest_input"] == "nixpkgs"

    async def test_dirty_checkout_revision_is_read_and_marked(self, plugin):
        _collect(plugin, metadata=_metadata(revision="a" * 40 + "-dirty", dirty=True))
        state = json.loads(plugin.data.latest_metric("flake_eval_epoch").metadata)
        assert state["flake_revision"] == "a" * 40 + "-dirty"
        assert plugin._revision_text == "a" * 12 + "-dirty"

    async def test_metadata_failure_is_offline_and_keeps_drift(self, plugin):
        _collect(plugin, eval_result=CmdResult(0, CURRENT + "\n", ""),
                 metadata=CmdResult(1, "", "error: unable to fetch"))
        assert _latest_metric("test-nixos", "flake_reachable") == 0.0
        assert _latest_metric("test-nixos", "up_to_date") == 1.0
        assert _latest_status("test-nixos") == "offline"

    async def test_stale_inputs_warn_when_max_input_age_set(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "max_input_age": "7d"})
        stale = int(time.time()) - 30 * 86400
        _collect(p, eval_result=CmdResult(0, CURRENT + "\n", ""),
                 metadata=_metadata(inputs={"nixpkgs": stale}))
        assert _latest_status("test-nixos") == "warning"

    async def test_stale_inputs_ignored_without_max_input_age(self, plugin):
        stale = int(time.time()) - 365 * 86400
        _collect(plugin, eval_result=CmdResult(0, CURRENT + "\n", ""),
                 metadata=_metadata(inputs={"nixpkgs": stale}))
        assert _latest_status("test-nixos") == "online"


class TestEvalSchedule:
    async def test_first_cycle_evaluates(self, plugin):
        assert len(plugin.commands()) == 3

    async def test_fresh_evaluation_is_not_repeated(self, plugin):
        _collect(plugin)
        assert len(plugin.commands()) == 1

    async def test_stale_evaluation_is_redone(self, plugin):
        _collect(plugin)
        _rewind_eval(plugin, 7200)
        assert len(plugin.commands()) == 3

    async def test_fresh_failure_is_not_retried_immediately(self, plugin):
        _collect(plugin, eval_result=CmdResult(1, "", "error: attribute missing"))
        assert len(plugin.commands()) == 1

    async def test_failed_evaluation_retries_after_retry_interval(self, plugin):
        _collect(plugin, eval_result=CmdResult(1, "", "error: attribute missing"))
        _rewind_eval(plugin, 20 * 60)                      # past 15m retry, short of 1h eval
        assert len(plugin.commands()) == 3

    async def test_failed_metadata_retries_too(self, plugin):
        _collect(plugin, metadata=CmdResult(1, "", "error: unable to download"))
        _rewind_eval(plugin, 20 * 60)
        assert len(plugin.commands()) == 3

    async def test_healthy_evaluation_ignores_retry_interval(self, plugin):
        _collect(plugin)
        _rewind_eval(plugin, 20 * 60)
        assert len(plugin.commands()) == 1

    def test_retry_interval_never_exceeds_eval_interval(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "eval_interval": "10m", "retry_interval": "15m"})
        assert p.retry_interval == 600

    async def test_dashboard_poll_forces_evaluation(self, plugin):
        _collect(plugin)                                   # fresh, so a tick would probe only
        plugin.engine = MagicMock(run_cycle_now=AsyncMock(return_value=True))
        assert await plugin.run_cycle() is True
        assert len(plugin.commands()) == 3

    async def test_probe_only_cycle_still_tracks_drift(self, plugin):
        _collect(plugin)                                   # evaluates, drift found
        result = _collect(plugin, probe=_probe(current=TARGET))   # a switch landed
        assert result.metrics["up_to_date"] == 1.0
        assert _latest_status("test-nixos") == "online"


class TestCommands:
    def test_local_flake_needs_no_refresh(self, plugin):
        eval_cmd = plugin.commands()[1].text
        assert "--refresh" not in eval_cmd
        assert '"/etc/nixos#nixosConfigurations.\\"$(uname -n)\\"' in eval_cmd

    def test_remote_flake_refreshes(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "flake": "github:owner/config"})
        assert "--refresh" in p.commands()[1].text
        assert "--refresh" in p.commands()[2].text

    def test_configuration_overrides_the_hostname(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "configuration": "ragnarok"})
        assert 'nixosConfigurations.\\"ragnarok\\"' in p.commands()[1].text
        assert "$(uname -n)" not in p.commands()[1].text

    def test_eval_never_writes_the_lock_file(self, plugin):
        assert "--no-write-lock-file" in plugin.commands()[1].text
        assert "--no-write-lock-file" in plugin.commands()[2].text

    def test_eval_carries_its_own_timeout(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "eval_timeout": "20m"})
        assert p.commands()[1].timeout == 1200

    def test_switch_targets_the_flake(self, plugin):
        assert plugin._switch_command() == "sudo -n nixos-rebuild switch --flake /etc/nixos"

    def test_switch_names_the_configuration_when_set(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "flake": "github:owner/config",
                                       "configuration": "ragnarok"})
        assert "'github:owner/config#ragnarok'" in p._switch_command()

    def test_switch_honours_require_sudo(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "require_sudo": False})
        assert not p._switch_command().startswith("sudo")

    def test_rebuild_args_are_appended(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "rebuild_args": ["--use-remote-sudo"]})
        assert "--use-remote-sudo" in p._switch_command()

    def test_update_targets_the_flake(self, plugin):
        assert "flake update --flake /etc/nixos" in plugin._update_command()


class TestLocalPath:
    @pytest.mark.parametrize("ref,expected", [
        ("/etc/nixos", "/etc/nixos"),
        ("path:/etc/nixos", "/etc/nixos"),
        ("git+file:///etc/nixos", "/etc/nixos"),
        ("git+file:///etc/nixos?ref=main", "/etc/nixos"),
        ("./config", "./config"),
        ("github:owner/config", None),
        ("gitlab:owner/config", None),
    ])
    def test_local_paths_are_recognised(self, make_plugin, ref, expected):
        assert make_plugin(NixosUpgrade, {**BASE_CFG, "flake": ref}).local_path == expected


async def _launch(plugin, action_id="switch", pid=4242):
    """Drive plan_action -> ActionPlan (launch) -> interpret_action, mirroring
    VigilEngine.dispatch_action for a launched detached job."""
    plan = plugin.plan_action(action_id)
    if plan is None:
        return None
    if hasattr(plan, "success"):        # CollectResult (refused) — no launch
        plugin.storage.apply(plan)
        return plan
    outcome = plugin.interpret_action(action_id, CmdResult(0, f"{pid}\n", ""))
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
    cmds = plugin.commands()
    assert len(cmds) == 1                # a running job polls with one command
    result = plugin.parse([poll_result])
    plugin.storage.apply(result)
    return result


class TestActions:
    def test_both_actions_exposed(self, plugin):
        assert {a['action_id'] for a in plugin.get_actions()} == {"update_flake", "switch"}

    def test_unknown_action_is_unhandled(self, plugin):
        assert plugin.plan_action("nonsense") is None

    async def test_switch_launch_records_running_job(self, plugin):
        outcome = await _launch(plugin, pid=1234)
        assert outcome.success is True
        job = plugin.jobs.running()
        assert job['kind'] == 'switch' and job['pid'] == 1234
        assert "nixos-rebuild switch --flake /etc/nixos" in job['command']

    async def test_update_launch_records_its_kind(self, plugin):
        await _launch(plugin, "update_flake")
        assert plugin.jobs.running()['kind'] == 'update'

    async def test_update_refused_for_a_remote_flake(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "flake": "github:owner/config"})
        refused = await _launch(p, "update_flake")
        assert refused.success is False
        assert any("local flake checkout" in m for m, _ in refused.logs)
        assert p.jobs.running() is None

    async def test_switch_allowed_for_a_remote_flake(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "flake": "github:owner/config"})
        assert (await _launch(p, "switch")).success is True

    async def test_launch_failure_records_no_job(self, plugin):
        plugin.plan_action("switch")
        outcome = plugin.interpret_action("switch", CmdResult(1, "", "setsid: not found"))
        plugin.storage.apply(outcome)
        assert outcome.success is False
        assert plugin.jobs.running() is None

    async def test_second_job_refused_while_one_runs(self, plugin):
        await _launch(plugin)
        refused = plugin.plan_action("update_flake")
        assert any("already running" in m for m, _ in refused.logs)


class TestJobPolling:
    async def test_poll_while_running_keeps_job_running(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(10, None, True, "building...\n"))
        assert plugin.jobs.running() is not None
        assert plugin.jobs.running()['progress'] == "building..."

    async def test_completion_marks_succeeded(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(20, 0, False, "activating the configuration...\n"))
        assert plugin.jobs.running() is None
        assert plugin.jobs.recent()[0]['state'] == 'succeeded'

    async def test_nonzero_exit_is_failure(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(5, 2, False, ""))
        assert plugin.jobs.recent()[0]['state'] == 'failed'

    async def test_vanished_process_is_failure(self, plugin):
        await _launch(plugin)
        _poll_once(plugin, _poll(5, None, False, ""))
        assert plugin.jobs.recent()[0]['state'] == 'failed'

    async def test_finished_job_forces_a_re_evaluation(self, plugin):
        _collect(plugin)                                   # evaluation is now fresh
        assert len(plugin.commands()) == 1
        await _launch(plugin)
        _poll_once(plugin, _poll(20, 0, False, "done\n"))
        assert len(plugin.commands()) == 3


class TestUI:
    async def test_detail_rows_cover_both_closures(self, plugin):
        _collect(plugin)
        rows = {r['label']: r['value'] for r in plugin._detail_rows}
        assert rows['Running closure'] == CURRENT
        assert rows['Flake closure'] == TARGET
        assert rows['Flake'] == "/etc/nixos"

    async def test_drift_card_states(self, plugin):
        assert plugin._drift_text == 'UNKNOWN'
        _collect(plugin)
        assert plugin._drift_text == 'OUT OF DATE'
        assert plugin._drift_color == 'warning'
        _collect(plugin, probe=_probe(current=TARGET))
        assert plugin._drift_text == 'UP TO DATE'
        assert plugin._drift_color == 'online'

    async def test_reboot_card_states(self, plugin):
        _collect(plugin)
        assert plugin._reboot_text == 'NOT NEEDED'
        _collect(plugin, probe=_probe(kernels=(KERNEL, NEW_KERNEL)))
        assert plugin._reboot_text == 'REQUIRED'

    async def test_update_button_hidden_for_a_remote_flake(self, make_plugin):
        p = make_plugin(NixosUpgrade, {**BASE_CFG, "flake": "github:owner/config"})
        button = p.UI_SPEC['buttons']['controls'][0]
        assert button['visible_if'](p) is False

    async def test_ui_spec_renders_without_data(self, plugin):
        spec = plugin.UI_SPEC
        assert spec['job_panel']['run_action_id'] == 'switch'
        assert spec['cards']['flake_card']['value'] == "/etc/nixos"
