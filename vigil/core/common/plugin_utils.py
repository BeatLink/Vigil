

def level_for(value: float, warning: float, threshold: float) -> str:
    if value >= threshold:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def format_bytes(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"
