"""GPS/location-match rule tests T1-T19 — the adjudicated test matrix.

T1/T2 run against the REAL 42-point fixture from live order 500119014; the
expected numbers were measured against the live API on 2026-07-13.
"""

import math

import httpx
import pytest

from gcqa.geo import (
    GeocodeCache,
    GeocodeResult,
    GpsPoint,
    R_EARTH_M,
    filter_valid_points,
    geocode_property,
    gps_precheck,
    haversine_m,
    medoid_point,
    split_outliers,
)
from tests.conftest import EXPECTED_MEDOID, OUTLIER_GUID, PROPERTY_GEOCODE

PC = dict(gps_outlier_m=800.0, min_gps_points=3, gps_property_match_m=500.0,
          gps_property_fail_km=10.0, gps_mixed_cluster_min_points=3)


def offset_point(lat: float, lon: float, north_m: float = 0.0, east_m: float = 0.0):
    """Displace a point by meters (small-angle; fine for test construction)."""
    dlat = math.degrees(north_m / R_EARTH_M)
    dlon = math.degrees(east_m / (R_EARTH_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def pt(guid, lat, lon, prefix="After", text="test"):
    return {"guid": guid, "lat": lat, "lon": lon, "descPrefix": prefix, "descText": text}


# --- T1: the real order ------------------------------------------------------

def test_t1_real_fixture(raw_gps_points, geocode_result):
    gps, fraud, gaps = gps_precheck(raw_gps_points, geocode_result, **PC)
    assert gps["valid"] == 42 and gps["unknown"] == 0
    assert abs(gps["medoid"][0] - EXPECTED_MEDOID[0]) < 0.001
    assert abs(gps["medoid"][1] - EXPECTED_MEDOID[1]) < 0.001
    assert len(gps["outliers"]) == 1
    outlier = gps["outliers"][0]
    assert outlier["guid"].startswith(OUTLIER_GUID)
    assert abs(outlier["dist_m"] - 723_286) < 2_000
    assert outlier["descText"] == "Rear of House"
    assert gps["medoid_to_property_m"] <= 50
    assert gps["location_match"] == "pass"
    assert fraud == [] and gaps == []
    assert gps["outlier_fraction"] == pytest.approx(1 / 42, abs=0.01)


# --- T2: the mean-center bug stays dead --------------------------------------

def test_t2_mean_ban_regression(raw_gps_points):
    valid, _, _ = filter_valid_points(raw_gps_points)
    center = medoid_point(valid)
    mean = (sum(p.lat for p in valid) / len(valid), sum(p.lon for p in valid) / len(valid))
    # The single 723 km outlier drags the arithmetic mean ~17 km off the cluster
    # (723 km / 42 points) — with a mean, every honest photo looks like an outlier.
    assert haversine_m(mean, (center.lat, center.lon)) > 15_000
    inliers, outliers = split_outliers(valid, center, 800.0)
    assert len(inliers) == 41 and len(outliers) == 1
    assert all(haversine_m((p.lat, p.lon), (center.lat, center.lon)) < 800 for p in inliers)


# --- T3: whole set at the WRONG property (the 1500 km scenario) --------------

def test_t3_wrong_property_cluster(raw_gps_points, geocode_result):
    dlat = 34.6358 - EXPECTED_MEDOID[0]
    dlon = -79.0135 - EXPECTED_MEDOID[1]
    moved = [dict(p, lat=p["lat"] + dlat, lon=p["lon"] + dlon) for p in raw_gps_points]
    gps, fraud, gaps = gps_precheck(moved, geocode_result, **PC)
    assert gps["location_match"] == "fail"
    assert "wrong_property" in fraud
    assert "gps_outlier" not in fraud          # internally tight cluster
    assert gps["medoid_to_property_m"] > 500_000


# --- T4 / T5: missing or minimal GPS never accuses ----------------------------

def test_t4_no_gps(geocode_result):
    points = [pt(f"g{i}", None, None) for i in range(5)]
    gps, fraud, gaps = gps_precheck(points, geocode_result, **PC)
    assert gps["valid"] == 0 and gps["unknown"] == 5
    assert gps["medoid"] is None
    assert gps["location_match"] == "unverified"
    assert "no_gps_metadata" in gaps and fraud == []


def test_t5a_single_point_at_property(geocode_result):
    lat, lon = offset_point(*PROPERTY_GEOCODE, north_m=9)
    gps, fraud, gaps = gps_precheck([pt("solo", lat, lon)], geocode_result, **PC)
    assert gps["location_match"] == "pass"
    assert "insufficient_gps_points" in gaps and fraud == []


def test_t5b_single_point_far_away_never_fails(geocode_result):
    gps, fraud, gaps = gps_precheck([pt("solo", 47.6, -122.3)], geocode_result, **PC)
    assert gps["location_match"] == "unverified"   # fail suppressed below min_gps_points
    assert fraud == [] and "insufficient_gps_points" in gaps


# --- T6: bimodal mixed upload -> escalate path, never fraud -------------------

def test_t6_mixed_clusters_suppress_signals(geocode_result):
    home = PROPERTY_GEOCODE
    other = offset_point(*home, north_m=100_000)   # second property 100 km away
    points = [pt(f"a{i}", *offset_point(*home, east_m=5 * i)) for i in range(4)]
    points += [pt(f"b{i}", *offset_point(*other, east_m=5 * i)) for i in range(4)]
    gps, fraud, gaps = gps_precheck(points, geocode_result, **PC)
    assert gps["mixed_clusters"] is True
    assert "mixed_clusters" in gaps
    assert "gps_outlier" not in fraud and "wrong_property" not in fraud
    assert gps["location_match"] == "unverified"   # capped even though medoid is at home
    # tiebreak toward the geocoded property: medoid must be in the home cluster
    assert haversine_m(tuple(gps["medoid"]), home) < 1_000


# --- T7: antimeridian and hemisphere sign sanity ------------------------------

def test_t7_antimeridian_and_sign():
    d = haversine_m((0.0, 179.9), (0.0, -179.9))
    assert abs(d - 22_239) < 50                    # 0.2 deg at the equator
    flipped = haversine_m((39.9, -83.81), (39.9, 83.81))
    assert flipped > 1_000_000                     # sign flips are never "equal"


# --- T8: junk coordinates are excluded, never accused -------------------------

def test_t8_junk_coordinates(geocode_result):
    points = [
        pt("null", 0.0, 0.0),
        pt("badlat", 91.0, -83.8),
        pt("badlon", 39.9, 200.0),
        pt("sentinel", -1.0, -1.0),
        pt("europe", 48.85, 2.35),                 # outside US box (swapped/sign case)
        pt("ok1", *offset_point(*PROPERTY_GEOCODE, east_m=5)),
        pt("ok2", *offset_point(*PROPERTY_GEOCODE, east_m=10)),
        pt("ok3", *offset_point(*PROPERTY_GEOCODE, east_m=15)),
    ]
    gps, fraud, gaps = gps_precheck(points, geocode_result, **PC)
    assert gps["valid"] == 3 and gps["unknown"] == 5
    assert "gps_suspect" in gps["notes"]
    assert gps["outliers"] == [] and fraud == []
    assert gps["location_match"] == "pass"


# --- T9 / T10: geocoder behavior ----------------------------------------------

@pytest.mark.asyncio
async def test_t9_geocoder_timeout_degrades(tmp_path):
    async def raise_timeout(request):
        raise httpx.ConnectTimeout("boom")

    cache = GeocodeCache(tmp_path / "geo.json", 180, 7)
    client = httpx.AsyncClient(transport=httpx.MockTransport(raise_timeout))
    result = await geocode_property(
        "1801 WOODWARD AVE", "SPRINGFIELD", "OH", "45506",
        cache=cache, timeout_s=1, retries=0, rate_limit_s=0,
        nominatim_enabled=True, multi_match_radius_m=500, client=client,
    )
    await client.aclose()
    assert result is None
    # transient failure must NOT be cached as a negative
    assert cache.get("1801 WOODWARD AVE, SPRINGFIELD, OH 45506") is None


@pytest.mark.asyncio
async def test_t10_multi_match_handling(tmp_path):
    def census_body(matches):
        return {"result": {"addressMatches": matches}}

    def match_at(lat, lon):
        return {"coordinates": {"x": lon, "y": lat}, "matchedAddress": "X"}

    calls = {"census": 0, "nominatim": 0}

    async def handler(request):
        if "census" in str(request.url):
            calls["census"] += 1
            return httpx.Response(200, json=census_body(
                [match_at(39.9039, -83.8134), match_at(39.98, -83.75)]  # 8 km apart -> ambiguous
            ))
        calls["nominatim"] += 1
        return httpx.Response(200, json=[
            {"lat": "39.9038310", "lon": "-83.8134230",
             "display_name": "1801, Woodward Avenue, Springfield, Ohio",
             "address": {"postcode": "45506", "city": "Springfield"}}
        ])

    cache = GeocodeCache(tmp_path / "geo.json", 180, 7)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await geocode_property(
        "1801 WOODWARD AVE", "SPRINGFIELD", "OH", "45506",
        cache=cache, timeout_s=1, retries=0, rate_limit_s=0,
        nominatim_enabled=True, multi_match_radius_m=500, client=client,
    )
    await client.aclose()
    assert result is not None and result.source == "nominatim"
    assert result.quality == "exact"               # house-number-level hit
    assert calls == {"census": 1, "nominatim": 1}


@pytest.mark.asyncio
async def test_t10b_census_multi_consistent(tmp_path):
    async def handler(request):
        components = {"city": "SPRINGFIELD", "zip": "45506"}
        return httpx.Response(200, json={"result": {"addressMatches": [
            {"coordinates": {"x": -83.8134, "y": 39.9039}, "matchedAddress": "A",
             "addressComponents": components},
            {"coordinates": {"x": -83.8135, "y": 39.9040}, "matchedAddress": "B",
             "addressComponents": components},
        ]}})

    cache = GeocodeCache(tmp_path / "geo.json", 180, 7)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await geocode_property(
        "1801 WOODWARD AVE", "SPRINGFIELD", "OH", "45506",
        cache=cache, timeout_s=1, retries=0, rate_limit_s=0,
        nominatim_enabled=False, multi_match_radius_m=500, client=client,
    )
    await client.aclose()
    assert result is not None and result.quality == "multi_consistent"


# --- T11 / T12: numeric edges --------------------------------------------------

def test_t11_antipodal_no_domain_error():
    d = haversine_m((10.0, 10.0), (-10.0, -170.0))
    assert abs(d - math.pi * R_EARTH_M) / (math.pi * R_EARTH_M) < 0.005


def test_t12_outlier_threshold_strictly_greater():
    center = GpsPoint("c", *PROPERTY_GEOCODE)
    near = GpsPoint("near", *offset_point(*PROPERTY_GEOCODE, north_m=799))
    far = GpsPoint("far", *offset_point(*PROPERTY_GEOCODE, north_m=801))
    _, outliers = split_outliers([center, near, far], center, 800.0)
    assert [p.guid for p, _ in outliers] == ["far"]


# --- T13-T19: red-team cases ---------------------------------------------------

def test_t13_quantized_coords_stay_inliers(raw_gps_points, geocode_result):
    """2-decimal coords (~500 m quantization here) must not become outliers."""
    points = list(raw_gps_points) + [pt("quant", 39.90, -83.81)]
    gps, fraud, _ = gps_precheck(points, geocode_result, **PC)
    assert all(not o["guid"] == "quant" for o in gps["outliers"])
    assert fraud == []


def test_t14_static_gps_is_note_not_signal(geocode_result):
    lat, lon = offset_point(*PROPERTY_GEOCODE, north_m=5)
    points = [pt(f"g{i}", lat, lon) for i in range(6)]
    gps, fraud, _ = gps_precheck(points, geocode_result, **PC)
    assert "gps_static" in gps["notes"]
    assert fraud == [] and gps["location_match"] == "pass"


def test_t16_approximate_geocode_never_fails():
    approx = GeocodeResult(lat=PROPERTY_GEOCODE[0], lon=PROPERTY_GEOCODE[1],
                           source="nominatim", quality="approximate")
    far = offset_point(*PROPERTY_GEOCODE, north_m=100_000)
    points = [pt(f"g{i}", *offset_point(*far, east_m=4 * i)) for i in range(5)]
    gps, fraud, _ = gps_precheck(points, approx, **PC)
    assert gps["location_match"] == "unverified"   # capped by quality, despite 100 km
    assert "wrong_property" not in fraud


def test_t18_mixed_and_far_is_unverified(geocode_result):
    far = offset_point(*PROPERTY_GEOCODE, north_m=50_000)
    farther = offset_point(*PROPERTY_GEOCODE, north_m=900_000)
    points = [pt(f"a{i}", *offset_point(*far, east_m=5 * i)) for i in range(4)]
    points += [pt(f"b{i}", *offset_point(*farther, east_m=5 * i)) for i in range(4)]
    gps, fraud, gaps = gps_precheck(points, geocode_result, **PC)
    assert gps["mixed_clusters"] is True
    assert gps["location_match"] == "unverified" and "wrong_property" not in fraud


@pytest.mark.asyncio
async def test_t20_census_relaxed_match_is_approximate(tmp_path):
    """The Census JSON endpoint has NO matchType field (verified live) — a zip-typo
    match snapped to another town must come back 'approximate' (component
    comparison), so it can never satisfy the wrong_property exact-quality gate."""
    async def handler(request):
        return httpx.Response(200, json={"result": {"addressMatches": [
            {"coordinates": {"x": -84.19, "y": 39.76}, "matchedAddress":
             "120 W 2ND ST, DAYTON, OH, 45402",
             "addressComponents": {"city": "DAYTON", "zip": "45402"}},
        ]}})

    cache = GeocodeCache(tmp_path / "geo.json", 180, 7)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await geocode_property(
        "120 W 2ND ST", "SPRINGFIELD", "OH", "45402",   # input city != matched city
        cache=cache, timeout_s=1, retries=0, rate_limit_s=0,
        nominatim_enabled=False, multi_match_radius_m=500, client=client,
    )
    await client.aclose()
    assert result is not None and result.quality == "approximate"
    # and an approximate geocode can never produce fail/wrong_property (T16 rule)
    far_cluster = [pt(f"g{i}", 39.9039, -83.8134 + 0.00005 * i) for i in range(5)]
    gps, fraud, _ = gps_precheck(far_cluster, result, **PC)
    assert gps["location_match"] == "unverified" and "wrong_property" not in fraud


def test_t21_at_property_points_block_wrong_property(geocode_result):
    """2 honest photos at the property + 3 Bid photos shot at the vendor's office
    15 km away: the office cluster wins home-cluster election (>= min_points),
    but photos AT the property must veto the wrong_property accusation."""
    points = [pt("home1", *offset_point(*PROPERTY_GEOCODE, east_m=5)),
              pt("home2", *offset_point(*PROPERTY_GEOCODE, east_m=10))]
    office = offset_point(*PROPERTY_GEOCODE, north_m=15_000)
    points += [pt(f"office{i}", *offset_point(*office, east_m=5 * i), prefix="Bid")
               for i in range(3)]
    gps, fraud, gaps = gps_precheck(points, geocode_result, **PC)
    assert "wrong_property" not in fraud and fraud == []
    assert gps["location_match"] == "unverified"
    assert "at_property_points_outside_home_cluster" in gps["notes"]


def test_t19_scattered_majority_outliers_fire_signal(geocode_result):
    points = [pt(f"home{i}", *offset_point(*PROPERTY_GEOCODE, east_m=5 * i)) for i in range(3)]
    scatter = [100_000, 300_000, 550_000, 900_000]
    points += [pt(f"far{i}", *offset_point(*PROPERTY_GEOCODE, north_m=m)) for i, m in enumerate(scatter)]
    gps, fraud, _ = gps_precheck(points, geocode_result, **PC)
    assert gps["mixed_clusters"] is False          # scattered, not a coherent second cluster
    assert "gps_outlier" in fraud                  # 4 of 7 valid points are outliers
    assert gps["location_match"] == "pass"         # medoid still at the property


def test_t22_exact_half_outliers_is_ambiguity_not_signal(geocode_result):
    """§11 says 'majority' — an exact 50/50 split must not fire gps_outlier."""
    points = [pt(f"home{i}", *offset_point(*PROPERTY_GEOCODE, east_m=5 * i)) for i in range(2)]
    points += [pt("far0", *offset_point(*PROPERTY_GEOCODE, north_m=100_000)),
               pt("far1", *offset_point(*PROPERTY_GEOCODE, north_m=400_000))]
    gps, fraud, _ = gps_precheck(points, geocode_result, **PC)
    assert len(gps["outliers"]) == 2 and gps["valid"] == 4
    assert "gps_outlier" not in fraud
