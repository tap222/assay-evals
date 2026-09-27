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
