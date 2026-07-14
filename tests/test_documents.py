"""Document verifier tests: extraction, dedupe, caps, prompt exposure.

Fixtures doc_update.htm / doc_invoice.htm are the real attachments downloaded
from test order 500119014.
"""

from pathlib import Path

import pytest

from gcqa.prechecks import run_prechecks
from gcqa.prefetch import (
    DOC_CHAR_CAP,
    DocMeta,
    fetch_documents,
    html_to_text,
    pdf_to_text,
    split_items,
)
from gcqa.prompt_builder import build_user_content
from gcqa.prechecks import Signals

FIXTURES = Path(__file__).parent / "fixtures" / "500119014"


def test_update_html_extracts_vendor_questionnaire():
    text = html_to_text((FIXTURES / "doc_update.htm").read_bytes())
    assert "backyard" in text.lower()                 # the questionnaire answer a human reads
    assert "Was exterior debris/trash removed?" in text
    assert "<" not in text[:500]                      # tags stripped


def test_invoice_html_extracts_line_items():
    text = html_to_text((FIXTURES / "doc_invoice.htm").read_bytes())
    assert "Invoice Number" in text
    assert "500119014" in text
    assert "$25.00" in text


def test_html_script_and_style_skipped():
    raw = b"<html><style>.x{color:red}</style><script>alert(1)</script><p>real content</p></html>"
    text = html_to_text(raw)
    assert text == "real content"


def test_pdf_extraction_failure_never_raises(tmp_path):
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-not-really")
    assert "pdf extraction failed" in pdf_to_text(bad)


def test_split_items_captures_documents(images):
    review, aux, documents, counts = split_items(images)
    assert counts["documents_listed"] == 8
    assert len(documents) == 8
    assert all(d["mimeType"] in ("text/html", "application/pdf") for d in documents)


class StubClient:
    """Serves fixture bytes for document downloads (no network)."""

    def __init__(self, payload: dict[str, bytes]):
        self.payload = payload
        self.downloads = 0

    async def download_photo(self, web_file_name: str, dest: Path) -> Path:
        self.downloads += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload[web_file_name])
        return dest


@pytest.mark.asyncio
async def test_fetch_documents_dedupes_identical_attachments(tmp_path):
    update = (FIXTURES / "doc_update.htm").read_bytes()
    invoice = (FIXTURES / "doc_invoice.htm").read_bytes()
    raw_docs = [
        {"guid": f"u{i}", "webFileName": f"s3/x/u{i}.htm", "descText": "Update",
         "mimeType": "text/html", "fileSize": len(update)} for i in range(3)
    ] + [
        {"guid": "inv1", "webFileName": "s3/x/inv1.htm", "descText": "Invoice",
         "mimeType": "text/html", "fileSize": len(invoice)},
    ]
    client = StubClient({f"s3/x/u{i}.htm": update for i in range(3)} |
                        {"s3/x/inv1.htm": invoice})
    docs = await fetch_documents(raw_docs, client, tmp_path)
    assert len(docs) == 2                             # 3 identical updates collapsed
    update_doc = next(d for d in docs if d.desc_text == "Update")
    assert update_doc.duplicates == 3
    assert client.downloads == 2                      # pre-dedupe skipped redundant downloads
    assert all(len(d.text) <= DOC_CHAR_CAP for d in docs)


def test_documents_reach_the_model_prompt():
    from gcqa.prefetch import OrderBundle
    bundle = OrderBundle(
        order_number="1", fields={"impediments": []}, photos=[], aux_gps=[],
        documents=[DocMeta(guid="g", desc_text="Update", mime="text/html", size=10,
                           text="Was exterior debris/trash removed? | No",
                           duplicates=3)],
    )
    blocks = build_user_content(bundle, Signals())
    doc_block = next(b["text"] for b in blocks
                     if b["type"] == "text" and b["text"].startswith("ORDER_DOCUMENTS"))
    assert "debris/trash removed? | No" in doc_block
    assert "x3 identical copies" in doc_block


def test_prechecks_document_signals(luggage, images, config, geocode_result):
    from tests.test_prechecks import make_bundle
    bundle = make_bundle(luggage, images)
    bundle.documents = [DocMeta(guid="g", desc_text="Invoice", mime="text/html",
                                size=10, text="x")]
    s = run_prechecks(bundle, config, geocode_result)
    assert s.documents["listed"] == 8
    assert s.documents["invoice_present"] is True
    assert s.documents["update_present"] is False
    assert "no_vendor_documents" not in s.documentation_gaps
