"""C0 tests against the real 500119014 fixtures."""

from pathlib import Path

from PIL import Image

from gcqa.prefetch import downscale, normalize_fields, split_items

FIXTURES = Path(__file__).parent / "fixtures" / "500119014"


def test_normalize_fields_real_order(luggage):
    f = normalize_fields(luggage)
    assert f["order_number"] == "500119014"
    assert f["work_code"] == "GCL"
    assert f["work_code_desc"] == "MONTH/BIMONTHLY GRASS CUT"
    assert f["address"] == "1801 WOODWARD AVE"
    assert (f["city"], f["state"], f["zip"]) == ("SPRINGFIELD", "OH", "45506")
    assert f["time_on_site_min"] == 38
    assert f["number_of_photos"] == 42
    assert f["grass_cut_completed"] == "Yes"
    assert f["grass_cut_backyard"] == "Yes"
    assert f["has_violations"] == "No"
    assert f["within_grass_season"] is True
    assert f["vendor"] == "DEVGUY"
    assert len(f["impediments"]) == 7
    assert f["impediments"][5] == "Ensure the lawn is maintained."
    # C1-only distance sentinels
    assert f["_api_dist_farthest_to_property"] == -1
    # PII / lock codes / Tbl mirrors never pass through (fixture values sanitized;
    # the guarantee is structural: only the curated key map is emitted)
    joined = " ".join(str(v) for v in f.values())
    assert "00000" not in joined and "redacted@example.com" not in joined
    assert not any("Lock" in k or "Tbl" in k for k in f)


def test_split_items_counts(images):
    review, aux, documents, counts = split_items(images)
    assert counts == {"items_listed": 50, "jpeg_listed": 42, "review_photos": 32,
                      "documents_listed": 8}
    assert len(review) == 32 and len(aux) == 10
    assert all(p.desc_prefix not in ("Bid", "Document") for p in review)
    assert all(p.desc_prefix in ("Bid", "Document") for p in aux)
    prefixes = {p.desc_prefix for p in review}
    assert prefixes == {"Before", "During", "After", "Condition"}


def test_downscale(tmp_path):
    small = tmp_path / "small.jpg"
    big = tmp_path / "big.jpg"
    Image.new("RGB", (360, 480)).save(small, "JPEG")
    Image.new("RGB", (4000, 3000)).save(big, "JPEG")
    downscale(small, 1092, 80)
    downscale(big, 1092, 80)
    with Image.open(small) as im:
        assert im.size == (360, 480)          # untouched below the cap
    with Image.open(big) as im:
        assert max(im.size) == 1092
        assert im.size == (1092, 819)
