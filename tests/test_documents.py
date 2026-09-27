"""Document extraction scored per field (assay_sdk/documents.py, assay/documents.py)."""
import json
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
    assert rows.counts == {"tp": 1, "fp": 2, "fn": 1, "rows": 2, "rows_extracted": 3} and round(rows.share, 2) == 0.4
    assert "1 row(s) invented" in rows.note and "Widget: '100', not '1000.00'" in rows.note
    assert f["line_items.amount"].share == 0.5 and f["line_items.amount"].part_of == "line_items"
    missing = score_document(None, TRUTH, {**TRUTH, "total": ""}, SCHEMA).fields["total"]
    assert missing.kind == "missing" and missing.counts == {"fn": 1}
    # weighted: invoice_number (3) right, date (1), total (3), po (1) wrong, line items (1) at 0.4
    assert round(s.accuracy, 3) == round((3 + 0.4) / 9, 3) and not s.all_correct
    assert score_document(None, TRUTH, TRUTH, SCHEMA).all_correct


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
                                                             "fp": 1, "fn": 1}
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
    assert not merged.correct and merged.notes == ["pages 3-6 came out as one document, which is 2"]
    assert merged.boundaries == {"tp": 1, "fp": 0, "fn": 1}
    cut = score_split(None, [(1, 3)], [(1, 1), (2, 3)])
    assert cut.notes == ["pages 1-3 is one document, cut into 2"]
    shifted = score_split(None, [1, 3, 5], [1, 4, 5], page_count=6)  # first pages, with the page count
    assert shifted.notes == ["the document starting on page 3 was split at page 4"] and shifted.right == [(5, 6)]
    assert score_split(None, [{"pages": [1, 2]}, {"start": 3, "end": 4}], [(1, 2), (3, 4)]).correct
    with pytest.raises(ValueError, match="page_count"):
        score_split(None, [1, 3], [1, 3])


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
           "(was 0%) · 1 over the limit" in out.stdout  # 1 of the page's 12 digits
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
                                                           "location_accuracy", "table_cell_f1")}
    assert all(m.status == "measured" for m in got.values())
    assert got["ocr_reading_order"].overall.value == 0.875  # 100% then 75%, the same page twice
    assert got["location_accuracy"].overall.value == 0.5 and got["table_cell_f1"].overall.n == 2
    assert {r.slice_value for r in got["location_accuracy"].results if r.dimension == "field"} == {"total"}
    live = {m["id"]: m for m in coverage.compute(src, w)["measures"]}
    assert live["ocr_cer"]["status"] == "live" and live["escape_rate"]["status"] == "blocked"
    assert live["escape_rate"]["missing"] == ["spot checks of published output (spot_check)"]
