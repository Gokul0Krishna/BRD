"""Changepoint detection on a series of boot times.

Pure numpy: arrays in, results out. Two detectors are provided so you can compare them:

  * PELT  (Killick et al. 2012): offline, finds the best set of mean-shift breakpoints by
    minimising  sum(squared error per segment) + penalty * (number of segments).
  * CUSUM: classic cumulative-sum alarm. Simpler, but needs a warm-up window and tends to
    react a little late.

Boot times are noisy and have a long right tail (one slow disk day, one fsck). Three things
keep that from causing false alarms:
  1. isolated spikes are smoothed out before detection (a lone slow boot is not a regime),
  2. a regime must last at least `min_size` boots,
  3. a shift must be big enough to matter (both in seconds and as a fraction of the level).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

METHODS = ("pelt", "cusum")


@dataclass(frozen=True)
class Changepoint:
    index: int  # position (0-based) of the first boot of the new regime
    before: float  # median boot time of the regime before
    after: float  # median boot time of the regime after

    @property
    def shift(self) -> float:
        return self.after - self.before

    @property
    def shift_rel(self) -> float:
        return self.shift / self.before if self.before else math.inf


def robust_sigma(x: np.ndarray) -> float:
    """Noise level estimated from first differences, so level shifts do not inflate it."""
    if len(x) < 3:
        return 1e-6
    d = np.diff(x)
    mad = float(np.median(np.abs(d - np.median(d))))
    return max(1.4826 * mad / math.sqrt(2), 1e-6)


def despike(x: np.ndarray, sigma: float, k: float = 5.0) -> np.ndarray:
    """Replace isolated one-boot spikes with the average of their neighbours.

    A point is a spike only if it sits more than k*sigma above (or below) BOTH neighbours.
    A genuine level shift never matches that: one neighbour is always at the new level.
    """
    y = x.copy()
    if len(x) < 3:
        return y
    prev, cur, nxt = x[:-2], x[1:-1], x[2:]
    spike = (cur - np.maximum(prev, nxt) > k * sigma) | (np.minimum(prev, nxt) - cur > k * sigma)
    y[1:-1][spike] = ((prev + nxt) / 2.0)[spike]
    return y


def pelt(x: np.ndarray, penalty: float, min_size: int = 3) -> list[int]:
    """PELT with an L2 (mean-shift) cost. Returns sorted breakpoint indices."""
    n = len(x)
    s1 = np.concatenate(([0.0], np.cumsum(x)))
    s2 = np.concatenate(([0.0], np.cumsum(x * x)))

    def cost(a: int, b: int) -> float:
        total = s1[b] - s1[a]
        return float(s2[b] - s2[a] - total * total / (b - a))

    best = [math.inf] * (n + 1)  # best[t] = optimal cost of x[:t]
    best[0] = 0.0
    last = [0] * (n + 1)  # last[t] = start of the final segment in that optimal split
    admissible: list[int] = []
    for t in range(min_size, n + 1):
        admissible.append(t - min_size)
        values = [best[s] + cost(s, t) + penalty for s in admissible]
        i = int(np.argmin(values))
        best[t], last[t] = values[i], admissible[i]
        # pruning: a start point that is already worse than best[t] can never win later
        admissible = [s for s, v in zip(admissible, values, strict=True) if v - penalty <= best[t]]

    breakpoints = []
    t = n
    while t > 0:
        s = last[t]
        if s > 0:
            breakpoints.append(s)
        t = s
    return sorted(breakpoints)


def cusum(
    x: np.ndarray, sigma: float, threshold: float = 8.0, drift: float = 0.5, warmup: int = 5
) -> list[int]:
    """Two-sided CUSUM. After each alarm it restarts from the detected change onset."""
    n = len(x)
    breakpoints: list[int] = []
    start = 0
    while start + warmup < n:
        mu = float(np.median(x[start : start + warmup]))
        g_up = g_down = 0.0
        onset_up = onset_down = start + warmup
        hit = None
        for t in range(start + warmup, n):
            z = (x[t] - mu) / sigma
            g_up = max(0.0, g_up + z - drift)
            g_down = max(0.0, g_down - z - drift)
            if g_up == 0.0:
                onset_up = t + 1
            if g_down == 0.0:
                onset_down = t + 1
            if g_up > threshold:
                hit = onset_up
                break
            if g_down > threshold:
                hit = onset_down
                break
        if hit is None or hit >= n:
            break
        breakpoints.append(hit)
        start = hit
    return breakpoints


def _regime_medians(x: np.ndarray, breakpoints: Sequence[int]) -> list[float]:
    bounds = [0, *breakpoints, len(x)]
    return [float(np.median(x[a:b])) for a, b in zip(bounds[:-1], bounds[1:], strict=True)]


def _drop_small_shifts(
    x: np.ndarray,
    breakpoints: list[int],
    min_shift_s: float,
    min_shift_rel: float,
    min_shift_abs_floor: float = 0.0,
) -> list[int]:
    """Repeatedly drop the weakest breakpoint whose shift is below the thresholds."""
    kept = list(breakpoints)
    while kept:
        medians = _regime_medians(x, kept)
        weakest_ratio, weakest_i = math.inf, -1
        for i in range(len(kept)):
            shift = abs(medians[i + 1] - medians[i])
            needed = max(min_shift_s, min_shift_rel * abs(medians[i]), min_shift_abs_floor)
            if shift < needed and shift / needed < weakest_ratio:
                weakest_ratio, weakest_i = shift / needed, i
        if weakest_i < 0:
            break
        kept.pop(weakest_i)
    return kept


def detect(
    series: Sequence[float],
    method: str = "pelt",
    *,
    min_size: int = 5,
    penalty_factor: float = 4.0,
    min_shift_s: float = 1.0,
    min_shift_rel: float = 0.05,
    min_shift_sigmas: float = 2.0,
) -> list[Changepoint]:
    """Find lasting shifts in a boot-time series (seconds, oldest first)."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; choose from {METHODS}")
    raw = np.asarray(series, dtype=float)
    n = len(raw)
    if n < 2 * min_size:
        return []

    sigma = robust_sigma(raw)
    smooth = despike(raw, sigma)
    if method == "pelt":
        penalty = penalty_factor * sigma**2 * math.log(n)
        candidates = pelt(smooth, penalty, min_size)
    else:
        candidates = cusum(smooth, sigma, warmup=max(min_size, 5))

    kept = _drop_small_shifts(raw, candidates, min_shift_s, min_shift_rel, min_shift_sigmas * sigma)
    medians = _regime_medians(raw, kept)
    return [Changepoint(idx, medians[i], medians[i + 1]) for i, idx in enumerate(kept)]
