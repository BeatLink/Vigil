"""The dashboard's Halon theme.

Colors live in ``static/halon-tokens.css`` (Layer 1) and component rules in
``static/halon.css`` (Layer 2); nothing here or in any other module may state a
literal color. Python call sites that write inline styles reference the tokens
by name — ``TEXT``, ``ACCENT``, ``STATUS_COLORS[...]`` are ``var(--token)``
strings the browser resolves per scheme, so light and dark need no Python
branch.

ECharts is the one exception: it paints to a canvas and cannot read custom
properties, so ``palette()`` resolves the same token names to the literals of
the scheme the client is actually in, parsed out of the token sheet at import.
Charts register with ``on_scheme_change`` and repaint when the client's scheme
flips.
"""

import re
from pathlib import Path
from typing import Callable, Dict, List, Tuple

_STATIC = Path(__file__).parent / 'static'
_TOKENS_CSS = _STATIC / 'halon-tokens.css'
_COMPONENTS_CSS = _STATIC / 'halon.css'

# Tokens as CSS references, for the inline styles NiceGUI call sites write.
ACCENT            = 'var(--accent)'
SURFACE           = 'var(--surface-default)'
SURFACE_SECONDARY = 'var(--surface-secondary)'
SURFACE_ROOT      = 'var(--surface-root)'
TEXT_HEADING      = 'var(--text-heading)'
TEXT              = 'var(--text-body)'
TEXT_SECONDARY    = 'var(--text-secondary)'
TEXT_TERTIARY     = 'var(--text-tertiary)'
TEXT_ON_FILL      = 'var(--text-on-fill)'
BORDER            = 'var(--border-default)'

# A monitor's state is drawn as a mark — a dot, a word, a table cell — never as
# a fill, so warning takes --status-warning-text rather than the bright fill
# value it would be illegible as (§3.6).
STATUS_COLORS = {
    'online':  'var(--status-success)',
    'warning': 'var(--status-warning-text)',
    'failed':  'var(--status-danger)',
    'offline': 'var(--text-tertiary)',
}

_STATUS_TOKENS = {
    'online':  'status-success',
    'warning': 'status-warning-text',
    'failed':  'status-danger',
    'offline': 'text-tertiary',
}

# Config keys are kept from Vigil's pre-Halon theme block and mapped onto the
# tokens they correspond to; an override applies to both schemes.
_CONFIG_TOKENS = {
    'primary':          'accent',
    'background':       'surface-default',
    'background_muted': 'surface-root',
    'text':             'text-body',
    'text_muted':       'text-secondary',
    'status_online':    'status-success',
    'status_warning':   'status-warning-text',
    'status_failed':    'status-danger',
    'status_offline':   'text-tertiary',
}

_overrides: Dict[str, str] = {}
_forced_scheme: str = 'auto'


def configure(cfg: dict) -> None:
    """Apply the ``theme:`` block of config.yaml. Each recognised key overrides
    one token in both schemes, so an override that only suits one of them is
    the operator's call to audit."""
    global _forced_scheme
    for key, value in (cfg or {}).items():
        if key == 'scheme':
            _forced_scheme = str(value)
        elif key in _CONFIG_TOKENS:
            _overrides[_CONFIG_TOKENS[key]] = str(value)


def forced_scheme() -> str:
    """The scheme ``theme.scheme`` pins every client to, or 'auto'."""
    return _forced_scheme


def _parse_token_sheet() -> Tuple[Dict[str, str], Dict[str, str]]:
    """The light and dark token tables, read out of Layer 1 so no literal is
    restated in Python. Dark is the light table with its block applied over."""
    css = _TOKENS_CSS.read_text()

    def block(selector: str) -> Dict[str, str]:
        match = re.search(re.escape(selector) + r'\s*\{(.*?)\n\}', css, re.S)
        if not match:
            return {}
        return {
            name: value.strip()
            for name, value in re.findall(r'--([\w-]+):\s*([^;]+);', match.group(1))
        }

    light = block(':root')
    dark = {**light, **block(':root[data-theme="dark"]')}
    return light, dark


def _resolve(table: Dict[str, str]) -> Dict[str, str]:
    """Flatten the one level of var() indirection the token sheet uses."""
    resolved = {}
    for name, value in table.items():
        seen = 0
        while value.startswith('var(--') and seen < 4:
            value = table.get(value[6:-1], value)
            seen += 1
        resolved[name] = value
    return resolved


_LIGHT, _DARK = (_resolve(table) for table in _parse_token_sheet())


def palette(scheme: str = 'light') -> Dict[str, str]:
    """Literal values for the canvas-painted charts, in the client's scheme."""
    table = dict(_DARK if scheme == 'dark' else _LIGHT)
    table.update(_overrides)
    colors = {key: table[token] for key, token in _STATUS_TOKENS.items()}
    colors.update(
        accent=table['accent'],
        surface=table['surface-default'],
        border=table['border-default'],
        text=table['text-body'],
        text_secondary=table['text-secondary'],
        text_on_fill=table['text-on-fill'],
    )
    return colors


class _SchemeState:
    """One client's color scheme and the charts that repaint when it flips."""

    def __init__(self) -> None:
        self.scheme = _forced_scheme if _forced_scheme in ('light', 'dark') else 'light'
        self.listeners: List[Callable[[Dict[str, str]], None]] = []


def _state() -> _SchemeState:
    from nicegui import context
    client = context.client
    state = getattr(client, '_halon_scheme', None)
    if state is None:
        state = _SchemeState()
        client._halon_scheme = state
    return state


def scheme() -> str:
    """Return the current client's color scheme name."""
    return _state().scheme


def current_palette() -> Dict[str, str]:
    """Return the token palette for the current client's scheme."""
    return palette(scheme())


def on_scheme_change(callback: Callable[[Dict[str, str]], None]) -> None:
    """Register a chart repaint. Called once now with the current palette, and
    again whenever the browser reports a different scheme."""
    state = _state()
    state.listeners.append(callback)
    callback(palette(state.scheme))


def _set_scheme(value: str) -> None:
    state = _state()
    if value not in ('light', 'dark') or value == state.scheme:
        return
    state.scheme = value
    colors = palette(value)
    for listener in list(state.listeners):
        try:
            listener(colors)
        except RuntimeError:
            state.listeners.remove(listener)


# The client reports its scheme so charts can be painted in it; a forced scheme
# is written onto <html>, where the token sheet's [data-theme] blocks win over
# the system preference in both directions.
_SCHEME_SCRIPT = '''
<script>
    (() => {
        const forced = "%s";
        if (forced === "light" || forced === "dark") {
            document.documentElement.dataset.theme = forced;
        }
        const query = window.matchMedia("(prefers-color-scheme: dark)");
        const report = () => {
            if (!window.emitEvent) return setTimeout(report, 100);
            emitEvent("halon_scheme",
                      document.documentElement.dataset.theme || (query.matches ? "dark" : "light"));
        };
        query.addEventListener("change", report);
        report();
    })();
</script>
'''


def override_css() -> str:
    """The ``theme:`` block's token overrides as a CSS rule, or '' if there are none."""
    if not _overrides:
        return ''
    lines = ''.join(f'    --{name}: {value};\n' for name, value in _overrides.items())
    return f':root, :root[data-theme="light"], :root[data-theme="dark"] {{\n{lines}}}\n'


_head_installed = False


def install() -> None:
    """Load the theme into the current page and start reporting its scheme.
    The sheets and the reporting script are shared head content, which NiceGUI
    accumulates process-wide, so they are added on the first page only."""
    global _head_installed
    from nicegui import ui

    if not _head_installed:
        ui.add_css(_TOKENS_CSS.read_text(), shared=True)
        ui.add_css(_COMPONENTS_CSS.read_text(), shared=True)
        override = override_css()
        if override:
            ui.add_css(override, shared=True)
        ui.add_head_html(_SCHEME_SCRIPT % _forced_scheme, shared=True)
        _head_installed = True

    ui.on('halon_scheme', lambda event: _set_scheme(event.args))
    ui.colors(
        primary=ACCENT, secondary=TEXT_SECONDARY, accent=ACCENT,
        positive=STATUS_COLORS['online'], negative=STATUS_COLORS['failed'],
        warning='var(--status-warning)', info=ACCENT,
        dark=SURFACE, dark_page=SURFACE_ROOT,
    )
