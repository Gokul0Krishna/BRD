import pytest

from bootdetective.parsers import pacman_log, systemd


# ---------------------------------------------------------------- pacman.log
def test_pacman_log_counts_every_event_type(fixtures):
    events = pacman_log.parse_pacman_log((fixtures / "pacman.log").read_text())
    by_action = {}
    for e in events:
        by_action[e.action] = by_action.get(e.action, 0) + 1
    assert by_action == {
        "upgraded": 2,
        "installed": 2,
        "removed": 1,
        "reinstalled": 1,
        "downgraded": 1,
    }


def test_pacman_log_matches_grep_count(fixtures):
    """Sprint 2 indicator: parser agrees with a dumb grep on the same file."""
    text = (fixtures / "pacman.log").read_text()
    grep_count = sum(" [ALPM] upgraded " in line for line in text.splitlines())
    parsed = [e for e in pacman_log.parse_pacman_log(text) if e.action == "upgraded"]
    assert len(parsed) == grep_count


def test_pacman_log_versions(fixtures):
    events = pacman_log.parse_pacman_log((fixtures / "pacman.log").read_text())
    nm = next(e for e in events if e.package == "networkmanager")
    assert (nm.action, nm.old_version, nm.new_version) == ("upgraded", "1.46.0-1", "1.48.0-1")
    htop = next(e for e in events if e.package == "htop")
    assert (htop.old_version, htop.new_version) == ("", "3.4.0-1")
    gone = next(e for e in events if e.package == "oldtool")
    assert (gone.old_version, gone.new_version) == ("1.0-1", "")


def test_pacman_log_ignores_noise_lines():
    noise = "[2026-09-10T08:12:11+0530] [ALPM] warning: /etc/x installed as /etc/x.pacnew\n"
    assert pacman_log.parse_pacman_log(noise) == []
    assert pacman_log.parse_pacman_log("") == []


def test_pacman_timestamps_both_styles():
    modern = pacman_log.parse_timestamp("2026-09-10T08:12:06+0530")
    utc = pacman_log.parse_timestamp("2026-09-10T02:42:06+0000")
    assert modern == utc
    assert isinstance(pacman_log.parse_timestamp("2016-01-01 12:00"), int)
    with pytest.raises(ValueError):
        pacman_log.parse_timestamp("yesterday")


# ---------------------------------------------------------------- timespans
@pytest.mark.parametrize(
    "text, seconds",
    [
        ("845ms", 0.845),
        ("5.123s", 5.123),
        ("1min 3.2s", 63.2),
        ("2h 1min", 7260.0),
        ("12us", 12e-6),
    ],
)
def test_parse_timespan(text, seconds):
    assert systemd.parse_timespan(text) == pytest.approx(seconds)


@pytest.mark.parametrize("bad", ["", "fast", "5 parsecs", "Bootup is not yet finished"])
def test_parse_timespan_rejects_garbage(bad):
    with pytest.raises(ValueError):
        systemd.parse_timespan(bad)


# ---------------------------------------------------------------- startup line
def test_startup_finished_full(fixtures):
    t = systemd.parse_startup_finished((fixtures / "systemd_time.txt").read_text())
    assert t == systemd.BootTiming(
        total=pytest.approx(20.096),
        firmware=pytest.approx(6.142),
        loader=pytest.approx(3.104),
        kernel=pytest.approx(2.518),
        initrd=None,
        userspace=pytest.approx(8.331),
    )


def test_startup_finished_without_firmware_with_initrd():
    t = systemd.parse_startup_finished(
        "Startup finished in 2.5s (kernel) + 1.5s (initrd) + 8.0s (userspace) = 12.0s."
    )
    assert (t.firmware, t.loader) == (None, None)
    assert (t.kernel, t.initrd, t.userspace, t.total) == (2.5, 1.5, 8.0, 12.0)


def test_startup_finished_total_falls_back_to_sum():
    t = systemd.parse_startup_finished("Startup finished in 3.0s (kernel) + 4.0s (userspace)")
    assert t.total == pytest.approx(7.0)


def test_startup_finished_ignores_user_manager_and_noise():
    assert systemd.parse_startup_finished("Startup finished in 87ms.") is None
    assert systemd.parse_startup_finished("Bootup is not yet finished") is None
    assert systemd.parse_startup_finished("") is None


# ---------------------------------------------------------------- blame
def test_blame_text(fixtures):
    units = systemd.parse_blame((fixtures / "systemd_blame.txt").read_text())
    assert units == {
        "NetworkManager-wait-online.service": pytest.approx(5.123),
        "slow-thing.service": pytest.approx(63.2),
        "plymouth-quit-wait.service": pytest.approx(0.845),
        "systemd-journald.service": pytest.approx(0.012),
    }


def test_blame_text_skips_junk_lines():
    assert systemd.parse_blame("Bootup is not yet finished\n\n") == {}


def test_blame_json_agrees_with_text(fixtures):
    from_json = systemd.parse_blame_json((fixtures / "systemd_blame.json").read_text())
    from_text = systemd.parse_blame((fixtures / "systemd_blame.txt").read_text())
    for unit, secs in from_json.items():
        assert from_text[unit] == pytest.approx(secs)


@pytest.mark.parametrize("bad", ["not json", '{"a": 1}', '[{"x": 1}]', "[1, 2]"])
def test_blame_json_bad_shape_raises_value_error(bad):
    with pytest.raises(ValueError):
        systemd.parse_blame_json(bad)


# ---------------------------------------------------------------- journal
def test_journal_startup(fixtures):
    boots = systemd.parse_journal_startup((fixtures / "journal_startup.jsonl").read_text())
    assert [b.boot_id for b in boots] == ["a" * 32, "b" * 32]  # user-manager + junk lines skipped
    assert boots[0].finished_at == 1788000000
    assert boots[0].timing.total == pytest.approx(19.0)
    assert boots[1].timing.firmware is None


def test_normalize_boot_id():
    assert systemd.normalize_boot_id("ABCDEF01-2345-6789-abcd-ef0123456789\n") == (
        "abcdef0123456789abcdef0123456789"
    )
