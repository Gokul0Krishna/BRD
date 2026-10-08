import subprocess

import pytest

from bootdetective import collector, db
from bootdetective.parsers.systemd import BootTiming


def test_estimate_finish_time_uses_kernel_start_plus_phases():
    timing = BootTiming(total=20.0, firmware=6.0, loader=3.0, kernel=2.0, userspace=8.0)
    # booted 100s ago, so kernel started at now-100; finish is 2+8s after that
    assert collector.estimate_finish_time(timing, now=10_000, uptime=100) == 10_000 - 100 + 10


def test_estimate_finish_time_falls_back_and_never_exceeds_now():
    assert collector.estimate_finish_time(BootTiming(total=5.0), now=500, uptime=50) == 500
    slow = BootTiming(total=90.0, kernel=10.0, userspace=80.0)
    assert collector.estimate_finish_time(slow, now=1000, uptime=20) == 1000


@pytest.fixture
def fake_system(monkeypatch, fixtures):
    """Replace every system call with fixture data so the whole collect flow runs offline."""
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd == ["systemd-analyze", "time"]:
            return (fixtures / "systemd_time.txt").read_text()
        if cmd == ["systemd-analyze", "--json=short", "blame"]:
            return (fixtures / "systemd_blame.json").read_text()
        if cmd[:2] == ["pacman", "-Qlq"]:
            return (
                "/usr/lib/systemd/system/foo.service\n"
                "/usr/lib/systemd/system/foo.service.d/\n"
                "/usr/bin/foo\n"
            )
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(collector, "_run", fake_run)
    monkeypatch.setattr(collector, "read_boot_id", lambda: "c" * 32)
    monkeypatch.setattr(collector, "read_uptime", lambda: 60.0)
    monkeypatch.setattr(collector, "wait_for_boot_to_finish", lambda *a, **k: None)
    return calls


def test_collect_records_boot_once(tmp_path, fake_system):
    conn = db.connect(tmp_path / "t.db")
    assert collector.collect_current_boot(conn) is True
    assert collector.collect_current_boot(conn) is False  # second run: no duplicate
    boots = db.list_boots(conn)
    assert len(boots) == 1 and boots[0].source == "live"
    assert boots[0].timing.total == pytest.approx(20.096)
    units = db.get_unit_times(conn, "c" * 32)
    assert "NetworkManager-wait-online.service" in units


def test_collect_falls_back_to_text_blame_when_json_unsupported(
    tmp_path, fake_system, monkeypatch, fixtures
):
    real = collector._run

    def no_json(cmd, timeout=60):
        if "--json=short" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        if cmd == ["systemd-analyze", "blame"]:
            return (fixtures / "systemd_blame.txt").read_text()
        return real(cmd, timeout)

    monkeypatch.setattr(collector, "_run", no_json)
    units = collector.read_unit_times()
    assert units["slow-thing.service"] == pytest.approx(63.2)
    assert "systemd-journald.service" not in units  # 12ms is below the noise floor


def test_live_collect_supersedes_journal_backfill(tmp_path, fake_system):
    conn = db.connect(tmp_path / "t.db")
    journal = db.BootRecord("c" * 32, 5, "journal", BootTiming(total=19.0))
    db.insert_boot(conn, journal, {})
    assert collector.collect_current_boot(conn) is True
    assert db.boot_source(conn, "c" * 32) == "live"
    assert db.get_unit_times(conn, "c" * 32)


def test_backfill_from_journal_adds_boots_and_is_repeatable(
    tmp_path, fake_system, monkeypatch, fixtures
):
    monkeypatch.setattr(
        collector, "_run", lambda cmd, timeout=60: (fixtures / "journal_startup.jsonl").read_text()
    )
    conn = db.connect(tmp_path / "t.db")
    assert collector.backfill_from_journal(conn) == 2
    assert collector.backfill_from_journal(conn) == 0


def test_ingest_pacman_log_is_repeatable(tmp_path, fixtures):
    conn = db.connect(tmp_path / "t.db")
    assert collector.ingest_pacman_log(conn, fixtures / "pacman.log") == 7
    assert collector.ingest_pacman_log(conn, fixtures / "pacman.log") == 0


def test_shipped_units_keeps_only_system_unit_files(fake_system):
    assert collector.shipped_units(["foo"]) == {"foo": {"foo.service"}}


def test_shipped_units_survives_missing_pacman(monkeypatch):
    def boom(cmd, timeout=60):
        raise FileNotFoundError("pacman")

    monkeypatch.setattr(collector, "_run", boom)
    assert collector.shipped_units(["foo"]) == {}
