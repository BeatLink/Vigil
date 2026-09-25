"""Pure readers for borg's JSON and text output."""

import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

_PRUNE_LINE = re.compile(
    r'^(?:Keeping archive \(rule: (?P<rule>[^)]+)\)|(?P<prune>Would prune)):\s+'
    r'(?P<name>\S+)\s+(?P<date>.+?)\s+\[[0-9a-f]+\]\s*$')


def _format_size(size: float) -> str:
    """Formats a raw byte count, unlike plugin_helpers.format_bytes which takes GB."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(size) < 1024.0 or unit == 'TB':
            return f"{size:.1f} {unit}" if unit != 'B' else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def _parse_archive_time(value: str) -> int:
    if not value:
        return 0
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    return int(dt.timestamp())


def _decode_json(stdout: str) -> Dict[str, Any]:
    """Decodes a borg --json payload, returning {} when it is not a JSON object."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class RepoView:
    """Decoded `borg list --json` output shared by the parse helpers."""
    valid: bool = False
    raw_count: int = 0
    newest_epoch: int = 0
    archives: List[Dict[str, Any]] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_stdout(cls, stdout: str) -> 'RepoView':
        """Parses the payload once, returning an invalid view on malformed output."""
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        raw = data.get('archives') or []
        if not isinstance(raw, list):
            return cls()

        archives = []
        for archive in raw:
            if not isinstance(archive, dict):
                continue
            archives.append({
                'name': archive.get('name') or archive.get('archive') or '?',
                'epoch': _parse_archive_time(
                    archive.get('start') or archive.get('time', '')
                ),
            })
        archives.sort(key=lambda a: a['epoch'], reverse=True)
        newest = max((a['epoch'] for a in archives if a['epoch'] > 0), default=0)

        info = {}
        repo = data.get('repository')
        if isinstance(repo, dict):
            info['location'] = repo.get('location') or ''
            info['last_modified'] = repo.get('last_modified') or ''
        enc = data.get('encryption')
        if isinstance(enc, dict):
            info['encryption'] = enc.get('mode') or ''

        return cls(valid=True, raw_count=len(raw), newest_epoch=newest,
                   archives=archives, info=info)


# Tab-separated with the path last, so a path containing a tab still reads back whole.
_LIST_FORMAT = "{type}\t{size}\t{isomtime}\t{path}{NL}"

# Keeps a folder's direct children and stands in one folder line for anything deeper, since borg stores no item for a parent it was not asked to back up.
_CHILDREN_AWK = r'''BEGIN { FS = "\t"; n = length(p) }
{
    path = $0
    for (k = 0; k < 3; k++) path = substr(path, index(path, "\t") + 1)
    if (substr(path, 1, n) != p) next
    rest = substr(path, n + 1)
    if (rest == "") next
    i = index(rest, "/")
    if (i == 0) { print; next }
    d = substr(rest, 1, i - 1)
    if (!(d in seen)) { seen[d] = 1; print "d\t\t\t" p d }
}'''


def _children_filter(member: str) -> str:
    """A shell filter reducing a `borg list` of `member` to that folder's direct children."""
    prefix = f"{member}/" if member else ""
    return f"awk -v p={shlex.quote(prefix)} {shlex.quote(_CHILDREN_AWK)}"


def _parse_listing(stdout: str) -> List[Dict[str, Any]]:
    """Pure: one entry per child, folders first, where a real item wins over a folder made up from a deeper path."""
    entries: Dict[str, Dict[str, Any]] = {}
    for line in (stdout or '').splitlines():
        fields = line.split('\t', 3)
        if len(fields) != 4 or not fields[3]:
            continue
        kind, size, mtime, path = fields
        name = path.rsplit('/', 1)[-1]
        if name in entries and not size:
            continue
        entries[name] = {
            'name': name, 'path': path, 'dir': kind == 'd', 'type': kind,
            'size': int(size) if size.isdigit() else None,
            'mtime': mtime.replace('T', ' ')[:16],
        }
    return sorted(entries.values(), key=lambda e: (not e['dir'], e['name'].casefold()))
