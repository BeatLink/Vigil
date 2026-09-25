"""The borg archive browser, driven inside a real NiceGUI client.

borg itself is stubbed at run_action: browsing answers with a canned listing
run through the plugin's real interpret_action, so the page, the plugin's
listing cache and the restore it asks for are all the real ones.
"""

import asyncio
import json
import time

import pytest
from nicegui import ui
from nicegui.testing import User

from vigil.core.connectors.types import CmdResult, CollectResult
from vigil.plugins.borg import Borg, _frame_line

pytest_plugins = ['nicegui.testing.user_plugin']
pytestmark = pytest.mark.nicegui_main_file(None)

LISTINGS = {
    '': "d\t\t\tStorage\n-\t12\t2026-09-20T10:00:00\treadme.txt",
    'Storage': "-\t2048\t2026-09-20T10:01:00\tStorage/notes.txt\n-\t5\t2026-09-20T10:02:00\tStorage/todo.txt",
}


def _framed(stdout: str) -> CmdResult:
    return CmdResult(0, "\n".join([_frame_line(0, "out", 0), stdout, _frame_line(0, "err", 0), ""]) + "\n", "")


@pytest.fixture
def plugin(make_plugin):
    p = make_plugin(Borg, {"name": "vault", "id": "vault", "repo": "/srv/repo", "restore_dir": "/var/tmp/r",
                           "ssh_config": {"host": "test.host"}})
    now = int(time.time())
    archives = [{'name': 'b2', 'epoch': now - 60}, {'name': 'b1', 'epoch': now - 3600}]
    p.storage.apply(CollectResult(metrics={'archive_list': 2.0},
                                  metadata={'archive_list': json.dumps({'archives': archives})}))
    p.calls = []

    async def run_action(action_id, **kwargs):
        p.calls.append((action_id, kwargs))
        if action_id == 'browse_archive':
            outcome = p.interpret_action(action_id, _framed(LISTINGS[kwargs['path']]), **kwargs)
            return outcome.success, outcome.metadata['content']
        return True, "Restore started (pid 7) into /var/tmp/r/b2-x"

    p.run_action = run_action
    return p


@pytest.fixture
def page(plugin, monkeypatch):
    # Off the change-bus scheduler, whose per-client registry would outlive this test's client; one inline run fills the archive list.
    monkeypatch.setattr('vigil.core.ui.model.schedule_callback', lambda callback, run_now=True: callback())

    @ui.page('/')
    def _page():
        from vigil.plugins.borg.browser import render_browser
        render_browser(plugin)


def _table(user: User) -> ui.table:
    return next(iter(user.find(ui.table).elements))


async def _until(condition):
    """Waits for the page's async handlers to reach `condition`, which should_see cannot, since it does not read table rows."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


def _names(user: User):
    return [r['name'] for r in _table(user).rows]


async def _pick(user: User, archive: str):
    next(iter(user.find(ui.select).elements)).value = archive
    await user.should_see('entries')


class TestBrowser:
    async def test_lists_the_archives_the_monitor_saw(self, user: User, page, plugin):
        await user.open('/')
        assert next(iter(user.find(ui.select).elements)).options == ['b2', 'b1']
        await user.should_see('Pick an archive')

    async def test_picking_an_archive_lists_its_top_level(self, user: User, page, plugin):
        await user.open('/')
        await _pick(user, 'b2')
        assert [r['name'] for r in _table(user).rows] == ['Storage', 'readme.txt']
        assert plugin.calls == [('browse_archive', {'archive': 'b2', 'path': ''})]

    async def test_opening_a_folder_lists_it_and_extends_the_trail(self, user: User, page, plugin):
        await user.open('/')
        await _pick(user, 'b2')
        user.find(ui.table).trigger('open', _table(user).rows[0])
        await _until(lambda: 'notes.txt' in _names(user))
        await user.should_see('Storage', kind=ui.button)
        assert _table(user).rows[0]['size'] == '2.0 KB'

    async def test_a_folder_already_listed_is_not_listed_again(self, user: User, page, plugin):
        await user.open('/')
        await _pick(user, 'b2')
        user.find(ui.table).trigger('open', _table(user).rows[0])
        await _until(lambda: 'notes.txt' in _names(user))
        user.find('/').click()
        await _until(lambda: 'readme.txt' in _names(user))
        assert len(plugin.calls) == 2

    async def test_restoring_ticked_paths_asks_first(self, user: User, page, plugin):
        await user.open('/')
        await _pick(user, 'b2')
        rows = _table(user).rows
        user.find(ui.table).trigger('selection', {'rows': rows, 'keys': [r['path'] for r in rows], 'added': True})
        await user.should_see('Restore 2 selected')
        user.find('Restore 2 selected').click()
        await user.should_see('Live files are not touched')
        assert not any(call[0] == 'restore_archive' for call in plugin.calls)
        user.find('Confirm').click()
        await _until(lambda: plugin.calls[-1][0] == 'restore_archive')
        assert plugin.calls[-1] == ('restore_archive', {'archive': 'b2', 'paths': ['Storage', 'readme.txt']})

    async def test_cancelling_restores_nothing(self, user: User, page, plugin):
        await user.open('/')
        await _pick(user, 'b2')
        rows = _table(user).rows
        user.find(ui.table).trigger('selection', {'rows': rows[:1], 'keys': [rows[0]['path']], 'added': True})
        user.find('Restore 1 selected').click()
        await user.should_see('Live files are not touched')
        user.find('Cancel').click()
        await asyncio.sleep(0.05)
        assert not any(call[0] == 'restore_archive' for call in plugin.calls)


class TestJobPanel:
    async def test_without_source_paths_it_says_the_other_jobs_still_run(self, user: User, plugin, monkeypatch):
        monkeypatch.setattr('vigil.core.ui.model.schedule_callback', lambda callback, run_now=True: callback())

        @ui.page('/jobs')
        def _page():
            from vigil.core.ui.components import render_job_panel
            render_job_panel(plugin, plugin.UI_SPEC['job_panel'])

        await user.open('/jobs')
        await user.should_see('Run Backup needs source_paths')
        await user.should_not_see('Not available')
