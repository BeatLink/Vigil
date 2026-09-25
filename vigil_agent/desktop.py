"""Desktop notifications for the session the agent runs in.

The notification goes through ``notify-send``, so it reaches whatever
notification daemon the desktop runs (GNOME, KDE, mako, dunst, ...). That
needs the user's session bus, which only a process running as the logged-in
user can reach: an agent running as a system service cannot show one.

When the server sends a link, the notification carries a default action, and
clicking it opens the link with ``xdg-open``.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

URGENCIES = ('low', 'normal', 'critical')

VIGIL_ICON = 'vigil'
"""The icon name that stands for Vigil's own icon, which ships with the agent."""

_ICON_FILE = Path(__file__).parent / 'icon.svg'

WAIT_SECONDS = 12 * 3600
"""How long to keep waiting for a click before giving up on a notification."""


def command(title: str, body: str, urgency: str, url: Optional[str],
            icon: Optional[str] = None) -> List[str]:
    """The notify-send argument list for one notification."""
    args = ['notify-send', '--app-name=Vigil', f'--urgency={urgency}']
    if icon:
        # 'vigil' is the bundled icon; anything else is an icon theme name or a file here.
        args.append(f'--icon={_ICON_FILE if icon == VIGIL_ICON else icon}')
    if url:
        # --wait keeps notify-send running until the notification closes, and
        # prints the action's name if it was clicked.
        args += ['--action=default=Open', '--wait']
    return args + ['--', title, body]


async def show(frame: Dict[str, Any]) -> None:
    """Show the notification a NOTIFY frame describes; open its link if it is clicked."""
    title = str(frame.get('title') or 'Vigil')
    body = str(frame.get('body') or '')
    urgency = frame.get('urgency') if frame.get('urgency') in URGENCIES else 'normal'
    url = frame.get('url') or None
    icon = str(frame['icon']) if frame.get('icon') else None

    try:
        proc = await asyncio.create_subprocess_exec(
            *command(title, body, urgency, url, icon),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logging.error(f"could not show a desktop notification (is notify-send installed?): {e}")
        return

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=WAIT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return
    except asyncio.CancelledError:
        proc.kill()
        raise

    if proc.returncode != 0:
        logging.error(f"notify-send failed ({proc.returncode}): {err.decode(errors='replace').strip()}")
        return
    if url and out.decode(errors='replace').strip() == 'default':
        await _open(str(url))


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
