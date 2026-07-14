"""§5.1 human-readable decision report.

The model narrative is display-only: every number, check, threshold, and
outcome in the report comes from the final adjudicated payload rendered by the
template — the narrative can never be the source of a fact. A render failure
writes a stub and never blocks or alters a verdict (§13).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TAG_ARTIFACT_RE = re.compile(r"</?[a-zA-Z_][a-zA-Z0-9_]*>")


def _clean_narrative(text: str | None) -> str | None:
    """Display-side cleanup only: strip stray XML-ish tag artifacts the model
    occasionally appends. The stored payload is never modified."""
    return _TAG_ARTIFACT_RE.sub("", text).strip() if text else text


def render_report(final: dict, fields: dict, audit_path: str) -> str:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), undefined=StrictUndefined,
                      trim_blocks=False, lstrip_blocks=False)
    template = env.get_template("report.md.j2")
    final = json.loads(json.dumps(final))          # deep copy — never mutate the record
    for section in ("drafter", "verifier"):
        if final.get(section):
            final[section]["rationale_plain"] = _clean_narrative(
                final[section].get("rationale_plain")
            )
    drafter_outcome = (final.get("drafter") or {}).get("outcome")
    superseded = drafter_outcome is not None and drafter_outcome != final.get("flag")
    check_first = sorted({
        (e.get("photo_guid") or "")[:8]
        for e in ((final.get("drafter") or {}).get("evidence") or [])
        if e.get("photo_guid")
    })
    address = ", ".join(
        str(fields.get(k) or "") for k in ("address", "city", "state")
    ) + " " + str(fields.get("zip") or "")
    return template.render(
        final=_dotdict(final),
        address=address.strip(" ,"),
        work_code_desc=fields.get("work_code_desc") or fields.get("work_code") or "",
        vendor=fields.get("vendor") or "",
        superseded=superseded,
        check_first=check_first,
        audit_path=audit_path,
    )


def write_report(final: dict, fields: dict, report_path: Path, audit_path: str) -> Path | None:
    """Render + write; on failure write a stub — a report bug never blocks a verdict."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_path.write_text(render_report(final, fields, audit_path))
    except Exception as e:
        report_path.write_text(
            f"# Work-Order QA Report — {final.get('order_number')}\n\n"
            f"Decision: {final.get('outcome')} (confidence {final.get('confidence')})\n\n"
            f"Report rendering failed ({type(e).__name__}: {e}); the authoritative "
            f"record is {audit_path}.\n"
        )
    return report_path


class _dotdict(dict):
    """Attribute access for Jinja templates over nested dicts (read-only).

    Missing keys resolve to None instead of raising: model-produced payloads
    legitimately omit optional keys (e.g. photo_guid on signal-sourced
    evidence), and the template guards every optional with truthiness checks.
    """

    def __getattr__(self, name):
        value = self.get(name)
        if isinstance(value, dict):
            return _dotdict(value)
        if isinstance(value, list):
            return [_dotdict(v) if isinstance(v, dict) else v for v in value]
        return value
