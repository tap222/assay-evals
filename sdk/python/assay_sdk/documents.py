"""Scoring document extraction: each field against its correct value, line items, rules.

    from assay_sdk.documents import score_document, Text, Money, Date, LineItems, total_of, before

    SCHEMA = {
        "invoice_number": Text(weight=3),
        "invoice_date": Date(day_first=True),
        "due_date": Date(day_first=True),
        "total": Money(tolerance=0.01, weight=3),
        "vendor.name": Text(),
        "line_items": LineItems({"description": Text(), "quantity": Number(), "amount": Money()},
                                key="description"),
    }
    RULES = [total_of("line_items.amount", equals="total"), before("invoice_date", "due_date")]

    def test_invoice_17(assay_case):
        extracted = my_pipeline("invoices/17.pdf", run=assay_case)
        score_document(assay_case, expected=LABELS["17"], extracted=extracted, schema=SCHEMA, rules=RULES)

Every field is a check of its own, and it says which way it went wrong:

  correct    the value matches (or both are empty: nothing there, nothing extracted)
  wrong      a value was extracted, and it isn't the right one
  missing    the document has a value, and nothing was extracted
  invented   nothing is there, and a value was extracted: usually the costliest error

"Matches" is per type: Text ignores case and spacing; Number and Money compare numbers with a
tolerance ("1.234,56 €" is 1234.56); Date reads the usual formats, with day_first for 03/04/2026.
A correct value that can't be read (a date that isn't one) is the label's problem: the check
couldn't be judged, and never counts against the extractor.

Line items are matched row to row whatever their order (by `key`, else by the most cells in
common), and scored by row: a row is right when all its cells are. `document` is one more check,
all fields correct, with the weighted share right as its score. Rules check the extracted values
against each other and need no correct values, so they run on production documents too
(check_rules). Each check carries its counts, from which `assay test` reports precision and
recall per field.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = ["Text", "Number", "Money", "Date", "LineItems", "score_document", "check_rules", "total_of",
           "before", "required", "rule", "DocumentScore", "FieldScore", "EVALUATOR", "classify_document",
           "score_split", "SplitScore"]

EVALUATOR = "assay.documents@1"
CORRECT, WRONG, MISSING, INVENTED = "correct", "wrong", "missing", "invented"


class Unreadable(ValueError):
    """A value that isn't what its type says (a date that isn't one)."""


def empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, tuple, dict)) and not v)


# ---------- comparators ----------

class _Field:
    weight: float = 1.0

    def read(self, v: Any) -> Any:
        return v

    def same(self, a: Any, b: Any) -> bool:
        return a == b

    def show(self, v: Any) -> str:
        return str(v)

    def why(self, expected: Any, actual: Any) -> Optional[str]:
        """What the difference looks like, when it's a common one."""
        return None


class Text(_Field):
    """Text, ignoring case and spacing (and punctuation, with ignore_punctuation). exact=True: as is."""

    def __init__(self, exact: bool = False, ignore_punctuation: bool = False, weight: float = 1.0):
        self.exact, self.ignore_punctuation, self.weight = exact, ignore_punctuation, weight

    def read(self, v):
        s = str(v)
        if self.exact:
            return s
        s = re.sub(r"\s+", " ", s).strip().lower()
        return re.sub(r"[^\w\s]", "", s).strip() if self.ignore_punctuation else s


_CURRENCY = re.compile(r"[$€£¥₹]|\b(usd|eur|gbp|jpy|inr|chf|cad|aud)\b", re.I)


def _number(v: Any, decimal_comma: Optional[bool]) -> Tuple[float, Optional[str]]:
    """(the number, its currency code or symbol if written). "1.234,56 €", "(12.00)", "USD 1,200"."""
    if isinstance(v, bool):
        raise Unreadable(f"{v!r} isn't a number")
    if isinstance(v, (int, float)):
        return float(v), None
    s = str(v).strip()
    cur = _CURRENCY.search(s)
    s = _CURRENCY.sub("", s).strip()
    neg = s.startswith("(") and s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = s.strip("()-+ ").replace(" ", "").replace(" ", "").replace("'", "")
    if not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        raise Unreadable(f"{v!r} isn't a number")
    if decimal_comma is None:  # the last separator is the decimal one when 1 or 2 digits follow it
        last = max(s.rfind(","), s.rfind("."))
        decimal_comma = last >= 0 and s[last] == "," and 0 < len(s) - last - 1 <= 2
        if s.count(".") > 1 and "," not in s:
            decimal_comma = True  # 1.234.567: the dots are thousands
    s = s.replace(".", "").replace(",", ".") if decimal_comma else s.replace(",", "")
    if s.count(".") > 1:
        raise Unreadable(f"{v!r} isn't a number")
    n = float(s)
    return (-n if neg else n), (cur.group(0).upper() if cur else None)


class Number(_Field):
    """A number within `tolerance` (absolute) or `relative` (a share of the correct value)."""

    def __init__(self, tolerance: float = 0.0, relative: float = 0.0, decimal_comma: Optional[bool] = None,
                 weight: float = 1.0):
        self.tolerance, self.relative, self.decimal_comma, self.weight = tolerance, relative, decimal_comma, weight

    def read(self, v):
        return _number(v, self.decimal_comma)[0]

    def same(self, a, b):
        return abs(a - b) <= max(self.tolerance, self.relative * abs(a)) + 1e-9

    def show(self, v):
        return f"{v:,.6g}" if abs(v) < 1e15 else str(v)

    def why(self, expected, actual):
        if expected and actual:
            for k in (10, 100, 1000):
                if abs(actual - expected * k) < 1e-6 * max(1, abs(expected * k)) or \
                        abs(actual * k - expected) < 1e-6 * max(1, abs(expected)):
                    return f"off by a factor of {k} (a decimal separator read wrong?)"
            if abs(actual + expected) < 1e-9:
                return "the sign is wrong"
        return None


class Money(Number):
    """An amount: currency symbols and codes, thousands separators and "1.234,56" are read, and
    compared within `tolerance` (default half a cent). currency=True: the currency must match too,
    when both give one."""

    def __init__(self, tolerance: float = 0.005, currency: bool = False, decimal_comma: Optional[bool] = None,
                 weight: float = 1.0):
        super().__init__(tolerance=tolerance, decimal_comma=decimal_comma, weight=weight)
        self.currency = currency

    def read(self, v):
        n, cur = _number(v, self.decimal_comma)
        return (n, _SYMBOL.get(cur, cur)) if self.currency else n

    def same(self, a, b):
        if self.currency:
            (x, cx), (y, cy) = a, b
            return (cx is None or cy is None or cx == cy) and super().same(x, y)
        return super().same(a, b)

    def show(self, v):
        if self.currency:
            n, cur = v
            return f"{n:,.2f}" + (f" {cur}" if cur else "")
        return f"{v:,.2f}"

    def why(self, expected, actual):
        if self.currency:
            if expected[1] and actual[1] and expected[1] != actual[1]:
                return f"the currency is {actual[1]}, not {expected[1]}"
            return super().why(expected[0], actual[0])
        return super().why(expected, actual)


_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


class Date(_Field):
    """A date in any of the usual formats: 2026-03-04, 04.03.2026, 4 March 2026, March 4th, 2026,
    03/04/2026 (day_first says which; when one part is over 12 it decides itself)."""

    def __init__(self, day_first: bool = False, weight: float = 1.0):
        self.day_first, self.weight = day_first, weight

    def read(self, v):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(v).strip().lower()).replace(",", " ")
        s = re.sub(r"\s+", " ", s)
        try:
            m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[t ].*)?", s)
            if m:
                return date(int(m[1]), int(m[2]), int(m[3]))
            m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s)  # dots: day first, everywhere they're used
            if m:
                return date(_year(m[3]), int(m[2]), int(m[1]))
            m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
            if m:
                a, b, y = int(m[1]), int(m[2]), _year(m[3])
                day_first = True if a > 12 else False if b > 12 else self.day_first
                return date(y, b, a) if day_first else date(y, a, b)
            m = re.fullmatch(r"(\d{1,2}) ([a-z]{3})[a-z]*\.? (\d{4})", s)
            if m and m[2] in _MONTHS:
                return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
            m = re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2}) (\d{4})", s)
            if m and m[1] in _MONTHS:
                return date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        except ValueError:
            pass
        raise Unreadable(f"{v!r} isn't a date")

    def show(self, v):
        return v.isoformat()

    def why(self, expected, actual):
        if expected.year == actual.year and expected.day == actual.month and expected.month == actual.day:
            return "day and month swapped"
        if expected.replace(year=actual.year) == actual:
            return "the year is wrong"
        return None


def _year(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


class LineItems(_Field):
    """Rows of cells, matched row to row whatever their order: by `key` (a cell that names the
    row), else by the most cells in common. A row is right when every cell is."""

    def __init__(self, fields: Dict[str, _Field], key: Optional[str] = None, weight: float = 1.0):
        if key is not None and key not in fields:
            raise ValueError(f"LineItems: key {key!r} isn't one of its fields")
        self.fields, self.key, self.weight = fields, key, weight


# ---------- results ----------

@dataclass
class FieldScore:
    field: str
    kind: str  # correct | wrong | missing | invented | unreadable (the correct value couldn't be read)
    expected: Any = None
    actual: Any = None
    note: Optional[str] = None
    weight: float = 1.0
    counts: Dict[str, float] = dc_field(default_factory=dict)  # tp, fp, fn (and rows and cells, for line items)
    share: float = 1.0  # how much of it is right: 0 or 1, or the row F1 for line items
    part_of: Optional[str] = None  # a line-item column: the table it's part of, already counted there

    @property
    def passed(self) -> Optional[bool]:
        return None if self.kind == "unreadable" else self.kind == CORRECT


@dataclass
class DocumentScore:
    fields: Dict[str, FieldScore]
    rules: Dict[str, Tuple[Optional[bool], str]]

    @property
    def all_correct(self) -> bool:
        return all(f.passed is not False for f in self.fields.values())

    @property
    def accuracy(self) -> Optional[float]:
        """The weighted share of fields right (line items by their row F1)."""
        judged = [f for f in self.fields.values() if f.passed is not None and not f.part_of]
        w = sum(f.weight for f in judged)
        return sum(f.weight * f.share for f in judged) / w if w else None

    def wrong(self) -> List[FieldScore]:
        return [f for f in self.fields.values() if f.passed is False]


def _get(doc: Any, path: str) -> Any:
    """doc["vendor"]["name"] for "vendor.name"; a key with a dot in it is tried first."""
    if isinstance(doc, dict) and path in doc:
        return doc[path]
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return getattr(cur, part, None) if cur is not None and not isinstance(cur, (str, int, float)) else None
    return cur


def _score_value(name: str, spec: _Field, exp: Any, act: Any) -> FieldScore:
    w = spec.weight
    if empty(exp) and empty(act):
        return FieldScore(name, CORRECT, exp, act, weight=w)
    if empty(exp):
        return FieldScore(name, INVENTED, exp, act, "a value the document doesn't have", w, {"fp": 1}, 0.0)
    try:
        e = spec.read(exp)
    except Unreadable as exc:
        return FieldScore(name, "unreadable", exp, act, f"the correct value can't be read: {exc}", w)
    if empty(act):
        return FieldScore(name, MISSING, exp, act, "nothing extracted", w, {"fn": 1}, 0.0)
    try:
        a = spec.read(act)
    except Unreadable as exc:
        return FieldScore(name, WRONG, exp, act, str(exc), w, {"fp": 1, "fn": 1}, 0.0)
    if spec.same(e, a):
        return FieldScore(name, CORRECT, exp, act, weight=w, counts={"tp": 1})
    why = spec.why(e, a)
    return FieldScore(name, WRONG, exp, act, f"{spec.show(a)}, not {spec.show(e)}" + (f": {why}" if why else ""),
                      w, {"fp": 1, "fn": 1}, 0.0)


def _cell_ok(spec: _Field, e: Any, a: Any) -> bool:
    if empty(e) and empty(a):
        return True
    if empty(e) or empty(a):
        return False
    try:
        return spec.same(spec.read(e), spec.read(a))
    except Unreadable:
        return False


def _score_rows(name: str, spec: LineItems, exp: Any, act: Any) -> Tuple[FieldScore, Dict[str, FieldScore]]:
    exp, act = list(exp or []), list(act or [])
    cols = list(spec.fields)
    same = {(i, j): sum(_cell_ok(spec.fields[c], _get(e, c), _get(a, c)) for c in cols)
            for i, e in enumerate(exp) for j, a in enumerate(act)}
    if spec.key:
        k = spec.fields[spec.key]
        pairs = [p for p in same if _cell_ok(k, _get(exp[p[0]], spec.key), _get(act[p[1]], spec.key))]
    else:
        pairs = [p for p, n in same.items() if n > 0]
    matched, used_e, used_a = [], set(), set()
    for i, j in sorted(pairs, key=lambda p: (-same[p], p)):  # most cells in common first
        if i not in used_e and j not in used_a:
            matched.append((i, j))
            used_e.add(i)
            used_a.add(j)
    right = sum(1 for i, j in matched if same[(i, j)] == len(cols))
    tp, fp, fn = right, len(act) - right, len(exp) - right
    f1 = 2 * tp / (2 * tp + fp + fn) if (tp + fp + fn) else 1.0
    notes = []
    if len(exp) - len(used_e):
        notes.append(f"{len(exp) - len(used_e)} row(s) missing")
    if len(act) - len(used_a):
        notes.append(f"{len(act) - len(used_a)} row(s) invented")
    per_col: Dict[str, FieldScore] = {}
    for c in cols:
        ok = sum(1 for i, j in matched if _cell_ok(spec.fields[c], _get(exp[i], c), _get(act[j], c)))
        bad = [(i, j) for i, j in matched if not _cell_ok(spec.fields[c], _get(exp[i], c), _get(act[j], c))]
        if bad:
            i, j = bad[0]
            label = _get(exp[i], spec.key) if spec.key else f"row {i + 1}"
            notes.append(f"{c} wrong in {len(bad)} row(s), e.g. {label}: {_get(act[j], c)!r}, not {_get(exp[i], c)!r}")
        n = len(exp)
        per_col[f"{name}.{c}"] = FieldScore(
            f"{name}.{c}", CORRECT if ok == n and not (len(act) - len(used_a)) else WRONG,
            n, ok, f"{ok} of {n} right" if ok < n else f"{len(act) - len(used_a)} row(s) invented"
            if len(act) - len(used_a) else None, spec.weight,
            {"tp": ok, "fp": len(act) - ok, "fn": n - ok}, ok / n if n else 1.0, part_of=name)
    whole = FieldScore(name, CORRECT if tp == len(exp) == len(act) else (MISSING if not act else WRONG),
                       len(exp), len(act), "; ".join(notes) or None, spec.weight,
                       {"tp": tp, "fp": fp, "fn": fn, "rows": len(exp), "rows_extracted": len(act)}, f1)
    return whole, per_col


# ---------- rules: the extracted values against each other ----------

@dataclass
class Rule:
    name: str
    fn: Callable[[Any], Tuple[Optional[bool], str]]  # (True/False, why); None: can't be checked here


def rule(name: str, fn: Callable[[Any], Any]) -> Rule:
    """A rule of your own: fn(extracted) returns True/False, or (True/False, why); None skips it."""
    def run(doc):
        out = fn(doc)
        return out if isinstance(out, tuple) else (out, "")
    return Rule(name, run)


def _sum(doc: Any, path: str, spec: Number) -> Optional[float]:
    """A field's value, or with rows.cell the sum over the rows; None when any is missing."""
    if "." in path and isinstance(_get(doc, path.split(".", 1)[0]), (list, tuple)):
        rows_path, cell = path.split(".", 1)
        vals = [_get(r, cell) for r in _get(doc, rows_path)]
    else:
        vals = [_get(doc, path)]
    if not vals or any(empty(v) for v in vals):
        return None
    try:
        return sum(_number(v, spec.decimal_comma)[0] for v in vals)
    except Unreadable:
        return None


def total_of(parts: Union[str, Sequence[str]], equals: str, tolerance: float = 0.01,
             decimal_comma: Optional[bool] = None) -> Rule:
    """The parts add up to the total: total_of("line_items.amount", equals="total"), or
    total_of(["subtotal", "tax"], equals="total"). Skipped when a value is missing."""
    parts = [parts] if isinstance(parts, str) else list(parts)
    spec = Number(decimal_comma=decimal_comma)

    def check(doc):
        got = [_sum(doc, p, spec) for p in parts]
        want = _sum(doc, equals, spec)
        if want is None or any(g is None for g in got):
            return None, ""
        s = sum(got)
        return abs(s - want) <= tolerance + 1e-9, f"{' + '.join(parts)} = {s:,.2f}, {equals} = {want:,.2f}"
    return Rule(f"{equals} = {' + '.join(parts)}", check)


def before(first: str, then: str, day_first: bool = False, same_day: bool = True) -> Rule:
    """One date is on or before another: before("invoice_date", "due_date")."""
    spec = Date(day_first=day_first)

    def check(doc):
        a, b = _get(doc, first), _get(doc, then)
        if empty(a) or empty(b):
            return None, ""
        try:
            x, y = spec.read(a), spec.read(b)
        except Unreadable:
            return None, ""
        return (x <= y if same_day else x < y), f"{first} {x.isoformat()}, {then} {y.isoformat()}"
    return Rule(f"{first} before {then}", check)


def required(*fields: str) -> Rule:
    """These fields have a value."""
    def check(doc):
        gone = [f for f in fields if empty(_get(doc, f))]
        return not gone, f"no {', '.join(gone)}" if gone else ""
    return Rule(f"has {', '.join(fields)}", check)


def _rules(rules: Sequence[Rule], extracted: Any) -> Dict[str, Tuple[Optional[bool], str]]:
    out = {}
    for r in rules:
        try:
            out[r.name] = r.fn(extracted)
        except Exception as exc:  # a rule that breaks is a bug in the rule, not a failed document
            out[r.name] = (None, f"the rule failed: {type(exc).__name__}: {exc}")
    return out


# ---------- recording ----------

def _record(run, name: str, f: FieldScore, confidence: Optional[float] = None) -> None:
    raw = json.dumps({"kind": f.kind, "weight": f.weight, "share": round(f.share, 6), **f.counts,
                      **({"part_of": f.part_of} if f.part_of else {}),
                      **({"confidence": float(confidence)} if confidence is not None else {})})
    if f.kind == "unreadable":
        run.check(name, "error", expected=f.expected, actual=f.actual, evaluator=EVALUATOR, reason=f.note,
                  error_kind="invalid", raw_output=raw)
        return
    # No score: a comparison, not a judge (a scored check is listed with the judges, for calibration).
    run.check(name, "pass" if f.passed else "fail", expected=f.expected, actual=f.actual, evaluator=EVALUATOR,
              reason=None if f.passed else f"{f.kind}: {f.note}" if f.note else f.kind,
              category=None if f.passed else f.kind, raw_output=raw)


def _record_rules(run, results: Dict[str, Tuple[Optional[bool], str]]) -> None:
    for name, (ok, why) in results.items():
        if ok is None and not why.startswith("the rule failed"):
            continue  # not checkable on this document (a value it needs is missing)
        run.check(f"rule: {name}", "error" if ok is None else "pass" if ok else "fail", evaluator=EVALUATOR,
                  reason=None if ok else why, error_kind="error" if ok is None else None,
                  raw_output=json.dumps({"kind": "rule"}))


def score_document(run, expected: Any, extracted: Any, schema: Optional[Dict[str, _Field]] = None,
                   rules: Sequence[Rule] = (), confidence: Optional[Dict[str, float]] = None) -> DocumentScore:
    """Score one document's extraction against its correct values, and record each field, the line
    items, `document` (all fields correct) and each rule as checks on `run` (None: only score).

    confidence: the extractor's confidence per field (0-1), where it gives one. Recorded with each
    field, so the report can say whether a confident value is a right one, which threshold would
    auto-approve safely, and how many wrong values a threshold lets through."""
    if schema is None:
        schema = {k: Text() for k in (expected or {})}
    fields: Dict[str, FieldScore] = {}
    for name, spec in schema.items():
        e, a = _get(expected, name), _get(extracted, name)
        if isinstance(spec, LineItems):
            whole, cols = _score_rows(name, spec, e, a)
            fields[name] = whole
            fields.update(cols)
        else:
            fields[name] = _score_value(name, spec, e, a)
    doc = DocumentScore(fields, _rules(rules, extracted))
    if run is not None:
        for name, f in fields.items():
            _record(run, name, f, (confidence or {}).get(name) if not f.part_of else None)
        bad = doc.wrong()
        acc = doc.accuracy
        run.check("document", "pass" if doc.all_correct else "fail", evaluator=EVALUATOR,
                  reason=None if doc.all_correct else "wrong: " + ", ".join(f"{f.field} ({f.kind})" for f in bad[:6]),
                  raw_output=json.dumps({"kind": "document", "accuracy": acc, "fields": len(fields),
                                         "wrong": len(bad)}))
        _record_rules(run, doc.rules)
    return doc


def check_rules(run, extracted: Any, rules: Sequence[Rule]) -> Dict[str, Tuple[Optional[bool], str]]:
    """Only the rules, on extracted values without correct ones (a production document)."""
    out = _rules(rules, extracted)
    if run is not None:
        _record_rules(run, out)
    return out


# ---------- document type ----------

def classify_document(run, expected: Any, predicted: Any, confidence: Optional[float] = None) -> bool:
    """Whether the document's type was classified right ("invoice" vs "Invoice " is the same type),
    recorded as the check `document_type`. The report counts every run's pairs into a confusion
    matrix, with precision and recall per type."""
    e, p = Text().read(expected) if not empty(expected) else None, Text().read(predicted) if not empty(predicted) else None
    if e is None:
        if run is not None:
            run.check("document_type", "error", expected=expected, actual=predicted, evaluator=EVALUATOR,
                      reason="no correct type to compare with", error_kind="invalid")
        return False
    ok = e == p
    if run is not None:
        run.check("document_type", "pass" if ok else "fail", expected=expected, actual=predicted, evaluator=EVALUATOR,
                  reason=None if ok else f"classified as {predicted!r}, not {expected!r}" if p else "no type given",
                  category=None if ok else "misclassified",
                  raw_output=json.dumps({"kind": "classification", "expected": e, "predicted": p,
                                         **({"confidence": float(confidence)} if confidence is not None else {})}))
    return ok


# ---------- splitting a file into its documents ----------

@dataclass
class SplitScore:
    expected: List[Tuple[int, int]]
    predicted: List[Tuple[int, int]]
    right: List[Tuple[int, int]]  # documents split exactly: the same first and last page
    notes: List[str]
    boundaries: Dict[str, int]  # tp, fp, fn over the pages a new document starts on (after the first)

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


def _segments(v: Any, page_count: Optional[int]) -> List[Tuple[int, int]]:
    """Documents as (first page, last page), 1-based: from ranges, page lists, or the first pages."""
    items = list(v or [])
    if items and all(isinstance(x, int) for x in items):  # first pages: each runs to the next one
        if page_count is None:
            raise ValueError("score_split: first pages alone need page_count")
        starts = sorted(set(items))
        return [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else page_count) for i, s in enumerate(starts)]
    out = []
    for x in items:
        if isinstance(x, dict):
            x = x.get("pages") or (x.get("start"), x.get("end"))
        x = list(x)
        out.append((int(min(x)), int(max(x))) if len(x) != 2 else (int(x[0]), int(x[1])))
    return sorted(out)


def _pages(s: Tuple[int, int]) -> str:
    return f"page {s[0]}" if s[0] == s[1] else f"pages {s[0]}-{s[1]}"


def score_split(run, expected: Any, predicted: Any, page_count: Optional[int] = None) -> SplitScore:
    """Score how a file was split into documents, recorded as the check `split`: passes when every
    document starts and ends on the right page. Documents are given as page ranges ((1, 2), (3, 3)),
    page lists, or their first pages (with page_count). Says what went wrong: documents merged,
    one cut in two, a boundary a page off."""
    exp, pred = _segments(expected, page_count), _segments(predicted, page_count)
    right = sorted(set(exp) & set(pred))
    starts = lambda segs: {s[0] for s in segs} - {min((x[0] for x in segs), default=1)}
    es, ps = starts(exp), starts(pred)
    missed, extra = sorted(es - ps), sorted(ps - es)
    notes, explained = [], set()
    for m in missed:  # a boundary a page or two off: one note, not a merge and a cut
        near = min((x for x in extra if abs(x - m) <= 2 and x not in explained), key=lambda x: abs(x - m),
                   default=None)
        if near is not None:
            notes.append(f"the document starting on page {m} was split at page {near}")
            explained |= {m, near}
    for e in exp:
        if e in right or e[0] in explained or e[1] + 1 in explained:
            continue
        over = [p for p in pred if p[0] <= e[1] and p[1] >= e[0]]
        if len(over) == 1 and over[0][0] <= e[0] and over[0][1] >= e[1] and over[0] != e:
            others = [x for x in exp if x != e and over[0][0] <= x[0] and x[1] <= over[0][1]]
            if others:
                note = f"{_pages(over[0])} came out as one document, which is {len(others) + 1}"
                if note not in notes:
                    notes.append(note)
                continue
        if len(over) > 1 and all(e[0] <= p[0] and p[1] <= e[1] for p in over):
            notes.append(f"{_pages(e)} is one document, cut into {len(over)}")
        else:
            notes.append(f"{_pages(e)}: " + (", ".join(_pages(p) for p in over) if over else "no document") + " instead")
    bounds = {"tp": len(es & ps), "fp": len(ps - es), "fn": len(es - ps)}
    score = SplitScore(exp, pred, right, notes, bounds)
    if run is not None:
        run.check("split", "pass" if score.correct else "fail", expected=json.dumps(exp), actual=json.dumps(pred),
                  evaluator=EVALUATOR, reason=None if score.correct else "; ".join(notes[:4]),
                  category=None if score.correct else "split_wrong",
                  raw_output=json.dumps({"kind": "split", "documents": len(exp), "tp": len(right),
                                         "fp": len(pred) - len(right), "fn": len(exp) - len(right),
                                         "boundaries": bounds}))
    return score
