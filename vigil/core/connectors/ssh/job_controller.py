"""Detached-job helpers.

A long-running job (a borg backup) is no longer a live SSH streaming channel
held open for hours. It is launched *detached on the target* with one ordinary
SSH command, then advanced by ordinary polling commands (the same
`fetch_output` path collection uses) on the owning plugin's normal monitor
cycle. Nothing lives in a Vigil-side coroutine, so a job survives a Vigil
restart and is re-adopted by polling.

This module holds only pure shell-command builders and result parsers plus a
thin DB coordinator. It performs no IO itself — the engine runs the strings it
builds through the SSH connector, exactly like any other command.
"""

import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Marker lines separating the sections of a single poll command's output, so
# one round-trip returns size + exit code + liveness + new output.
_SIZE = "===VIGIL_SIZE==="
_EXIT = "===VIGIL_EXIT==="
_ALIVE = "===VIGIL_ALIVE==="
_OUT = "===VIGIL_OUT==="


def workdir_for(token: str) -> str:
    """Per-job working dir on the target, keyed by an opaque token the plugin
    picks (it names the dir before the Job row's id exists)."""
    safe = "".join(c for c in token if c.isalnum() or c in "-_.")
    return f"$HOME/.cache/vigil/jobs/{safe}"


def launch_command(command: str, workdir: str) -> str:
    """Build the one detached command whose stdout is the remote PID.

    The job's stdout+stderr go to `out`; its exit status is written to `exit`
    on completion. `setsid`/`&` detach it from this SSH channel so it keeps
    running after the channel closes. `command` is embedded verbatim (it is
    already a fully-built, quoted shell command from the plugin)."""
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    inner = f'{{ {command}; }} > "$d/out" 2>&1; echo $? > "$d/exit"'
    return (
        f'd={d}; mkdir -p "$d"; : > "$d/out"; rm -f "$d/exit"; '
        f'setsid sh -c {shlex.quote(inner)} < /dev/null > /dev/null 2>&1 & '
        f'echo $!'
    )


def parse_launch(stdout: str) -> Optional[int]:
    """Extract the remote PID from launch_command()'s output."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    try:
        return int(line)
    except ValueError:
        return None


def poll_command(workdir: str, pid: int, offset: int) -> str:
    """One command returning the job's output size, exit code (empty if still
    running), liveness, and any output beyond `offset` bytes."""
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    start = offset + 1  # tail -c is 1-indexed
    return (
        f'd={d}; '
        f'echo {_SIZE}; wc -c < "$d/out" 2>/dev/null || echo 0; '
        f'echo {_EXIT}; cat "$d/exit" 2>/dev/null; '
        f'echo {_ALIVE}; kill -0 {int(pid)} 2>/dev/null && echo 1 || echo 0; '
        f'echo {_OUT}; tail -c +{start} "$d/out" 2>/dev/null'
    )


@dataclass(frozen=True)
class PollResult:
    size: int
    exit_code: Optional[int]
    alive: bool
    new_output: str            # bytes of the output file past the poll offset


def parse_poll(stdout: str) -> PollResult:
    sections = {'size': [], 'exit': [], 'alive': [], 'out': []}
    current = None
    for line in stdout.split("\n"):
        if line == _SIZE:
            current = 'size'; continue
        if line == _EXIT:
            current = 'exit'; continue
        if line == _ALIVE:
            current = 'alive'; continue
        if line == _OUT:
            current = 'out'; continue
        if current is None:
            continue
        sections[current].append(line)

    def _int(lines: list) -> Optional[int]:
        s = "".join(lines).strip()
        return int(s) if s.lstrip("-").isdigit() else None

    # Rejoin the output section with \n so a trailing partial line (no newline
    # on the target yet) stays partial — split_lines then leaves it unconsumed.
    return PollResult(
        size=_int(sections['size']) or 0,
        exit_code=_int(sections['exit']),
        alive=("".join(sections['alive']).strip() == '1'),
        new_output="\n".join(sections['out']),
    )


def cancel_command(pid: int) -> str:
    """Terminate the detached job, then force-kill after a short grace. Best
    effort — mirrors the old terminate()->kill() escalation."""
    p = int(pid)
    return f'kill {p} 2>/dev/null; sleep 2; kill -9 {p} 2>/dev/null; true'


def cleanup_command(workdir: str) -> str:
    d = shlex.quote(workdir) if not workdir.startswith("$") else f'"{workdir}"'
    return f'rm -rf {d}'


def split_lines(new_output: str) -> Tuple[List[str], int]:
    """Split a poll's new bytes into whole lines to append as JobOutput,
    returning (lines, consumed_byte_count). A trailing partial line (no
    newline yet) is left unconsumed so the next poll completes it."""
    if not new_output:
        return [], 0
    consumed = new_output.rfind("\n")
    if consumed == -1:
        return [], 0
    complete = new_output[:consumed]
    lines = complete.split("\n")
    return lines, consumed + 1
