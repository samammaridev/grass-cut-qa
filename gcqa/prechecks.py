"""C1 Pre-check engine: deterministic signals before any LLM spend.

Invariant (ORCHESTRATION.md §11): code may short-circuit only to
escalate_to_human — never to potential_fraud or good_to_pay. Accusations and
payments always require model + gates + verifier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import Config
from .geo import GeocodeResult, gps_precheck
from .prefetch import OrderBundle


class PipelineAbort(Exception):
    """Order identity/type mismatch — a pipeline error, never a review outcome."""


@dataclass
class Signals:
    identity_ok: bool = True
    work_code_ok: bool = True
    in_season: bool = True
    photo_count: int = 0
    photo_count_ok: bool = True
    gps: dict = field(default_factory=dict)
    time_on_site_min: int | None = None
    time_on_site_ok: bool = True
    photo_time_span_min: int | None = None
    date_skew_days: int | None = None
    documents: dict = field(default_factory=dict)
    photo_integrity: dict = field(default_factory=dict)
    documentation_gaps: list[str] = field(default_factory=list)
    fraud_signals: list[str] = field(default_factory=list)
    short_circuit: str | None = None          # only ever "escalate_to_human"
    short_circuit_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
)


def parse_dt(value: str | None) -> datetime | None:
    """Parse the API's three date formats defensively (API_DOCUMENTATION.md §3.2)."""
    if not value or not value.strip():
        return None
    value = value.strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
    try:  # ISO with Z or fractional seconds — keep local wall time (tz stripped),
        # consistent with the %z strptime branch: all API timestamps are compared
        # as wall-clock values, never mixed-offset arithmetic.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _taken_upload_stamped(photo) -> bool:
    """True when takenDate equals dateCreated modulo a whole-hour timezone
    offset (±3 s): the app stamped the capture time AT UPLOAD because the
    photo carried none. Found live on order 500119020 — takenDate tracked
    dateCreated second-for-second at +4 h, while the on-image watermarks
    showed the photos were really taken two days earlier."""
    t, c = parse_dt(photo.taken_date), parse_dt(photo.date_created)
    if not (t and c):
        return False
    off = (t - c).total_seconds()
    return abs(off - round(off / 3600) * 3600) <= 3


def _photo_integrity(photos, pc) -> dict:
    """Deterministic photo-evidence checks (found live on order 500119014:
    Before/After 'Front Lawn' shot 4 seconds apart, Before AFTER the After).

    - reused_pairs: exact (sha256) or near (dHash <= phash_max_distance)
      duplicates across DIFFERENT stages — the same image cannot be both
      Before and After.
    - staged_pairs: same-station Before->After pairs where After precedes
      Before, or the gap is under min_before_after_gap_min — no real work
      fits in that window.
    - same_stage_dupes: identical images within one stage (double uploads —
      a documentation gap, not an accusation).
    - taken_dates_unreliable: most takenDates are upload-stamped, so ALL
      timestamp-based checks (staged pairs, adjacency) are suppressed — the
      upload order proves nothing about capture order. The on-image watermark
      is then the only capture record; the reviewer is directed to read it.
    """
    reused, staged, same_stage = [], [], []

    with_both = [p for p in photos if p.taken_date and p.date_created]
    stamped = sum(1 for p in with_both if _taken_upload_stamped(p))
    taken_unreliable = bool(with_both) and stamped / len(with_both) > 0.8

    for i in range(len(photos)):
        for j in range(i + 1, len(photos)):
            a, b = photos[i], photos[j]
            exact = a.sha256 and a.sha256 == b.sha256
            near = (not exact and a.dhash is not None and b.dhash is not None
                    and bin(a.dhash ^ b.dhash).count("1") <= pc.phash_max_distance)
            if not (exact or near):
                continue
            pair = {"guid_a": a.guid[:8], "stage_a": a.desc_prefix, "station_a": a.desc_text,
                    "guid_b": b.guid[:8], "stage_b": b.desc_prefix, "station_b": b.desc_text,
                    "match": "exact" if exact else "near"}
            if a.desc_prefix != b.desc_prefix:
                reused.append(pair)
            else:
                same_stage.append(pair)

    by_station: dict[str, dict] = {}
    for p in [] if taken_unreliable else photos:
        if p.desc_prefix in ("Before", "After"):
            by_station.setdefault(p.desc_text, {}).setdefault(p.desc_prefix, []).append(p)
    for station, stages in by_station.items():
        for before in stages.get("Before", []):
            for after in stages.get("After", []):
                tb, ta = parse_dt(before.taken_date), parse_dt(after.taken_date)
                if not (tb and ta):
                    continue
                gap_s = (ta - tb).total_seconds()
                if gap_s < pc.min_before_after_gap_min * 60:
                    staged.append({
                        "station": station,
                        "before": {"guid": before.guid[:8], "taken": before.taken_date},
                        "after": {"guid": after.guid[:8], "taken": after.taken_date},
                        "gap_seconds": int(gap_s),
                        "issue": "after_precedes_before" if gap_s < 0 else "gap_too_short",
                    })

    # Photos taken seconds apart but labeled as DIFFERENT stations: code cannot
    # prove they show the same scene (re-shots evade perceptual hashes — found
    # live: same uncut lawn filed as "After Side Lawn 1" and "Before Rear Lawn"
    # 5 s apart, dHash distance 22). So code builds the suspect list and the
    # vision model is directed to compare each pair explicitly.
    adjacent = []
    timed = [] if taken_unreliable else \
        [(parse_dt(p.taken_date), p) for p in photos if parse_dt(p.taken_date)]
    timed.sort(key=lambda t: t[0])
    for i in range(len(timed) - 1):
        (ta, a), (tb, b) = timed[i], timed[i + 1]
        gap_s = (tb - ta).total_seconds()
        if gap_s <= pc.cross_station_adjacency_s and a.desc_text != b.desc_text:
            adjacent.append({
                "a": {"guid": a.guid[:8], "stage": a.desc_prefix, "station": a.desc_text,
                      "taken": a.taken_date},
                "b": {"guid": b.guid[:8], "stage": b.desc_prefix, "station": b.desc_text,
                      "taken": b.taken_date},
                "gap_seconds": int(gap_s),
            })

    return {"reused_pairs": reused, "staged_pairs": staged,
            "same_stage_dupes": same_stage, "adjacent_cross_station_pairs": adjacent,
            "taken_dates_unreliable": taken_unreliable}


def run_prechecks(bundle: OrderBundle, cfg: Config,
                  geocode: GeocodeResult | None) -> Signals:
    pc = cfg.prechecks
    s = Signals()
    fields = bundle.fields

    # --- identity / order-type (aborts, not outcomes) ---
    echoed = fields.get("order_number")
    if echoed and str(echoed) != str(bundle.order_number):
        s.identity_ok = False
        raise PipelineAbort(
            f"order identity mismatch: requested {bundle.order_number}, API echoed {echoed}"
        )
    work_code = (fields.get("work_code") or "").upper()
    if not work_code.startswith("GC"):
        s.work_code_ok = False
        raise PipelineAbort(f"not a grass-cut order: work code {work_code!r}")

    # --- season ---
    s.in_season = bool(fields.get("within_grass_season"))
    if not s.in_season:
        s.short_circuit = "escalate_to_human"
        s.short_circuit_reason = "order outside the property's grass season window"

    # --- photo counts / documentation integrity ---
    stats = bundle.fetch_stats
    s.photo_count = len(bundle.photos)
    listed = stats.get("review_photos", s.photo_count)
    if s.photo_count == 0:
        s.short_circuit = "escalate_to_human"
        s.short_circuit_reason = "no photo documentation available"
    elif s.photo_count < pc.min_review_photos:
        s.short_circuit = "escalate_to_human"
        s.short_circuit_reason = (
            f"only {s.photo_count} review photos (minimum {pc.min_review_photos})"
        )
    if listed and s.photo_count < listed * pc.min_photo_success_ratio:
        s.photo_count_ok = False
        s.documentation_gaps.append("photo_download_incomplete")
        s.short_circuit = "escalate_to_human"
        s.short_circuit_reason = (
            f"downloaded {s.photo_count}/{listed} review photos "
            f"(below {pc.min_photo_success_ratio:.0%}) — documentation unavailable"
        )
    declared = fields.get("number_of_photos")
    jpeg_listed = stats.get("jpeg_listed")
    if declared and jpeg_listed and jpeg_listed < declared * pc.min_photo_success_ratio:
        s.documentation_gaps.append("photo_count_mismatch")

    # --- GPS / location match (review photos + Bid/Document aux points) ---
    # Aux photos contribute coordinates only: their station text stays out of
    # Signals so no Bid/Document content ever reaches the model (§9).
    raw_points = [
        {"guid": p.guid, "lat": p.lat, "lon": p.lon,
         "descPrefix": p.desc_prefix, "descText": p.desc_text}
        for p in bundle.photos
    ] + [
        {"guid": p.guid, "lat": p.lat, "lon": p.lon, "descPrefix": "aux", "descText": ""}
        for p in bundle.aux_gps
    ]
    gps, gps_fraud, gps_gaps = gps_precheck(
        raw_points, geocode,
        gps_outlier_m=pc.gps_outlier_m,
        min_gps_points=pc.min_gps_points,
        gps_property_match_m=pc.gps_property_match_m,
        gps_property_fail_km=pc.gps_property_fail_km,
        gps_mixed_cluster_min_points=pc.gps_mixed_cluster_min_points,
    )
    # API-precomputed distances: cross-check annotation only — units unverified,
    # -1 = not computed on the test order. Never authoritative.
    gps["api_distance_fields"] = {
        "door_knock": fields.get("_api_dist_door_knock", -1),
        "farthest_to_property": fields.get("_api_dist_farthest_to_property", -1),
        "farthest_between": fields.get("_api_dist_farthest_between", -1),
    }
    api_far = gps["api_distance_fields"]["farthest_to_property"]
    own_far = gps["medoid_to_property_m"]
    if api_far not in (None, -1) and own_far is not None and api_far > 0:
        if own_far > 2 * api_far or api_far > 2 * max(own_far, 1):
            gps["notes"] = sorted(set(gps["notes"]) | {"api_distance_discrepancy"})
    s.gps = gps
    s.fraud_signals.extend(gps_fraud)
    s.documentation_gaps.extend(gps_gaps)
    # §11: majority of the photo set lacking usable metadata is itself a
    # documentation gap (never a fraud signal).
    if gps["unknown"] > gps["valid"]:
        s.documentation_gaps.append("gps_metadata_sparse")

    # --- photo integrity: duplicates + Before/After staging (code-only facts;
    #     the vision model cannot do pixel identity or EXIF chronology) ---
    s.photo_integrity = _photo_integrity(bundle.photos, pc)
    if s.photo_integrity["reused_pairs"]:
        s.fraud_signals.append("photo_reuse")
    if s.photo_integrity["staged_pairs"]:
        # a Before/After pair shot seconds apart (or After before Before) cannot
        # evidence work — the station's before/after proof is void
        s.fraud_signals.append("staged_documentation")
    if s.photo_integrity["same_stage_dupes"]:
        s.documentation_gaps.append("duplicate_photos_same_stage")
    if s.photo_integrity["taken_dates_unreliable"]:
        # takenDate was stamped at upload — capture chronology is unknowable
        # from metadata. A gap, never an accusation; the on-image watermark
        # becomes the capture record and the reviewer is directed to it.
        s.documentation_gaps.append("capture_times_upload_stamped")

    # --- vendor documents (questionnaire / invoice / audit reports) ---
    kinds = [d.desc_text for d in bundle.documents]
    s.documents = {
        "listed": stats.get("documents_listed", 0),
        "unique": len(bundle.documents),
        "kinds": sorted(set(kinds)),
        "invoice_present": any("invoice" in k.lower() for k in kinds),
        "update_present": any("update" in k.lower() for k in kinds),
    }
    if stats.get("documents_listed", 0) == 0:
        # informational only — absence of vendor paperwork is never a fraud signal
        s.documentation_gaps.append("no_vendor_documents")

    # --- time on site ---
    # -1 is the API's "not recorded" sentinel (observed live on 500119020):
    # missing data is a documentation gap, never a fraud signal.
    s.time_on_site_min = fields.get("time_on_site_min")
    if s.time_on_site_min is None or s.time_on_site_min < 0:
        s.documentation_gaps.append("time_on_site_missing")
    elif s.time_on_site_min < pc.min_time_on_site_min:
        s.time_on_site_ok = False
        s.fraud_signals.append("short_visit")

    # --- photo timestamps: span, skew vs claimed cut date, EXIF-vs-upload sanity ---
    taken = [parse_dt(p.taken_date) for p in bundle.photos]
    taken = [t for t in taken if t]
    if taken:
        span = max(taken) - min(taken)
        s.photo_time_span_min = int(span.total_seconds() // 60)
        cut = parse_dt(fields.get("date_grass_cut"))
        if cut:
            mid = sorted(taken)[len(taken) // 2]
            s.date_skew_days = abs((mid.date() - cut.date()).days)
            if s.date_skew_days > pc.date_skew_days:
                s.fraud_signals.append("date_skew")
        # A photo "taken" after its own server-side upload time is impossible —
        # forged EXIF (vendor controls takenDate, not dateCreated).
        for p in bundle.photos:
            t, c = parse_dt(p.taken_date), parse_dt(p.date_created)
            if t and c and (t - c).total_seconds() > 86400:
                if "date_skew" not in s.fraud_signals:
                    s.fraud_signals.append("date_skew")
                s.documentation_gaps.append("taken_after_upload")
                break
    else:
        s.documentation_gaps.append("no_taken_dates")

    s.fraud_signals = sorted(set(s.fraud_signals))
    s.documentation_gaps = sorted(set(s.documentation_gaps))
    return s
