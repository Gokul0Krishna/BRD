
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_LINE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \[ALPM\] "
    r"(?P<action>installed|upgraded|downgraded|reinstalled|removed) "
    r"(?P<pkg>\S+) \((?P<ver>[^)]*)\)\s*$"
)

@dataclass(frozen=True)
class PackageEvent:
    """One package change. Empty string means "not applicable" (e.g. no old version)."""

    ts: int  # unix seconds
    action: str  # installed | upgraded | downgraded | reinstalled | removed
    package: str
    old_version: str = ""
    new_version: str = ""

def parse_timestamp(raw: str) -> int:
    """Parse pacman's two timestamp styles into unix seconds.

    Modern pacman: 2026-09-14T21:03:11+0530
    Old pacman:    2016-01-01 12:00   (no timezone -> interpreted as local time)
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"unrecognised pacman timestamp: {raw!r}")


def parse_line(line: str) -> PackageEvent | None:
    """Return a PackageEvent for an install/upgrade/remove line, else None."""
    m = _LINE.match(line.rstrip("\n"))
    if m is None:
        return None
    action, ver = m["action"], m["ver"]
    if action in ("upgraded", "downgraded"):
        old, sep, new = ver.partition(" -> ")
        if not sep:
            return None
    elif action == "installed":
        old, new = "", ver
    elif action == "reinstalled":
        old, new = ver, ver
    else:  # removed
        old, new = ver, ""
    try:
        ts = parse_timestamp(m["ts"])
    except ValueError:
        return None
    return PackageEvent(ts=ts, action=action, package=m["pkg"], old_version=old, new_version=new)


def parse_pacman_log(text: str) -> list[PackageEvent]:
    """Parse a whole pacman.log. Lines that are not package events are skipped."""
    events = []
    for line in text.splitlines():
        event = parse_line(line)
        if event is not None:
            events.append(event)
    return events