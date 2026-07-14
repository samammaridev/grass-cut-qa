"""GPS / location-match rule (ORCHESTRATION.md §11, adjudicated spec 2026-07-13).

Validated against live test order 500119014: 42 GPS points, 41 inliers within
55 m, one outlier 723.286 km away; Census geocode of the property address lands
9 m from the photo medoid.

Hard rules encoded here:
- Cluster center is the MEDOID. The arithmetic mean of lat/lon is banned: on
  the real order a single 723 km outlier drags the mean 22.34 km off, making
  every honest photo look like an outlier (regression-tested in test_geo.py).
- Code alone only ever produces location_match "pass" or "unverified" outcomes
  for the pipeline; "fail" is a deterministic *signal* (wrong_property) into
  the drafter -> hook gates -> verifier -> adjudicator chain, never a verdict.
- Missing, junk, or out-of-range GPS is never a fraud signal.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

R_EARTH_M = 6371008.8
# Points outside this box (US incl. AK/HI/PR) are excluded as suspect (swapped
# axes, hemisphere sign flips) rather than "repaired" — exclusion can't accuse.
US_LAT = (17.0, 72.0)
US_LON = (-180.0, -65.0)
NULL_ISLAND_EPS = 1e-6

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "SafeGuard-GCQA/0.1 (operations@safemonitor.net)"


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in meters. asin argument clamped against float rounding."""
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * R_EARTH_M * math.asin(min(1.0, math.sqrt(max(0.0, h))))


@dataclass(frozen=True)
class GpsPoint:
    guid: str
    lat: float
    lon: float
    desc_prefix: str = ""
    desc_text: str = ""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    source: str            # "census" | "nominatim"
    quality: str           # "exact" | "multi_consistent" | "approximate"
    matched_address: str = ""
    cached: bool = False


def filter_valid_points(raw: list[dict]) -> tuple[list[GpsPoint], int, list[str]]:
    """Split photo metadata into usable GPS points vs unknowns.

    Returns (valid_points, unknown_count, notes). Exclusion reasons are notes,
    never signals.
    """
    valid: list[GpsPoint] = []
    unknown = 0
    notes: set[str] = set()
    for item in raw:
        lat, lon = item.get("lat"), item.get("lon")
        if lat is None or lon is None:
            unknown += 1
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            unknown += 1
            notes.add("gps_suspect")
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            unknown += 1
            notes.add("gps_suspect")
            continue
        if abs(lat) < NULL_ISLAND_EPS and abs(lon) < NULL_ISLAND_EPS:
            unknown += 1
            notes.add("gps_suspect")
            continue
        # integral lat==lon pairs (0/0 handled above, -1/-1, 5/5 ...) are sentinel patterns
        if lat == lon and float(lat).is_integer():
            unknown += 1
            notes.add("gps_suspect")
            continue
        if not (US_LAT[0] <= lat <= US_LAT[1] and US_LON[0] <= lon <= US_LON[1]):
            unknown += 1
            notes.add("gps_suspect")
            continue
        valid.append(
            GpsPoint(
                guid=str(item.get("guid", "")),
                lat=lat,
                lon=lon,
                desc_prefix=str(item.get("descPrefix", "")),
                desc_text=str(item.get("descText", "")),
            )
        )
    return valid, unknown, sorted(notes)


def medoid_point(points: list[GpsPoint], tiebreak_ref: tuple[float, float] | None = None) -> GpsPoint:
    """Point minimizing the sum of haversine distances to all others (O(n^2), n<=~100).

    Ties break toward tiebreak_ref (the geocoded property, when known), then lowest guid.
    NOTE: never replace with an arithmetic mean — see module docstring.
    """
    if not points:
        raise ValueError("medoid of empty point set")
    best: GpsPoint | None = None
    best_key: tuple = ()
    for p in points:
        total = sum(haversine_m((p.lat, p.lon), (q.lat, q.lon)) for q in points)
        ref_d = haversine_m((p.lat, p.lon), tiebreak_ref) if tiebreak_ref else 0.0
        key = (round(total, 6), round(ref_d, 6), p.guid)
        if best is None or key < best_key:
            best, best_key = p, key
    return best


def split_outliers(
    points: list[GpsPoint], center: GpsPoint, threshold_m: float
) -> tuple[list[GpsPoint], list[tuple[GpsPoint, float]]]:
    """Inliers vs outliers (strictly > threshold_m from the medoid)."""
    inliers: list[GpsPoint] = []
    outliers: list[tuple[GpsPoint, float]] = []
    for p in points:
        d = haversine_m((p.lat, p.lon), (center.lat, center.lon))
        if d > threshold_m:
            outliers.append((p, d))
        else:
            inliers.append(p)
    outliers.sort(key=lambda t: -t[1])
    return inliers, outliers


def build_clusters(points: list[GpsPoint], link_m: float) -> list[list[GpsPoint]]:
    """Single-linkage clustering (union-find): points chained by pairwise links
    <= link_m form one cluster. O(n^2), n <= ~100. Largest first."""
    n = len(points)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = points[i], points[j]
            if haversine_m((a.lat, a.lon), (b.lat, b.lon)) <= link_m:
                parent[find(i)] = find(j)

    groups: dict[int, list[GpsPoint]] = {}
    for i, p in enumerate(points):
        groups.setdefault(find(i), []).append(p)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0].guid))


def choose_home_cluster(
    clusters: list[list[GpsPoint]], geocode_ref: tuple[float, float] | None,
    min_points: int,
) -> list[GpsPoint]:
    """Pick the cluster the location verdict is judged on.

    Preference: among clusters with >= min_points members (tiny clusters and
    singletons can be device-drift artifacts and must not become the anchor),
    the one nearest the geocoded property; without a geocode, the largest.
    If no cluster reaches min_points, fall back to all clusters.

    This choice is what makes the rule robust to the scattered-points case:
    a global medoid can land on a stray point *between* clusters, turning the
    honest at-property cluster into "outliers" (caught by test T19).
    """
    eligible = [c for c in clusters if len(c) >= min_points] or clusters
    if geocode_ref is None:
        return eligible[0]  # largest (pre-sorted)
    return min(
        eligible,
        key=lambda c: (
            min(haversine_m((p.lat, p.lon), geocode_ref) for p in c),
            -len(c),
            c[0].guid,
        ),
    )


# ---------------------------------------------------------------------------
# Geocoding: Census primary, Nominatim fallback, persistent cache.
# ---------------------------------------------------------------------------

_nominatim_lock = asyncio.Lock()
_nominatim_last_call = 0.0


def normalize_address(address: str, city: str, state: str, zipcode: str) -> str:
    one_line = f"{address}, {city}, {state} {zipcode}"
    return re.sub(r"\s+", " ", one_line).strip().upper()


class GeocodeCache:
    """address -> geocode result, persisted as JSON. Network failures are never cached."""

    def __init__(self, path: Path, positive_ttl_days: int, negative_ttl_days: int):
        self._path = path
        self._pos_ttl = positive_ttl_days * 86400
        self._neg_ttl = negative_ttl_days * 86400
        try:
            self._data: dict = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, key: str) -> GeocodeResult | None | str:
        """GeocodeResult on positive hit, "negative" on cached no-match, None on miss."""
        entry = self._data.get(key)
        if not entry:
            return None
        age = time.time() - entry.get("ts", 0)
        if entry.get("negative"):
            return "negative" if age < self._neg_ttl else None
        if age >= self._pos_ttl:
            return None
        return GeocodeResult(
            lat=entry["lat"], lon=entry["lon"], source=entry["source"],
            quality=entry["quality"], matched_address=entry.get("matched_address", ""),
            cached=True,
        )

    def put(self, key: str, result: GeocodeResult | None) -> None:
        if result is None:
            self._data[key] = {"negative": True, "ts": time.time()}
        else:
            self._data[key] = {
                "lat": result.lat, "lon": result.lon, "source": result.source,
                "quality": result.quality, "matched_address": result.matched_address,
                "ts": time.time(),
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data))
        except OSError:
            pass  # cache persistence is best-effort


async def _census_geocode(
    client: httpx.AsyncClient, one_line: str, city: str, zipcode: str,
    timeout_s: float, retries: int, multi_match_radius_m: float,
) -> GeocodeResult | None | str:
    """Returns GeocodeResult, None (definitive no-match/ambiguous), or "error" (transient).

    Quality is derived by comparing the INPUT city/zip against the match's
    addressComponents. The JSON onelineaddress endpoint returns NO matchType
    field (verified live 2026-07-13) — Census can silently relax a component
    (e.g. snap a typo'd zip to a same-named street in another town, which its
    own batch endpoint classifies Non_Exact), and an accusation-capable "exact"
    must never come from a relaxed match.
    """
    params = {"address": one_line, "benchmark": "Public_AR_Current", "format": "json"}
    for attempt in range(retries + 1):
        try:
            resp = await client.get(CENSUS_URL, params=params, timeout=timeout_s)
            resp.raise_for_status()
            matches = resp.json()["result"]["addressMatches"]
            break
        except (httpx.HTTPError, KeyError, ValueError):
            if attempt == retries:
                return "error"
            await asyncio.sleep(1.5 ** attempt)
    if not matches:
        return None
    first = matches[0]
    lat, lon = float(first["coordinates"]["y"]), float(first["coordinates"]["x"])  # y=lat, x=lon
    components = first.get("addressComponents") or {}
    zip_ok = str(components.get("zip", "")).strip() == str(zipcode).strip()
    city_ok = str(components.get("city", "")).strip().upper() == str(city).strip().upper()
    quality = "exact" if (zip_ok and city_ok) else "approximate"
    if len(matches) > 1:
        spread_ok = all(
            haversine_m((lat, lon), (float(m["coordinates"]["y"]), float(m["coordinates"]["x"])))
            <= multi_match_radius_m
            for m in matches[1:]
        )
        if not spread_ok:
            return None  # ambiguous — let the fallback try, else unverified
        if quality == "exact":
            quality = "multi_consistent"
    return GeocodeResult(lat=lat, lon=lon, source="census", quality=quality,
                         matched_address=str(first.get("matchedAddress", "")))


async def _nominatim_geocode(
    client: httpx.AsyncClient, one_line: str, city: str, zipcode: str,
    timeout_s: float, rate_limit_s: float,
) -> GeocodeResult | None | str:
    global _nominatim_last_call
    async with _nominatim_lock:  # usage policy: max 1 req/s globally
        wait = rate_limit_s - (time.monotonic() - _nominatim_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _nominatim_last_call = time.monotonic()
        try:
            resp = await client.get(
                NOMINATIM_URL,
                params={"q": one_line, "format": "json", "limit": 1,
                        "countrycodes": "us", "addressdetails": 1},
                headers={"User-Agent": NOMINATIM_UA},
                timeout=timeout_s,
            )
            resp.raise_for_status()
            hits = resp.json()
        except (httpx.HTTPError, ValueError):
            return "error"
    if not hits:
        return None
    hit = hits[0]
    display = str(hit.get("display_name", ""))
    house_number = one_line.split(" ", 1)[0]
    # "exact" (accusation-capable) requires house-number-level match AND a
    # corroborating component (zip or city) — the house number alone can match
    # a same-numbered address in a different town.
    addr = hit.get("address") or {}
    zip_ok = str(addr.get("postcode", "")).strip()[:5] == str(zipcode).strip()[:5]
    hit_city = str(addr.get("city") or addr.get("town") or addr.get("village") or "").upper()
    city_ok = bool(hit_city) and hit_city == str(city).strip().upper()
    house_ok = display.split(",")[0].strip() == house_number
    quality = "exact" if house_ok and (zip_ok or city_ok) else "approximate"
    return GeocodeResult(lat=float(hit["lat"]), lon=float(hit["lon"]), source="nominatim",
                         quality=quality, matched_address=display)


async def geocode_property(
    address: str, city: str, state: str, zipcode: str, *,
    cache: GeocodeCache, timeout_s: float, retries: int, rate_limit_s: float,
    nominatim_enabled: bool, multi_match_radius_m: float,
    client: httpx.AsyncClient | None = None,
) -> GeocodeResult | None:
    """Census primary, Nominatim fallback, cache-first. None = unverifiable (never an error)."""
    key = normalize_address(address, city, state, zipcode)
    cached = cache.get(key)
    if isinstance(cached, GeocodeResult):
        return cached
    if cached == "negative":
        return None

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        result = await _census_geocode(client, key, city, zipcode,
                                       timeout_s, retries, multi_match_radius_m)
        if result == "error" or result is None:
            if nominatim_enabled:
                nm = await _nominatim_geocode(client, key, city, zipcode,
                                              timeout_s, rate_limit_s)
                if isinstance(nm, GeocodeResult):
                    cache.put(key, nm)
                    return nm
                if nm is None and result is None:
                    cache.put(key, None)  # both definitively found nothing
                return None  # transient failure somewhere: not cached
            if result is None:
                cache.put(key, None)
            return None
        cache.put(key, result)
        return result
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# The pure precheck: points + geocode -> gps signal block
# ---------------------------------------------------------------------------

def gps_precheck(
    raw_points: list[dict],
    geocode: GeocodeResult | None,
    *,
    gps_outlier_m: float,
    min_gps_points: int,
    gps_property_match_m: float,
    gps_property_fail_km: float,
    gps_mixed_cluster_min_points: int,
) -> tuple[dict, list[str], list[str]]:
    """Compute the Signals.gps block. Returns (gps_dict, fraud_signals, documentation_gaps).

    Invariants: never emits a fraud signal for missing/junk GPS; "fail" requires
    exact-quality geocode + >= min_gps_points + no mixed clusters + >= fail radius.
    """
    fraud: list[str] = []
    gaps: list[str] = []
    valid, unknown, notes = filter_valid_points(raw_points)
    notes = list(notes)

    gps: dict = {
        "medoid": None, "medoid_guid": None,
        "valid": len(valid), "unknown": unknown,
        "outlier_fraction": 0.0, "outliers": [],
        "mixed_clusters": False,
        "property_geocode": None, "medoid_to_property_m": None,
        "location_match": "unverified", "notes": notes,
    }
    if geocode is not None:
        gps["property_geocode"] = {
            "lat": geocode.lat, "lon": geocode.lon, "source": geocode.source,
            "quality": geocode.quality, "cached": geocode.cached,
        }

    if not valid:
        gaps.append("no_gps_metadata")
        return gps, fraud, gaps
    if len(valid) < min_gps_points:
        gaps.append("insufficient_gps_points")

    ref = (geocode.lat, geocode.lon) if geocode else None
    clusters = build_clusters(valid, gps_outlier_m)
    home = choose_home_cluster(clusters, ref, min_gps_points)
    center = medoid_point(home, tiebreak_ref=ref)
    gps["medoid"] = [round(center.lat, 6), round(center.lon, 6)]
    gps["medoid_guid"] = center.guid

    home_ids = {id(p) for p in home}
    outliers = sorted(
        ((p, haversine_m((p.lat, p.lon), (center.lat, center.lon)))
         for p in valid if id(p) not in home_ids),
        key=lambda t: -t[1],
    )
    gps["outliers"] = [
        {"guid": p.guid, "dist_m": int(round(d)), "descPrefix": p.desc_prefix,
         "descText": p.desc_text}
        for p, d in outliers
    ]
    gps["outlier_fraction"] = round(len(outliers) / len(valid), 3)

    # A coherent second cluster = the honest two-properties-one-upload case:
    # cap at unverified + suppress signals, never accuse.
    mixed = any(
        len(c) >= gps_mixed_cluster_min_points for c in clusters if c is not home
    )
    gps["mixed_clusters"] = mixed
    if mixed:
        gaps.append("mixed_clusters")

    # gps_static: zero variance across >= 2 points is physically implausible for
    # a real device walking a property — informational note, never a signal.
    if len(valid) >= 2 and len({(p.lat, p.lon) for p in valid}) == 1:
        notes.append("gps_static")

    # Strict-majority-outlier fraud signal (suppressed for honest mixed uploads).
    # Strict >: an exact 50/50 split is ambiguity, not evidence.
    if len(valid) >= min_gps_points and 2 * len(outliers) > len(valid) and not mixed:
        fraud.append("gps_outlier")

    # Location verdict: cluster medoid vs geocoded property.
    if geocode is None:
        gps["location_match"] = "unverified"
    else:
        d_prop = haversine_m((center.lat, center.lon), (geocode.lat, geocode.lon))
        gps["medoid_to_property_m"] = int(round(d_prop))
        # Guard against the small-at-property-cluster bypass: if ANY valid photo
        # sits at the property, "the whole set is elsewhere" is false and
        # wrong_property must not fire (e.g. 2 honest photos at the property +
        # 3 Bid photos shot at the vendor's office would otherwise elect the
        # office as home cluster and accuse an honest order).
        at_property = any(
            haversine_m((p.lat, p.lon), (geocode.lat, geocode.lon)) <= gps_property_match_m
            for p in valid
        )
        if mixed:
            gps["location_match"] = "unverified"
        elif d_prop <= gps_property_match_m:
            gps["location_match"] = "pass"
        elif (
            d_prop >= gps_property_fail_km * 1000.0
            and geocode.quality in ("exact", "multi_consistent")
            and len(home) >= min_gps_points   # the HOME cluster must be substantial —
            and not at_property               # len(valid) would let a 2-point cluster
        ):                                    # elected via the eligibility fallback accuse
            gps["location_match"] = "fail"
            fraud.append("wrong_property")
        else:
            gps["location_match"] = "unverified"
            if at_property and d_prop >= gps_property_fail_km * 1000.0:
                notes.append("at_property_points_outside_home_cluster")

    gps["notes"] = sorted(set(notes))
    return gps, fraud, gaps
