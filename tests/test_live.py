"""Live smoke tests against the real Safeguard dev API (pytest -m live).

Excluded by default (pyproject addopts). Needs .env credentials. This is the
M1 GPS proof: every photo of test order 500119014 compared, live.
"""

from pathlib import Path

import pytest

from gcqa.api_client import SafeguardClient
from gcqa.config import load_config, load_env
from gcqa.geo import GeocodeCache, geocode_property, haversine_m
from gcqa.prechecks import run_prechecks
from gcqa.prefetch import prefetch
from tests.conftest import EXPECTED_MEDOID, OUTLIER_GUID, ROOT

ORDER = "500119014"

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_prefetch_and_prechecks(tmp_path):
    cfg = load_config(ROOT / "config" / "qa.yaml")
    creds = load_env(ROOT / ".env")

    async with SafeguardClient(creds) as client:
        bundle = await prefetch(ORDER, client, cfg, tmp_path / "cache")

    assert bundle.fetch_stats["items_listed"] == 50
    assert bundle.fetch_stats["jpeg_listed"] == 42
    assert len(bundle.photos) == 32 and len(bundle.aux_gps) == 10
    assert bundle.fetch_stats["failed"] == 0
    assert all(p.local_path and Path(p.local_path).exists() for p in bundle.photos)

    geocode = await geocode_property(
        bundle.fields["address"], bundle.fields["city"],
        bundle.fields["state"], bundle.fields["zip"],
        cache=GeocodeCache(tmp_path / "geocode.json", 180, 7),
        timeout_s=cfg.geocode.timeout_s, retries=cfg.geocode.retries,
        rate_limit_s=cfg.geocode.rate_limit_s,
        nominatim_enabled=cfg.geocode.nominatim_enabled,
        multi_match_radius_m=cfg.geocode.multi_match_radius_m,
    )
    assert geocode is not None and geocode.quality in ("exact", "multi_consistent")

    signals = run_prechecks(bundle, cfg, geocode)

    # ---- the GPS proof: all 42 points, live ----
    gps = signals.gps
    assert gps["valid"] == 42 and gps["unknown"] == 0
    assert haversine_m(tuple(gps["medoid"]), EXPECTED_MEDOID) < 100
    assert len(gps["outliers"]) == 1
    assert gps["outliers"][0]["guid"].startswith(OUTLIER_GUID)
    assert abs(gps["outliers"][0]["dist_m"] - 723_286) < 2_000
    assert gps["medoid_to_property_m"] <= 50
    assert gps["location_match"] == "pass"
    assert signals.fraud_signals == []
    assert signals.short_circuit is None

    print(
        f"\nLIVE GPS PROOF — order {ORDER}:\n"
        f"  photos: {len(bundle.photos)} review + {len(bundle.aux_gps)} aux "
        f"({gps['valid']} GPS points, {gps['unknown']} unknown)\n"
        f"  medoid: {gps['medoid']}  (property {gps['medoid_to_property_m']} m away, "
        f"geocode source={geocode.source}/{geocode.quality})\n"
        f"  outliers: {[(o['guid'][:8], o['dist_m']) for o in gps['outliers']]}\n"
        f"  location_match: {gps['location_match']}  fraud_signals: {signals.fraud_signals}\n"
        f"  time_on_site: {signals.time_on_site_min} min, span {signals.photo_time_span_min} min, "
        f"date_skew {signals.date_skew_days} d"
    )
