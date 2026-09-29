"""Document extraction scored per field (assay_sdk/documents.py, assay/documents.py)."""
import json
import sys
from datetime import date, datetime, timedelta

import pytest

from assay_sdk.documents import (Date, LineItems, Money, Number, Text, before, check_rules, required, rule,
                                 score_document, total_of)

from test_ci import project, run  # noqa: F401

SCHEMA = {"invoice_number": Text(weight=3), "invoice_date": Date(day_first=True), "total": Money(weight=3),
          "po_number": Text(),
          "line_items": LineItems({"description": Text(), "amount": Money()}, key="description")}
TRUTH = {"invoice_number": "INV-17", "invoice_date": "04/03/2026", "total": "1.234,56 €", "po_number": None,
         "line_items": [{"description": "Widget", "amount": "1000.00"}, {"description": "Bolt", "amount": "234.56"}]}


def test_values_match_by_type_not_by_spelling():
    assert Money().same(Money().read("1.234,56 €"), Money().read("$1,234.56"))
    assert Money().read("(12.00)") == -12.0 and Number().read("1.234.567") == 1234567
    assert Money(decimal_comma=True).read("1.234") == 1234 and Money().read("1.234") == 1.234
    assert Number(relative=0.01).same(100, 100.9) and not Number().same(100, 100.9)
    d = Date(day_first=True)
    assert {d.read(v) for v in ("2026-03-04", "04.03.2026", "04/03/2026", "4th March 2026", "March 4, 2026",
                                datetime(2026, 3, 4, 9))} == {date(2026, 3, 4)}
    assert Date().read("04/03/2026") == date(2026, 4, 3)  # month first unless day_first
    assert Date().read("13/03/2026") == date(2026, 3, 13)  # but 13 can only be a day
    assert Text().read("  INV-17 ") == Text().read("inv-17") and Text(exact=True).read("A") != "a"
    assert Money(currency=True).same(Money(currency=True).read("€5"), Money(currency=True).read("5 EUR"))
    assert not Money(currency=True).same(Money(currency=True).read("$5"), Money(currency=True).read("5 EUR"))


def test_each_field_says_which_way_it_went_wrong():
    got = {"invoice_number": " inv-17", "invoice_date": "2026-04-03", "total": "123456", "po_number": "PO-9",
           "line_items": [{"description": "bolt", "amount": "234.56"}, {"description": "Widget", "amount": "100"},
                          {"description": "Nut", "amount": "1"}]}
    s = score_document(None, TRUTH, got, SCHEMA)
    f = s.fields
    assert f["invoice_number"].kind == "correct"
    assert f["invoice_date"].kind == "wrong" and "day and month swapped" in f["invoice_date"].note
    assert f["total"].kind == "wrong" and "factor of 100" in f["total"].note
    assert f["po_number"].kind == "invented" and f["po_number"].counts == {"fp": 1}
    rows = f["line_items"]  # matched by description, whatever the order: Bolt right, Widget wrong, Nut invented
    assert rows.counts == {"tp": 1, "fp": 2, "fn": 1, "rows": 2, "rows_extracted": 3, "rows_invented": 1} and \
        round(rows.share, 2) == 0.4
    assert "1 row(s) invented" in rows.note and "Widget: '100', not '1000.00'" in rows.note
    assert f["line_items.amount"].share == 0.5 and f["line_items.amount"].part_of == "line_items"
    missing = score_document(None, TRUTH, {**TRUTH, "total": ""}, SCHEMA).fields["total"]
    assert missing.kind == "missing" and missing.counts == {"fn": 1}
    # weighted: invoice_number (3) right, date (1), total (3), po (1) wrong, line items (1) at 0.4
    assert round(s.accuracy, 3) == round((3 + 0.4) / 9, 3) and not s.all_correct
    assert score_document(None, TRUTH, TRUTH, SCHEMA).all_correct


def test_headers_and_line_items_are_cells_under_one_definition():
    got = {"invoice_number": " inv-17", "invoice_date": "2026-04-03", "total": "123456", "po_number": "PO-9",
           "line_items": [{"description": "bolt", "amount": "234.56"}, {"description": "Widget", "amount": "100"},
                          {"description": "Nut", "amount": "1"}]}
    s = score_document(None, TRUTH, got, SCHEMA)
    # headers: number right, date and total wrong (fp and fn), po invented (fp)
    # rows: Bolt's two cells right, Widget's description right and amount wrong, Nut's two cells invented
    assert s.cells == {"tp": 4, "fp": 6, "fn": 3}
    assert s.precision == 0.4 and s.recall == 4 / 7 and s.f1 == 8 / 17
    assert s.fields["line_items.amount"].counts == {"tp": 1, "fp": 2, "fn": 1}
    lost = score_document(None, TRUTH, {**TRUTH, "line_items": TRUTH["line_items"][:1]}, SCHEMA)
    assert lost.cells == {"tp": 5, "fp": 0, "fn": 2}  # the missing row's two cells are missing
    assert score_document(None, TRUTH, TRUTH, SCHEMA).f1 == 1.0
    assert score_document(None, {}, {}, {"x": Text()}).f1 == 1.0 and \
        score_document(None, {}, {}, {"x": Text()}).precision is None


def test_zero_is_a_value_not_an_empty_one():
    spec = {"discount": Money()}
    assert score_document(None, {"discount": 0}, {"discount": None}, spec).fields["discount"].kind == "missing"
    assert score_document(None, {"discount": None}, {"discount": "0.00"}, spec).fields["discount"].kind == "invented"
    assert score_document(None, {"discount": "0"}, {"discount": 0.0}, spec).fields["discount"].kind == "correct"
    assert score_document(None, {"discount": None}, {"discount": ""}, spec).fields["discount"].counts == {}


def test_a_correct_value_that_cant_be_read_is_the_labels_problem():
    s = score_document(None, {**TRUTH, "invoice_date": "sometime in March"}, TRUTH, SCHEMA)
    assert s.fields["invoice_date"].kind == "unreadable" and s.fields["invoice_date"].passed is None
    assert s.all_correct  # nothing counted against the extractor


def test_rules_need_no_correct_values():
    rules = [total_of("line_items.amount", equals="total"), total_of(["subtotal", "tax"], equals="total"),
             before("invoice_date", "due_date", day_first=True), required("invoice_number", "vendor"),
             rule("positive total", lambda d: float(d["total"]) > 0), rule("broken", lambda d: 1 / 0)]
    doc = {"invoice_number": "INV-17", "total": "1234.56", "subtotal": "1000", "tax": "234.56",
           "invoice_date": "04/03/2026", "due_date": "01/03/2026",
           "line_items": [{"amount": "1000.00"}, {"amount": "200"}]}
    out = check_rules(None, doc, rules)
    assert out["total = line_items.amount"] == (False, "line_items.amount = 1,200.00, total = 1,234.56")
    assert out["total = subtotal + tax"][0] is True
    assert out["invoice_date before due_date"][0] is False  # due before it was issued
    assert out["has invoice_number, vendor"] == (False, "no vendor")
    assert out["positive total"] == (True, "")
    assert out["broken"][0] is None and "ZeroDivisionError" in out["broken"][1]
    assert check_rules(None, {"total": "5"}, [total_of("line_items.amount", equals="total")])[
        "total = line_items.amount"] == (None, "")  # no line items: not checkable, not failed


class Recorder:
    def __init__(self):
        self.checks = []

    def check(self, field, status, **kw):
        self.checks.append({"field": field, "status": status, **kw})


def test_each_field_rule_and_the_document_is_a_check():
    r = Recorder()
    got = {**TRUTH, "invoice_date": "2026-04-03"}
    score_document(r, TRUTH, got, SCHEMA, rules=[total_of("line_items.amount", equals="total", decimal_comma=None)])
    by = {c["field"]: c for c in r.checks}
    assert by["invoice_date"]["status"] == "fail" and by["invoice_date"]["category"] == "wrong"
    assert by["invoice_date"]["reason"].startswith("wrong: 2026-04-03, not 2026-03-04")
    assert json.loads(by["invoice_date"]["raw_output"]) == {"kind": "wrong", "weight": 1.0, "share": 0.0,
                                                             "fp": 1, "fn": 1, "made_up": "format",
                                                             "value": "2026-04-03"}
    doc = json.loads(by["document"]["raw_output"])
    assert doc["cells"] == {"tp": 6, "fp": 1, "fn": 1} and doc["f1"] == round(12 / 14, 6)
    assert by["document"]["status"] == "fail" and by["document"]["reason"] == "wrong: invoice_date (wrong)"
    assert by["rule: total = line_items.amount"]["status"] == "pass"
    assert all(c["evaluator"] == "assay.documents@1" for c in r.checks)


SUITE = '''
import os
from assay_sdk.documents import score_document, Text, Money, Date, LineItems, total_of

SCHEMA = {"invoice_number": Text(weight=3), "invoice_date": Date(day_first=True), "total": Money(weight=3),
          "line_items": LineItems({"description": Text(), "amount": Money()}, key="description")}
TRUTH = {
    "inv-1": {"invoice_number": "1", "invoice_date": "04/03/2026", "total": "30",
              "line_items": [{"description": "a", "amount": "10"}, {"description": "b", "amount": "20"}]},
    "inv-2": {"invoice_number": "2", "invoice_date": "05/03/2026", "total": "5",
              "line_items": [{"description": "c", "amount": "5"}]},
    "inv-3": {"invoice_number": "3", "invoice_date": "20/03/2026", "total": "7",
              "line_items": [{"description": "d", "amount": "7"}]},
}

def extract(doc):  # the pipeline under test; the PR reads 04/03 as April 3
    out = dict(TRUTH[doc])
    if os.environ.get("MODE") == "after" and doc == "inv-1":
        out["invoice_date"] = "2026-04-03"
    return out

def test_inv_1(assay_case):
    score_document(assay_case, TRUTH["inv-1"], extract("inv-1"), SCHEMA, rules=[total_of("line_items.amount", equals="total")])

def test_inv_2(assay_case):
    score_document(assay_case, TRUTH["inv-2"], extract("inv-2"), SCHEMA, rules=[total_of("line_items.amount", equals="total")])

def test_inv_3(assay_case):
    score_document(assay_case, TRUTH["inv-3"], extract("inv-3"), SCHEMA, rules=[total_of("line_items.amount", equals="total")])
'''


def test_a_pr_that_breaks_a_field_fails_and_says_which(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_invoices.py").write_text(SUITE)
    first = run(project)
    assert first.returncode == 0, first.stdout
    assert "Documents    3 · all fields correct 3/3 (100%) · weighted field accuracy 100%" in first.stdout
    out = run(project, env={"MODE": "after", "GITHUB_STEP_SUMMARY": str(project / "step.md")})
    assert out.returncode == 1
    assert "Documents    3 · all fields correct 2/3 (66.7%, was 100%) · weighted field accuracy 95.8% (was 100%)" \
        in out.stdout
    # 17 cells a run (3 headers and 2 rows of 2 in inv-1, 3 and 1 row in the others); one read wrong
    assert "cell F1 94.1% (was 100%), precision 94.1%, recall 94.1%" in out.stdout
    # 3 values a document, the line-item tables not counted; no text given, so only format is told
    assert "made up, of 9 values extracted: 1 format (was 0) (inferred and fabricated need the text" in out.stdout
    line = next(x for x in out.stdout.splitlines() if x.strip().startswith("invoice_date"))
    assert "66.7%" in line and "1 wrong" in line  # precision and recall, 2 of 3
    assert "tests/test_invoices.py::test_inv_1  All fields correct, invoice_date" in out.stdout
    assert "Judges" not in out.stdout  # comparisons, not judges: nothing to calibrate
    assert "wrong: 2026-04-03, not 2026-03-04: day and month swapped" in out.stdout
    md = (project / ".assay" / "summary.md").read_text()
    assert "**Documents:** all fields correct 2/3 (66.7%, was 100%)" in md and "invoice\\_date 66.7%" in md

    # The dashboard's field accuracy is measured from the same checks.
    from assay import store
    from assay.measures.ground_truth import FieldAccuracy
    from assay.models import Window
    from assay.sources.events import EventsSource
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    now = datetime.utcnow()
    m = FieldAccuracy().compute(EventsSource(engine, "local"), Window(now - timedelta(days=1), now + timedelta(days=1)))
    assert m.status == "measured" and m.overall.n == 6  # both runs' documents, each once
    assert m.overall.denominator == 6 * 8  # weights 3 + 1 + 3 + 1 per document; baseline copies left out
    by_field = {r.slice_value: r.value for r in m.results if r.dimension == "field"}
    assert by_field["invoice_date"] < 1 and by_field["total"] == 1 and "line_items.amount" not in by_field
    from assay.measures.documents import FieldCellF1
    f1 = FieldCellF1().compute(EventsSource(engine, "local"), Window(now - timedelta(days=1), now + timedelta(days=1)))
    assert f1.status == "measured" and f1.overall.n == 6
    assert (f1.overall.numerator, f1.overall.denominator) == (2 * 33, 2 * 33 + 2)  # both runs, one cell wrong


def test_field_accuracy_waits_for_scored_documents():
    from assay import store
    from assay.measures.ground_truth import FieldAccuracy
    from assay.models import Window
    from assay.sources.events import EventsSource
    engine = store.make_engine("sqlite://")
    store.metadata.create_all(engine)
    now = datetime.utcnow()
    out = FieldAccuracy().compute(EventsSource(engine, "t"), Window(now - timedelta(days=1), now))
    assert out.status == "unmeasured" and "score_document" in out.reason


# ---------- phase 2: document types, splitting, confidence ----------

from types import SimpleNamespace  # noqa: E402

from assay_sdk.documents import classify_document, score_split  # noqa: E402


def rows_of(recorder, case="c"):
    return [SimpleNamespace(evaluator=c.get("evaluator"), raw_output=c.get("raw_output"), status=c["status"],
                            field=c["field"], case_id=f"{case}{i}") for i, c in enumerate(recorder.checks)]


def test_a_document_type_is_right_or_it_says_what_it_was_taken_for():
    r = Recorder()
    assert classify_document(r, "Invoice", " invoice")
    assert not classify_document(r, "invoice", "receipt", confidence=0.4)
    assert not classify_document(r, "invoice", None)
    assert [c["status"] for c in r.checks] == ["pass", "fail", "fail"]
    assert r.checks[1]["reason"] == "classified as 'receipt', not 'invoice'" and r.checks[1]["category"] == "misclassified"
    assert json.loads(r.checks[1]["raw_output"]) == {"kind": "classification", "expected": "invoice",
                                                     "predicted": "receipt", "confidence": 0.4}
    from assay.documents import summarize
    t = summarize(rows_of(r))["types"]
    assert t["n"] == 3 and t["right"] == 1 and t["mistakes"] == [["invoice", "(none)", 1], ["invoice", "receipt", 1]]
    assert t["per_type"]["invoice"] == {"precision": 1.0, "recall": 1 / 3, "n": 3}
    assert t["per_type"]["receipt"]["precision"] == 0.0


def test_a_split_says_what_went_wrong():
    merged = score_split(None, [(1, 2), (3, 3), (4, 6)], [(1, 2), (3, 6)])
    assert not merged.correct and merged.notes == ["pages 3-6 came out as one document, which is 2",
                                                   "1 page to move by hand"]
    assert merged.boundaries == {"tp": 1, "fp": 0, "fn": 1}
    cut = score_split(None, [(1, 3)], [(1, 1), (2, 3)])
    assert cut.notes == ["pages 1-3 is one document, cut into 2", "1 page to move by hand"]
    shifted = score_split(None, [1, 3, 5], [1, 4, 5], page_count=6)  # first pages, with the page count
    assert shifted.notes == ["the document starting on page 3 was split at page 4", "1 page to move by hand"]
    assert shifted.right == [(5, 6)]
    assert score_split(None, [{"pages": [1, 2]}, {"start": 3, "end": 4}], [(1, 2), (3, 4)]).correct
    with pytest.raises(ValueError, match="page_count"):
        score_split(None, [1, 3], [1, 3])


def test_a_split_has_its_panoptic_quality_and_the_pages_to_move():
    s = score_split(None, [(1, 2), (3, 3), (4, 6)], [(1, 3), (4, 6)])
    # (1,2)~(1,3) share 2 of 3 pages, (4,6) exact; page 3 alone matches nothing
    assert s.panoptic == {"iou": pytest.approx(5 / 3), "tp": 2, "fp": 0, "fn": 1}
    assert s.pq == pytest.approx((5 / 3) / 2.5) and s.sq == pytest.approx(5 / 6) and s.rq == pytest.approx(0.8)
    assert s.drags == 1 and s.pages == 6  # drag page 3 out to a document of its own
    halves = score_split(None, [(1, 10)], [(1, 5), (6, 10)])
    assert halves.pq == 0 and halves.drags == 5  # half the pages isn't over half: no match
    assert score_split(None, [(1, 3), (4, 6)], [(1, 2), (3, 4), (5, 6)]).drags == 2  # pages 3 and 4
    assert score_split(None, [(1, 4)], [(1, 1), (2, 2), (3, 3), (4, 4)]).drags == 3
    right = score_split(None, [(1, 2), (3, 6)], [(1, 2), (3, 6)])
    assert right.pq == 1.0 and right.drags == 0 and right.notes == []
    assert score_split(None, [(1, 4)], [(1, 2)]).drags == 2  # pages left out are dragged in


SPLITS = '''
import os
from assay_sdk.documents import score_split
TRUTH = [(1, 2), (3, 3), (4, 6)]

def test_file_1(assay_case):
    score_split(assay_case, TRUTH, TRUTH if os.environ.get("MODE") != "after" else [(1, 3), (4, 6)])

def test_file_2(assay_case):
    score_split(assay_case, TRUTH, TRUTH if os.environ.get("MODE") != "after" else [(1, 6)])
'''


def test_split_quality_and_its_cost_in_the_report_and_on_the_dashboard(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_split.py").write_text(SPLITS)
    (project / "assay.toml").write_text("[documents]\nseconds_per_drag = 20\nrework_per_hour = 36\n")
    assert run(project).returncode == 0
    out = run(project, env={"MODE": "after"})
    # file 1: 2 matches, IoU 2/3 and 1, page 3 unmatched; 1 page to move.
    # file 2, all in one: (4,6) is 3 of its 6 pages, not over half: nothing matches; keep (4,6), move 3.
    # PQ (5/3) / (2 + 0.5 * 1 + 0.5 * 4) = 37.0%; 4 pages at 20 s, $36 an hour: 80 s, $0.80
    assert "panoptic quality 37.0% (was 100%) · pages to move by hand 4 of 12 (was 0), " \
           "about 1 minute by hand ($0.80)" in out.stdout
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    pq, drags = REGISTRY["split_pq"].compute(src, w), REGISTRY["split_drag_rate"].compute(src, w)
    assert drags.overall.numerator == 4 and drags.overall.denominator == 24  # both runs' files
    assert pq.status == "measured" and 0 < pq.overall.value < 1
    live = {m["id"]: m["status"] for m in coverage.compute(src, w)["measures"]}
    assert live["split_pq"] == live["split_drag_rate"] == "live"
    (project / "assay.toml").write_text("[documents]\nseconds_per_drag = 0\n")
    from assay.local import load_config, SetupError
    with pytest.raises(SetupError, match="a positive number"):
        load_config(project)


def test_confidence_says_what_threshold_is_safe_and_what_yours_lets_through():
    from assay.documents import confidence
    # 40 values: confident ones right, unconfident ones often wrong; one confident mistake at 0.95.
    pairs = [(0.99, True)] * 20 + [(0.95, False)] + [(0.95, True)] * 9 + [(0.6, True)] * 5 + [(0.6, False)] * 5
    c = confidence(pairs, target=0.99, threshold=0.9)
    assert c["n"] == 40 and round(c["accuracy"], 3) == round(34 / 40, 3)
    assert c["suggested"]["threshold"] == 0.99 and c["suggested"]["approved"] == 0.5  # 0.95 lets the mistake in
    assert c["suggested"]["low"] < 0.99  # 20 of 20 can't prove 99%
    assert c["at"] == {"threshold": 0.9, "approved": 0.75, "wrong": 1, "accuracy": 29 / 30, "wrong_total": 6}
    # 0.99 and 0.95 share the top tenth: 30 values, 29 right, said 0.977; the 0.6 tenth: 10 values, half right.
    assert round(c["ece"], 4) == round((abs(29 / 30 - (0.99 * 20 + 0.95 * 10) / 30) * 30 + 0.1 * 10) / 40, 4)
    assert confidence([(0.9, True)] * 5, 0.99)["suggested"] is None  # too few to call anything safe


PHASE2 = '''
import os
from assay_sdk.documents import classify_document, score_split, score_document, Text, Money

SCHEMA = {"number": Text(), "total": Money()}
FILES = {"f1": ([(1, 2), (3, 4)], "invoice"), "f2": ([(1, 1), (2, 3)], "receipt"), "f3": ([(1, 3)], "invoice")}
after = os.environ.get("MODE") == "after"

def run_file(case, name):
    truth, kind = FILES[name]
    split = [(1, 4)] if after and name == "f1" else truth           # the PR merges f1's two documents
    guessed = "receipt" if after and name == "f3" else kind         # and takes f3 for a receipt
    classify_document(case, kind, guessed, confidence=0.97)
    score_split(case, truth, split)
    conf = {"number": 0.99, "total": 0.93 if name == "f2" else 0.99}
    got = {"number": "1", "total": "9" if name == "f2" else "10"}   # f2's total is wrong, and fairly sure of it
    score_document(case, {"number": "1", "total": "10"}, got, SCHEMA, confidence=conf)

def test_f1(assay_case): run_file(assay_case, "f1")
def test_f2(assay_case): run_file(assay_case, "f2")
def test_f3(assay_case): run_file(assay_case, "f3")
'''


def test_types_splitting_and_confidence_in_the_report(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_files.py").write_text(PHASE2)
    (project / "assay.toml").write_text('[test]\ncommand = "pytest -q tests"\n\n[documents]\nauto_approve = 0.9\n')
    first = run(project)
    assert first.returncode == 1 and "f2" in first.stdout  # no baseline yet: f2's wrong total fails
    from assay.__main__ import main
    assert main(["accept"]) == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    assert "Types        3 classified · right 2/3 (66.7%, was 100%)" in out.stdout
    assert "invoice → receipt 1" in out.stdout
    assert "Splitting    3 files · split right 2/3 (66.7%, was 100%) · with several documents 1/2" in out.stdout
    assert "pages 1-4 came out as one document, which is 2" in out.stdout
    assert "Confidence   9 values" in out.stdout and "overconfident" in out.stdout  # fields and types
    assert "at your auto_approve 0.9: 100% approved, 2 wrong values among them (was 1), of 2 wrong in all: " \
        "they'd skip review" in out.stdout  # the confident misclassification is the second
    md = (project / ".assay" / "summary.md").read_text()
    assert "types right 2/3 (66.7%, was 100%)" in md and "files split right 2/3" in md

    from assay import store
    from assay.measures.ground_truth import SplitStraightThrough
    from assay.models import Window
    from assay.sources.events import EventsSource
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    now = datetime.utcnow()
    m = SplitStraightThrough().compute(EventsSource(engine, "local"), Window(now - timedelta(days=1), now + timedelta(1)))
    assert m.status == "measured" and m.overall.n == 4 and m.overall.value == 0.75  # f1, f2 in both runs; f1 broke once


def test_documents_config_is_checked(project):
    from assay import local
    (project / "assay.toml").write_text('[test]\ncommand = "true"\n\n[documents]\nauto_approve = 90\n')
    with pytest.raises(local.SetupError, match="a share from 0 to 1"):
        local.load_config(project)
    (project / "assay.toml").write_text('[test]\ncommand = "true"\n\n[documents]\nthreshold = 0.9\n')
    with pytest.raises(local.SetupError, match="Use auto_approve, target"):
        local.load_config(project)


# ---------- phase 3: OCR, locations, values on the page ----------

import time  # noqa: E402

from assay_sdk.documents import Rule, appears_in, iou, score_locations, score_ocr  # noqa: E402

PAGE = "INVOICE 17\nDate: 4 March 2026\nTotal: 1,234.56 EUR\nThank you"


def test_ocr_counts_characters_words_and_digits():
    s = score_ocr(None, PAGE, "INV0ICE 17\nDate: 4  March 2026\nTota1: 1,284.56 EUR\nThank you")
    assert (s.char_errors, s.word_errors, s.digit_errors, s.digits) == (3, 3, 3, 13)  # spacing doesn't count
    assert s.lines == [("INVOICE 17", "INV0ICE 17"), ("Total: 1,234.56 EUR", "Tota1: 1,284.56 EUR")]
    assert score_ocr(None, PAGE, PAGE.lower(), case=False).cer == 0 and score_ocr(None, PAGE, PAGE.lower()).cer > 0
    r = Recorder()
    score_ocr(r, PAGE, PAGE.replace("234", "284"), page=2, max_cer=0.05, max_digit_errors=0)
    c = r.checks[0]
    assert c["field"] == "ocr page 2" and c["status"] == "fail" and c["category"] == "ocr"
    assert c["reason"] == "1 wrong digit: 'Total: 1,284.56 EUR' for 'Total: 1,234.56 EUR'"


def test_a_long_page_is_scored_line_by_line():
    page = "\n".join(f"line {i} amount {i * 3.17:.2f} for the item number {i}" for i in range(400))
    started = time.time()
    s = score_ocr(None, page, page.replace("7", "1"))
    assert time.time() - started < 1.5 and 0 < s.cer < 0.05  # every line differs, still quick
    lines = page.split("\n")
    lines.insert(200, "a line the OCR made up")
    assert score_ocr(None, page, "\n".join(lines)).char_errors == len("a line the OCR made up")


def test_a_value_read_from_the_wrong_place():
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)
    r = Recorder()
    out = score_locations(r, {"total": {"page": 1, "bbox": [10, 10, 50, 20]}, "date": {"page": 1, "bbox": [0, 0, 10, 10]},
                              "vendor": {"page": 1, "bbox": [0, 0, 5, 5]}, "iban": {"page": 1, "bbox": [0, 0, 100, 10]}},
                          {"total": {"page": 1, "bbox": [12, 10, 50, 21]}, "date": {"page": 2, "bbox": [0, 0, 10, 10]},
                           "iban": {"page": 1, "bbox": [0, 0, 20, 10]}})
    assert out == {"total": (True, ""), "date": (False, "on page 2, not 1"), "vendor": (False, "no location given"),
                   "iban": (False, "overlaps the right box by 0.20 (under 0.5)")}
    assert [c["field"] for c in r.checks] == ["location: total", "location: date", "location: vendor", "location: iban"]
    xywh = score_locations(None, {"a": {"bbox": [0, 0, 10, 10]}}, {"a": {"bbox": [0, 0, 10, 10]}}, box="xywh")
    assert xywh == {"a": (True, "")}


def test_a_value_thats_not_on_the_page_was_made_up():
    schema = {"total": Money(), "invoice_date": Date(day_first=True), "vendor": Text(), "number": Text()}
    rule_ = appears_in(PAGE, schema)
    assert isinstance(rule_, Rule)
    ok, why = rule_.fn({"total": "1234.56", "invoice_date": "2026-03-04", "number": "17"})
    assert ok and why == ""  # found as "1,234.56 EUR" and "4 March 2026"
    ok, why = rule_.fn({"total": "1234.50", "invoice_date": "04/03/2026", "vendor": "Acme"})
    assert not ok and why == "not in the document's text: total '1234.50', vendor 'Acme'"
    assert check_rules(None, {"total": "999"}, [appears_in(PAGE, schema)])["values appear in the text"][0] is False


PHASE3 = '''
import os
from assay_sdk.documents import score_ocr, score_locations, check_rules, appears_in, Money

PAGE = "Total: 1,234.56 EUR\\nDue: 30 April 2026\\nThank you"
after = os.environ.get("MODE") == "after"

def test_page(assay_case):
    read = PAGE.replace("234", "284") if after else PAGE          # the PR's OCR model misreads a digit
    score_ocr(assay_case, PAGE, read, page=1, max_digit_errors=0)
    got = {"total": {"page": 1, "bbox": [10, 10, 60, 20]}}
    score_locations(assay_case, {"total": {"page": 1, "bbox": [10, 10, 60, 20]}}, got)
    check_rules(assay_case, {"total": "1284.56" if after else "1234.56"}, [appears_in(PAGE, {"total": Money()})])
'''


def test_ocr_and_grounding_in_the_report(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_pages.py").write_text(PHASE3)
    assert run(project).returncode == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    assert "OCR          1 page · characters wrong 2.2% (was 0%) · words wrong 11.1% (was 0%) · digits wrong 8.3% " \
           "(was 0%) · letters wrong 0% · 1 over the limit" in out.stdout  # 1 of the page's 12 digits
    assert "read as: '3' as '8' 1" in out.stdout and "since the baseline: new '3' as '8' 1" in out.stdout
    assert "words read as: '1,234.56' as '1,284.56' 1" in out.stdout
    assert "new OCR confusions: '3' as '8' 1" in (project / ".assay" / "summary.md").read_text()
    assert "'Total: 1,284.56 EUR' for 'Total: 1,234.56 EUR'" in out.stdout
    assert "Locations    1 field · right page and box 1/1 (100%) · mean overlap 1.00" in out.stdout
    assert "not in the document's text: total '1284.56'" in out.stdout  # no labels needed to catch it
    assert "OCR characters wrong 2.2%, was 0%" in (project / ".assay" / "summary.md").read_text()


# ---------- reading order, tables, spot checks, and the dashboard ----------

from assay_sdk.documents import score_table, spot_check  # noqa: E402

TWO_COLUMNS = ["Left one", "Left two", "Left three", "Right one", "Right two", "Right three"]


def test_reading_order_is_scored_apart_from_the_text():
    across = "\n".join(TWO_COLUMNS[i] for i in (0, 3, 1, 4, 2, 5))  # a two-column page read across
    s = score_ocr(None, "\n".join(TWO_COLUMNS), across)
    assert s.cer > 0.5 and round(s.order, 2) == 0.67 and s.order_free_cer == 0  # read right, out of order
    typo = score_ocr(None, "\n".join(TWO_COLUMNS), across.replace("three", "thr3e"))
    assert 0 < typo.order_free_cer < 0.05  # the typos, and only those
    assert score_ocr(None, "\n".join(TWO_COLUMNS), "\n".join(TWO_COLUMNS)).order == 1.0
    r = Recorder()
    score_ocr(r, "\n".join(TWO_COLUMNS), across, max_cer=1.0, min_order=0.9)
    assert r.checks[0]["status"] == "fail" and r.checks[0]["reason"] == "67% of lines in reading order"


TABLE = [["Item", "Qty", "Amount"], ["Widget", "2", "10.00"], ["Bolt", "5", "2.50"]]


def test_a_table_says_what_happened_to_its_structure():
    assert score_table(None, TABLE, TABLE).correct
    moved = score_table(None, TABLE, [["Amount", "Item", "Qty"], ["2.50", "Bolt", "5"], ["10.00", "Widget", "2"]],
                        cells={"Amount": Money()})
    assert moved.correct and moved.f1 == 1.0  # columns and rows in another order: the same table
    merged = score_table(None, TABLE, [["Item Qty", "Amount"], ["Widget 2", "10.00"], ["Bolt 5", "2.50"]])
    assert merged.notes == ["columns 'Item' and 'Qty' merged into one"] and not merged.shape_right
    assert round(merged.recall, 2) == 0.33 and merged.precision == 0.5
    lost = score_table(None, TABLE, TABLE[:2])
    assert lost.notes == ["1 row(s) missing"] and lost.recall == 0.5 and lost.precision == 1.0
    garbled = score_table(None, TABLE, [TABLE[0], TABLE[1], ["Bolt", "5", "25.0"]], cells={"Amount": Money()})
    assert garbled.notes == ["1 cell(s) wrong, e.g. Amount in row 2: '25.0', not '2.50'"] and garbled.shape_right
    extra = score_table(None, TABLE, [r + ["x"] for r in TABLE])
    assert extra.notes == ["a column that isn't there: 'x'"]
    assert score_table(None, [["a", "b"], ["c", "d"]], [["a", "b", "x"], ["c", "d", "y"]], header=False).notes == \
        ["3 columns, not 2"]


def test_spot_checks_of_published_output_give_the_escape_rate(project, monkeypatch):
    import assay_sdk as assay
    from assay import local, store
    from assay.measures.ground_truth import EscapeRate
    from assay.models import Window
    from assay.sources.events import EventsSource
    path = project / "spot.jsonl"
    monkeypatch.setenv("ASSAY_PATH", str(path))
    assay.init()
    assert spot_check("doc-1", "total", "1,234.56", "1234.56", Money(), reviewed=True)
    assert not spot_check("doc-2", "total", "1,284.56", "1234.56", Money(), auto_approved=True, checked_by="sam")
    assert spot_check("doc-3", "vendor", "ACME", "Acme", auto_approved=True)
    assert not spot_check("doc-4", "iban", None, "DE89 3704", auto_approved=True)
    assay.shutdown()
    engine = store.make_engine("sqlite://")
    store.metadata.create_all(engine)
    assert local.load_file(engine, str(path), "t")[1] == []
    now = datetime.utcnow()
    m = EscapeRate().compute(EventsSource(engine, "t"), Window(now - timedelta(days=1), now + timedelta(days=1)))
    assert m.status == "measured" and m.overall.value == 0.5 and m.overall.n == 4
    by_path = {r.slice_value: r.value for r in m.results if r.dimension == "path"}
    assert by_path == {"auto-approved": 2 / 3, "reviewed": 0.0}  # auto-approval lets more through


DASH = '''
import os
from assay_sdk.documents import score_ocr, score_locations, score_table

after = os.environ.get("MODE") == "after"
PAGE = "Left one\\nLeft two\\nRight one\\nRight two"
TABLE = [["Item", "Amount"], ["Widget", "10.00"], ["Bolt", "2.50"]]

def test_page(assay_case):
    read = "Left one\\nRight one\\nLeft two\\nRight two" if after else PAGE   # the PR reads across the columns
    score_ocr(assay_case, PAGE, read, page=1, max_cer=1.0)
    score_locations(assay_case, {"total": {"page": 1, "bbox": [0, 0, 10, 10]}},
                    {"total": {"page": 2 if after else 1, "bbox": [0, 0, 10, 10]}})
    score_table(assay_case, TABLE, [["Item Amount"], ["Widget 10.00"], ["Bolt 2.50"]] if after else TABLE,
                name="items")
'''


def test_dashboard_measures_and_the_report(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_page.py").write_text(DASH)
    assert run(project).returncode == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    # [1, 3, 2, 4]: three of the four lines keep the page's order
    assert "reading order 75.0% of lines (was 100%) · with them put back in order, characters wrong 0%" in out.stdout
    assert "e.g. 'Right one' for ''" not in out.stdout  # only moved: no misleading example
    assert "Tables       1 · right 0/1 (0%, was 100%) · structure right 0/1 · cells right: F1 0% (was 100%)" \
        in out.stdout
    assert "items (tests/test_page.py::test_page): columns 'Item' and 'Amount' merged into one" in out.stdout
    assert "row(s) missing" not in out.stdout  # rows matched by place when no column matches
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    got = {mid: REGISTRY[mid].compute(src, w) for mid in ("ocr_cer", "ocr_digit_error_rate", "ocr_reading_order",
                                                           "location_accuracy", "table_cell_f1",
                                                           "ocr_letter_error_rate")}
    assert all(m.status == "measured" for m in got.values())
    assert got["ocr_reading_order"].overall.value == 0.875  # 100% then 75%, the same page twice
    assert got["location_accuracy"].overall.value == 0.5 and got["table_cell_f1"].overall.n == 2
    assert {r.slice_value for r in got["location_accuracy"].results if r.dimension == "field"} == {"total"}
    live = {m["id"]: m for m in coverage.compute(src, w)["measures"]}
    assert live["ocr_cer"]["status"] == "live" and live["escape_rate"]["status"] == "blocked"
    assert live["escape_rate"]["missing"] == ["spot checks of published output (spot_check)"]


from assay_sdk.documents import superseded_values  # noqa: E402


def test_values_a_later_document_replaced_and_whether_output_followed(project, monkeypatch):
    import assay_sdk as assay
    from assay import coverage, local, store
    from assay.measures.ground_truth import SupersededValues
    from assay.models import Window
    from assay.sources.events import EventsSource
    path = project / "superseded.jsonl"
    monkeypatch.setenv("ASSAY_PATH", str(path))
    assay.init()
    schema = {"total": Money(), "due_date": Date(day_first=True), "po_number": Text()}
    old = {"total": "1,234.56", "due_date": "30/04/2026", "po_number": "PO-1", "vendor": "Acme"}
    new = {"total": "1,200.00", "due_date": "30/04/2026", "po_number": "", "vendor": "Acme"}  # a credit note's correction
    got = superseded_values("inv-17", "inv-17-corrected", old, new,
                            output={"total": "1234.56", "due_date": "2026-04-30", "po_number": None}, schema=schema)
    assert got == {"total": "escaped", "po_number": "updated"}  # due_date and vendor unchanged: not counted
    assert superseded_values("inv-18", "inv-18b", {"total": "5"}, {"total": "6"}, {"total": "5"}, flagged=["total"],
                             schema=schema, link="amends") == {"total": "flagged"}
    assert superseded_values("inv-19", "inv-19b", {"total": "5"}, {"total": "6"}, {"total": "6.00"},
                             schema=schema) == {"total": "updated"}
    assay.shutdown()
    engine = store.make_engine("sqlite://")
    store.metadata.create_all(engine)
    assert local.load_file(engine, str(path), "t")[1] == []
    now = datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    m = SupersededValues().compute(EventsSource(engine, "t"), w)
    assert m.status == "measured" and m.overall.value == 0.25 and m.overall.denominator == 4
    assert {r.slice_value: r.value for r in m.results if r.dimension == "link"} == {"amends": 0.0, "replaces": 1 / 3}
    assert {r.slice_value: r.value for r in m.results if r.dimension == "field"} == {"po_number": 0.0, "total": 1 / 3}
    from sqlalchemy import select
    t = store.eval_results
    with engine.connect() as conn:
        reason = conn.execute(select(t.c.reason).where(t.c.status == "fail")).scalar()
    assert reason == "output holds the old value ('1234.56'); inv-17-corrected replaces it with '1,200.00'"
    assert {x["id"]: x["status"] for x in coverage.compute(EventsSource(engine, "t"), w)["measures"]}[
        "superseded_value_rate"] == "live"


def test_field_accuracy_counts_fields_only(project):
    """Types, pages, tables, splits and locations are checks of their own, not fields at 0%."""
    (project / "tests").mkdir()
    (project / "tests" / "test_mixed.py").write_text('''
from assay_sdk.documents import score_document, classify_document, score_ocr, score_table, score_locations, Text

def test_doc(assay_case):
    score_document(assay_case, {"number": "1"}, {"number": "1"}, {"number": Text()})
    classify_document(assay_case, "invoice", "invoice")
    score_ocr(assay_case, "a page", "a page")
    score_table(assay_case, [["A"], ["x"]], [["A"], ["x"]])
    score_locations(assay_case, {"number": {"bbox": [0, 0, 1, 1]}}, {"number": {"bbox": [0, 0, 1, 1]}})
''')
    assert run(project).returncode == 0
    from assay import store
    from assay.measures.ground_truth import FieldAccuracy
    from assay.models import Window
    from assay.sources.events import EventsSource
    now = datetime.utcnow()
    m = FieldAccuracy().compute(EventsSource(store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}"), "local"),
                                Window(now - timedelta(days=1), now + timedelta(days=1)))
    assert m.overall.value == 1.0 and {r.slice_value for r in m.results if r.dimension == "field"} == {"number"}


# ---------- how a value was made up: format, inferred, fabricated ----------

DEED = {"county": Text(), "grantee": Text(), "recorded": Date(day_first=True), "parcel": Text(),
        "price": Money(), "governing_law": Text()}
DEED_TEXT = ("WARRANTY DEED. Jane Roe, grantor, conveys to John Doe, grantee, land in Kings County. "
             "Recorded 04/03/2026. Price $1,250,000.00. The grantor resides in California; California "
             "taxes are paid. This deed is governed by the laws of New York.")
DEED_TRUTH = {"county": "Kings", "grantee": "John Doe", "recorded": "04/03/2026", "parcel": None,
              "price": "1250000", "governing_law": "New York"}
DEED_GOT = {"county": "Queens", "grantee": "Jane Roe", "recorded": "03/04/2026", "parcel": "12-345",
            "price": "1,250.000", "governing_law": "California"}


def test_a_made_up_value_says_how():
    s = score_document(None, DEED_TRUTH, DEED_GOT, DEED, text=DEED_TEXT)
    f = s.fields
    assert f["county"].made_up == "fabricated"  # a guessed county, nowhere in the deed
    assert f["parcel"].made_up == "fabricated" and f["parcel"].kind == "invented"
    assert f["grantee"].made_up == "inferred"  # the grantor, given as the grantee
    assert f["governing_law"].made_up == "inferred"  # the state mentioned most, not the one that governs
    assert "inferred: it's in the document, but not as this field" in f["governing_law"].note
    assert f["recorded"].made_up == "format" and f["price"].made_up == "format"
    assert s.made_up == {"format": 2, "inferred": 2, "fabricated": 2}
    # without the text only format errors can be told
    blind = score_document(None, DEED_TRUTH, DEED_GOT, DEED)
    assert blind.made_up == {"format": 2, "inferred": 0, "fabricated": 0} and not blind.fields["county"].grounded
    # missing and correct values weren't made up
    assert score_document(None, DEED_TRUTH, {**DEED_TRUTH, "county": ""}, DEED, text=DEED_TEXT).fields[
        "county"].made_up is None


def test_the_right_value_in_the_wrong_shape():
    assert Text().reshaped("inv-17", "inv17") and Text().reshaped("jane doe", "doe, jane")
    assert not Text().reshaped("john doe", "jane doe")
    assert Number().reshaped(1234.56, 123456) and not Number().reshaped(1234.56, 1234.0)
    m = Money(currency=True)
    assert not m.reshaped(m.read("€100"), m.read("$1000"))  # another currency is other information
    assert Date().reshaped(date(2026, 3, 4), date(2026, 4, 3)) and not Date().reshaped(date(2026, 3, 4),
                                                                                       date(2026, 3, 5))


DEEDS = f'''
from assay_sdk.documents import score_document, Text, Money, Date
DEED = {{"county": Text(), "grantee": Text(), "recorded": Date(day_first=True), "parcel": Text(),
        "price": Money(), "governing_law": Text()}}
TEXT = {DEED_TEXT!r}
TRUTH = {DEED_TRUTH!r}
GOT = {DEED_GOT!r}

def test_deed_1(assay_case):
    score_document(assay_case, TRUTH, GOT, DEED, text=TEXT)

def test_deed_2(assay_case):
    score_document(assay_case, TRUTH, TRUTH, DEED, text=TEXT)
'''


def test_made_up_values_in_the_report_and_on_the_dashboard(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_deeds.py").write_text(DEEDS)
    out = run(project)
    # 6 values extracted in deed 1, 5 in deed 2 (no parcel)
    assert "made up, of 11 values extracted: 2 format, 2 inferred, 2 fabricated" in out.stdout
    assert "governing_law" in out.stdout and "1 wrong, 1 inferred" in out.stdout
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    got = {mid: REGISTRY[mid].compute(src, w) for mid in ("fabricated_value_rate", "inferred_value_rate",
                                                           "format_error_rate")}
    assert {mid: (m.overall.numerator, m.overall.denominator) for mid, m in got.items()} == {
        "fabricated_value_rate": (2, 11), "inferred_value_rate": (2, 11), "format_error_rate": (2, 11)}
    by_field = {r.slice_value: r.value for r in got["inferred_value_rate"].results if r.dimension == "field"}
    assert by_field["grantee"] == 0.5 and by_field["county"] == 0
    live = {m["id"]: m["status"] for m in coverage.compute(src, w)["measures"]}
    assert live["inferred_value_rate"] == live["format_error_rate"] == "live"


def test_inferred_and_fabricated_wait_for_the_text(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_deeds.py").write_text(DEEDS.replace(", text=TEXT", ""))
    out = run(project)
    assert "made up, of 11 values extracted: 2 format (inferred and fabricated need the text" in out.stdout
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    inferred = REGISTRY["inferred_value_rate"].compute(src, w)
    assert inferred.status == "unmeasured" and "text=" in inferred.reason
    assert REGISTRY["format_error_rate"].compute(src, w).status == "measured"
    live = {m["id"]: m for m in coverage.compute(src, w)["measures"]}
    assert live["inferred_value_rate"]["status"] == "blocked"
    assert live["inferred_value_rate"]["missing"] == ["fields scored with the document's text (score_document, text=)"]


# ---------- the first bugs every extraction scorer hits, right by default ----------

def test_a_field_only_the_extractor_gave_is_invented_not_ignored():
    s = score_document(None, {"total": "1250"}, {"total": "1250", "po_number": "PO-9", "notes": ""})
    assert s.fields["po_number"].kind == "invented" and s.cells == {"tp": 1, "fp": 1, "fn": 0}
    assert "notes" in s.fields and s.fields["notes"].kind == "correct"  # empty both sides: nothing to count
    assert not s.all_correct


def test_equal_values_match_without_a_schema():
    s = score_document(None, {"total": "1250", "rate": 0.5, "date": "2026-03-04", "paid": "$1,250.00",
                              "vendor": {"name": "Acme Co"}},
                       {"total": "1,250.00", "rate": "0.50", "date": "4 March 2026", "paid": "1250 USD",
                        "vendor": {"name": "  ACME co"}})
    assert s.all_correct, {k: f.note for k, f in s.fields.items() if not f.passed}
    assert set(s.fields) == {"total", "rate", "date", "paid", "vendor.name"}
    assert score_document(None, {"total": "1250"}, {"total": "1205"}).fields["total"].kind == "wrong"


def test_types_are_read_from_the_values():
    from assay_sdk.documents import infer_schema
    got = infer_schema({"n": "1,250.00", "amt": "€12", "d": "04.03.2026", "zip": "02139", "id": "INV-17",
                        "parcel": "12-345", "flag": True, "items": [{"desc": "a", "amt": "5"}]},
                       {"extra": 7})
    assert {k: type(v).__name__ for k, v in got.items()} == {
        "n": "Number", "amt": "Money", "d": "Date", "zip": "Text", "id": "Text", "parcel": "Text", "flag": "Text",
        "items": "LineItems", "extra": "Number"}
    assert type(got["items"].fields["amt"]).__name__ == "Number"
    s = score_document(None, {"zip": "02139"}, {"zip": "2139"})
    assert s.fields["zip"].kind == "wrong"  # a leading zero is part of an identifier


def test_fields_outside_the_schema_are_named():
    s = score_document(None, TRUTH, {**TRUTH, "notes": "rush", "vendor": {"name": "x"}, "blank": ""}, SCHEMA)
    assert s.unscored == ["notes", "vendor"] and s.all_correct  # named, not scored against the schema
    r = Recorder()
    score_document(r, TRUTH, {**TRUTH, "notes": "rush"}, SCHEMA)
    assert json.loads(next(c for c in r.checks if c["field"] == "document")["raw_output"])["unscored"] == ["notes"]
    assert score_document(None, {"vendor": {"name": "a"}}, {"vendor": {"name": "a"}},
                          {"vendor.name": Text()}).unscored == []


def test_unscored_fields_in_the_report(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text('''
from assay_sdk.documents import score_document, Text
def test_a(assay_case):
    score_document(assay_case, {"n": "1"}, {"n": "1", "po_number": "PO-9"}, {"n": Text()})
def test_b(assay_case):
    score_document(assay_case, {"n": "2"}, {"n": "2", "po_number": "PO-3", "notes": "x"}, {"n": Text()})
''')
    out = run(project)
    assert "extracted but not in the schema, so not scored: po_number (2 documents), notes (1 document)" in out.stdout


# ---------- line items: the best matching, completeness; groups; TEDS ----------

from assay_sdk.documents import Group, score_table, teds  # noqa: E402


def test_rows_are_paired_to_get_the_most_cells_right():
    # most-in-common-first pairs the first rows (3 cells) and strands the second (0): 3 right.
    # The best matching pairs across, 2 + 2: 4 right, as DocILE scores it.
    spec = {"li": LineItems({c: Text() for c in "pqrs"})}
    exp = [dict(zip("pqrs", "abcd")), dict(zip("pqrs", "stcx"))]
    got = [dict(zip("pqrs", "abcx")), dict(zip("pqrs", "abyz"))]
    s = score_document(None, {"li": exp}, {"li": got}, spec)
    assert s.cells["tp"] == 4 and "missing" not in (s.fields["li"].note or "")
    t = score_table(None, [list("pqrs")] + [list(r.values()) for r in exp],
                    [list("pqrs")] + [list(r.values()) for r in got])
    assert t.cells_right == 4 and "row(s) missing" not in t.notes


def test_a_repeated_row_is_duplicated_not_invented():
    spec = {"li": LineItems({"d": Text(), "amt": Money()}, key="d")}
    row = {"d": "Widget", "amt": "10"}
    s = score_document(None, {"li": [row]}, {"li": [row, dict(row), {"d": "Nut", "amt": "1"}]}, spec)
    assert s.fields["li"].counts["rows_duplicated"] == 1 and s.fields["li"].counts["rows_invented"] == 1
    assert "1 row(s) duplicated" in s.fields["li"].note
    t = score_table(None, [["A"], ["x"]], [["A"], ["x"], ["x"]])
    assert "1 row(s) duplicated" in t.notes and "row(s) that aren't there" not in " ".join(t.notes)


PARTY = Group({"name": Text(), "address": Text(), "role": Text()})


def test_a_group_is_right_only_when_all_its_parts_are():
    spec = {"grantor": PARTY, "county": Text()}
    exp = {"grantor": {"name": "Jane Roe", "address": "1 Main St", "role": "seller"}, "county": "Kings"}
    got = {"grantor": {"name": "Jane Roe", "address": "1 Main St", "role": "buyer"}, "county": "Kings"}
    s = score_document(None, exp, got, spec, text="Jane Roe of 1 Main St, seller, to John Doe, buyer. Kings County.")
    g = s.fields["grantor"]
    assert g.kind == "wrong" and g.note == "role wrong (2 of 3 right)" and g.counts["members"] == 3
    assert s.fields["grantor.role"].made_up == "inferred" and s.fields["grantor.role"].grouped
    assert s.accuracy == 0.5  # the group once, wrong; the county right
    assert s.cells == {"tp": 3, "fp": 1, "fn": 1}  # its parts are cells
    assert score_document(None, exp, {"county": "Kings"}, spec).fields["grantor"].kind == "missing"
    assert score_document(None, {"county": "Kings"}, got, spec).fields["grantor"].kind == "invented"
    assert score_document(None, exp, exp, spec).all_correct


def test_several_parties_are_line_items_of_groups():
    spec = {"grantees": LineItems({"name": Text(), "role": Text()}, key="name")}
    exp = [{"name": "John Doe", "role": "buyer"}, {"name": "Ann Doe", "role": "buyer"}]
    got = [{"name": "Ann Doe", "role": "buyer"}, {"name": "John Doe", "role": "seller"}]
    f = score_document(None, {"grantees": exp}, {"grantees": got}, spec).fields
    assert f["grantees"].counts["tp"] == 1 and f["grantees.role"].counts == {"tp": 1, "fp": 1, "fn": 1}


def test_teds_scores_structure_and_text():
    t = [["a", "b"], ["c", "abc"]]
    assert teds(t, t) == 1.0
    assert teds(t, [["a", "b"], ["c", "xyz"]]) == pytest.approx(1 - 1 / 7)  # one cell renamed, 7 nodes
    assert teds(t, [["a", "b"]]) == pytest.approx(1 - 3 / 7)  # a row and its two cells gone
    assert teds(t, [["a", "b"], ["c", "xyz"]], structure_only=True) == 1.0
    assert teds(t, [["A ", "b"], ["c", "ABC"]]) == 1.0  # case and spacing don't count
    s = score_table(None, t, [["a", "b"], ["c", "abd"]])
    assert s.teds == pytest.approx(1 - (1 / 3) / 7) and s.teds_structure == 1.0


GROUPED = '''
from assay_sdk.documents import score_document, score_table, Group, LineItems, Text, Money
SPEC = {"grantor": Group({"name": Text(), "address": Text(), "role": Text()}),
        "items": LineItems({"d": Text(), "amt": Money()}, key="d")}
TRUTH = {"grantor": {"name": "Jane Roe", "address": "1 Main St", "role": "seller"},
         "items": [{"d": "fee", "amt": "10"}, {"d": "tax", "amt": "2"}]}
TEXT = "Jane Roe, 1 Main St, seller, to John Doe, buyer. fee 10 tax 2"

def test_deed(assay_case):
    got = {"grantor": {**TRUTH["grantor"], "role": "buyer"},
           "items": [{"d": "fee", "amt": "10"}, {"d": "fee", "amt": "10"}]}
    score_document(assay_case, TRUTH, got, SPEC, text=TEXT)
    score_table(assay_case, [["d", "amt"], ["fee", "10"], ["tax", "2"]], [["d", "amt"], ["fee", "10"]])
'''


def test_groups_completeness_and_teds_in_the_report_and_on_the_dashboard(project):
    from assay import store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_deed.py").write_text(GROUPED)
    out = run(project)
    assert "line items complete 0/1: 1 row missing, 1 row duplicated" in out.stdout
    assert "TEDS 70.0%" in out.stdout  # 10 nodes with the header, a row and its 2 cells gone
    assert "made up, of 3 values extracted: 1 inferred, 0 fabricated" in out.stdout
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    acc = REGISTRY["field_accuracy"].compute(src, w)
    assert {r.slice_value for r in acc.results if r.dimension == "field"} == {"grantor", "items"}
    inferred = REGISTRY["inferred_value_rate"].compute(src, w)
    assert (inferred.overall.numerator, inferred.overall.denominator) == (1, 3)  # the grantor's three parts
    assert REGISTRY["table_teds"].compute(src, w).overall.value == pytest.approx(0.7, abs=1e-6)


# ---------- gates: line items, and fields where one error is one too many ----------

COLLAPSE = '''
import os
from assay_sdk.documents import score_document, Text, Money, LineItems
SCHEMA = {"invoice_number": Text(), "tax_number": Text(), "total": Money(),
          "line_items": LineItems({"description": Text(), "amount": Money()}, key="description")}
ROWS = [{"description": d, "amount": str(10 * (i + 1))} for i, d in enumerate("abcdefghij")]
TRUTH = {f"inv-{n}": {"invoice_number": str(n), "tax_number": f"DE{n}99", "total": "550", "line_items": ROWS}
         for n in range(1, 5)}

def extract(doc):
    out = {**TRUTH[doc], "line_items": [dict(r) for r in ROWS]}
    out["line_items"][0]["amount"] = "1"  # always one row wrong: the check was failing already
    if os.environ.get("MODE") == "after":  # the alternative model: headers fine, line items collapse
        for r in out["line_items"][1:8]:
            r["amount"] = "0"
    if os.environ.get("TAX") == "wrong" and doc == "inv-4":
        out["tax_number"] = "DE499X"
    return out

def test_inv_1(assay_case): score_document(assay_case, TRUTH["inv-1"], extract("inv-1"), SCHEMA)
def test_inv_2(assay_case): score_document(assay_case, TRUTH["inv-2"], extract("inv-2"), SCHEMA)
def test_inv_3(assay_case): score_document(assay_case, TRUTH["inv-3"], extract("inv-3"), SCHEMA)
def test_inv_4(assay_case): score_document(assay_case, TRUTH["inv-4"], extract("inv-4"), SCHEMA)
'''


def test_line_items_collapsing_fails_the_run_though_the_check_was_already_failing(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_inv.py").write_text(COLLAPSE)
    from assay.__main__ import main
    assert run(project).returncode == 1  # one row wrong in every document, and no baseline yet
    assert main(["accept"]) == 0  # the team accepts it: a known, small line-item error
    assert run(project).returncode == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1, out.stdout
    # 9 of 10 rows right in every document before, 2 of 10 now: the headers stay right
    assert "failed: line_items: F1 20.0%, was 90.0%; per document down 70.0 points (95% interval 70.0 to 70.0, " \
           "4 documents), surely more than the 2 allowed" in out.stdout
    assert "1 document gate failed: line_items: F1 20.0%" in out.stdout
    md = (project / ".assay" / "summary.md").read_text()
    assert "weighted field accuracy 80.0%" in md  # the average still looks fine
    assert "gates failed: line\\_items: F1 20.0%, was 90.0%" in md
    # a small drop stays within the default
    (project / "tests" / "test_inv.py").write_text(COLLAPSE.replace("[1:8]", "[1:1]"))
    assert run(project).returncode == 0


def test_one_wrong_tax_number_fails_the_run(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_inv.py").write_text(COLLAPSE)
    (project / "assay.toml").write_text("[documents.gates]\ntax_number = { max_errors = 0 }\nline_items = {}\n")
    from assay.__main__ import main
    run(project)
    assert main(["accept"]) == 0
    assert run(project).returncode == 0
    out = run(project, env={"TAX": "wrong", "MODE": "after"})  # line items collapse too, but that gate is off
    assert out.returncode == 1
    assert "Gates        0 of 1 held:" in out.stdout
    assert "failed: tax_number: 1 wrong, missing or invented, at most 0 allowed" in out.stdout


def test_gates_are_checked_when_read(project):
    from assay.local import load_config, SetupError
    for bad, says in [("tax_number = 0", "a table of rules"), ("total = { max_wrong = 0 }", "unknown max_wrong"),
                      ("total = { min_recall = 95 }", "a share from 0 to 1"),
                      ("total = { max_errors = 0.5 }", "a whole number")]:
        (project / "assay.toml").write_text(f"[documents.gates]\n{bad}\n")
        with pytest.raises(SetupError, match=says):
            load_config(project)


def test_gates_need_something_to_gate():
    from assay.documents import check_gates
    now = {"fields": {"total": {"precision": 1.0, "recall": 0.9, "f1": 0.95, "errors": 1, "table": False}}}
    got = check_gates(now, None, {"total": {"min_recall": 0.95, "max_drop": 0.01}, "tax_number": {"max_errors": 0}})
    assert [(g["field"], g["rule"], g["passed"]) for g in got] == [
        ("tax_number", "scored", False), ("total", "min_recall", False)]  # max_drop: no baseline to compare with



# ---------- documents with zero errors, and critical fields ----------

def test_critical_fields_say_whether_a_document_could_go_straight_through():
    got = {**TRUTH, "po_number": "PO-9"}  # a low-stakes field wrong
    s = score_document(None, TRUTH, got, SCHEMA, critical=["invoice_number", "total"])
    assert not s.all_correct and s.critical_correct is True
    assert s.fields["total"].critical and not s.fields["po_number"].critical
    assert score_document(None, TRUTH, {**TRUTH, "total": "1"}, SCHEMA, critical=["total"]).critical_correct is False
    assert score_document(None, TRUTH, TRUTH, SCHEMA).critical_correct is None
    with pytest.raises(ValueError, match="critical vat isn't in the schema"):
        score_document(None, TRUTH, TRUTH, SCHEMA, critical=["vat"])


def test_showing_a_target_takes_enough_values():
    from assay.documents import values_to_show
    from assay.calibrate import wilson
    n = values_to_show(0.999)
    assert n == 3838 and wilson(n, n)[0] >= 0.999 > wilson(n - 1, n - 1)[0]
    assert values_to_show(0.99) == 381


STP = '''
import os
from assay_sdk.documents import score_document, Text, Money
SCHEMA = {"tax_number": Text(), "total": Money(), "note": Text()}
TRUTH = {"tax_number": "DE123", "total": "10", "note": "rush"}

def case(n, run):
    got = dict(TRUTH)
    got["note"] = "" if n % 2 else "rush"  # half the documents: a low-stakes field missing
    if os.environ.get("MODE") == "after" and n == 2:  # a document that was right: now one of 4
        got["tax_number"] = "DE128"
    score_document(run, TRUTH, got, SCHEMA, critical=["tax_number", "total"])

def test_1(assay_case): case(1, assay_case)
def test_2(assay_case): case(2, assay_case)
def test_3(assay_case): case(3, assay_case)
def test_4(assay_case): case(4, assay_case)
'''


def test_document_accuracy_and_critical_fields_in_the_report_gates_and_dashboard(project):
    from assay import coverage, store
    from assay.__main__ import main
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_stp.py").write_text(STP)
    (project / "assay.toml").write_text("[documents.gates]\ncritical = { min_accuracy = 0.999 }\n"
                                        "document = { min_accuracy = 0.4 }\n")
    first = run(project)
    assert "critical fields (tax_number, total): 8 of 8 right (100%, 95% interval 67.56% to 100%) · " \
           "documents with all of them right 4/4" in first.stdout
    assert "99.90% can't be shown with 8 values: even all right, the interval's low end would be 67.56%; " \
           "it takes 3,838 in a row" in first.stdout
    assert "Gates        2 of 2 held" in first.stdout  # zero-error documents 2/4, over 40%
    assert main(["accept"]) == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    assert "failed: critical: accuracy 87.50%, at least 99.90% required" in out.stdout
    assert "failed: document: accuracy 25.0%, at least 40.0% required" in out.stdout
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    got = {mid: REGISTRY[mid].compute(src, w).overall for mid in
           ("document_accuracy", "critical_document_accuracy", "critical_field_accuracy")}
    assert (got["document_accuracy"].numerator, got["document_accuracy"].denominator) == (3, 8)  # both runs
    assert (got["critical_document_accuracy"].numerator, got["critical_document_accuracy"].denominator) == (7, 8)
    assert (got["critical_field_accuracy"].numerator, got["critical_field_accuracy"].denominator) == (15, 16)
    live = {m["id"]: m["status"] for m in coverage.compute(src, w)["measures"]}
    assert live["document_accuracy"] == live["critical_field_accuracy"] == "live"


def test_critical_measures_wait_for_critical_fields(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_stp.py").write_text(STP.replace(', critical=["tax_number", "total"]', ""))
    run(project)
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    m = REGISTRY["critical_field_accuracy"].compute(src, w)
    assert m.status == "unmeasured" and "critical=" in m.reason
    assert REGISTRY["document_accuracy"].compute(src, w).status == "measured"
    live = {x["id"]: x for x in coverage.compute(src, w)["measures"]}
    assert live["critical_field_accuracy"]["status"] == "blocked"


def test_every_measure_is_in_a_dashboard_group():
    from assay.measures import GROUPS, REGISTRY
    grouped = [m for ids in GROUPS.values() for m in ids]
    assert sorted(grouped) == sorted(REGISTRY) and len(grouped) == len(set(grouped))


def _tabme_mndd(pred, gold):
    """MNDD as TABME's reference code computes it (github.com/aldolipani/TABME, utils/evaluation.py,
    num_of_swaps): exact documents set aside, then every pairing tried."""
    import itertools
    p = [d for d in pred if d not in gold]
    g = [d for d in gold if d not in pred]
    if len(g) < len(p):
        p, g = g, p
    return min((sum(len(a) - len(set(a) & set(b)) for a, b in zip(p, perm))
                for perm in itertools.permutations(g)), default=0)


def test_drags_are_the_minimum_number_of_drags_and_drops_of_the_paper():
    import random
    from assay_sdk.documents import _drags
    rng = random.Random(7)

    def split(n):
        cuts = sorted(rng.sample(range(2, n + 1), rng.randint(0, min(5, n - 1))))
        starts = [1] + cuts
        return [(s, starts[i + 1] - 1 if i + 1 < len(starts) else n) for i, s in enumerate(starts)]
    pages = lambda segs: [list(range(a, b + 1)) for a, b in segs]
    for _ in range(300):
        n = rng.randint(1, 12)
        gold, pred = split(n), split(n)
        assert _drags(gold, pred)[0] == _tabme_mndd(pages(pred), pages(gold)), (gold, pred)
    # the paper's first two examples: page 6 to a new document is 1; pages 4 and 5 out are 2
    assert _drags([(1, 3), (4, 5), (6, 6)], [(1, 3), (4, 6)])[0] == 1
    assert _drags([(1, 3), (4, 5)], [(1, 5)])[0] == 2


def test_split_rework_cost_on_the_dashboard(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_split.py").write_text(SPLITS)
    run(project, env={"MODE": "after"})  # 4 pages to drag over 2 files
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    m = REGISTRY["split_rework_cost"].compute(src, w)
    assert m.status == "unmeasured" and "seconds_per_drag and rework_per_hour (or review_per_hour)" in m.reason
    src.cost_rates = {"seconds_per_drag": 30, "review_per_hour": 60}  # no rework rate: the review rate
    m = REGISTRY["split_rework_cost"].compute(src, w)
    assert m.status == "measured" and m.overall.value == pytest.approx(4 * 30 / 3600 * 60 / 2)  # $1 a file
    src.cost_rates = {"seconds_per_drag": 30, "review_per_hour": 60, "rework_per_hour": 30}
    assert REGISTRY["split_rework_cost"].compute(src, w).overall.numerator == pytest.approx(1.0)
    assert {x["id"]: x["status"] for x in coverage.compute(src, w)["measures"]}["split_rework_cost"] == "live"


# ---------- risk-coverage: what an auto-approve threshold lets through ----------

def test_risk_coverage_scores_the_ranking():
    from assay.documents import confidence, risk_coverage
    assert risk_coverage([(0.9, True), (0.8, False)]) == {"aurc": 0.25, "best": 0.25}  # wrong one last: the best
    assert risk_coverage([(0.9, False), (0.8, True)])["aurc"] == 0.75
    assert risk_coverage([(0.9, True), (0.9, False)])["aurc"] == 0.5  # tied: approved together
    # a wrong value at 0.99, above the right ones at 0.95; another at 0.6
    pairs = [[0.99, False]] + [[0.95, True]] * 8 + [[0.6, False]]
    c = confidence(pairs, threshold=0.9)
    at = {r["threshold"]: r for r in c["curve"]}
    assert at[0.99]["approved"] == 0.1 and at[0.99]["wrong"] == 1 and at[0.95]["wrong"] == 1
    assert at[0.9]["approved"] == 0.9 and at[0.5]["wrong"] == 2
    assert c["band"]["n"] == 9 and c["band"]["right"] == pytest.approx(8 / 9)
    assert c["aurc"] > c["best"]  # the confident wrong value costs along the whole curve


CONFIDENT = '''
from assay_sdk.documents import score_document, Text
S = {"v": Text()}

def case(i, run):
    wrong = i % 10 == 0            # 1 in 10 wrong, and stated 0.97: overconfident at the top
    conf = 0.97 if i % 2 == 0 else 0.6
    score_document(run, {"v": "a"}, {"v": "b" if wrong else "a"}, S, confidence={"v": conf})
''' + "".join(f"\ndef test_{i}(assay_case): case({i}, assay_case)\n" for i in range(40))


def test_the_report_gives_the_band_check_and_the_risk_coverage_table(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_conf.py").write_text(CONFIDENT)
    (project / "assay.toml").write_text("[documents]\nauto_approve = 0.9\n")
    out = run(project)
    # 20 at 0.97, 4 of them wrong; 20 at 0.6, all right
    assert "stated 0.9 or more: 20 values, says 97.0% on average, right 80.0% (95% interval 58.4% to 91.9%): " \
           "overconfident; 80 more to reach the 100 a check needs" in out.stdout
    assert "risk-coverage: AURC 0." in out.stdout and "the best possible 0." in out.stdout
    row = next(x for x in out.stdout.splitlines() if x.strip().startswith("0.9 "))
    assert row.split()[1:4] == ["50.0%", "80.0%", "4"] and row.rstrip().endswith("yours")
    assert next(x for x in out.stdout.splitlines() if x.strip().startswith("0.5 ")).split()[1:4] == \
        ["100%", "90.0%", "4"]


def test_confidence_on_the_dashboard(project):
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    (project / "tests").mkdir()
    (project / "tests" / "test_conf.py").write_text(CONFIDENT)
    run(project)
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    src, now = EventsSource(engine, "local"), datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    got = {mid: REGISTRY[mid].compute(src, w) for mid in ("confidence_aurc", "confident_error_rate", "confidence_ece")}
    assert all(m.status == "measured" and m.overall.n == 40 for m in got.values())
    assert (got["confident_error_rate"].overall.numerator, got["confident_error_rate"].overall.denominator) == (4, 20)
    from assay.documents import risk_coverage
    pairs = [(0.97 if i % 2 == 0 else 0.6, i % 10 != 0) for i in range(40)]
    assert got["confidence_aurc"].overall.value == pytest.approx(risk_coverage(pairs)["aurc"])
    live = {m["id"]: m["status"] for m in coverage.compute(src, w)["measures"]}
    assert live["confidence_aurc"] == "live"
    (project / "tests" / "test_conf.py").write_text(CONFIDENT.replace(', confidence={"v": conf}', ""))
    other = store.make_engine("sqlite://")
    store.metadata.create_all(other)
    assert REGISTRY["confidence_aurc"].compute(EventsSource(other, "t"), w).status == "unmeasured"



# ---------- OCR: letters apart from digits, and what was read as what ----------

def test_ocr_says_what_was_read_as_what():
    from assay_sdk.documents import score_ocr
    s = score_ocr(None, "Total due 1,250.00\nInvoice modern clinic\nWill pay",
                  "Tota1 due 1,25O.OO\nInvoice modem c1inic\nWiII pay")
    assert s.confusions == {("l", "1"): 2, ("0", "O"): 3, ("rn", "m"): 1, ("l", "I"): 2}
    assert s.word_confusions[("modern", "modem")] == 1 and s.word_confusions[("Will", "WiII")] == 1
    assert s.letters == 34 and s.letter_errors == 9 and s.letter_error_rate == pytest.approx(9 / 34)
    lost = score_ocr(None, "invoice", "invoce")
    assert lost.confusions == {("i", ""): 1}
    assert score_ocr(None, "a\nb\nc", "c\nb\na").confusions == {}  # only moved: nothing misread


def test_confusions_diff_against_the_baseline():
    from assay.documents import confusion_diff, confusion_lines
    now = [["l", "1", 9], ["0", "O", 5], ["rn", "m", 2]]
    before = [["0", "O", 5], ["rn", "m", 6], ["S", "5", 3]]
    d = confusion_diff(now, before)
    assert d["new"] == [["l", "1", 9, 0]] and d["fewer"] == [["rn", "m", 2, 6]] and d["gone"] == [["S", "5", 0, 3]]
    assert d["more"] == []
    lines = confusion_lines(now, before, "read as")
    assert lines[0].strip() == "read as: 'l' as '1' 9, '0' as 'O' 5, 'rn' as 'm' 2"
    assert lines[1].strip() == "since the baseline: new 'l' as '1' 9; fixed 'S' as '5' (was 3)"


# ---------- OCR without labels: engines ranked against corrected text ----------

from assay_sdk.documents import OcrCorrectionError, anls, correct_ocr, rank_ocr  # noqa: E402

PAGES = {1: "Invoice 17\nTotal 1,250.00", 2: "Pay to Acme\nDue 4 March"}
ENGINES = {"good": dict(PAGES), "meh": {1: "Invoice 17\nTota1 1,25O.OO", 2: "Pay to Acme\nDue 4 March"},
           "bad": {1: "lnvoice l7\nTota1 1,25O.OO", 2: "Pay t0 Acrne"}}


def test_anls_is_one_minus_edits_over_the_longer_and_nothing_below_tau():
    assert anls("Total 1250", "Total  1250") == 1.0
    assert anls("Total 1250", "Tota1 125O") == pytest.approx(0.8)
    assert anls("abc", "xyz") == 0.0 and anls("", "") == 1.0


def test_engines_are_ranked_without_labels_and_checked_where_there_are_some():
    corrected = {"claude": dict(PAGES), "other": {1: PAGES[1], 2: "Pay to Acme\nDue 4 Mar"}}
    r = rank_ocr(None, ENGINES, corrected, truth={1: PAGES[1]})
    assert r.order == ["good", "meh", "bad"] and r.pages == 2
    assert r.scores["good"] == pytest.approx((r.by_corrector["claude"]["good"] + r.by_corrector["other"]["good"]) / 2)
    assert r.kendall == 1.0 and r.ndcg == pytest.approx(1.0) and r.same_best
    missing = rank_ocr(None, {"a": {1: PAGES[1]}, "b": dict(PAGES)}, {"c": dict(PAGES)})
    assert missing.scores["a"] == 0.5  # a page it didn't read scores 0


def test_an_engine_is_never_scored_against_its_own_corrections():
    with pytest.raises(ValueError, match="good would be scored against its own corrections"):
        rank_ocr(None, ENGINES, {"good": dict(PAGES)})
    with pytest.raises(ValueError, match="meh would be scored"):
        rank_ocr(None, ENGINES, {"claude": dict(PAGES)}, same_model={"meh": "claude"})


class FakeClaude:
    """Stands in for anthropic.Anthropic(): records the request, answers with `text`."""

    def __init__(self, text, stop="end_turn"):
        self.text, self.stop, self.requests = text, stop, []
        self.messages = self

    def create(self, **req):
        self.requests.append(req)
        return {"content": [{"type": "text", "text": self.text}], "stop_reason": self.stop, "model": req["model"],
                "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_a_page_is_corrected_by_a_model_with_the_image_when_given():
    from assay_sdk.llm import Judge
    fake = FakeClaude("<ocr>\nInvoice 17\nTotal 1,250.00\n</ocr>")
    got = correct_ocr("lnvoice 17\nTota1 1,25O.OO", Judge("anthropic", "claude-opus-5-5", client=fake),
                      image=b"\x89PNG fake")
    assert got == "Invoice 17\nTotal 1,250.00"
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["extra_body"] == {"fallbacks": "default"}
    blocks = req["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert "lnvoice 17" in blocks[1]["text"] and "Correct only OCR errors" in blocks[1]["text"]
    with pytest.raises(OcrCorrectionError, match="declined"):
        correct_ocr("x", Judge("anthropic", "claude-opus-5-5", client=FakeClaude("", stop="refusal")))
    with pytest.raises(OcrCorrectionError, match="cut off"):
        correct_ocr("x", Judge("anthropic", "claude-opus-5-5", client=FakeClaude("partial", stop="max_tokens")))


RANKED = f'''
from assay_sdk.documents import rank_ocr
PAGES = {PAGES!r}
ENGINES = {ENGINES!r}

def test_rank(assay_case):
    rank_ocr(assay_case, ENGINES, {{"claude": PAGES}}, truth={{1: PAGES[1]}})
'''


def test_the_ranking_in_the_report_apart_from_ocr_scored_against_labels(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_rank.py").write_text(RANKED)
    out = run(project)
    assert out.returncode == 0, out.stdout
    assert "OCR ranking  3 engines · 2 pages · against text corrected by claude (no labels): good 100%, " \
           "meh " in out.stdout
    assert "on the 1 labelled page: the same best engine · Kendall tau 1.00 · NDCG 1.00" in out.stdout
    assert "OCR          " not in out.stdout  # nothing scored against a model's reading counts as OCR accuracy


# ---------- robustness slices: facets, and the unseen template ----------

def test_facets_are_recorded_with_every_check():
    r = Recorder()
    score_document(r, TRUTH, TRUTH, SCHEMA, facets={"source": "scanned", "stamps": True, "template_seen": False,
                                                    "language": "de", "currency": None})
    want = {"source": "scanned", "stamps": "yes", "template_seen": "unseen", "language": "de"}
    assert all(json.loads(c["raw_output"]).get("facets") == want for c in r.checks if c["field"] != "rule")
    with pytest.raises(ValueError, match="lowercase letters"):
        score_document(None, TRUTH, TRUTH, SCHEMA, facets={"Template Seen": False})


SLICED = '''
import os
from assay_sdk.documents import score_document, Text, Money
SCHEMA = {"number": Text(), "total": Money(), "vendor": Text()}

def case(i, run):
    unseen = i % 5 == 0                      # 1 in 5 from a layout the model was never tuned on
    truth = {"number": str(i), "total": "10", "vendor": "Acme"}
    got = dict(truth)
    if os.environ.get("MODE") == "after" and unseen:
        got["total"] = "100"                 # the new model breaks only on unseen layouts
    score_document(run, truth, got, SCHEMA, facets={"template_seen": not unseen,
                                                    "source": "scanned" if i % 2 else "digital"})
'''


def sliced(n):
    return SLICED + "".join(f"\ndef test_{i}(assay_case): case({i}, assay_case)\n" for i in range(n))


def test_a_slice_that_got_worse_is_named_though_the_average_held(project):
    from assay.__main__ import main
    (project / "tests").mkdir()
    (project / "tests" / "test_sliced.py").write_text(sliced(40))
    (project / "assay.toml").write_text('[documents.gates]\n'
                                        '"document[template_seen=unseen]" = { min_accuracy = 0.9 }\n')
    first = run(project)
    assert first.returncode == 0, first.stdout
    assert "Gates        1 of 1 held" in first.stdout
    assert main(["accept"]) == 0
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    # 8 unseen documents, all right before and wrong now: 1 in 256 by chance
    assert "Slices       4 by facet · documents with zero errors · worse beyond chance: template_seen=unseen" \
        in out.stdout
    line = next(x for x in out.stdout.splitlines() if x.strip().startswith("template_seen=unseen"))
    assert "8 · zero errors 0% (was 100%, down 100.0 points: worse beyond chance, p 0.016)" in line
    # each source slice lost 4 of 20: within chance, and said so
    digital = next(x for x in out.stdout.splitlines() if x.strip().startswith("source=digital"))
    assert "down 20.0 points, within chance" in digital
    assert "weighted field accuracy 93.3%" in out.stdout  # the average, hiding it
    assert "failed: document[template_seen=unseen]: accuracy 0%, at least 90.0% required" in out.stdout
    md = (project / ".assay" / "summary.md").read_text()
    assert "slices worse beyond chance: template\\_seen=unseen" in md and "source=" not in md.split("beyond chance:")[1]


def test_a_small_slice_is_not_called_worse_on_noise(project):
    from assay.__main__ import main
    (project / "tests").mkdir()
    (project / "tests" / "test_sliced.py").write_text(sliced(20))
    run(project)
    assert main(["accept"]) == 0
    out = run(project, env={"MODE": "after"})
    # 4 unseen documents all flipped: 1 in 16 by chance, 1 in 4 across four slices
    line = next(x for x in out.stdout.splitlines() if x.strip().startswith("template_seen=unseen"))
    assert "down 100.0 points, within chance (p 0.25, 4 of 4 documents)" in line
    assert "worse beyond chance" not in out.stdout


def test_the_dashboard_slices_by_facet_and_marks_new_templates():
    from assay import store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    engine = store.make_engine("sqlite://")
    now = datetime.utcnow()
    t, d = store.eval_results, store.event_documents
    with engine.begin() as conn:
        conn.execute(d.insert(), [
            {"tenant": "t", "document_id": "old-1", "received_at": now - timedelta(days=90),
             "facets": {"template": "acme-v3", "source": "digital"}},
            {"tenant": "t", "document_id": "a", "received_at": now - timedelta(days=1),
             "facets": {"template": "acme-v3", "source": "digital"}},
            {"tenant": "t", "document_id": "b", "received_at": now - timedelta(days=1),
             "facets": {"template": "globex-v1", "source": "scanned"}}])  # globex-v1: first seen yesterday
        rows = []
        for doc, ok in (("a", True), ("b", False)):
            raw = {"kind": "correct" if ok else "wrong", "weight": 1.0, "share": 1.0 if ok else 0.0,
                   **({"tp": 1} if ok else {"fp": 1, "fn": 1})}
            rows.append(dict(result_id=f"{doc}-total", tenant="t", run_id="r1", case_id=doc, document_id=doc,
                             field="total", evaluator="assay.documents@1", status="pass" if ok else "fail",
                             ts=now, raw_output=json.dumps(raw)))
            rows.append(dict(result_id=f"{doc}-doc", tenant="t", run_id="r1", case_id=doc, document_id=doc,
                             field="document", evaluator="assay.documents@1", status="pass" if ok else "fail",
                             ts=now, raw_output=json.dumps({"kind": "document", "accuracy": 1.0 if ok else 0.0})))
        conn.execute(t.insert(), rows)
    src, w = EventsSource(engine, "t"), Window(now - timedelta(days=7), now + timedelta(days=1))
    acc = REGISTRY["field_accuracy"].compute(src, w)
    by = {(r.dimension, r.slice_value): r.value for r in acc.results}
    assert by[("source", "scanned")] == 0 and by[("source", "digital")] == 1
    assert by[("template_new", "yes")] == 0 and by[("template_new", "no")] == 1  # globex-v1 is new
    assert not any(dim == "template" for dim, _ in by)  # the id itself isn't a slice
    zero = {(r.dimension, r.slice_value): r.value for r in REGISTRY["document_accuracy"].compute(src, w).results}
    assert zero[("template_new", "yes")] == 0 and ("language", "UNRECORDED") not in zero


def test_documents_are_sent_with_their_facets(tmp_path):
    from fastapi.testclient import TestClient
    from assay import store
    from assay.api import create_app
    from assay.config import Settings
    from assay.models import Window
    from assay.sources.events import EventsSource
    url = f"sqlite:///{tmp_path / 'f.db'}"
    client = TestClient(create_app(Settings(store_url=url)))
    now = datetime.utcnow()
    ok = client.post("/v1/events/documents", headers={"X-Tenant": "t"}, json=[
        {"document_id": "d1", "received_at": now.isoformat(),
         "facets": {"source": "scanned", "stamps": True, "template_seen": False}}])
    assert ok.json() == {"ingested": 1}
    bad = client.post("/v1/events/documents", headers={"X-Tenant": "t"}, json=[
        {"document_id": "d2", "received_at": now.isoformat(), "facets": {"Bad Key": "x"}}])
    assert bad.status_code == 422
    docs = list(EventsSource(store.make_engine(url), "t").documents(
        Window(now - timedelta(days=1), now + timedelta(days=1))))
    assert docs[0].facets == {"source": "scanned", "stamps": "yes", "template_seen": "unseen"}



# ---------- stability: the same document extracted several times ----------

def test_values_compare_on_what_they_mean():
    from assay_sdk.documents import canonical
    assert canonical(Money(), "1,250.00") == canonical(Money(), 1250) == "1,250.00"
    assert canonical(Date(), "4 March 2026") == canonical(Date(), "2026-03-04")
    li = LineItems({"d": Text(), "a": Money()})
    assert canonical(li, [{"d": "a", "a": "1"}, {"d": "b", "a": "2"}]) == \
        canonical(li, [{"d": "B", "a": "2.00"}, {"d": "a", "a": 1}])  # rows in any order
    assert canonical(Money(), None) == "" and canonical(Date(), "sometime") == "sometime"


def test_stability_counts_values_and_names_what_pass_fail_cant_see():
    from assay.documents import _stability
    reps = {("c1", "total"): [("10.00", True)] * 3,
            ("c1", "date"): [("2026-03-04", True), ("2026-04-03", False), ("2026-03-04", True)],  # flips
            ("c2", "total"): [("100.00", False), ("1,000.00", False), ("10.00", False)],  # wrong, moving
            ("c2", "date"): [("2026-01-01", False)] * 3,  # wrong, but the same way: stable
            ("c3", "total"): [("5.00", True)]}  # one attempt: nothing to compare
    s = _stability(reps)
    assert (s["attempts"], s["fields"], s["same"], s["documents"], s["documents_same"]) == (3, 4, 2, 2, 0)
    assert s["wrong_and_moving"] == [["c2", "total", ["1,000.00", "10.00", "100.00"]]] and s["flipping"] == 1
    assert _stability({("c", "f"): [("1", True)]}) is None


REPEATED = '''
import os
from assay_sdk.documents import score_document, Text, Money
S = {"number": Text(), "total": Money()}

def case(i, run):
    truth = {"number": str(i), "total": "10"}
    got = dict(truth)
    if i == 3:  # wrong every time, never the same way: a stable fail to pass/fail
        got["total"] = str(100 + int(os.environ.get("ASSAY_TEST_ATTEMPT", "0")))
    score_document(run, truth, got, S)
''' + "".join(f"\ndef test_{i}(assay_case): case({i}, assay_case)\n" for i in range(5))


def test_the_report_says_how_repeatable_extraction_is(project):
    from assay.__main__ import main
    (project / "tests").mkdir()
    (project / "tests" / "test_rep.py").write_text(REPEATED)
    (project / "assay.toml").write_text(
        '[test]\ncommand = "' + sys.executable + ' -m pytest -q -p no:cacheprovider -p assay_sdk.pytest_plugin tests"\n'
        "repeat = 4\n\n[documents.gates]\nstability = { min_accuracy = 0.99 }\n")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["test"])
    out = buf.getvalue()
    assert code == 1, out
    assert "Stability    4 attempts · fields with the same value every time 90.0% (9/10)" in out
    assert "documents fully repeatable 4/5" in out
    assert "1 wrong every time and never the same way, which pass/fail can't see as flaky: e.g. total in " \
           "tests/test_rep.py::test_3: '100.00', '101.00', '102.00', '103.00'" in out
    assert "failed: stability: accuracy 90.0%, at least 99.0% required" in out
    from assay import coverage, store
    from assay.measures import REGISTRY
    from assay.models import Window
    from assay.sources.events import EventsSource
    src, now = EventsSource(store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}"), "local"), \
        datetime.utcnow()
    w = Window(now - timedelta(days=1), now + timedelta(days=1))
    m = REGISTRY["value_stability"].compute(src, w)
    assert (m.overall.numerator, m.overall.denominator) == (9, 10)
    assert {r.slice_value: r.value for r in m.results if r.dimension == "field"} == {"number": 1.0, "total": 0.8}
    assert {x["id"]: x["status"] for x in coverage.compute(src, w)["measures"]}["value_stability"] == "live"


# ---------- drops tested with intervals, not a fixed number of points ----------

def _field(per_case, table=True):
    n = len(per_case)
    right = sum(per_case.values())
    return {"precision": None, "recall": None, "f1": right / n, "errors": 0, "table": table, "per_case": per_case}


def test_a_drop_fails_only_when_surely_past_the_tolerance():
    from assay.documents import check_gates, paired_drop
    before = {"fields": {"line_items": _field({f"c{i}": 1.0 for i in range(6)})}}
    collapse = {"fields": {"line_items": _field({f"c{i}": 0.2 for i in range(6)})}}
    g = check_gates(collapse, before, None)[0]
    assert g["passed"] is False and g["drop"] == pytest.approx(0.8) and "surely more than the 2 allowed" in g["why"]
    # two documents much worse, four the same: could be worse, too few to tell
    mixed = {"fields": {"line_items": _field({"c0": 0.5, "c1": 0.6, **{f"c{i}": 1.0 for i in range(2, 6)}})}}
    g = check_gates(mixed, before, None)[0]
    assert g["passed"] is True and g["unsure"] and "add documents to tell" in g["why"]
    lo, hi = g["interval"]
    assert lo < 0.02 < hi
    steady = {"fields": {"line_items": _field({f"c{i}": 1.0 for i in range(6)})}}
    g = check_gates(steady, before, None)[0]
    assert g["passed"] and not g["unsure"] and "within the 2 allowed" in g["why"]
    assert paired_drop({"c0": 1.0}, {"c0": 0.0}) is None  # one document: nothing to test
    assert check_gates(mixed, before, {"line_items": {}}) == []  # turned off


def test_slices_worse_beyond_chance_are_corrected_for_how_many_there_are():
    from assay.documents import slice_changes
    before = {"a": {"zero_errors": 1.0, "cases": {f"c{i}": True for i in range(8)}},
              "b": {"zero_errors": 1.0, "cases": {f"d{i}": True for i in range(8)}}}
    now = {"a": {"zero_errors": 0.0, "cases": {f"c{i}": False for i in range(8)}},  # all 8 lost
           "b": {"zero_errors": 0.75, "cases": {f"d{i}": i >= 2 for i in range(8)}}}  # 2 of 8 lost
    ch = slice_changes(now, before)
    assert ch["a"]["p"] == pytest.approx(1 / 256) and ch["a"]["q"] == pytest.approx(2 / 256) and ch["a"]["worse"]
    assert ch["b"]["p"] == pytest.approx(0.25) and not ch["b"]["worse"]
    gained = slice_changes({"a": {"zero_errors": 1.0, "cases": {"c0": True}}},
                           {"a": {"zero_errors": 0.0, "cases": {"c0": False}}})
    assert gained["a"]["p"] == 1.0 and not gained["a"]["worse"]
