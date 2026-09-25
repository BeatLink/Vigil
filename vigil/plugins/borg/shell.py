"""Shell plumbing shared by every borg call: framing several calls into one command, reading them back apart, and naming what went wrong."""

import re
from typing import Any, Dict, List, Optional

from vigil.core.connectors.types import CmdResult


_POLL_BASE_DIR_VAR = "__vigil_poll_base"
_POLL_DEADLINE_VAR = "__vigil_poll_end"
_POLL_LEFT_FN = "__vigil_poll_left"
_POLL_RC_VAR = "__vigil_poll_rc"
_FRAME = "__VIGIL_BORG__"
_CUT_SHORT = "no output: the poll ended before this call ran"


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _redact(command: str) -> str:
    return re.sub(
        r"(BORG_PASS(?:PHRASE|COMMAND)=)('(?:[^']|'\\'')*'|\"[^\"]*\"|\S+)",
        r"\1*****",
        command,
    )


def _frame_line(index: int, stream: str, code) -> str:
    """The marker that separates one poll call's captured output from the next."""
    return f"{_FRAME} {index} {stream} {code}"


def _split_poll(result: CmdResult, count: int) -> List[CmdResult]:
    """Splits a sequential poll's framed transcript back into one CmdResult per borg call."""
    streams: Dict[int, Dict[str, str]] = {}
    codes: Dict[int, int] = {}
    key = None
    buf: List[str] = []

    def flush() -> None:
        if key is not None:
            streams.setdefault(key[0], {})[key[1]] = "\n".join(buf).strip()

    for line in (result.stdout or "").splitlines():
        if not line.startswith(_FRAME + " "):
            buf.append(line)
            continue
        flush()
        buf = []
        fields = line.split()
        try:
            index, stream, code = int(fields[1]), fields[2], int(fields[3])
        except (IndexError, ValueError):
            key = None
            continue
        key = (index, stream)
        codes[index] = code
    flush()

    # No frame arrived at all, so the shell or sudo failed before the first borg call and the whole transcript is that failure.
    if not codes:
        return [result] + [CmdResult(result.exit_code, "", result.stderr)] * (count - 1)

    results = []
    for index in range(count):
        if index not in codes:
            results.append(CmdResult(result.exit_code or 1, "", _CUT_SHORT))
            continue
        captured = streams.get(index, {})
        results.append(CmdResult(codes[index], captured.get('out', ''), captured.get('err', '')))
    return results


def _failure_hint(stderr: str) -> Optional[str]:
    text = (stderr or "").lower()
    if "permission denied (publickey)" in text or "publickey" in text:
        return ("Hint: borg could not authenticate to the repo server — set "
                "`ssh_key` to a private key on that host which the borg server "
                "authorizes (borg makes its own SSH connection, so Vigil's own "
                "login key does not apply).")
    if "command not found" in text:
        return ("Hint: the borg binary is not on PATH for that user — under sudo "
                "it must be on root's PATH too (set `borg_bin` to an absolute path).")
    if "a password is required" in text or "sudo: a terminal is required" in text:
        return ("Hint: sudo needs a password — grant the SSH user passwordless "
                "sudo for borg (NOPASSWD).")
    if "not allowed to set the following environment variables" in text:
        return ("Hint: sudoers forbids setting BORG_PASSPHRASE — the rule needs "
                "the SETENV tag to pass the passphrase through sudo.")
    if "passphrase" in text or "not a valid repository" in text:
        return ("Hint: the repo is encrypted and the passphrase was missing or "
                "wrong — check `passphrase_file` / `passphrase_command`.")
    if "permission denied" in text:
        return ("Hint: the SSH user cannot read the repo — add it to the repo's "
                "group or set `require_sudo: true`.")
    if "does not exist" in text or "no such file" in text:
        return "Hint: the `repo` path does not exist on that host."
    if "failed to create/acquire the lock" in text:
        # borg locks its local chunks cache under <base dir>/.cache/borg as well as the repo, and the two mean different things.
        if "/.cache/borg/" in text:
            return ("Hint: the lock is on borg's local chunks cache on this host, "
                    "not on the repo — another borg process is using the same "
                    "`cache_dir`. Give it a cache_dir of its own, or raise "
                    "`lock_wait` past the time that process needs.")
        return ("Hint: the repo is locked by another borg process — a backup may "
                "be running.")
    return None


def _list_unavailable(exit_code: int, detail: str) -> bool:
    """True when the listing could not be gathered at all, as opposed to borg reporting a repo problem."""
    text = detail.lower()
    # A lock borg gave up waiting for says only that something else held the repo or its cache, which leaves the archives unread rather than bad.
    return (exit_code == -1 or "command not found" in text
            or "permission denied" in text
            or "failed to create/acquire the lock" in text)


def _archive_member(path: str) -> Optional[str]:
    """A path as borg stores it inside an archive, or None when it could climb out of the restore dir."""
    member = (path or '').strip().strip('/')
    if any(part in ('..', '.') for part in member.split('/')):
        return None
    return member
