"""C0 Prefetcher: fetch order + images, download/downscale photos, build the OrderBundle.

The OrderBundle is the only input C1-C5 ever see; snapshots of it (plus the
frozen geocode result) make evals hermetic (ORCHESTRATION.md §12).
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .api_client import SafeguardClient
from .config import Config
from .geo import GeocodeResult

REVIEW_EXCLUDED_PREFIXES = {"Bid", "Document"}
DOCUMENT_MIMES = {"text/html", "application/pdf"}
DOC_CHAR_CAP = 6_000          # per document
DOC_TOTAL_CAP = 18_000        # across all documents (~4.5k tokens)
PDF_MAX_PAGES = 8

# The curated ~30 question keys that feed the pipeline (ORCHESTRATION.md §9).
# Everything else in the 205-key map — Property.Tbl* mirrors, lock codes,
# loan/policy numbers, client contact PII — never leaves this module.
_Q = {
    "order_number": "WorkOrder.OrderNumber",
    "invoice_number": "WorkOrder.InvoiceNumber",
    "work_code": "WorkOrder.TblWorkCode",
    "work_code_desc": "WorkOrder.TblWorkCodeDesc",
    "address": "Property.Address1",
    "city": "Property.City",
    "state": "Property.State",
    "zip": "Property.ZipCode",
    "time_on_site_min": "WorkOrder.TimeOnSite",
    "number_of_photos": "WorkOrder.NumberOfPhotos",
    "date_grass_cut": "WorkOrder.DateGrassCut",
    "completed_date": "WorkOrder.CompletedDate",
    "grass_cut_completed": "WorkOrder.GrassCutCompleted",
    "grass_cut_backyard": "WorkOrder.GrassCutBackyard",
    "has_violations": "WorkOrder.GCHasViolations",
    "trees_violation_damage": "WorkOrder.TreesCausingViolationDamage",
    "has_pool_hottub": "WorkOrder.GCHasPoolHotTub",
    "shrubs_present": "Workorder.ShrubsPresent",
    "special_instructions": "WorkOrder.SpecialInstructions",
    "vendor": "WorkOrder.ResponsibleVendor",
    "vendor_history": "WorkOrder.VendorAssignmentHistory",
    "wf_remarks": "WorkOrder.TblWfRemarks",
    "upstream_ai_findings": "WorkOrder.AI.Findings",
    "within_grass_season": "System.CurrentDate.WithinGrassSeason",
}
# C1-only inputs (underscore prefix = stripped by prompt_builder, never sent to the model)
_Q_C1 = {
    "_api_dist_door_knock": "WorkOrder.DoorKnockPhotoToPropertyLocationDistance",
    "_api_dist_farthest_to_property": "WorkOrder.FarthestImageToPropertyDistance",
    "_api_dist_farthest_between": "WorkOrder.FarthestDistanceBetweenImages",
}
_IMPEDIMENT_PREFIX = "AuditAssistant.Impediment."


@dataclass
class PhotoMeta:
    guid: str
    desc_prefix: str
    desc_text: str
    taken_date: str | None
    lat: float | None
    lon: float | None
    width: int | None
    height: int | None
    web_file_name: str
    date_created: str | None = None
    scan_date: str | None = None
    local_path: str | None = None
    sha256: str | None = None         # exact-duplicate detection
    dhash: int | None = None          # perceptual near-duplicate detection


def compute_dhash(path: Path, size: int = 8) -> int:
    """Difference hash: 64-bit perceptual fingerprint (PIL-only, no extra deps)."""
    with Image.open(path) as im:
        img = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(img.getdata())
    bits = "".join(
        "1" if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else "0"
        for r in range(size) for c in range(size)
    )
    return int(bits, 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class DocMeta:
    """A non-photo order attachment (vendor questionnaire, invoice, audit report)
    with its extracted text — read by both reviewers like a human would."""
    guid: str
    desc_text: str
    mime: str
    size: int
    text: str = ""
    truncated: bool = False
    duplicates: int = 1               # identical attachments collapsed into this one


@dataclass
class OrderBundle:
    order_number: str
    fields: dict
    photos: list[PhotoMeta]           # review set: JPEG, non-Bid/Document, downloaded
    aux_gps: list[PhotoMeta]          # Bid/Document JPEG metadata — GPS clustering only,
    documents: list[DocMeta] = field(default_factory=list)  # deduped, text-extracted
    fetch_stats: dict = field(default_factory=dict)  # aux never downloaded/sent to model


def _answer(questions: dict, key: str) -> str | None:
    entry = questions.get(key)
    if not entry:
        return None
    answers = entry.get("answers") or []
    if not answers:
        return None
    value = answers[0].get("value")
    return value if value is not None else None


def _norm_enum(value: str | None) -> str | None:
    """'Default.Yes' -> 'Yes', 'WasGrassCutCompletedAtBackyard.Yes' -> 'Yes', plain stays."""
    if value is None:
        return None
    value = value.strip()
    return value.rsplit(".", 1)[-1] if "." in value else value


def _norm_int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value is not None and value.strip() != "" else None
    except (ValueError, AttributeError):
        return None


def normalize_fields(luggage: dict) -> dict:
    questions = luggage.get("propertyRecord", {}).get("questions", {})
    fields: dict = {}
    for out_key, q_key in _Q.items():
        fields[out_key] = _answer(questions, q_key)
    for out_key, q_key in _Q_C1.items():
        fields[out_key] = _norm_int(_answer(questions, q_key))

    for key in ("grass_cut_completed", "grass_cut_backyard", "has_violations",
                "trees_violation_damage", "has_pool_hottub", "shrubs_present"):
        fields[key] = _norm_enum(fields[key])
    fields["time_on_site_min"] = _norm_int(fields["time_on_site_min"])
    fields["number_of_photos"] = _norm_int(fields["number_of_photos"])
    fields["within_grass_season"] = str(fields["within_grass_season"]).lower() == "true"
    if fields["special_instructions"]:
        fields["special_instructions"] = fields["special_instructions"].strip() or None

    impediments: list[tuple[int, str]] = []
    for q_key in questions:
        if q_key.startswith(_IMPEDIMENT_PREFIX):
            try:
                idx = int(q_key[len(_IMPEDIMENT_PREFIX):])
            except ValueError:
                continue
            text = _answer(questions, q_key)
            if text:
                impediments.append((idx, text.strip()))
    fields["impediments"] = [t for _, t in sorted(impediments)]
    return fields


def _photo_meta(item: dict) -> PhotoMeta:
    return PhotoMeta(
        guid=str(item.get("guid", "")),
        desc_prefix=str(item.get("descPrefix", "")),
        desc_text=str(item.get("descText", "")),
        taken_date=item.get("takenDate"),
        lat=item.get("latitude"),
        lon=item.get("longitude"),
        width=item.get("imageWidth"),
        height=item.get("imageHeight"),
        web_file_name=str(item.get("webFileName", "")),
        date_created=item.get("dateCreated"),
        scan_date=item.get("scanDate"),
    )


def split_items(images_payload: dict) -> tuple[list[PhotoMeta], list[PhotoMeta], list[dict], dict]:
    """items -> (review photos, aux GPS-only photos, raw document items, counts)."""
    items = images_payload.get("items", [])
    jpegs = [i for i in items if i.get("mimeType") == "image/jpeg"]
    review = [_photo_meta(i) for i in jpegs if i.get("descPrefix") not in REVIEW_EXCLUDED_PREFIXES]
    aux = [_photo_meta(i) for i in jpegs if i.get("descPrefix") in REVIEW_EXCLUDED_PREFIXES]
    documents = [i for i in items if i.get("mimeType") in DOCUMENT_MIMES]
    counts = {"items_listed": len(items), "jpeg_listed": len(jpegs),
              "review_photos": len(review), "documents_listed": len(documents)}
    return review, aux, documents, counts


# ---------------------------------------------------------------------------
# Document text extraction — the agent reads attachments like a human reviewer
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(raw: bytes) -> str:
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", errors="ignore"))
    return " | ".join(parser.parts)


def pdf_to_text(path: Path, max_pages: int = PDF_MAX_PAGES) -> str:
    import pypdf
    try:
        reader = pypdf.PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
        return "\n".join(t.strip() for t in pages if t.strip())
    except Exception as e:  # a corrupt attachment never blocks the review
        return f"[pdf extraction failed: {type(e).__name__}]"


def extract_document_text(path: Path, mime: str) -> str:
    if mime == "application/pdf":
        return pdf_to_text(path)
    return html_to_text(path.read_bytes())


async def fetch_documents(raw_docs: list[dict], client: SafeguardClient,
                          doc_dir: Path) -> list[DocMeta]:
    """Download, dedupe (identical attachments are common), and text-extract.

    Pre-dedupe by (size, descText, mime) skips redundant downloads; a content
    sha256 post-dedupe is the backstop. Per-doc and total char caps keep the
    token cost bounded.
    """
    by_key: dict[tuple, list[dict]] = {}
    for d in raw_docs:
        key = (d.get("fileSize"), d.get("descText"), d.get("mimeType"))
        by_key.setdefault(key, []).append(d)

    docs: list[DocMeta] = []
    seen_hashes: dict[str, DocMeta] = {}
    total = 0
    for group in by_key.values():
        d = group[0]
        guid = str(d.get("guid", ""))
        suffix = ".pdf" if d.get("mimeType") == "application/pdf" else ".htm"
        dest = doc_dir / f"{guid}{suffix}"
        try:
            if not dest.exists():
                await client.download_photo(d["webFileName"], dest)
        except Exception:
            continue                              # a missing doc is a gap, not a failure
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if digest in seen_hashes:
            seen_hashes[digest].duplicates += len(group)
            continue
        text = extract_document_text(dest, d.get("mimeType", ""))
        truncated = False
        if len(text) > DOC_CHAR_CAP:
            text, truncated = text[:DOC_CHAR_CAP], True
        remaining = DOC_TOTAL_CAP - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text, truncated = text[:remaining], True
        total += len(text)
        meta = DocMeta(guid=guid, desc_text=str(d.get("descText", "")),
                       mime=str(d.get("mimeType", "")), size=int(d.get("fileSize") or 0),
                       text=text, truncated=truncated, duplicates=len(group))
        seen_hashes[digest] = meta
        docs.append(meta)
    return docs


def downscale(path: Path, long_edge: int, quality: int) -> None:
    """Downscale in place if the long edge exceeds the limit (vision cost control)."""
    with Image.open(path) as im:
        w, h = im.size
        longest = max(w, h)
        if longest <= long_edge:
            return
        scale = long_edge / longest
        resized = im.convert("RGB").resize((round(w * scale), round(h * scale)))
        resized.save(path, "JPEG", quality=quality)


async def prefetch(order_number: str, client: SafeguardClient, cfg: Config,
                   cache_dir: Path) -> OrderBundle:
    luggage = await client.get_luggage(order_number)
    images = await client.get_images(order_number)

    fields = normalize_fields(luggage)
    review, aux, raw_docs, counts = split_items(images)

    photo_dir = cache_dir / order_number
    stats = await client.download_all(
        [{"guid": p.guid, "webFileName": p.web_file_name} for p in review], photo_dir
    )
    downloaded: list[PhotoMeta] = []
    for p in review:
        local = photo_dir / f"{p.guid}.jpg"
        if p.guid in stats.failed or not local.exists():
            continue
        downscale(local, cfg.runtime.photo_long_edge_px, cfg.runtime.jpeg_quality)
        p.local_path = str(local)
        p.sha256 = hashlib.sha256(local.read_bytes()).hexdigest()
        try:
            p.dhash = compute_dhash(local)
        except Exception:
            p.dhash = None                 # a corrupt image is a gap, never a crash
        downloaded.append(p)

    documents = await fetch_documents(raw_docs, client, photo_dir)

    counts.update({
        "downloaded": len(downloaded),
        "failed": len(stats.failed),
        "failed_guids": stats.failed,
        "missing_gps": sum(1 for p in review if p.lat is None or p.lon is None),
        "missing_taken_date": sum(1 for p in review if not p.taken_date),
        "documents_unique": len(documents),
    })
    return OrderBundle(order_number=order_number, fields=fields,
                       photos=downloaded, aux_gps=aux, documents=documents,
                       fetch_stats=counts)


# ---------------------------------------------------------------------------
# Snapshots: frozen OrderBundle + geocode for hermetic eval replay (§12)
# ---------------------------------------------------------------------------

def save_snapshot(bundle: OrderBundle, geocode: GeocodeResult | None, snap_dir: Path) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = snap_dir / "photos"
    photos_dir.mkdir(exist_ok=True)
    rel_photos = []
    for p in bundle.photos:
        meta = asdict(p)
        if p.local_path:
            dest = photos_dir / f"{p.guid}.jpg"
            if Path(p.local_path).resolve() != dest.resolve():
                shutil.copyfile(p.local_path, dest)
            meta["local_path"] = f"photos/{p.guid}.jpg"
        rel_photos.append(meta)
    doc = {
        "order_number": bundle.order_number,
        "fields": bundle.fields,
        "photos": rel_photos,
        "aux_gps": [asdict(p) for p in bundle.aux_gps],
        "documents": [asdict(d) for d in bundle.documents],
        "fetch_stats": bundle.fetch_stats,
        "geocode": asdict(geocode) if geocode else None,
    }
    (snap_dir / "bundle.json").write_text(json.dumps(doc, indent=1))


def load_snapshot(snap_dir: Path) -> tuple[OrderBundle, GeocodeResult | None]:
    doc = json.loads((snap_dir / "bundle.json").read_text())
    photos = []
    for meta in doc["photos"]:
        if meta.get("local_path"):
            meta["local_path"] = str(snap_dir / meta["local_path"])
        photos.append(PhotoMeta(**meta))
    bundle = OrderBundle(
        order_number=doc["order_number"],
        fields=doc["fields"],
        photos=photos,
        aux_gps=[PhotoMeta(**m) for m in doc["aux_gps"]],
        documents=[DocMeta(**m) for m in doc.get("documents", [])],
        fetch_stats=doc["fetch_stats"],
    )
    geocode = GeocodeResult(**doc["geocode"]) if doc.get("geocode") else None
    return bundle, geocode
