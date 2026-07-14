import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "500119014"
ROOT = Path(__file__).parent.parent

# Verified live 2026-07-13: Census geocode of 1801 WOODWARD AVE, SPRINGFIELD, OH 45506
PROPERTY_GEOCODE = (39.903896218216, -83.813385360942)
# Verified live: medoid of the 42 GPS points on the test order
EXPECTED_MEDOID = (39.903816, -83.813375)
OUTLIER_GUID = "57cad809"  # Condition | Rear of House — 723.286 km away


@pytest.fixture(scope="session")
def luggage() -> dict:
    return json.loads((FIXTURES / "luggage.json").read_text())


@pytest.fixture(scope="session")
def images() -> dict:
    return json.loads((FIXTURES / "images.json").read_text())


@pytest.fixture(scope="session")
def raw_gps_points(images) -> list[dict]:
    """All 42 GPS-bearing JPEG points from the real order (review + Bid)."""
    return [
        {"guid": i["guid"], "lat": i.get("latitude"), "lon": i.get("longitude"),
         "descPrefix": i.get("descPrefix", ""), "descText": i.get("descText", "")}
        for i in images["items"] if i.get("mimeType") == "image/jpeg"
    ]


@pytest.fixture()
def config():
    from gcqa.config import load_config
    return load_config(ROOT / "config" / "qa.yaml")


@pytest.fixture()
def geocode_result():
    from gcqa.geo import GeocodeResult
    return GeocodeResult(lat=PROPERTY_GEOCODE[0], lon=PROPERTY_GEOCODE[1],
                         source="census", quality="exact",
                         matched_address="1801 WOODWARD AVE, SPRINGFIELD, OH, 45506")
