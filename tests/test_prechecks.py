"""C1 tests: the §11 pre-check table against the real order + adversarial variants."""

import pytest

from gcqa.prechecks import PipelineAbort, parse_dt, run_prechecks
from gcqa.prefetch import OrderBundle, normalize_fields, split_items
from tests.conftest import OUTLIER_GUID


def make_bundle(luggage, images, order="500119014", **field_overrides) -> OrderBundle:
    fields = normalize_fields(luggage)
    fields.update(field_overrides)
    review, aux, _docs, counts = split_items(images)
    counts.update({"downloaded": len(review), "failed": 0, "failed_guids": []})
    return OrderBundle(order_number=order, fields=fields, photos=review,
                       aux_gps=aux, fetch_stats=counts)


def test_real_order_signals(luggage, images, config, geocode_result):
    s = run_prechecks(make_bundle(luggage, images), config, geocode_result)
    assert s.short_circuit is None
    assert s.photo_count == 32 and s.photo_count_ok
    assert s.in_season and s.time_on_site_ok
    assert s.time_on_site_min == 38
    assert s.gps["valid"] == 42                       # review + Bid aux points
    assert s.gps["location_match"] == "pass"
    assert len(s.gps["outliers"]) == 1
    assert s.gps["outliers"][0]["guid"].startswith(OUTLIER_GUID)
    # Found by a human reviewer on the live gallery, now pinned forever:
    # Before/After "Front Lawn" were taken 4 seconds apart — staged documentation.
    assert s.fraud_signals == ["staged_documentation"]
    staged = s.photo_integrity["staged_pairs"]
    front = [p for p in staged if p["station"] == "Grass Recut - Front Lawn"]
    assert front and 0 <= front[0]["gap_seconds"] <= 10
    assert s.date_skew_days is not None and s.date_skew_days <= 3


def test_wrong_property_variant(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    for p in bundle.photos + bundle.aux_gps:
        if p.lat is not None:
            p.lat, p.lon = p.lat - 5.268, p.lon + 4.8   # ~700+ km shift
    s = run_prechecks(bundle, config, geocode_result)
    assert s.gps["location_match"] == "fail"
    assert "wrong_property" in s.fraud_signals
    assert s.short_circuit is None                    # signal, never a code verdict


def test_out_of_season_short_circuits(luggage, images, config, geocode_result):
    s = run_prechecks(make_bundle(luggage, images, within_grass_season=False),
                      config, geocode_result)
    assert s.short_circuit == "escalate_to_human"
    assert "season" in s.short_circuit_reason


def test_no_photos_short_circuits(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    bundle.photos = []
    bundle.fetch_stats["review_photos"] = 0
    s = run_prechecks(bundle, config, geocode_result)
    assert s.short_circuit == "escalate_to_human"


def test_partial_download_short_circuits(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    bundle.photos = bundle.photos[:20]                # 20/32 < 0.9 ratio
    s = run_prechecks(bundle, config, geocode_result)
    assert not s.photo_count_ok
    assert s.short_circuit == "escalate_to_human"
    assert "documentation" in s.short_circuit_reason


def test_short_time_on_site_is_flag_not_verdict(luggage, images, config, geocode_result):
    s = run_prechecks(make_bundle(luggage, images, time_on_site_min=3),
                      config, geocode_result)
    assert "short_visit" in s.fraud_signals
    assert s.short_circuit is None


def test_missing_time_on_site_is_gap_not_signal(luggage, images, config, geocode_result):
    """The API's -1 sentinel (observed live on 500119020) means 'not recorded' —
    it must become a documentation gap, never a short_visit fraud signal."""
    for missing in (-1, None):
        s = run_prechecks(make_bundle(luggage, images, time_on_site_min=missing),
                          config, geocode_result)
        assert "short_visit" not in s.fraud_signals
        assert "time_on_site_missing" in s.documentation_gaps


def test_forged_taken_date_flags_date_skew(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    # photo "taken" 5 days after its own server-side upload time = forged EXIF
    bundle.photos[0].taken_date = "2026-07-15 12:00:00"
    bundle.photos[0].date_created = "2026-07-10T13:21:38"
    s = run_prechecks(bundle, config, geocode_result)
    assert "date_skew" in s.fraud_signals
    assert "taken_after_upload" in s.documentation_gaps


def test_identity_mismatch_aborts(luggage, images, config, geocode_result):
    with pytest.raises(PipelineAbort):
        run_prechecks(make_bundle(luggage, images, order="999999999"),
                      config, geocode_result)


def test_non_grass_work_code_aborts(luggage, images, config, geocode_result):
    with pytest.raises(PipelineAbort):
        run_prechecks(make_bundle(luggage, images, work_code="WINT"),
                      config, geocode_result)


def test_parse_dt_all_api_formats():
    assert parse_dt("2026-07-10T19:43:35Z") is not None
    assert parse_dt("2026-07-08T00:00:00-04:00") is not None
    assert parse_dt("2026-07-10 15:43:35") is not None
    assert parse_dt("2000-11-30 00:00:00") is not None
    assert parse_dt("2026-07-08") is not None
    assert parse_dt("") is None and parse_dt(None) is None and parse_dt("garbage") is None


def test_photo_integrity_detects_reuse_and_staging(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    # exact reuse across stages: give a Before and an After the same sha
    before = next(p for p in bundle.photos if p.desc_prefix == "Before")
    after = next(p for p in bundle.photos if p.desc_prefix == "After")
    before.sha256 = after.sha256 = "d" * 64
    s = run_prechecks(bundle, config, geocode_result)
    assert "photo_reuse" in s.fraud_signals
    assert any(p["match"] == "exact" for p in s.photo_integrity["reused_pairs"])


def test_after_precedes_before_is_staged(luggage, images, config, geocode_result):
    bundle = make_bundle(luggage, images)
    before = next(p for p in bundle.photos
                  if p.desc_prefix == "Before" and p.desc_text == "Grass Recut - Rear Lawn")
    before.taken_date = "2026-07-08 23:59:00"        # "before" taken after every After
    s = run_prechecks(bundle, config, geocode_result)
    rear = [p for p in s.photo_integrity["staged_pairs"]
            if p["station"] == "Grass Recut - Rear Lawn"]
    assert rear and rear[0]["issue"] == "after_precedes_before"
    assert "staged_documentation" in s.fraud_signals


def test_adjacent_cross_station_pairs_listed(luggage, images, config, geocode_result):
    """Found by a human reviewer: 'After Side Lawn 1' and 'Before Rear Lawn' are the
    same uncut scene shot 5 s apart (re-shots evade perceptual hashing, dHash d=22) —
    code lists timestamp-adjacent cross-station pairs for directed visual review."""
    s = run_prechecks(make_bundle(luggage, images), config, geocode_result)
    pairs = s.photo_integrity["adjacent_cross_station_pairs"]
    assert pairs, "expected adjacent cross-station pairs on the real fixture"
    culprit = [p for p in pairs
               if {p["a"]["guid"], p["b"]["guid"]} == {"96d00c96", "cfd19d17"}]
    assert culprit and culprit[0]["gap_seconds"] <= 10


def test_upload_stamped_taken_dates_suppress_staging(luggage, images, config, geocode_result):
    """Order 500119020 pattern, pinned: takenDate tracked dateCreated
    second-for-second at a whole-hour offset (the app stamped capture time at
    upload), while on-image watermarks showed the real capture two days
    earlier. Metadata chronology proves nothing there — staging/adjacency are
    suppressed and a documentation gap directs the reviewer to the watermarks."""
    bundle = make_bundle(luggage, images)
    for p in bundle.photos:
        if p.taken_date:                       # taken = upload + exactly 4 h
            p.date_created = parse_dt(p.taken_date).replace(
                hour=(parse_dt(p.taken_date).hour - 4) % 24
            ).strftime("%Y-%m-%dT%H:%M:%S")
    s = run_prechecks(bundle, config, geocode_result)
    assert s.photo_integrity["taken_dates_unreliable"] is True
    assert s.photo_integrity["staged_pairs"] == []
    assert s.photo_integrity["adjacent_cross_station_pairs"] == []
    assert "staged_documentation" not in s.fraud_signals
    assert "capture_times_upload_stamped" in s.documentation_gaps


def test_genuine_capture_clock_keeps_staging(luggage, images, config, geocode_result):
    """The real 500119014 data: taken 07-08, uploaded 07-10 — two distinct
    clocks, so the staged pairs stand."""
    s = run_prechecks(make_bundle(luggage, images), config, geocode_result)
    assert s.photo_integrity["taken_dates_unreliable"] is False
    assert "staged_documentation" in s.fraud_signals
