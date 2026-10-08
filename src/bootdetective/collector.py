"""The collector: the only place (besides cli.py) that talks to the operating system.

Everything here is I/O glue. The parsing it relies on lives in parsers/, which is pure and tested.
The collector must FAIL SOFT: a problem here may never slow or block boot, so cli.py catches
every exception and exits 0.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from bootdetective import db
from bootdetective.parsers import pacman_log, systemd
from bootdetective.parsers.systemd import BootTiming

LOG = logging.getLogger("bootdetective")

# systemd's catalog ID for the "Startup finished" journal message.
STARTUP_FINISHED_ID = "b07a249cd024414a82dd00cd181378ff"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")
MIN_UNIT_SECONDS = 0.05  # units faster than this are noise; don't store them
UNIT_SUFFIXES = (".service", ".socket", ".timer", ".mount", ".automount", ".path", ".target")


def _run(cmd: list[str], timeout: float = 60) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def read_boot_id() -> str:
    return systemd.normalize_boot_id(BOOT_ID_PATH.read_text())


def read_uptime() -> float:
    return float(UPTIME_PATH.read_text().split()[0])


def wait_for_boot_to_finish(timeout: float = 300) -> None:
    """Block until systemd says startup is done. Exit code 1 ('degraded') is fine.

    This only works because the service is Type=simple. With Type=oneshot the collector
    would itself be a pending boot job, and boot could never finish while it waits.
    """
    try:
        subprocess.run(
            ["systemctl", "is-system-running", "--wait"],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("boot did not finish within %ss; trying anyway", timeout)


def estimate_finish_time(timing: BootTiming, now: float, uptime: float) -> int:
    """Wall-clock time (unix seconds) at which startup finished.

    Kernel start is `now - uptime`; startup finishes kernel+initrd+userspace later. This is
    accurate when run right after boot (as the service does). Falls back to `now`.
    """
    after_kernel_start = (timing.kernel or 0.0) + (timing.initrd or 0.0) + (timing.userspace or 0.0)
    if after_kernel_start <= 0:
        return int(now)
    return int(min(now, now - uptime + after_kernel_start))


def read_unit_times() -> dict[str, float]:
    """Per-unit start-up times. Prefers JSON output, falls back to scraping the text."""
    try:
        units = systemd.parse_blame_json(_run(["systemd-analyze", "--json=short", "blame"]))
    except (subprocess.CalledProcessError, ValueError):
        units = systemd.parse_blame(_run(["systemd-analyze", "blame"]))
    return {u: s for u, s in units.items() if s >= MIN_UNIT_SECONDS}


def collect_current_boot(conn: sqlite3.Connection) -> bool:
    """Record the running boot. Returns False if it was already recorded."""
    boot_id = read_boot_id()
    if db.boot_source(conn, boot_id) == "live":
        LOG.info("boot %s already recorded", boot_id[:8])
        return False

    wait_for_boot_to_finish()
    timing = systemd.parse_startup_finished(_run(["systemd-analyze", "time"]))
    if timing is None:
        raise RuntimeError("could not parse `systemd-analyze time` output")
    units = read_unit_times()
    finished_at = estimate_finish_time(timing, time.time(), read_uptime())

    record = db.BootRecord(boot_id, finished_at, "live", timing)
    # replace=True: a live reading beats a journal backfill of the same boot (it has unit data).
    db.insert_boot(conn, record, units, replace=True)
    LOG.info("recorded boot %s: %.1fs total, %d units", boot_id[:8], timing.total, len(units))
    return True


def ingest_pacman_log(conn: sqlite3.Connection, path: Path) -> int:
    """Import package events from pacman.log. Safe to repeat. Returns number of new events."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return db.insert_package_events(conn, pacman_log.parse_pacman_log(text))


def backfill_from_journal(conn: sqlite3.Connection) -> int:
    """Recover past boots (total/phase times only) from the journal. Returns new boots added.

    Per-unit timings cannot be recovered for old boots. Only boots still in the journal
    are found, so a non-persistent journal gives you just the current boot.
    """
    out = _run(
        [
            "journalctl",
            "--no-pager",
            "-o",
            "json",
            f"MESSAGE_ID={STARTUP_FINISHED_ID}",
            "_PID=1",
        ],
        timeout=300,
    )
    added = 0
    for jb in systemd.parse_journal_startup(out):
        record = db.BootRecord(jb.boot_id, jb.finished_at, "journal", jb.timing)
        if db.insert_boot(conn, record, {}):  # never replace: don't clobber live data
            added += 1
    return added


def shipped_units(packages: Iterable[str]) -> dict[str, set[str]]:
    """Map package -> names of the system unit files it ships (via `pacman -Qlq`)."""
    result: dict[str, set[str]] = {}
    for pkg in packages:
        try:
            out = _run(["pacman", "-Qlq", pkg], timeout=30)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue  # removed packages, or not running on Arch
        names = {
            Path(line).name
            for line in out.splitlines()
            if "/systemd/system/" in line and line.endswith(UNIT_SUFFIXES)
        }
        if names:
            result[pkg] = names
    return result
