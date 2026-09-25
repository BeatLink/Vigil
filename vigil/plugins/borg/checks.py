"""The restore canary verdict and the check units' journal, read and folded into one record per unit."""

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from vigil.core.connectors.types import CmdResult
from vigil.plugins.borg.shell import _CUT_SHORT, _list_unavailable

# systemd's own lines for a unit run; borgmatic also logs messages starting "Finished", so these are read from PID 1 only.
_UNIT_FINISHED, _UNIT_FAILED = 'Finished ', 'Failed with result'

# A check unit's previous run is looked for at most this far before its outcome when its start has scrolled out of the journal.
_CHECK_RUN_WINDOW = 86400


def _journal_epoch(line: str) -> Optional[int]:
    """Read the epoch off a journalctl `-o short-unix` line."""
    stamp = line.split(' ', 1)[0] if line else ''
    try:
        return int(float(stamp))
    except ValueError:
        return None


def _journal_message(line: str) -> str:
    """Strip the timestamp, host and process prefix off a journalctl line."""
    _, _, message = line.partition(': ')
    return (message or line).strip()[:300]


def _canary_verdict(extracted: CmdResult, live: CmdResult) -> Tuple[Optional[bool], str]:
    """Pure: (True/False, detail) once the restore check reached a verdict, (None, reason) when it could not run."""
    detail = (extracted.stderr or extracted.stdout or '').strip()
    if extracted.exit_code != 0:
        if 'never matched' in detail:
            return False, "the canary is not in the newest archive — is it inside the backup set?"
        if detail == _CUT_SHORT or _list_unavailable(extracted.exit_code, detail):
            return None, f"could not extract the canary: {detail[:200]}"
        return False, f"extracting the canary failed (exit {extracted.exit_code}): {detail[:200]}"
    if live.exit_code != 0:
        return False, f"the live canary is unreadable: {(live.stderr or live.stdout).strip()[:200]}"
    if extracted.stdout.strip() != live.stdout.strip():
        return False, "the restored canary differs from the live file"
    return True, "the restored canary matches the live file"


def _check_unit_script(units: List[str]) -> str:
    """One brace group printing, per unit, its last success, last outcome, last start and any borgmatic command failures."""
    journal = "journalctl -o short-unix --no-pager -q"
    parts = []
    for unit in units:
        u = shlex.quote(unit)
        parts += [
            f'echo "unit={unit}"',
            f"echo \"ok=$({journal} -u {u} _PID=1 -g '^{_UNIT_FINISHED}' -n 1 2>/dev/null | grep -v '^-- ' | tail -1)\"",
            f"echo \"end=$({journal} -u {u} _PID=1 -g '^{_UNIT_FINISHED}|{_UNIT_FAILED}' -n 1 2>/dev/null | grep -v '^-- ' | tail -1)\"",
            f"echo \"start=$({journal} -u {u} _PID=1 -g '^Starting ' -n 1 2>/dev/null | grep -v '^-- ' | tail -1)\"",
            f"{journal} -u {u} -g 'returned non-zero exit status' -n 20 2>/dev/null | grep -v '^-- ' | sed 's/^/err=/'",
        ]
    return "{ " + "; ".join(parts) + "; }"


def _parse_check_units(stdout: str) -> Dict[str, Dict[str, Any]]:
    """Group the check script's key=value lines by unit."""
    units: Dict[str, Dict[str, Any]] = {}
    current = None
    for line in (stdout or '').splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        if key == 'unit':
            current = units.setdefault(value.strip(), {'err': []})
        elif current is not None and key == 'err':
            current['err'].append(value.strip())
        elif current is not None:
            current[key] = value.strip()
    return units


def _observed_unit(fields: Dict[str, Any], repo: str) -> Dict[str, Any]:
    """Pure: one unit's last success and last outcome, where a failed run that names only other repositories counts as a success for this one."""
    ok = _journal_epoch(fields.get('ok', ''))
    end_line = fields.get('end', '')
    end = _journal_epoch(end_line)
    start = _journal_epoch(fields.get('start', ''))
    state = {'ok': ok, 'end': end, 'failed': False, 'error': None}
    if end is None or _UNIT_FAILED not in end_line:
        return state

    since = start if start is not None and start <= end else end - _CHECK_RUN_WINDOW
    run_errors = [e for e in fields.get('err', []) if since <= (_journal_epoch(e) or -1) <= end]
    names_repo = re.compile(r"\s" + re.escape(repo) + r"(?:::\S*)?'")
    ours = [e for e in run_errors if names_repo.search(e)]
    if run_errors and not ours:
        state['ok'] = max(ok or 0, end)
        return state
    state['failed'] = True
    state['error'] = _journal_message(ours[-1]) if ours else _journal_message(end_line)
    return state


def _merge_unit(stored: Dict[str, Any], seen: Dict[str, Any]) -> Dict[str, Any]:
    """Fold this poll's reading into the stored one, so a rotated journal forgets nothing already seen."""
    merged = dict(stored)
    oks = [v for v in (stored.get('ok'), seen.get('ok')) if v]
    merged['ok'] = max(oks) if oks else None
    if (seen.get('end') or 0) >= (stored.get('end') or 0):
        merged.update(end=seen.get('end'), failed=seen.get('failed', False), error=seen.get('error'))
    return merged
