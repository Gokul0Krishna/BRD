"""Rank which package changes most likely caused a boot-time change.

Pure functions: the caller (cli.py) gathers events, unit timings, and unit ownership, so this
module can be tested with plain Python data.

Scoring is deliberately simple and transparent. Every point has a human-readable reason.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from bootdetective.parsers.pacman_log import PackageEvent

# Packages that run early in boot or sit in the boot path.
BOOT_CRITICAL = re.compile(
    r"^(linux(-(lts|zen|hardened|rt|firmware.*|api-headers))?|systemd(-.*)?|mkinitcpio(-.*)?"
    r"|dracut|grub|refind|efibootmgr|(amd|intel)-ucode|nvidia(-.*)?|mesa|cryptsetup|lvm2"
    r"|mdadm|btrfs-progs|e2fsprogs|xfsprogs|dbus(-.*)?|udev.*|networkmanager|iwd|dhcpcd)$"
)

SCORE_BASE = 1.0
SCORE_REMOVED = 0.5
SCORE_SLOW_UNIT = 5.0
SCORE_BOOT_CRITICAL = 2.0
SCORE_SHIPS_UNITS = 1.0
SCORE_UPSTREAM_BUMP = 0.5


@dataclass
class Suspect:
    package: str
    action: str
    old_version: str
    new_version: str
    score: float
    reasons: list[str] = field(default_factory=list)


def template_of(unit: str) -> str:
    """'getty@tty1.service' -> 'getty@.service'; non-instance units are returned unchanged."""
    name, dot, suffix = unit.rpartition(".")
    if dot and "@" in name:
        return name.split("@", 1)[0] + "@." + suffix
    return unit


def collapse_events(events: Sequence[PackageEvent]) -> list[PackageEvent]:
    """Merge repeated changes to one package into a single old -> new event."""
    first: dict[str, PackageEvent] = {}
    last: dict[str, PackageEvent] = {}
    for e in events:
        first.setdefault(e.package, e)
        last[e.package] = e
    merged = []
    for pkg, f in first.items():
        l_ = last[pkg]
        merged.append(PackageEvent(l_.ts, l_.action, pkg, f.old_version, l_.new_version))
    return merged


def upstream_version(version: str) -> str:
    """'1:2.3.4-2' -> '1:2.3.4' (drop the pkgrel, so a rebuild is not counted as a new release)."""
    return re.sub(r"-[^-]+$", "", version)


def slow_units(
    before: Sequence[Mapping[str, float]],
    after: Sequence[Mapping[str, float]],
    min_delta: float = 0.5,
) -> dict[str, float]:
    """Units that got slower: {unit: median_after - median_before}, biggest first.

    `before` / `after` are lists of {unit: seconds}, one per boot. A unit missing from a boot
    counts as 0s there (so brand-new units show up). Returns {} if either side has no data,
    which happens when boots were backfilled from the journal.
    """
    if not before or not after:
        return {}
    units = {u for boot in (*before, *after) for u in boot}
    deltas = {}
    for unit in units:
        b = statistics.median(boot.get(unit, 0.0) for boot in before)
        a = statistics.median(boot.get(unit, 0.0) for boot in after)
        if a - b >= min_delta:
            deltas[unit] = a - b
    return dict(sorted(deltas.items(), key=lambda kv: kv[1], reverse=True))


def rank_suspects(
    events: Sequence[PackageEvent],
    *,
    slow_units: Mapping[str, float] | None = None,
    shipped_units: Mapping[str, set[str]] | None = None,
) -> list[Suspect]:
    """Score each changed package, best suspect first.

    slow_units:    {unit: seconds slower} for units that regressed.
    shipped_units: {package: unit file names it ships}.
    """
    slow_units = slow_units or {}
    shipped_units = shipped_units or {}
    suspects = []
    for e in collapse_events(events):
        score = SCORE_REMOVED if e.action == "removed" else SCORE_BASE
        reasons = []

        shipped = shipped_units.get(e.package, set())
        hits = {u: d for u, d in slow_units.items() if u in shipped or template_of(u) in shipped}
        if hits:
            unit, delta = max(hits.items(), key=lambda kv: kv[1])
            score += SCORE_SLOW_UNIT
            reasons.append(f"ships {unit}, which got {delta:.1f}s slower")
        elif shipped:
            score += SCORE_SHIPS_UNITS
            reasons.append("ships systemd units")

        if BOOT_CRITICAL.match(e.package):
            score += SCORE_BOOT_CRITICAL
            reasons.append("part of the boot path (kernel, firmware, init, drivers, network)")

        if (
            e.old_version
            and e.new_version
            and upstream_version(e.old_version) != upstream_version(e.new_version)
        ):
            score += SCORE_UPSTREAM_BUMP
            reasons.append("new upstream version")

        suspects.append(Suspect(e.package, e.action, e.old_version, e.new_version, score, reasons))

    # Ties: prefer packages changed later (closer to the regression), then name.
    last_ts = {e.package: e.ts for e in events}
    suspects.sort(key=lambda s: (-s.score, -last_ts.get(s.package, 0), s.package))
    return suspects
