import sqlite3

import pytest

from bootdetective import db
from bootdetective.parsers.pacman_log import PackageEvent
from bootdetective.parsers.systemd import BootTiming


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def boot(boot_id="a" * 32, finished=1000, source="live", total=15.0):
    return db.BootRecord(boot_id, finished, source, BootTiming(total=total, kernel=2.0))


def test_schema_is_versioned_and_migrate_is_idempotent(conn):
    assert db.current_version(conn) == len(db.MIGRATIONS)
    db.migrate(conn)
    db.migrate(conn)
    assert db.current_version(conn) == len(db.MIGRATIONS)


def test_inserting_the_same_boot_twice_does_not_duplicate(conn):
    """Sprint 1 indicator: rerunning the collector never duplicates a boot."""
    assert db.insert_boot(conn, boot(), {"a.service": 1.0}) is True
    assert db.insert_boot(conn, boot(), {"a.service": 1.0}) is False
    assert db.count_boots(conn) == 1


def test_boots_roundtrip_in_chronological_order(conn):
    db.insert_boot(conn, boot("b" * 32, finished=2000, total=20.0), {"x.service": 2.5})
    db.insert_boot(conn, boot("a" * 32, finished=1000, total=15.0), {})
    boots = db.list_boots(conn)
    assert [b.finished_at for b in boots] == [1000, 2000]
    assert boots[1].timing == BootTiming(total=20.0, kernel=2.0)
    assert db.get_unit_times(conn, "b" * 32) == {"x.service": 2.5}


def test_live_reading_replaces_journal_backfill_but_not_vice_versa(conn):
    db.insert_boot(conn, boot(source="journal", total=14.0), {})
    assert db.boot_source(conn, "a" * 32) == "journal"
    db.insert_boot(conn, boot(source="live", total=15.0), {"a.service": 1.0}, replace=True)
    assert db.boot_source(conn, "a" * 32) == "live"
    assert db.get_unit_times(conn, "a" * 32) == {"a.service": 1.0}
    # a later backfill must not clobber the live data
    assert db.insert_boot(conn, boot(source="journal", total=9.0), {}) is False
    assert db.list_boots(conn)[0].timing.total == 15.0


def test_package_events_are_idempotent_including_installs(conn):
    events = [
        PackageEvent(10, "installed", "htop", "", "3.4-1"),
        PackageEvent(20, "upgraded", "foo", "1-1", "2-1"),
    ]
    assert db.insert_package_events(conn, events) == 2
    assert db.insert_package_events(conn, events) == 0
    assert db.count_package_events(conn) == 2


def test_package_events_between_is_exclusive_start_inclusive_end(conn):
    db.insert_package_events(
        conn, [PackageEvent(t, "upgraded", f"p{t}", "1", "2") for t in (10, 20, 30)]
    )
    assert [e.package for e in db.package_events_between(conn, 10, 30)] == ["p20", "p30"]


def test_readonly_open_cannot_write_and_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "nope.db", readonly=True)
    db.connect(tmp_path / "t.db").close()
    ro = db.connect(tmp_path / "t.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO schema_version VALUES (99)")
    ro.close()


def test_newer_schema_is_refused(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO schema_version VALUES (99)")
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaError):
        db.connect(tmp_path / "t.db")
    with pytest.raises(db.SchemaError):
        db.connect(tmp_path / "t.db", readonly=True)
