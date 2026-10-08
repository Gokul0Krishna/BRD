import numpy as np
import pytest

from bootdetective.analysis import changepoints as cp


def make_series(n, jumps, sd, rng, base=15.0, spike_p=0.05):
    """Fake boot times: a base level, planted jumps, Gaussian noise, and rare slow-boot spikes."""
    x = np.full(n, base)
    for idx, delta in jumps:
        x[idx:] += delta
    x = x + rng.normal(0, sd, n)
    spikes = rng.random(n) < spike_p
    x[spikes] += rng.uniform(4, 12, spikes.sum())
    return x


def test_pelt_exact_on_noiseless_step():
    x = np.array([10.0] * 10 + [20.0] * 10)
    assert cp.pelt(x, penalty=1.0, min_size=3) == [10]


def test_detects_planted_jump_in_at_least_90_percent_of_series():
    """Sprint 3 indicator: >= 90% of planted jumps found within +-2 boots."""
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(100):
        idx = int(rng.integers(20, 80))
        found = cp.detect(make_series(100, [(idx, 3.0)], 0.8, rng))
        hits += any(abs(c.index - idx) <= 2 for c in found)
    assert hits >= 90


def test_few_false_alarms_on_stable_series():
    """Sprint 3 indicator: fewer than 1 false alarm per 100 stable boots."""
    rng = np.random.default_rng(1)
    alarms = sum(len(cp.detect(make_series(100, [], 0.8, rng))) for _ in range(100))
    assert alarms / (100 * 100) * 100 < 1.0  # alarms per 100 boots


def test_detects_a_decrease():
    rng = np.random.default_rng(2)
    found = cp.detect(make_series(80, [(40, -4.0)], 0.6, rng))
    assert len(found) == 1
    assert abs(found[0].index - 40) <= 2
    assert found[0].shift < 0


def test_two_changes():
    rng = np.random.default_rng(3)
    found = cp.detect(make_series(120, [(40, 4.0), (85, -3.5)], 0.6, rng))
    assert len(found) == 2
    assert abs(found[0].index - 40) <= 2 and abs(found[1].index - 85) <= 2


def test_single_slow_boot_is_not_a_regime():
    x = np.full(60, 15.0) + np.random.default_rng(4).normal(0, 0.3, 60)
    x[30] += 25.0
    assert cp.detect(x) == []


def test_two_slow_boots_in_a_row_are_not_a_regime():
    x = np.full(60, 15.0) + np.random.default_rng(5).normal(0, 0.3, 60)
    x[30:32] += 20.0
    assert cp.detect(x) == []


def test_tiny_shifts_are_ignored():
    x = np.concatenate([np.full(30, 15.0), np.full(30, 15.4)])
    x = x + np.random.default_rng(6).normal(0, 0.05, 60)
    assert cp.detect(x) == []  # +0.4s is below the 1s / 5% floor


def test_constant_and_short_series():
    assert cp.detect([15.0] * 50) == []
    assert cp.detect([15.0, 16.0, 30.0]) == []
    assert cp.detect([]) == []


def test_cusum_finds_a_big_jump():
    rng = np.random.default_rng(8)
    found = cp.detect(make_series(100, [(50, 6.0)], 0.5, rng, spike_p=0.0), method="cusum")
    assert len(found) == 1
    assert abs(found[0].index - 50) <= 2


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        cp.detect([1.0] * 30, method="magic")


def test_changepoint_reports_before_after_shift():
    rng = np.random.default_rng(9)
    found = cp.detect(make_series(80, [(40, 4.0)], 0.4, rng, spike_p=0.0))[0]
    assert found.before == pytest.approx(15.0, abs=0.5)
    assert found.after == pytest.approx(19.0, abs=0.5)
    assert found.shift_rel == pytest.approx(4.0 / 15.0, abs=0.05)


def test_known_history_fixture(fixtures):
    """The core promise: a committed history with a known regression is found.

    The fixture is synthetic (seeded): +4.2s planted at boot index 35, with slow-boot spikes
    deliberately landing right on the change (indices 35 and 37).
    """
    series = [float(v) for v in (fixtures / "known_boot_totals.txt").read_text().split()]
    found = cp.detect(series)
    assert len(found) == 1
    assert abs(found[0].index - 35) <= 2
    assert 3.0 < found[0].shift < 5.5
