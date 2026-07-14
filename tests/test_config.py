import pytest

from gcqa.config import ConfigError, load_config, load_env
from tests.conftest import ROOT


def test_load_real_config():
    cfg = load_config(ROOT / "config" / "qa.yaml")
    assert cfg.models["drafter"] == "claude-sonnet-5"
    assert cfg.thresholds.auto_decide["potential_fraud"] == 0.95
    assert cfg.gates.min_fraud_photo_evidence == 2
    assert "location_unverified" in cfg.gates.verifier_runs_on
    assert cfg.prechecks.gps_outlier_m == 800
    assert cfg.prechecks.gps_property_match_m == 500
    assert cfg.prechecks.gps_property_fail_km == 10
    assert cfg.geocode.nominatim_enabled is True
    assert cfg.runtime.max_turns == 8
    assert cfg.config_version.startswith("sha256:")


def test_config_version_changes_with_content(tmp_path):
    src = (ROOT / "config" / "qa.yaml").read_text()
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text(src)
    b.write_text(src.replace("gps_outlier_m: 800", "gps_outlier_m: 900"))
    assert load_config(a).config_version != load_config(b).config_version


def test_missing_key_fails_fast(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("models: {drafter: x}\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "SAFEGUARD_AUTH_URL=https://a/json\nSAFEGUARD_AUTH_USER=poc\n"
        "SAFEGUARD_AUTH_PASSWORD=secret\nSAFEGUARD_API_BASE=https://api/\n"
        "SAFEGUARD_IMAGE_BASE=https://img\n# comment\n"
    )
    creds = load_env(env)
    assert creds.auth_user == "poc"
    assert creds.api_base == "https://api"       # trailing slash stripped

    with pytest.raises(ConfigError):
        load_env(tmp_path / "missing.env")
