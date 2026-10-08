"""End to end: build a database with a planted regression, run the real CLI, read the output."""

import numpy as np
import pytest

from bootdetective import cli, collector, db
from bootdetective.parsers.pacman_log import PackageEvent
from bootdetective.parsers.systemd import BootTiming

DAY = 86_400
T0 = 1_780_000_000
SLOW_FROM = 25  # index of the first slow boot


@pytest.fixture
def planted_db(tmp_path):
    """40 daily boots. At boot 25, NetworkManager-wait-online goes from 1s to ~5s."""
    rng = np.random.default_rng(11)
    conn = db.connect(tmp_path / "boots.db")
    for i in range(40):
        slow = i >= SLOW_FROM
        wait_online = (4.9 if slow else 1.0) + rng.normal(0, 0.15)
        total = 12.0 + wait_online + rng.normal(0, 0.4)
        record = db.BootRecord(
            f"{i:032x}", T0 + i * DAY, "live", BootTiming(total=total, kernel=2.0)
        )
        db.insert_boot(
            conn,
            record,
            {
                "NetworkManager-wait-online.service": wait_online,
                "systemd-journald.service": 0.3,
            },
        )
    boundary = T0 + (SLOW_FROM - 1) * DAY + 3600  # between boot 25 and boot 26 (1-based)
    db.insert_package_events(
        conn,
        [
            PackageEvent(boundary, "upgraded", "networkmanager", "1.46.0-1", "1.48.0-1"),
            PackageEvent(boundary + 5, "upgraded", "htop", "3.3.0-1", "3.4.0-1"),
            PackageEvent(boundary + 9, "upgraded", "linux", "6.10.3-1", "6.10.5-1"),
            PackageEvent(
                T0 + 5 * DAY + 3600, "upgraded", "vlc", "3.0-1", "3.1-1"
            ),  # unrelated, earlier
        ],
    )
    conn.close()
    return tmp_path / "boots.db"


@pytest.fixture(autouse=True)
def fake_pacman(monkeypatch):
    shipped = {"networkmanager": {"NetworkManager-wait-online.service"}}
    monkeypatch.setattr(
        collector, "shipped_units", lambda pkgs: {p: shipped[p] for p in pkgs if p in shipped}
    )


def run(args, capsys):
    code = cli.main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_report_names_the_planted_culprit(planted_db, capsys):
    code, out, _ = run(["--db", str(planted_db), "report"], capsys)
    assert code == 0
    assert "rose" in out
    assert (
        f"boot #{SLOW_FROM + 1}" in out
        or f"boot #{SLOW_FROM}" in out
        or f"boot #{SLOW_FROM + 2}" in out
    )
    assert "NetworkManager-wait-online.service" in out
    first_suspect = out.split("best suspect first:")[1].splitlines()[1]
    assert "networkmanager 1.46.0-1 -> 1.48.0-1" in first_suspect
    assert "vlc" not in out  # an unrelated earlier update is outside the window


def test_report_with_too_little_history(tmp_path, capsys):
    conn = db.connect(tmp_path / "few.db")
    db.insert_boot(conn, db.BootRecord("a" * 32, T0, "live", BootTiming(total=15.0)), {})
    conn.close()
    code, out, _ = run(["--db", str(tmp_path / "few.db"), "report"], capsys)
    assert code == 0 and "need at least" in out


def test_report_on_stable_history_finds_nothing(tmp_path, capsys):
    rng = np.random.default_rng(3)
    conn = db.connect(tmp_path / "ok.db")
    for i in range(40):
        rec = db.BootRecord(
            f"{i:032x}", T0 + i * DAY, "live", BootTiming(total=15 + rng.normal(0, 0.4))
        )
        db.insert_boot(conn, rec, {})
    conn.close()
    code, out, _ = run(["--db", str(tmp_path / "ok.db"), "report"], capsys)
    assert code == 0 and "No lasting change" in out


def test_history_and_explain(planted_db, capsys):
    code, out, _ = run(["--db", str(planted_db), "history", "-n", "5"], capsys)
    assert code == 0 and "trend:" in out and out.count("live") == 5
    code, out, _ = run(["--db", str(planted_db), "explain", str(SLOW_FROM + 1)], capsys)
    assert code == 0
    assert "networkmanager 1.46.0-1 -> 1.48.0-1" in out
    assert "NetworkManager-wait-online.service" in out


def test_explain_rejects_out_of_range(planted_db, capsys):
    code, _, err = run(["--db", str(planted_db), "explain", "999"], capsys)
    assert code == 2 and "between 1 and 40" in err


def test_missing_database_gives_friendly_error(tmp_path, capsys):
    code, _, err = run(["--db", str(tmp_path / "none.db"), "history"], capsys)
    assert code == 1 and "systemctl enable" in err


def test_collect_fails_soft(tmp_path, monkeypatch, capsys):
    def explode(conn):
        raise RuntimeError("systemd-analyze exploded")

    monkeypatch.setattr(collector, "collect_current_boot", explode)
    monkeypatch.setattr(collector, "ingest_pacman_log", lambda conn, path: 0)
    assert cli.main(["--db", str(tmp_path / "x.db"), "collect"]) == 0  # never fails boot
    assert cli.main(["--db", str(tmp_path / "x.db"), "collect", "--strict"]) == 1
    capsys.readouterr()


def test_plot_without_matplotlib_is_friendly(planted_db, tmp_path, monkeypatch, capsys):
    from bootdetective import report

    def no_mpl(*a, **k):
        raise ImportError("matplotlib")

    monkeypatch.setattr(report, "plot_history", no_mpl)
    code, _, err = run(["--db", str(planted_db), "plot", str(tmp_path / "x.png")], capsys)
    assert code == 1 and "python-matplotlib" in err
