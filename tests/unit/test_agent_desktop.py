"""The agent's desktop notifications: the notify-send call and opening the link on a click."""

import asyncio

from vigil_agent import desktop


class FakeDesktop:
    """Stands in for notify-send, busctl and xdg-open, with a notification server behind them."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.open = {}
        self._next_id = 100
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', self._exec)

    async def _exec(self, *args, **kwargs):
        self.calls.append(list(args))
        if args[0] == 'notify-send':
            replace = next((a.split('=', 1)[1] for a in args if a.startswith('--replace-id=')), None)
            if replace is None:
                self._next_id += 1
            notification_id = int(replace) if replace else self._next_id
            proc = FakeNotifySend(notification_id, waits='--wait' in args)
            self.open[notification_id] = proc
            return proc
        if args[0] == 'busctl':
            proc = self.open.pop(int(args[-1]), None)
            if proc is not None:
                proc.finish(b'')
        return FakeProc()

    def click(self, notification_id, action=b'default\n'):
        self.open.pop(notification_id).finish(action)

    def names(self):
        return [c[0] for c in self.calls]


class FakeProc:
    returncode = 0

    async def communicate(self):
        return b'', b''


class FakeNotifySend:
    """A notify-send that prints its id, then waits for a click or a close when --wait is given."""

    def __init__(self, notification_id, waits):
        self.id = notification_id
        self.returncode = None
        self._done = asyncio.Event()
        self._rest = b''
        self.stdout = self
        if not waits:
            self.finish(b'')

    async def readline(self):
        for _ in range(3):  # a real notify-send takes a moment to report the id
            await asyncio.sleep(0)
        return f"{self.id}\n".encode()

    def finish(self, rest):
        self._rest, self.returncode = rest, 0
        self._done.set()

    def kill(self):
        self.finish(b'')

    async def wait(self):
        await self._done.wait()

    async def communicate(self):
        await self._done.wait()
        return self._rest, b''


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


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
        fake = FakeDesktop(monkeypatch)
        task = asyncio.create_task(desktop.Notifier().show(
            {'title': 'Nas is failed', 'body': 'why', 'urgency': 'critical',
             'url': 'https://v/monitor/nas'}))
        await _settle()
        fake.click(101)
        await task
        assert fake.calls == [
            ['notify-send', '--app-name=Vigil', '--urgency=critical', '--print-id',
             '--action=default=Open', '--action=dismiss=Dismiss', '--wait', '--', 'Nas is failed', 'why'],
            ['xdg-open', 'https://v/monitor/nas'],
        ]

    async def test_without_a_link_it_offers_only_dismiss(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        task = asyncio.create_task(desktop.Notifier().show({'title': '-t', 'body': 'b', 'urgency': 'bogus'}))
        await _settle()
        fake.click(101, b'dismiss\n')
        await task
        assert fake.calls == [['notify-send', '--app-name=Vigil', '--urgency=normal', '--print-id',
                               '--action=dismiss=Dismiss', '--wait', '--', '-t', 'b']]

    async def test_the_dismiss_button_closes_without_opening_the_link(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        task = asyncio.create_task(desktop.Notifier().show({'title': 't', 'body': 'b', 'url': 'https://v'}))
        await _settle()
        fake.click(101, b'dismiss\n')
        await task
        assert fake.names() == ['notify-send']

    async def test_a_timeout_closes_it_even_when_critical(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        await asyncio.wait_for(desktop.Notifier().show(
            {'title': 't', 'body': 'b', 'urgency': 'critical', 'timeout': 0.05}), timeout=2)
        assert '--expire-time=50' in fake.calls[0]
        assert fake.calls[1] == desktop.close_command(101)
        assert fake.open == {}

    async def test_a_missing_notify_send_is_logged_not_raised(self, monkeypatch, caplog):
        async def missing(*args, **kwargs):
            raise FileNotFoundError('notify-send')

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', missing)
        await desktop.Notifier().show({'title': 't', 'body': 'b'})
        assert 'notify-send installed' in caplog.text


class TestKeys:
    FAILED = {'title': 'Nas is failed', 'body': 'b', 'url': 'https://v/monitor/nas', 'key': 'nas'}

    async def test_dismiss_closes_the_keys_notification_and_opens_nothing(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        notifier = desktop.Notifier()
        task = asyncio.create_task(notifier.show(self.FAILED))
        await _settle()
        await notifier.dismiss({'key': 'nas'})
        await task
        assert fake.calls[1] == desktop.close_command(101)
        assert 'xdg-open' not in fake.names()
        assert fake.open == {}

    async def test_a_newer_notification_for_the_key_replaces_it(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        notifier = desktop.Notifier()
        first = asyncio.create_task(notifier.show(self.FAILED))
        await _settle()
        second = asyncio.create_task(notifier.show({**self.FAILED, 'title': 'Nas is still failed'}))
        await _settle()
        assert first.cancelled()
        assert '--replace-id=101' in fake.calls[1]
        await notifier.dismiss({'key': 'nas'})
        await second
        assert fake.calls[2] == desktop.close_command(101)

    async def test_a_dismiss_right_after_a_replacement_still_closes_it(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        notifier = desktop.Notifier()
        first = asyncio.create_task(notifier.show(self.FAILED))
        await _settle()
        # Each frame becomes its own task, in arrival order, as the agent's client does.
        replacement = asyncio.create_task(notifier.show({**self.FAILED, 'title': '1 failed'}))
        dismissal = asyncio.create_task(notifier.dismiss({'key': 'nas'}))
        await dismissal
        await asyncio.wait_for(replacement, timeout=2)  # left open, the replacement would wait forever
        assert first.cancelled()
        assert fake.calls[-1] == desktop.close_command(101)
        assert fake.open == {}

    async def test_dismissing_an_unknown_key_does_nothing(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        await desktop.Notifier().dismiss({'key': 'nas'})
        assert fake.calls == []

    async def test_a_notification_closed_by_the_user_is_forgotten(self, monkeypatch):
        fake = FakeDesktop(monkeypatch)
        notifier = desktop.Notifier()
        task = asyncio.create_task(notifier.show(self.FAILED))
        await _settle()
        fake.open.pop(101).finish(b'')
        await task
        await notifier.dismiss({'key': 'nas'})
        assert 'busctl' not in fake.names()


def test_the_agent_advertises_notify_and_dismiss():
    from vigil_agent.client import CAPABILITIES
    assert {'notify', 'dismiss'} <= set(CAPABILITIES)


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
