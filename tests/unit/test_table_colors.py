"""A spec table whose column colors its cells with a plugin's own callable, rendered inside a real NiceGUI client."""

from types import SimpleNamespace

import pytest
from nicegui import json, ui
from nicegui.testing import User

from vigil.core.ui.components import render_table_with_actions
from vigil.core.ui.theme import STATUS_COLORS

pytest_plugins = ['nicegui.testing.user_plugin']
pytestmark = pytest.mark.nicegui_main_file(None)


async def test_a_callable_cell_color_by_still_serialises(user: User):
    plugin = SimpleNamespace(rows=[{'key': 'a', 'grade': 'failed'}])
    page = SimpleNamespace(on_refresh=lambda fn: None)
    spec = {'row_key': 'key', 'rows_attr': 'rows',
            'columns': [{'name': 'grade', 'label': 'Grade', 'field': 'grade',
                         'cell_color_by': lambda row: row.get('grade')}]}
    tables = []

    @ui.page('/')
    def _page():
        tables.append(render_table_with_actions(plugin, page, spec))

    await user.open('/')
    table = tables[0]
    json.dumps(table.props)
    assert 'cell_color_by' not in table.props['columns'][0]
    assert table.props['rows'][0]['_color_grade'] == STATUS_COLORS['failed']
