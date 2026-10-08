import random

import pytest

from bootdetective.analysis import ranking as rk
from bootdetective.parsers.pacman_log import PackageEvent


def up(pkg, old="1.0-1", new="1.1-1", ts=100):
    return PackageEvent(ts, "upgraded", pkg, old, new)


def test_collapse_merges_repeated_changes():
    merged = rk.collapse_events([up("foo", "1.0-1", "1.1-1", 1), up("foo", "1.1-1", "1.2-1", 2)])
    assert len(merged) == 1
    assert (merged[0].old_version, merged[0].new_version, merged[0].ts) == ("1.0-1", "1.2-1", 2)


def test_template_of():
    assert rk.template_of("getty@tty1.service") == "getty@.service"
    assert rk.template_of("sshd.service") == "sshd.service"


def test_upstream_version_ignores_pkgrel():
    assert rk.upstream_version("1:2.3.4-2") == "1:2.3.4"
    assert rk.upstream_version("2.3.4-1") == rk.upstream_version("2.3.4-7")


def test_slow_units_finds_regression_and_new_units():
    before = [{"a.service": 1.0, "b.service": 2.0}] * 3
    after = [{"a.service": 5.0, "b.service": 2.1, "new.service": 3.0}] * 3
    slow = rk.slow_units(before, after)
    assert slow == {"a.service": pytest.approx(4.0), "new.service": pytest.approx(3.0)}
    assert list(slow) == ["a.service", "new.service"]  # biggest first


def test_slow_units_without_data_is_empty():
    assert rk.slow_units([], [{"a": 1.0}]) == {}
    assert rk.slow_units([{"a": 1.0}], []) == {}


def test_slow_units_is_robust_to_one_outlier_boot():
    before = [{"a": 1.0}, {"a": 1.0}, {"a": 30.0}, {"a": 1.0}, {"a": 1.0}]
    after = [{"a": 1.0}] * 5
    assert rk.slow_units(before, after) == {}


def test_package_shipping_the_slow_unit_ranks_first():
    events = [up("linux"), up("networkmanager"), up("htop")]
    slow = {"NetworkManager-wait-online.service": 3.9}
    shipped = {"networkmanager": {"NetworkManager-wait-online.service", "NetworkManager.service"}}
    ranked = rk.rank_suspects(events, slow_units=slow, shipped_units=shipped)
    assert ranked[0].package == "networkmanager"
    assert "3.9s slower" in ranked[0].reasons[0]


def test_template_instance_units_match():
    ranked = rk.rank_suspects(
        [up("util-linux"), up("systemd")],
        slow_units={"getty@tty1.service": 2.0},
        shipped_units={"util-linux": {"getty@.service"}},
    )
    assert ranked[0].package == "util-linux"


def test_boot_critical_packages_outrank_ordinary_ones_without_unit_data():
    ranked = rk.rank_suspects([up("htop"), up("linux"), up("vlc")])
    assert ranked[0].package == "linux"


def test_removed_packages_score_lower():
    removed = PackageEvent(100, "removed", "oldtool", "1.0-1", "")
    ranked = rk.rank_suspects([removed, up("htop", "1.0-1", "1.0-2")])
    assert ranked[-1].package == "oldtool"


def test_pkgrel_only_bump_gets_no_upstream_bonus():
    rebuild = rk.rank_suspects([up("foo", "1.0-1", "1.0-2")])[0]
    release = rk.rank_suspects([up("foo", "1.0-1", "1.1-1")])[0]
    assert release.score > rebuild.score


def test_staged_regressions_culprit_in_top_3_at_least_80_percent():
    """Sprint 4 indicator, simulated: plant a slow-unit regression among ~30 updates."""
    rng = random.Random(0)
    names = [f"pkg{i}" for i in range(30)] + ["linux", "mesa", "systemd", "mkinitcpio"]
    in_top3 = 0
    trials = 50
    for _ in range(trials):
        culprit = rng.choice([n for n in names if n.startswith("pkg")])
        events = [up(n, ts=i) for i, n in enumerate(rng.sample(names, 25) + [culprit])]
        ranked = rk.rank_suspects(
            events,
            slow_units={"culprit.service": 5.0},
            shipped_units={culprit: {"culprit.service"}},
        )
        in_top3 += culprit in [s.package for s in ranked[:3]]
    assert in_top3 / trials >= 0.8
