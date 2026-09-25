"""Desktop notifications for the session the agent runs in.

The notification goes through ``notify-send``, so it reaches whatever
notification daemon the desktop runs (GNOME, KDE, mako, dunst, ...). That
needs the user's session bus, which only a process running as the logged-in
user can reach: an agent running as a system service cannot show one.

When the server sends a link, the notification carries a default action, and
clicking it opens the link with ``xdg-open``. Each notification can belong to
a key, the monitor it is about: a newer one for the same key replaces it, and
dismissing the key closes it.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

URGENCIES = ('low', 'normal', 'critical')

VIGIL_ICON = 'vigil'
"""The icon name that stands for Vigil's own icon, which ships with the agent."""

_ICON_FILE = Path(__file__).parent / 'icon.svg'

WAIT_SECONDS = 12 * 3600
"""How long to keep waiting for a click before giving up on a notification."""

ID_SECONDS = 10.0
"""How long notify-send may take to report the new notification's id."""


def command(title: str, body: str, urgency: str, url: Optional[str],
            icon: Optional[str] = None, replace_id: Optional[int] = None) -> List[str]:
    """The notify-send argument list for one notification."""
    args = ['notify-send', '--app-name=Vigil', f'--urgency={urgency}', '--print-id']
    if icon:
        # 'vigil' is the bundled icon; anything else is an icon theme name or a file here.
        args.append(f'--icon={_ICON_FILE if icon == VIGIL_ICON else icon}')
    if replace_id is not None:
        args.append(f'--replace-id={replace_id}')
    if url:
        # --wait keeps notify-send running until the notification closes, and
        # prints the action's name if it was clicked.
        args += ['--action=default=Open', '--wait']
    return args + ['--', title, body]


def close_command(notification_id: int) -> List[str]:
    """The call that closes one notification through the desktop's notification service."""
    return ['busctl', '--user', 'call', 'org.freedesktop.Notifications',
            '/org/freedesktop/Notifications', 'org.freedesktop.Notifications',
            'CloseNotification', 'u', str(notification_id)]


@dataclass
class _Shown:
    id: int
    task: Optional[asyncio.Task]


class Notifier:
    """Shows notifications and remembers which one each key has on screen."""

    def __init__(self) -> None:
        self._shown: Dict[str, _Shown] = {}
        # Frames for one key are handled in the order they arrived, or a dismiss can overtake the notification it closes.
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, key: Optional[str]) -> asyncio.Lock:
        return self._locks.setdefault(key or '', asyncio.Lock())

    async def show(self, frame: Dict[str, Any]) -> None:
        """Show the notification a NOTIFY frame describes; open its link if it is clicked."""
        title = str(frame.get('title') or 'Vigil')
        body = str(frame.get('body') or '')
        urgency = frame.get('urgency') if frame.get('urgency') in URGENCIES else 'normal'
        url = frame.get('url') or None
        icon = str(frame['icon']) if frame.get('icon') else None
        key = str(frame['key']) if frame.get('key') else None

        async with self._lock(key):
            started = await self._start(title, body, urgency, url, icon, key)
        if started is None:
            return
        proc, mine = started
        try:
            rest, err = await asyncio.wait_for(proc.communicate(), timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return
        except asyncio.CancelledError:
            # Replaced by a newer notification: stop waiting on this one without closing it.
            proc.kill()
            raise
        finally:
            if mine is not None and url and self._shown.get(key) is mine:
                del self._shown[key]

        if proc.returncode != 0:
            logging.error(f"notify-send failed ({proc.returncode}): {err.decode(errors='replace').strip()}")
            return
        if url and rest.decode(errors='replace').strip() == 'default':
            await _open(str(url))

    async def _start(self, title: str, body: str, urgency: str, url: Optional[str],
                     icon: Optional[str], key: Optional[str]):
        """Show the notification and record its id under the key, replacing the key's previous one."""
        replace_id = None
        previous = self._shown.pop(key, None) if key else None
        if previous is not None:
            replace_id = previous.id
            if previous.task is not None:
                previous.task.cancel()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command(title, body, urgency, url, icon, replace_id),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            logging.error(f"could not show a desktop notification (is notify-send installed?): {e}")
            return None
        mine: Optional[_Shown] = None
        try:
            first = await asyncio.wait_for(proc.stdout.readline(), timeout=ID_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if key and first.strip().isdigit():
            mine = self._shown[key] = _Shown(int(first), asyncio.current_task() if url else None)
        return proc, mine

    async def dismiss(self, frame: Dict[str, Any]) -> None:
        """Close the notification a key has on screen, if any."""
        key = str(frame.get('key') or '')
        async with self._lock(key):
            shown = self._shown.pop(key, None)
        if shown is None:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *close_command(shown.id),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
        except OSError as e:
            logging.error(f"could not close a desktop notification (is busctl installed?): {e}")
            return
        if proc.returncode != 0:
            logging.error(f"closing notification {shown.id} failed: {err.decode(errors='replace').strip()}")

    def cancel_all(self) -> None:
        """Stop waiting on every notification, as the agent shuts down."""
        for shown in self._shown.values():
            if shown.task is not None:
                shown.task.cancel()
        self._shown.clear()


async def _open(url: str) -> None:
    """Open a link in the user's browser."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'xdg-open', url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except OSError as e:
        logging.error(f"could not open {url} (is xdg-open installed?): {e}")
        return
    if proc.returncode != 0:
        logging.error(f"xdg-open {url} failed: {err.decode(errors='replace').strip()}")
