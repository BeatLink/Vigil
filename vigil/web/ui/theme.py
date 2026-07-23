"""
Centralized theme and styling configuration for the Vigil UI.

Module-level constants are the defaults. Call configure() once at startup
(before any UI module is imported) to apply values from config.yaml.
"""

# Colors
PRIMARY          = "#00ACFF"
ACCENT           = "#FF5500"
BACKGROUND_MUTED = "#FAFAFA"
BACKGROUND       = "#FFFFFF"
TEXT             = "#111827"
TEXT_MUTED       = "#6B7280"

# Dict is mutated in-place by configure() so references held by already-imported
# modules will automatically see the updated values.
STATUS_COLORS = {
    'online':  "lime",
    'warning': "gold",
    'failed':  "red",
    'offline': "lightgray",
}


def configure(cfg: dict) -> None:
    """Apply theme overrides from the config file's 'theme:' section."""
    import vigil.web.ui.theme as _m
    if 'primary'          in cfg: _m.PRIMARY          = cfg['primary']
    if 'accent'           in cfg: _m.ACCENT            = cfg['accent']
    if 'background'       in cfg: _m.BACKGROUND        = cfg['background']
    if 'background_muted' in cfg: _m.BACKGROUND_MUTED  = cfg['background_muted']
    if 'text'             in cfg: _m.TEXT               = cfg['text']
    if 'text_muted'       in cfg: _m.TEXT_MUTED         = cfg['text_muted']
    # STATUS_COLORS is a shared dict — mutate in-place so all holders see the change
    if 'status_online'    in cfg: STATUS_COLORS['online']  = cfg['status_online']
    if 'status_warning'   in cfg: STATUS_COLORS['warning'] = cfg['status_warning']
    if 'status_failed'    in cfg: STATUS_COLORS['failed']  = cfg['status_failed']
    if 'status_offline'   in cfg: STATUS_COLORS['offline'] = cfg['status_offline']
