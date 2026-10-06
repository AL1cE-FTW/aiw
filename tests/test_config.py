import pytest

from twitch_shorts.config import load_config


def test_load_config_merges_keywords_and_env(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[detect]\ntop_n = 3\n[detect.keywords]\n"ナイス" = 0\n"ゆうき" = 2.0\n[render]\nlayout = "crop"\n',
                 encoding="utf-8")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "envid")
    cfg = load_config(p)
    assert cfg.detect.top_n == 3
    assert cfg.render.layout == "crop"
    assert cfg.detect.keywords["ゆうき"] == 2.0
    assert "ナイス" not in cfg.detect.keywords
    assert "草" in cfg.detect.keywords
    assert cfg.twitch.client_id == "envid"


def test_unknown_key_is_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[detect]\ntopn = 3\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_multiple_configs_are_layered(tmp_path):
    a = tmp_path / "a.toml"
    a.write_text('[detect]\ntop_n = 3\npre_roll = 10.0\n[render]\nlayout = "crop"\n')
    b = tmp_path / "b.toml"
    b.write_text('[detect]\npre_roll = 22.0\n[detect.keywords]\n"yuukipog" = 1.5\n')
    cfg = load_config([a, b])
    assert (cfg.detect.top_n, cfg.detect.pre_roll, cfg.render.layout) == (3, 22.0, "crop")
    assert cfg.detect.keywords["yuukipog"] == 1.5
