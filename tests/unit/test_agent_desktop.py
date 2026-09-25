"""The agent's desktop notifications: the notify-send call and opening the link on a click."""

import asyncio

from vigil_agent import desktop


class FakeProc:
    def __init__(self, out=b'', returncode=0):
        self._out = out
        self.returncode = returncode

    async def communicate(self):
        return self._out, b''


def _record(monkeypatch, clicked):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return FakeProc(b'default\n' if clicked and args[0] == 'notify-send' else b'')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_exec)
    return calls


class TestIcon:
    def test_vigil_means_the_bundled_icon(self):
        args = desktop.command('t', 'b', 'normal', None, 'vigil')
        icon = next(a for a in args if a.startswith('--icon='))
        assert icon.endswith('vigil_agent/icon.svg')
        assert (desktop._ICON_FILE).is_file()

    def test_other_icons_pass_through(self):
        assert '--icon=dialog-error' in desktop.command('t', 'b', 'normal', None, 'dialog-error')

    def test_no_icon_adds_no_flag(self):
        assert not any(a.startswith('--icon') for a in desktop.command('t', 'b', 'normal', None))


class TestShow:
    async def test_a_click_opens_the_link(self, monkeypatch):
        calls = _record(monkeypatch, clicked=True)
        await desktop.show({'title': 'Nas is failed', 'body': 'why', 'urgency': 'critical',
                            'url': 'https://v/monitor/nas'})
        assert calls == [
            ['notify-send', '--app-name=Vigil', '--urgency=critical',
             '--action=default=Open', '--wait', '--', 'Nas is failed', 'why'],
            ['xdg-open', 'https://v/monitor/nas'],
        ]

    async def test_dismissing_opens_nothing(self, monkeypatch):
        calls = _record(monkeypatch, clicked=False)
        await desktop.show({'title': 't', 'body': 'b', 'url': 'https://v'})
        assert [c[0] for c in calls] == ['notify-send']

    async def test_without_a_link_it_does_not_wait(self, monkeypatch):
        calls = _record(monkeypatch, clicked=False)
        await desktop.show({'title': '-t', 'body': 'b', 'urgency': 'bogus'})
        assert calls == [['notify-send', '--app-name=Vigil', '--urgency=normal', '--', '-t', 'b']]

    async def test_a_missing_notify_send_is_logged_not_raised(self, monkeypatch, caplog):
        async def missing(*args, **kwargs):
            raise FileNotFoundError('notify-send')

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', missing)
        await desktop.show({'title': 't', 'body': 'b'})
        assert 'notify-send installed' in caplog.text


def test_the_agent_advertises_notify():
    from vigil_agent.client import CAPABILITIES
    assert 'notify' in CAPABILITIES


class TestNotifyOnly:
    async def test_commands_are_refused(self, monkeypatch):
        from vigil_agent import executor, protocol as proto
        from vigil_agent.client import AgentClient

        async def must_not_run(*args, **kwargs):
            raise AssertionError("a notify-only agent ran a command")

        monkeypatch.setattr(executor, 'run', must_not_run)
        sent = []

        class Socket:
            async def send(self, raw):
                sent.append(proto.decode(raw))

        client = AgentClient('ws://v', 'desk', 't', notify_only=True)
        client._socket = Socket()
        await client._handle_exec({'t': proto.EXEC, 'id': 1, 'cmd': 'id'})
        assert sent[0]['rc'] == -1
        assert 'only shows notifications' in sent[0]['err']

    def test_the_setting_is_read_from_config_and_environment(self, tmp_path, monkeypatch):
        from vigil_agent.config import AgentConfig

        config_file = tmp_path / "agent.yaml"
        config_file.write_text("url: ws://v\nid: desk\ntoken: t\nnotify_only: true\n")
        for var in ('VIGIL_AGENT_URL', 'VIGIL_AGENT_ID', 'VIGIL_AGENT_TOKEN',
                    'VIGIL_AGENT_NOTIFY_ONLY'):
            monkeypatch.delenv(var, raising=False)
        assert AgentConfig.load(str(config_file)).notify_only is True

        monkeypatch.setenv('VIGIL_AGENT_NOTIFY_ONLY', 'false')
        assert AgentConfig.load(str(config_file)).notify_only is False
