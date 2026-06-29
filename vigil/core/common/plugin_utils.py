"""
Shared utility functions used across multiple plugins.
"""


def level_for(value: float, warning: float, threshold: float) -> str:
    """Map a metric value to a status level string using warning/threshold bounds."""
    if value >= threshold:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def format_bytes(gb: float) -> str:
    """Format a GB float as a human-readable string (MB / GB / TB)."""
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"
