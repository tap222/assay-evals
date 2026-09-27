"""Measures that need labelled ground truth.

Registered now so they appear in the catalog and on the dashboard as
"unmeasured" with the reason, rather than being absent. Each one gets a real
compute() once the labels it needs can be ingested.
"""
from __future__ import annotations

from collections import defaultdict

from assay.measures.base import Measure, MeasureOutput, SliceResult, unmeasured
from assay.models import UNRECORDED, Window


class _AwaitingTruth(Measure):
    tag = "Accuracy"
    waiting_on: str = ""

    def compute(self, source, window: Window) -> MeasureOutput:
        return unmeasured(self.id, f"Needs ground truth: {self.waiting_on}")


class SplitStraightThrough(_AwaitingTruth):
    """From files scored against their correct boundaries (assay_sdk.documents.score_split): the share
    of files holding several documents where every document came out on the right pages. A test
    set's files had no person split them, so right is straight-through."""
    id = "split_stp"
    name = "Document splitting straight-through"
    question = "What share of multi-document files split correctly with no human touch?"
    dimensions = ("segment",)
    waiting_on = "files with human-confirmed document boundaries (assay_sdk.documents.score_split)."

    def compute(self, source, window: Window) -> MeasureOutput:
        scores = source.split_scores(window) if hasattr(source, "split_scores") else None
        if scores is None:
            return super().compute(source, window)
        multi = [s for s in scores if s["documents"] > 1]

        def one(dim, val, group):
            return SliceResult(dim, val, sum(s["right"] for s in group) / len(group) if group else None, len(group),
                               numerator=sum(s["right"] for s in group), denominator=len(group))
        results = [one(None, None, multi)]
        by = defaultdict(list)
        for s in multi:
            by[s["segment"] if s["segment"] not in (None, "") else UNRECORDED].append(s)
        results += [one("segment", v, g) for v, g in sorted(by.items())]
        return MeasureOutput(self.id, "measured", results)


class FieldAccuracy(_AwaitingTruth):
    """From fields scored against their correct values (assay_sdk.documents.score_document): the
    share right, each field weighted by what an error costs (its `weight`), line items by their
    row F1. Unmeasured until the first scored document arrives."""
    id = "field_accuracy"
    name = "Severity-weighted field accuracy"
    question = "How often is each extracted field right, weighted by what an error costs?"
    dimensions = ("segment", "document_type", "field")
    waiting_on = "a labelled evaluation set with correct values per field (assay_sdk.documents.score_document)."

    def compute(self, source, window: Window) -> MeasureOutput:
        scores = source.field_scores(window) if hasattr(source, "field_scores") else None
        if scores is None:
            return super().compute(source, window)
        if not scores:
            return MeasureOutput(self.id, "measured", [SliceResult(None, None, None, 0)])

        def one(dim, val, group):
            w = sum(s["weight"] for s in group)
            right = sum(s["weight"] * s["share"] for s in group)
            return SliceResult(dim, val, right / w if w else None, len({s["document_id"] for s in group}),
                               numerator=right, denominator=w)
        results = [one(None, None, scores)]
        for dim in self.dimensions:
            by = defaultdict(list)
            for s in scores:
                by[s[dim] if s[dim] not in (None, "") else UNRECORDED].append(s)
            results += [one(dim, v, g) for v, g in sorted(by.items())]
        return MeasureOutput(self.id, "measured", results)


class SupersededValues(_AwaitingTruth):
    """From documents checked against the later ones that amend or replace them
    (assay_sdk.documents.superseded_values): of the values a later document changed, the share
    that output still holds, unmarked. Updated values and ones flagged as superseded are fine."""
    id = "superseded_value_rate"
    name = "Superseded values reaching output"
    question = "How often does a value that a later document replaced reach output unflagged?"
    higher_is_better = False
    dimensions = ("segment", "document_type", "link", "field")
    waiting_on = "links between documents that amend or replace each other (assay_sdk.documents.superseded_values)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "superseded", "assay.superseded@1") \
            if hasattr(source, "document_checks") else None
        if rows is None:
            return super().compute(source, window)
        for r in rows:
            r["link"] = r["raw"].get("link")
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions,
                       lambda g: (sum(r["raw"].get("outcome") == "escaped" for r in g), len(g)))


class EscapeRate(_AwaitingTruth):
    """From spot checks of published output (assay_sdk.documents.spot_check): the share of values
    checked that were wrong. Everything published has cleared automation and, where it applied,
    review; `path` says which way each went out (reviewed, or auto-approved), so the slices say
    which one lets more through. A sample: its n says how far to trust it."""
    id = "escape_rate"
    name = "Escape rate"
    question = "How often does a wrong value clear both automation and human review?"
    higher_is_better = False
    dimensions = ("segment", "document_type", "path", "field")
    waiting_on = "a re-verified spot-check sample of published output (assay_sdk.documents.spot_check)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "spot_check", "assay.spotcheck@1") \
            if hasattr(source, "document_checks") else None
        if rows is None:
            return super().compute(source, window)
        for r in rows:
            r["path"] = r["raw"].get("path")
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions, lambda g: (sum(not r["passed"] for r in g), len(g)))
