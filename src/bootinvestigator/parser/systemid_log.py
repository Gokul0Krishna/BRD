"""Parse systemd-analyze and journal output.

Pure functions only: text goes in, records come out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_TOKEN = r"\d+(?:\.\d+)?\s*(?:µs|us|ms|min|h|d|s)"
_SPAN_FULL = re.compile(rf"(?:{_TOKEN}\s*)+")
_SPAN_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(µs|us|ms|min|h|d|s)")
_UNIT_SECONDS = {
    "µs": 1e-6,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}

_PHASES = ("firmware", "loader", "kernel", "initrd", "userspace")


@dataclass(frozen=True)
class BootTiming:
    """How long each boot phase took, in seconds. Phases the machine lacks are None."""

    total: float
    firmware: float | None = None
    loader: float | None = None
    kernel: float | None = None
    initrd: float | None = None
    userspace: float | None = None


@dataclass(frozen=True)
class JournalBoot:
    """A past boot recovered from the journal's "Startup finished" message."""

    boot_id: str
    finished_at: int  # unix seconds
    timing: BootTiming


def normalize_boot_id(raw: str) -> str:
    """/proc uses dashes, the journal doesn't. Store one canonical form: 32 lowercase hex."""
    return raw.strip().replace("-", "").lower()


def parse_timespan(text: str) -> float:
    """'1min 3.2s' -> 63.2, '845ms' -> 0.845. Raises ValueError on anything else."""
    text = text.strip()
    if not _SPAN_FULL.fullmatch(text):
        raise ValueError(f"not a systemd timespan: {text!r}")
    return sum(float(v) * _UNIT_SECONDS[u] for v, u in _SPAN_PART.findall(text))


def parse_startup_finished(text: str) -> BootTiming | None:
    """Parse the 'Startup finished in ...' line (from `systemd-analyze time` or the journal).

    Handles machines without firmware/loader/initrd phases. Returns None if the text has no
    system-manager startup line (the *user* manager logs a phase-less variant; it is ignored).
    """
    m = re.search(r"Startup finished in ([^\n]+)", text)
    if m is None:
        return None
    body = m.group(1).strip().rstrip(".").strip()
    left, _, right = body.partition("=")

    phases: dict[str, float] = {}
    for piece in left.split("+"):
        pm = re.fullmatch(r"\s*(?P<span>.+?)\s*\((?P<phase>\w+)\)\s*", piece)
        if pm is None or pm["phase"] not in _PHASES:
            continue
        try:
            phases[pm["phase"]] = parse_timespan(pm["span"])
        except ValueError:
            continue
    if not phases:
        return None

    try:
        total = parse_timespan(right) if right.strip() else sum(phases.values())
    except ValueError:
        total = sum(phases.values())
    return BootTiming(total=total, **phases)


def parse_blame(text: str) -> dict[str, float]:
    """Parse `systemd-analyze blame` text: '1min 3.2s foo.service' -> {'foo.service': 63.2}."""
    units: dict[str, float] = {}
    for line in text.splitlines():
        span, _, unit = line.strip().rpartition(" ")
        if not span or not unit:
            continue
        try:
            units[unit] = parse_timespan(span)
        except ValueError:
            continue
    return units


def parse_blame_json(text: str) -> dict[str, float]:
    """Parse `systemd-analyze --json=short blame` ([{"time": usec, "unit": name}, ...]).

    Raises ValueError if the shape is unexpected, so callers can fall back to the text parser.
    """
    data = json.loads(text)  # JSONDecodeError is a ValueError
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")
    units: dict[str, float] = {}
    try:
        for item in data:
            units[str(item["unit"])] = float(item["time"]) / 1e6
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unexpected blame JSON shape: {exc}") from exc
    return units


def parse_journal_startup(text: str) -> list[JournalBoot]:
    """Parse `journalctl -o json` lines carrying the 'Startup finished' message.

    One entry per boot (the first one wins). Entries from the user manager (_PID != 1)
    and unparseable lines are skipped.
    """
    seen: dict[str, JournalBoot] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("MESSAGE")
        boot_id = entry.get("_BOOT_ID")
        realtime = entry.get("__REALTIME_TIMESTAMP")
        if not isinstance(message, str) or not boot_id or not realtime:
            continue
        if str(entry.get("_PID", "1")) != "1":
            continue
        timing = parse_startup_finished(message)
        if timing is None:
            continue
        try:
            finished_at = int(realtime) // 1_000_000
        except ValueError:
            continue
        bid = normalize_boot_id(boot_id)
        seen.setdefault(bid, JournalBoot(bid, finished_at, timing))
    return list(seen.values())
