"""Command-line interface. With collector.py, one of only two modules that touch the system."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from bootdetective import __version__, collector, db, report
from bootdetective.analysis import changepoints, ranking
from bootdetective.config import Config, load_config

LOG = logging.getLogger("bootdetective")
MIN_BOOTS = 10  # below this there is not enough history to call anything a "change"
UNIT_WINDOW = 5  # boots on each side of a change used to compare per-unit timings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootdetective",
        description="Find boot-time regressions and the pacman upgrades behind them.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="path to a config.toml")
    p.add_argument("--db", type=Path, help="database file (overrides the config)")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="record the current boot (run by the systemd service)")
    c.add_argument("--strict", action="store_true", help="exit non-zero on failure")

    sub.add_parser("backfill", help="import old boots from the journal and the pacman log")

    h = sub.add_parser("history", help="list recorded boots")
    h.add_argument("-n", "--last", type=int, default=20, help="how many recent boots to show")

    r = sub.add_parser("report", help="find boot-time changes and their likely culprits")
    r.add_argument("--method", choices=changepoints.METHODS, help="detector (default: config)")
    r.add_argument("--lookback", type=int, default=1, help="boots before a change to search")
    r.add_argument("--top", type=int, default=3, help="suspects to show per change")

    e = sub.add_parser("explain", help="break down one boot (default: the latest)")
    e.add_argument("boot", nargs="?", type=int, help="boot number from `history`")

    g = sub.add_parser("plot", help="save a chart of boot times (needs python-matplotlib)")
    g.add_argument("output", type=Path, help="image file to write, e.g. boots.png")
    return p


class NoDatabaseError(RuntimeError):
    """The collector has not created a database yet."""


def _open_readonly(cfg: Config) -> sqlite3.Connection:
    try:
        return db.connect(cfg.db_path, readonly=True)
    except FileNotFoundError as exc:
        raise NoDatabaseError(str(cfg.db_path)) from exc


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    try:
        conn = db.connect(cfg.db_path)
        try:
            new_events = collector.ingest_pacman_log(conn, cfg.pacman_log)
            LOG.info("pacman log: %d new events", new_events)
            collector.collect_current_boot(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - fail soft: never let a bug here disturb boot
        LOG.exception("collection failed")
        return 1 if args.strict else 0
    return 0


def cmd_backfill(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect(cfg.db_path)
    try:
        events = collector.ingest_pacman_log(conn, cfg.pacman_log)
        boots = collector.backfill_from_journal(conn)
        print(f"Imported {events} package events and {boots} boots from the journal.")
        print(f"The database now holds {db.count_boots(conn)} boots.")
        if boots <= 1:
            print(
                "Tip: only the current boot is in your journal. For older history, make it "
                "persistent: set Storage=persistent in /etc/systemd/journald.conf."
            )
    finally:
        conn.close()
    return 0


def cmd_history(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_readonly(cfg)
    boots = db.list_boots(conn)
    if not boots:
        print("No boots recorded yet.")
        return 0
    print(report.format_history(boots, last=args.last))
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_readonly(cfg)
    boots = db.list_boots(conn)
    if len(boots) < MIN_BOOTS:
        print(f"Only {len(boots)} boots recorded; need at least {MIN_BOOTS} to find a trend.")
        print("Keep using the machine, or run `sudo bootdetective backfill` to import history.")
        return 0

    totals = [b.timing.total for b in boots]
    cps = changepoints.detect(
        totals,
        args.method or cfg.method,
        min_size=cfg.min_size,
        penalty_factor=cfg.penalty_factor,
        min_shift_s=cfg.min_shift_s,
        min_shift_rel=cfg.min_shift_rel,
    )
    print(f"Analysed {len(boots)} boots.  trend: {report.sparkline(totals)}\n")
    if not cps:
        print("No lasting change in boot time found.")
        return 0

    for cp in cps:
        print(_describe(conn, boots, cp, args))
        print()
    return 0


def _describe(
    conn: sqlite3.Connection,
    boots: list[db.BootRecord],
    cp: changepoints.Changepoint,
    args: argparse.Namespace,
) -> str:
    i = cp.index
    lo = max(0, i - args.lookback)
    events = db.package_events_between(conn, boots[lo].finished_at, boots[i].finished_at)

    def units_of(window: list[db.BootRecord]) -> list[dict[str, float]]:
        found = [db.get_unit_times(conn, b.boot_id) for b in window]
        return [u for u in found if u]  # boots recovered from the journal have none

    slow = ranking.slow_units(
        units_of(boots[max(0, i - UNIT_WINDOW) : i]), units_of(boots[i : i + UNIT_WINDOW])
    )
    changed = {e.package for e in events if e.action != "removed"}
    shipped = collector.shipped_units(changed) if slow else {}
    suspects = ranking.rank_suspects(events, slow_units=slow, shipped_units=shipped)
    return report.format_changepoint(
        cp, boots, slow, suspects, lookback=args.lookback, top=args.top
    )


def cmd_explain(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_readonly(cfg)
    boots = db.list_boots(conn)
    if not boots:
        print("No boots recorded yet.")
        return 0
    number = args.boot if args.boot is not None else len(boots)
    if not 1 <= number <= len(boots):
        print(
            f"Boot number must be between 1 and {len(boots)} (see `bootdetective history`).",
            file=sys.stderr,
        )
        return 2
    boot = boots[number - 1]
    previous = boots[number - 2] if number > 1 else None
    events = (
        db.package_events_between(conn, previous.finished_at, boot.finished_at) if previous else []
    )
    print(
        report.format_explain(
            number,
            boot,
            db.get_unit_times(conn, boot.boot_id),
            events,
            number - 1 if previous else None,
        )
    )
    return 0


def cmd_plot(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_readonly(cfg)
    boots = db.list_boots(conn)
    if not boots:
        print("No boots recorded yet.")
        return 0
    cps = changepoints.detect(
        [b.timing.total for b in boots],
        cfg.method,
        min_size=cfg.min_size,
        penalty_factor=cfg.penalty_factor,
        min_shift_s=cfg.min_shift_s,
        min_shift_rel=cfg.min_shift_rel,
    )
    try:
        report.plot_history(boots, cps, args.output)
    except ImportError:
        print("Plotting needs matplotlib: sudo pacman -S python-matplotlib", file=sys.stderr)
        return 1
    print(f"Wrote {args.output}")
    return 0


COMMANDS = {
    "collect": cmd_collect,
    "backfill": cmd_backfill,
    "history": cmd_history,
    "report": cmd_report,
    "explain": cmd_explain,
    "plot": cmd_plot,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    cfg = load_config(args.config)
    if args.db is not None:
        cfg = replace(cfg, db_path=args.db)
    try:
        return COMMANDS[args.command](cfg, args)
    except NoDatabaseError as exc:
        print(
            f"No database at {exc}. Enable the collector first:\n"
            "  sudo systemctl enable --now bootdetective-collect.service",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError as exc:
        print(f"Required program not found: {exc.filename or exc}", file=sys.stderr)
        return 1
    except db.SchemaError as exc:
        print(f"Database problem: {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(
            f"Permission denied: {exc}. Backfill and collect need root (use sudo).", file=sys.stderr
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(map(str, exc.cmd))}\n{exc.stderr or ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
