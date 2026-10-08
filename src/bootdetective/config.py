"""Configuration loading (TOML). Search order, first file found wins:

  1. the path given with --config
  2. $BOOTDETECTIVE_CONFIG
  3. ~/.config/bootdetective/config.toml
  4. /etc/bootdetective/config.toml

Unknown keys are ignored so old programs can read newer config files.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_DB = Path("/var/lib/bootdetective/bootdetective.db")
DEFAULT_PACMAN_LOG = Path("/var/log/pacman.log")


@dataclass(frozen=True)
class Config:
    db_path: Path = DEFAULT_DB
    pacman_log: Path = DEFAULT_PACMAN_LOG
    method: str = "pelt"  # pelt | cusum
    min_size: int = 5  # fewest boots a "regime" must last to count
    penalty_factor: float = 4.0  # higher = fewer, more certain changepoints
    min_shift_s: float = 1.0  # ignore shifts smaller than this many seconds...
    min_shift_rel: float = 0.05  # ...or this fraction of the previous level


def candidate_paths(explicit: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit is not None:
        paths.append(explicit)
    if env := os.environ.get("BOOTDETECTIVE_CONFIG"):
        paths.append(Path(env))
    paths.append(Path.home() / ".config" / "bootdetective" / "config.toml")
    paths.append(Path("/etc/bootdetective/config.toml"))
    return paths


def parse_config(data: dict) -> Config:
    """Turn parsed TOML into a Config. Pure, so it is easy to test."""
    cfg = Config()
    db = data.get("database", {})
    det = data.get("detection", {})
    if "path" in db:
        cfg = replace(cfg, db_path=Path(db["path"]))
    if "pacman_log" in db:
        cfg = replace(cfg, pacman_log=Path(db["pacman_log"]))
    if "method" in det:
        cfg = replace(cfg, method=str(det["method"]))
    if "min_size" in det:
        cfg = replace(cfg, min_size=int(det["min_size"]))
    for key in ("penalty_factor", "min_shift_s", "min_shift_rel"):
        if key in det:
            cfg = replace(cfg, **{key: float(det[key])})
    return cfg


def load_config(explicit: Path | None = None) -> Config:
    for path in candidate_paths(explicit):
        if path.is_file():
            with path.open("rb") as fh:
                return parse_config(tomllib.load(fh))
    return Config()
