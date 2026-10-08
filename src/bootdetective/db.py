"""SQLite storage: schema, migrations, and queries.

Rules:
  * Every boot is keyed on its boot ID, so running the collector twice never duplicates rows.
  * The schema is versioned from day one. To change it, APPEND a new script to MIGRATIONS.
    Never edit an old one: real users already have databases built from it.
  * No WAL mode: it keeps read-only access (a normal user reading root's database) simple.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bootdetective.parsers.pacman_log import PackageEvent
from bootdetective.parsers.systemd import BootTiming

MIGRATIONS: list[str] = [
    # version 1
    """
    CREATE TABLE boots (
        boot_id       TEXT PRIMARY KEY,           -- 32 lowercase hex, from normalize_boot_id()
        finished_at   INTEGER NOT NULL,           -- unix seconds when startup finished
        source        TEXT NOT NULL,              -- 'live' (collector) or 'journal' (backfill)
        total_s       REAL NOT NULL,
        firmware_s    REAL,
        loader_s      REAL,
        kernel_s      REAL,
        initrd_s      REAL,
        userspace_s   REAL
    );
    CREATE INDEX idx_boots_finished ON boots(finished_at);

    CREATE TABLE unit_times (
        boot_id  TEXT NOT NULL REFERENCES boots(boot_id) ON DELETE CASCADE,
        unit     TEXT NOT NULL,
        seconds  REAL NOT NULL,
        PRIMARY KEY (boot_id, unit)
    );

    CREATE TABLE package_events (
        id           INTEGER PRIMARY KEY,
        ts           INTEGER NOT NULL,
        action       TEXT NOT NULL,
        package      TEXT NOT NULL,
        old_version  TEXT NOT NULL DEFAULT '',    -- '' not NULL, so UNIQUE below works
        new_version  TEXT NOT NULL DEFAULT '',
        UNIQUE (ts, action, package, old_version, new_version)
    );
    CREATE INDEX idx_package_events_ts ON package_events(ts);
    """,
]


class SchemaError(RuntimeError):
    """The database exists but cannot be used by this version of the program."""


@dataclass(frozen=True)
class BootRecord:
    boot_id: str
    finished_at: int
    source: str
    timing: BootTiming


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:  # table does not exist yet
        return 0
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to the latest schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = current_version(conn)
    if current > len(MIGRATIONS):
        raise SchemaError(
            f"database schema v{current} is newer than this program supports "
            f"(v{len(MIGRATIONS)}). Upgrade bootdetective."
        )
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(script)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database. Read-write opens create and migrate; read-only never writes."""
    path = Path(path)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if readonly:
        version = current_version(conn)
        if version != len(MIGRATIONS):
            raise SchemaError(
                f"database schema is v{version}, expected v{len(MIGRATIONS)}. "
                "Run `sudo bootdetective collect` once to upgrade it."
            )
    else:
        migrate(conn)
    return conn


# ---------------------------------------------------------------- boots


def boot_source(conn: sqlite3.Connection, boot_id: str) -> str | None:
    """Return 'live' / 'journal' if this boot is stored, else None."""
    row = conn.execute("SELECT source FROM boots WHERE boot_id = ?", (boot_id,)).fetchone()
    return None if row is None else str(row["source"])


def insert_boot(
    conn: sqlite3.Connection,
    record: BootRecord,
    units: Mapping[str, float],
    *,
    replace: bool = False,
) -> bool:
    """Store a boot and its per-unit timings atomically. Returns False if it already existed.

    replace=True overwrites an existing row (used when a live reading supersedes a journal
    backfill of the same boot, since only the live reading has per-unit timings).
    """
    t = record.timing
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    with conn:
        cur = conn.execute(
            f"{verb} INTO boots (boot_id, finished_at, source, total_s, firmware_s, loader_s, "
            "kernel_s, initrd_s, userspace_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.boot_id,
                record.finished_at,
                record.source,
                t.total,
                t.firmware,
                t.loader,
                t.kernel,
                t.initrd,
                t.userspace,
            ),
        )
        if cur.rowcount == 0:
            return False
        conn.executemany(
            "INSERT OR REPLACE INTO unit_times (boot_id, unit, seconds) VALUES (?, ?, ?)",
            [(record.boot_id, unit, secs) for unit, secs in units.items()],
        )
    return True


def _row_to_boot(r: sqlite3.Row) -> BootRecord:
    return BootRecord(
        boot_id=r["boot_id"],
        finished_at=r["finished_at"],
        source=r["source"],
        timing=BootTiming(
            total=r["total_s"],
            firmware=r["firmware_s"],
            loader=r["loader_s"],
            kernel=r["kernel_s"],
            initrd=r["initrd_s"],
            userspace=r["userspace_s"],
        ),
    )


def list_boots(conn: sqlite3.Connection) -> list[BootRecord]:
    """All boots, oldest first."""
    rows = conn.execute("SELECT * FROM boots ORDER BY finished_at, boot_id").fetchall()
    return [_row_to_boot(r) for r in rows]


def count_boots(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM boots").fetchone()[0])


def get_unit_times(conn: sqlite3.Connection, boot_id: str) -> dict[str, float]:
    rows = conn.execute("SELECT unit, seconds FROM unit_times WHERE boot_id = ?", (boot_id,))
    return {r["unit"]: r["seconds"] for r in rows}


# ---------------------------------------------------------------- packages


def insert_package_events(conn: sqlite3.Connection, events: Iterable[PackageEvent]) -> int:
    """Store package events, skipping ones already present. Returns how many were new."""
    before = count_package_events(conn)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO package_events (ts, action, package, old_version, new_version) "
            "VALUES (?, ?, ?, ?, ?)",
            [(e.ts, e.action, e.package, e.old_version, e.new_version) for e in events],
        )
    return count_package_events(conn) - before


def count_package_events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM package_events").fetchone()[0])


def package_events_between(conn: sqlite3.Connection, t0: int, t1: int) -> list[PackageEvent]:
    """Package events with t0 < ts <= t1, oldest first."""
    rows = conn.execute(
        "SELECT ts, action, package, old_version, new_version FROM package_events "
        "WHERE ts > ? AND ts <= ? ORDER BY ts, id",
        (t0, t1),
    )
    return [
        PackageEvent(r["ts"], r["action"], r["package"], r["old_version"], r["new_version"])
        for r in rows
    ]
