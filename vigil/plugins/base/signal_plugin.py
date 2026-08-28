"""Shared scaffolding for a single-signal monitor. Each one collects one
signal — cpu, smart, throughput — and declares the cards and charts that show
it; this base assembles the usual host-card / charts / events page around them
so every signal monitor lays out the same way.
"""

from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.ui.spec_types import CardSpec, ChartSpec, LayoutRow, UISpec

# 'offline' sits below 'warning': a signal that cannot be measured is less
# alarming than one measuring a bad number.
SEVERITY = {'online': 0, 'offline': 1, 'warning': 2, 'failed': 3}

LOG_LEVEL = {'online': 'INFO', 'warning': 'WARNING', 'offline': 'WARNING', 'failed': 'ERROR'}


def worst_status(statuses: List[str]) -> str:
    """The most severe of the given statuses, defaulting to online."""
    return max(statuses, key=lambda s: SEVERITY.get(s, 1)) if statuses else 'online'


class SignalPlugin(Plugin):
    """A monitor for one host signal. Subclasses stay pure: each declares its
    own commands, parses its own results into metrics/logs/a status, and names
    the cards and charts that render them."""

    def cards(self) -> Dict[str, CardSpec]:
        return {}

    def charts(self) -> Dict[str, ChartSpec]:
        return {}

    def card_row(self) -> List[str]:
        """Cards that join the host card's top row; the rest are placed by rows()."""
        return list(self.cards())

    def rows(self) -> List[LayoutRow]:
        """Full-width layout rows this monitor adds above its charts."""
        return []

    @property
    def UI_SPEC(self) -> UISpec:
        charts = self.charts()
        layout = ([['host_card'] + self.card_row()]
                  + self.rows()
                  + [[name] for name in charts]
                  + [['events']])
        return {
            'layout': layout,
            'cards': self.cards(),
            'charts': charts,
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
