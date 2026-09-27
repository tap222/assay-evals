"""Document quality over time, from the checks assay_sdk.documents records: OCR (characters and
digits wrong, reading order), fields read from the right place, table cells, and the escape
rate from spot checks of published output. Each is unmeasured until its first check arrives,
and says which call sends it."""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, List, Optional, Tuple

from assay.measures.base import Measure, MeasureOutput, SliceResult, unmeasured
from assay.models import UNRECORDED, Window


def _slices(measure_id: str, rows: List[dict], dimensions, ratio: Callable[[List[dict]], Tuple[float, float]]
            ) -> MeasureOutput:
    """A ratio (numerator, denominator) overall and per slice, n being the documents in it."""
    def one(dim, val, group):
        num, den = ratio(group)
        return SliceResult(dim, val, num / den if den else None, len({r["document_id"] for r in group}),
                           numerator=num, denominator=den)
    results = [one(None, None, rows)]
    for dim in dimensions:
        by = defaultdict(list)
        for r in rows:
            by[r.get(dim) if r.get(dim) not in (None, "") else UNRECORDED].append(r)
        results += [one(dim, v, g) for v, g in sorted(by.items())]
    return MeasureOutput(measure_id, "measured", results)


class _FromChecks(Measure):
    tag = "Accuracy"
    kind: str = ""
    evaluator: str = "assay.documents@1"
    sent_by: str = ""
    dimensions = ("document_type", "segment")

    def ratio(self, rows: List[dict]) -> Tuple[float, float]:  # pragma: no cover
        raise NotImplementedError

    def rows(self, source, window: Window) -> Optional[List[dict]]:
        return source.document_checks(window, self.kind, self.evaluator) if hasattr(source, "document_checks") \
            else None

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = self.rows(source, window)
        if rows is None:
            return unmeasured(self.id, f"Nothing sent yet: {self.sent_by}.")
        return _slices(self.id, rows, self.dimensions, self.ratio)


class OcrCharacterErrors(_FromChecks):
    id = "ocr_cer"
    name = "OCR characters wrong"
    question = "Of the characters on the pages, what share did OCR read wrong?"
    higher_is_better = False
    kind = "ocr"
    sent_by = "OCR text scored against what the page says (assay_sdk.documents.score_ocr)"

    def ratio(self, rows):
        return sum(r["raw"].get("char_errors") or 0 for r in rows), sum(r["raw"].get("chars") or 0 for r in rows)


class OcrDigitErrors(OcrCharacterErrors):
    id = "ocr_digit_error_rate"
    name = "OCR digits wrong"
    question = "Of the digits on the pages, what share did OCR read wrong? A wrong digit is a wrong amount."

    def ratio(self, rows):
        return sum(r["raw"].get("digit_errors") or 0 for r in rows), sum(r["raw"].get("digits") or 0 for r in rows)


class OcrReadingOrder(OcrCharacterErrors):
    id = "ocr_reading_order"
    name = "OCR reading order"
    question = "What share of lines did OCR read in the page's order?"
    higher_is_better = True

    def ratio(self, rows):  # lines weighted by the page's characters: a long page counts for more
        scored = [r for r in rows if r["raw"].get("order") is not None]
        return (sum(r["raw"]["order"] * (r["raw"].get("chars") or 1) for r in scored),
                sum(r["raw"].get("chars") or 1 for r in scored))


class LocationAccuracy(_FromChecks):
    id = "location_accuracy"
    name = "Fields read from the right place"
    question = "What share of fields came from the right page and box?"
    kind = "location"
    sent_by = "field locations scored against the correct boxes (assay_sdk.documents.score_locations)"
    dimensions = ("document_type", "segment", "field")

    def rows(self, source, window):
        rows = super().rows(source, window)
        for r in rows or []:  # "location: total" is the field total
            r["field"] = r["field"].split(": ", 1)[-1]
        return rows

    def ratio(self, rows):
        return sum(r["passed"] for r in rows), len(rows)


class TableCellAccuracy(_FromChecks):
    id = "table_cell_f1"
    name = "Table cells right"
    question = "Of table cells, how many were read right, counting those lost and those made up (F1)?"
    kind = "table"
    sent_by = "tables scored against the correct ones (assay_sdk.documents.score_table)"

    def ratio(self, rows):
        right = sum(r["raw"].get("cells_right") or 0 for r in rows)
        return 2 * right, sum((r["raw"].get("cells") or 0) + (r["raw"].get("cells_read") or 0) for r in rows)
