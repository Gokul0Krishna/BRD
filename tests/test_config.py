from pathlib import Path

from bootdetective import config


def test_defaults_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BOOTDETECTIVE_CONFIG", raising=False)
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.method == "pelt" and cfg.db_path == config.DEFAULT_DB


def test_parse_config_overrides_and_ignores_unknown_keys():
    cfg = config.parse_config(
        {
            "database": {"path": "/tmp/x.db"},
            "detection": {"method": "cusum", "min_size": 7, "penalty_factor": 2, "future_key": 1},
            "something_new": {},
        }
    )
    assert cfg.db_path == Path("/tmp/x.db")
    assert (cfg.method, cfg.min_size, cfg.penalty_factor) == ("cusum", 7, 2.0)


def test_load_config_reads_a_toml_file(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[detection]\nmin_shift_s = 2.5\n")
    assert config.load_config(f).min_shift_s == 2.5
