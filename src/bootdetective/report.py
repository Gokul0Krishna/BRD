"""Turn results into text. Pure formatting: no I/O except plot_history() writing an image."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from bootdetective.analysis.changepoints import Changepoint
from bootdetective.analysis.ranking import Suspect
from bootdetective.db import BootRecord
from bootdetective.parsers.pacman_log import PackageEvent

_BARS = "▁▂▃▄▅▆▇█"


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}min {seconds % 60:.0f}s"


def format_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def sparkline(values: Sequence[float], width: int = 60) -> str:
    """A one-line chart. Long series are thinned to `width` points."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(
        _BARS[min(len(_BARS) - 1, int((v - lo) / span * (len(_BARS) - 1) + 0.5))] for v in values
    )


def _version_change(old: str, new: str) -> str:
    if old and new and old != new:
        return f"{old} -> {new}"
    return new or old


def format_history(boots: Sequence[BootRecord], last: int = 20) -> str:
    """A table of the most recent boots. Numbers match `explain`."""
    start = max(0, len(boots) - last)
    lines = [f"{'#':>4}  {'finished':<16}  {'total':>8}  {'kernel':>7}  {'userspace':>9}  source"]
    for i, b in enumerate(boots[start:], start=start + 1):
        t = b.timing
        lines.append(
            f"{i:>4}  {format_time(b.finished_at):<16}  {format_seconds(t.total):>8}  "
            f"{format_seconds(t.kernel):>7}  {format_seconds(t.userspace):>9}  {b.source}"
        )
    lines.append("")
    lines.append("trend: " + sparkline([b.timing.total for b in boots]))
    return "\n".join(lines)


def format_changepoint(
    cp: Changepoint,
    boots: Sequence[BootRecord],
    slow: Mapping[str, float],
    suspects: Sequence[Suspect],
    *,
    lookback: int = 1,
    top: int = 3,
) -> str:
    boot = boots[cp.index]
    word = "rose" if cp.shift > 0 else "fell"
    lines = [
        f"Boot time {word} from ~{cp.before:.1f}s to ~{cp.after:.1f}s "
        f"({cp.shift:+.1f}s, {cp.shift_rel:+.0%}) at boot #{cp.index + 1} "
        f"({format_time(boot.finished_at)})"
    ]
    if slow:
        lines.append("  Units that got slower:")
        for unit, delta in list(slow.items())[:5]:
            lines.append(f"    {unit:<44} {delta:+.1f}s")
    else:
        lines.append("  (no per-unit data around this change, so units can't be compared)")

    first = max(0, cp.index - lookback) + 1
    if suspects:
        lines.append(
            f"  Packages changed between boot #{first} and #{cp.index + 1}, best suspect first:"
        )
        for rank, s in enumerate(suspects[:top], start=1):
            lines.append(
                f"    {rank}. {s.package} {_version_change(s.old_version, s.new_version)}  "
                f"(score {s.score:.1f})"
            )
            for reason in s.reasons:
                lines.append(f"         - {reason}")
        if len(suspects) > top:
            lines.append(f"    ... and {len(suspects) - top} more")
    else:
        lines.append(f"  No packages changed between boot #{first} and #{cp.index + 1}.")
    return "\n".join(lines)


def format_explain(
    number: int,
    boot: BootRecord,
    units: Mapping[str, float],
    events: Sequence[PackageEvent],
    previous_number: int | None,
) -> str:
    t = boot.timing
    parts = [
        ("firmware", t.firmware),
        ("loader", t.loader),
        ("kernel", t.kernel),
        ("initrd", t.initrd),
        ("userspace", t.userspace),
    ]
    breakdown = " + ".join(f"{format_seconds(v)} {name}" for name, v in parts if v is not None)
    lines = [
        f"Boot #{number}  {format_time(boot.finished_at)}  ({boot.source})",
        f"  total {format_seconds(t.total)}" + (f" = {breakdown}" if breakdown else ""),
    ]
    if units:
        lines.append("  Slowest units:")
        for unit, secs in sorted(units.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            lines.append(f"    {unit:<44} {format_seconds(secs)}")
    else:
        lines.append("  (no per-unit data: this boot was recovered from the journal)")

    if previous_number is None:
        lines.append("  This is the first recorded boot.")
    elif events:
        lines.append(f"  Packages changed since boot #{previous_number}:")
        for e in events[:15]:
            lines.append(
                f"    {e.action:<11} {e.package} {_version_change(e.old_version, e.new_version)}"
            )
        if len(events) > 15:
            lines.append(f"    ... and {len(events) - 15} more")
    else:
        lines.append(f"  No packages changed since boot #{previous_number}.")
    return "\n".join(lines)


def plot_history(
    boots: Sequence[BootRecord], changepoints: Sequence[Changepoint], path: Path
) -> None:
    """Save a PNG of boot times with changepoints marked. Needs the optional matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    totals = [b.timing.total for b in boots]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(range(1, len(totals) + 1), totals, marker="o", markersize=3, linewidth=1)
    for cp in changepoints:
        ax.axvline(cp.index + 1, color="red", linestyle="--", alpha=0.7)
        ax.text(cp.index + 1, max(totals), f" {cp.shift:+.1f}s", color="red", va="top")
    ax.set_xlabel("boot #")
    ax.set_ylabel("total boot time (s)")
    ax.set_title("Boot time history")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
