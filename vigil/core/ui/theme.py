"""
Centralized theme and styling configuration for the Vigil UI.
"""

# Colors


PRIMARY          = "#00ACFF"
ACCENT           = "FF5500"
BACKGROUND_MUTED       = "#FAFAFA"
BACKGROUND          = "#FFFFFF"
TEXT    = "#111827"
TEXT_MUTED      = "#6B7280"
STATUS_COLORS = {
    'online':  "lime",
    'warning': "gold",
    'failed':  "red",
    'offline': "gray",
}


# ── Status severity (for aggregation logic) ─────────────
STATUS_SEVERITY = {
    'online':  0,
    'offline': 1,
    'warning': 2,
    'failed':  3,
}

# Logic for status aggregation
SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}
