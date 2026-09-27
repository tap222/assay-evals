"""What a connected source can and can't answer, and what to add to unlock more.

Run against any source, it reports how many records of each kind arrived and
how often each field is filled in, then says for every measure whether it is
live, partial (works, but something would make it more useful) or blocked
(and exactly which field would unblock it).
"""
from __future__ import annotations

from dataclasses import fields as dc_fields
from typing import Dict, List, Optional

from assay.measures import REGISTRY
from assay.models import CallRecord, DocumentRecord, ErrorReport, IndexedRecord, ReviewRecord, StageRun, Window

RECORDS = {
    "documents": DocumentRecord, "stage_runs": StageRun, "calls": CallRecord,
    "indexed": IndexedRecord, "reviews": ReviewRecord, "errors": ErrorReport,
}

# Fields each measure can't work without: [(record, field or None for "any rows")].
REQUIRES: Dict[str, List[tuple]] = {
    "document_volume": [("documents", "received_at")],
    "stage_failure_rate": [("stage_runs", "status")],
    "call_error_rate": [("calls", "status")],
    "call_latency_p95": [("calls", "latency_ms")],
    "time_to_complete_p90": [("documents", "completed_at")],
    "input_mix_drift": [("documents", "received_at")],
    "cost_per_document": [("documents", "received_at"), ("calls", "cost_usd")],
    "cost_per_page": [("documents", "page_count"), ("calls", "cost_usd")],
    "total_spend": [("calls", "cost_usd")],
    "human_touch_rate": [("reviews", None)],
    "cost_coverage": [("calls", None)],
    "fallback_attribution": [("calls", "resolving_layer"), ("calls", "gate_reason")],
    "model_mismatch": [("calls", "model_declared"), ("calls", "model_served")],
    "revision_coverage": [("calls", None)],
    "noop_stage_rate": [("stage_runs", "did_work")],
    "source_positions": [("indexed", None)],
    "handoff_loss": [("documents", "completed_at"), ("documents", "file_hash")],
    "reported_error_rate": [("errors", None)],
    "errors_by_origin": [("errors", None), ("stage_runs", "outputs")],
    "prompt_error_rate": [("errors", None), ("calls", "prompt_version")],
}

# Fields that make a working measure more useful: (record, field, why).
IMPROVES: Dict[str, List[tuple]] = {
    "*": [("documents", "segment", "break every measure out by customer, region or business unit"),
          ("documents", "document_type", "break every measure out by document type")],
    "input_mix_drift": [("documents", "segment", "drift needs something to compare; segment is the usual one")],
    "cost_per_document": [("reviews", None, "add review and rework time: people cost is usually most of it"),
                          ("calls", "resolving_layer", "separate fallback escalation from normal inference")],
    "time_to_complete_p90": [("documents", "processing_mode", "compare realtime and batch service levels")],
    "call_latency_p95": [("calls", "model_served", "see which model is slow"),
                         ("calls", "prompt_version", "compare prompt versions")],
    "call_error_rate": [("calls", "prompt_version", "compare prompt versions")],
    "errors_by_origin": [("stage_runs", "sequence", "order steps exactly instead of by start time")],
}

WAITING_ON_GROUND_TRUTH = {"superseded_value_rate"}
# Measured from scored checks (assay_sdk.documents): live once the first one arrives.
SCORED = {"field_accuracy": ("field_scores", (), "fields scored against their correct values (score_document)"),
          "split_stp": ("split_scores", (), "files scored against their correct boundaries (score_split)"),
          "escape_rate": ("document_checks", ("spot_check", "assay.spotcheck@1"),
                          "spot checks of published output (spot_check)"),
          "ocr_cer": ("document_checks", ("ocr",), "OCR text scored against the page (score_ocr)"),
          "ocr_digit_error_rate": ("document_checks", ("ocr",), "OCR text scored against the page (score_ocr)"),
          "ocr_reading_order": ("document_checks", ("ocr",), "OCR text scored against the page (score_ocr)"),
          "location_accuracy": ("document_checks", ("location",), "field locations scored (score_locations)"),
          "table_cell_f1": ("document_checks", ("table",), "tables scored against the correct ones (score_table)")}


def _profile(records: Optional[list], cls) -> dict:
    if records is None:
        return {"available": False, "rows": 0, "fields": {}}
    rows = len(records)
    names = [f.name for f in dc_fields(cls)]
    fill = {n: (sum(1 for r in records if getattr(r, n) not in (None, "")) / rows if rows else 0.0) for n in names}
    return {"available": True, "rows": rows, "fields": fill}


def _label(record: str, field: Optional[str]) -> str:
    return record if field is None else f"{record}.{field}"


def compute(source, window: Window, rates: Optional[Dict[str, float]] = None) -> dict:
    rates = rates or {}
    getters = {"documents": source.documents, "stage_runs": source.stage_runs, "calls": source.calls,
               "indexed": source.indexed, "reviews": getattr(source, "reviews", lambda w: None),
               "errors": getattr(source, "errors", lambda w: None)}
    profiles = {}
    for name, cls in RECORDS.items():
        try:
            out = getters[name](window)
            profiles[name] = _profile(None if out is None else list(out), cls)
        except Exception as exc:  # a broken mapping for one record type shouldn't hide the rest
            profiles[name] = {"available": False, "rows": 0, "fields": {}, "error": str(exc).splitlines()[0]}
    downstream = source.downstream_hashes()

    def have(record, field) -> bool:
        p = profiles[record]
        if not p["available"] or not p["rows"]:
            return False
        return field is None or p["fields"].get(field, 0) > 0

    measures = []
    for mid, m in REGISTRY.items():
        entry = {"id": mid, "name": m.name, "tag": m.tag, "missing": [], "improve": []}
        if mid in WAITING_ON_GROUND_TRUTH:
            entry.update(status="blocked", missing=["labelled ground truth (not ingestible yet)"])
            measures.append(entry)
            continue
        if mid in SCORED:
            method, args, what = SCORED[mid]
            got = getattr(source, method)(window, *args) if hasattr(source, method) else None
            entry.update(status="live" if got else "blocked", missing=[] if got else [what])
            measures.append(entry)
            continue
        entry["missing"] = [_label(r, f) for r, f in REQUIRES.get(mid, []) if not have(r, f)]
        if mid == "handoff_loss" and downstream is None:
            entry["missing"].append("a downstream system to check delivery against "
                                    "(ASSAY_DOWNSTREAM_URL, or delivered_downstream on document events)")
        general = [(r, f, why) for r, f, why in IMPROVES["*"] if f in m.dimensions]
        for r, f, why in IMPROVES.get(mid, []) + general:
            if not have(r, f) and _label(r, f) not in entry["missing"]:
                entry["improve"].append({"field": _label(r, f), "why": why})
        if mid in ("cost_per_document", "cost_per_page", "human_touch_rate") and have("reviews", None) \
                and "review_per_hour" not in rates and profiles["reviews"]["fields"].get("cost_usd", 0) < 1:
            entry["improve"].append({"field": "rate card: review_per_hour",
                                     "why": "review minutes can't be priced without an hourly rate"})
        entry["status"] = "blocked" if entry["missing"] else "partial" if entry["improve"] else "live"
        measures.append(entry)

    counts = {s: sum(1 for m in measures if m["status"] == s) for s in ("live", "partial", "blocked")}
    return {"window": [window.start.isoformat(), window.end.isoformat()], "records": profiles,
            "measures": measures, "counts": counts}
