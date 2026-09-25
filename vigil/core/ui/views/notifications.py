"""Notification controls: a monitor's mute switch, and the dialog that tests channels and lists mutes."""

from typing import Any

from nicegui import ui

from vigil.core.contracts import EngineLike
from ..components import action_button, card


def render_mute_button(engine: EngineLike, plugin: Any) -> None:
    """The switch that mutes a monitor's notifications, or a group's for everything in it."""
    notifications = engine.notifications
    if not notifications.channels:
        return

    window = notifications.maintenance_for(plugin.id)
    if window is not None:
        action_button(f'In maintenance: {window}', icon='construction').props('disable')

    @ui.refreshable
    def button() -> None:
        muter = notifications.muted_by(plugin.id)
        if muter is not None and muter != plugin.id:
            group = notifications.monitor_name(muter)
            action_button(f'Muted by {group}', icon='notifications_off').props('disable')
            return
        muted = muter == plugin.id

        def toggle() -> None:
            notifications.set_muted(plugin.id, not muted)
            ui.notify(f'{plugin.name} {"unmuted" if muted else "muted"}', type='positive')
            button.refresh()

        action_button('Unmute' if muted else 'Mute', on_click=toggle,
                      icon='notifications_off' if muted else 'notifications')

    button()


def open_notifications_dialog(engine: EngineLike) -> None:
    """A dialog listing each channel with a test button, and every muted monitor with an unmute button."""
    notifications = engine.notifications

    async def send_test(channel_id: str) -> None:
        # A failed test is the answer the operator asked for, so it is shown rather than raised.
        try:
            await notifications.send_test(channel_id)
        except Exception as e:
            ui.notify(f'Test to {channel_id} failed: {e}', type='negative')
            return
        ui.notify(f'Test sent to {channel_id}', type='positive')

    @ui.refreshable
    def muted_list() -> None:
        muted = sorted(notifications.muted_monitors(), key=lambda p: p.name.lower())
        if not muted:
            ui.label('No monitors are muted.').classes('halon-caption')
            return
        for plugin in muted:
            def unmute(p=plugin) -> None:
                notifications.set_muted(p.id, False)
                ui.notify(f'{p.name} unmuted', type='positive')
                muted_list.refresh()

            with ui.row().classes('w-full items-center justify-between gap-2'):
                ui.label(plugin.name)
                action_button('Unmute', on_click=unmute, icon='notifications', weight='flat')

    with ui.dialog() as dialog, card('w-full').style('max-width: 32rem;'):
        ui.label('Notifications').classes('halon-title-section mb-4')
        ui.label('Channels').classes('halon-label')
        for channel_id, channel in notifications.channels.items():
            with ui.row().classes('w-full items-center justify-between gap-2'):
                with ui.column().classes('gap-0'):
                    ui.label(channel_id)
                    ui.label(channel.TYPE).classes('halon-caption')
                action_button('Send test', on_click=lambda c=channel_id: send_test(c),
                              icon='send', weight='flat').mark(f'test-{channel_id}')
        if notifications.windows:
            ui.separator().classes('my-2')
            ui.label('Maintenance').classes('halon-label')
            active = notifications.active_windows()
            for window in notifications.windows:
                with ui.row().classes('w-full items-center justify-between gap-2'):
                    with ui.column().classes('gap-0'):
                        ui.label(window.name)
                        ui.label(window.describe()).classes('halon-caption')
                    if window in active:
                        ui.label('Active now').classes('halon-caption')
        ui.separator().classes('my-2')
        ui.label('Muted').classes('halon-label')
        muted_list()
        with ui.row().classes('w-full justify-end mt-4'):
            action_button('Close', on_click=dialog.close, icon=None, weight='flat')
    dialog.open()
