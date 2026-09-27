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
    id = "split_stp"
    name = "Document splitting straight-through"
    question = "What share of multi-document files split correctly with no human touch?"
    waiting_on = "files with human-confirmed document boundaries."


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
    id = "superseded_value_rate"
    name = "Superseded values reaching output"
    question = "How often does a value that a later document replaced reach output unflagged?"
    higher_is_better = False
    dimensions = ("segment",)
    waiting_on = "links between documents that amend or replace each other."


class EscapeRate(_AwaitingTruth):
    id = "escape_rate"
    name = "Escape rate"
    question = "How often does a wrong value clear both automation and human review?"
    higher_is_better = False
    dimensions = ("segment",)
    waiting_on = "a re-verified spot-check sample of published output."
