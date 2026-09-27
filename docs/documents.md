[Assay](../README.md) › [Documentation](README.md)

# Document extraction: each field against its correct value

An extraction pipeline gets a document's fields right or wrong in different ways, and they don't
cost the same. `assay_sdk.documents` scores each field against its correct value, says which way
it went wrong, matches line items whatever their order, and checks the extracted values against
each other. Every result is an Assay check, so it's compared with its baseline, told apart from
noise with repeats, and gates a pull request like any other.

```python
from assay_sdk.documents import score_document, Text, Number, Money, Date, LineItems, total_of, before

SCHEMA = {
    "invoice_number": Text(weight=3),              # weight: what an error costs, relative to the others
    "invoice_date": Date(day_first=True),
    "due_date": Date(day_first=True),
    "total": Money(tolerance=0.01, weight=3),
    "vendor.name": Text(),                          # a dotted path into nested output
    "line_items": LineItems({"description": Text(), "quantity": Number(), "amount": Money()},
                            key="description"),
}
RULES = [total_of("line_items.amount", equals="total"), before("invoice_date", "due_date", day_first=True)]

def test_invoice_17(assay_case):
    extracted = my_pipeline("invoices/17.pdf", run=assay_case)
    score_document(assay_case, expected=LABELS["17"], extracted=extracted, schema=SCHEMA, rules=RULES)
```

## Which way a field went wrong

| Kind | When | Counts as |
|---|---|---|
| correct | the value matches, or the document has none and nothing was extracted | a true positive (or nothing, when both are empty) |
| wrong | a value was extracted, and it isn't the right one | a false positive and a false negative |
| missing | the document has a value, and nothing was extracted | a false negative |
| invented | the document has no value, and one was extracted | a false positive: usually the costliest error, since nothing looks wrong |

A wrong value says what the difference looks like when it's a common one: "2026-04-03, not
2026-03-04: day and month swapped", "123,456.00, not 1,234.56: off by a factor of 100 (a decimal
separator read wrong?)", "the currency is USD, not EUR", "the sign is wrong", "the year is wrong".

A correct value that can't be read (a date that isn't one) is the label's problem, not the
extractor's: that check couldn't be judged, and isn't counted.

## Values match by type, not by spelling

- **Text:** case and spacing don't count (`exact=True` makes them count;
  `ignore_punctuation=True` drops punctuation too).
- **Number:** within `tolerance` (absolute) or `relative` (a share of the correct value).
  "1.234,56", "1,234.56", "1 234,56", "(12.00)" and "1.234.567" are read as the numbers they are.
  With only dots and three digits after one ("1.234"), it's read as a decimal: say
  `decimal_comma=True` for documents written the European way.
- **Money:** a Number within half a cent by default, with currency symbols and codes read.
  `currency=True` makes the currency count too, when both values give one.
- **Date:** 2026-03-04, 04.03.2026, 4 March 2026, 4th March 2026, March 4, 2026, and 03/04/2026:
  `day_first` says which is the day, unless one part is over 12 and decides it.

## Line items

Rows are matched row to row whatever their order: by `key` when given (a cell that names the row,
like its description), else by the most cells in common. A row is right when all its cells are.
`line_items` is scored by its rows: precision and recall over rows, and its share right is the
row F1. Missing rows, invented rows and the wrong cells are named ("1 row(s) invented; amount
wrong in 1 row(s), e.g. Widget: '100.00', not '1000.00'"). Each column is a check of its own
too (`line_items.amount`), which says which column breaks; it isn't counted again in the
document's accuracy.

## The whole document

`document` is one more check: **All fields correct**, the share of documents that need no
correction. The weighted share of fields right (each field by its `weight`, line items by their
row F1) is the document's accuracy.

## Rules: the extracted values against each other

Rules need no correct values, so they run on production documents too:

```python
from assay_sdk.documents import check_rules, total_of, before, required, rule

check_rules(run, extracted, [
    total_of("line_items.amount", equals="total"),            # the rows add up to the total
    total_of(["subtotal", "tax"], equals="total"),
    before("invoice_date", "due_date", day_first=True),       # not due before it was issued
    required("invoice_number", "vendor.name"),
    rule("positive total", lambda d: float(d["total"]) > 0),  # your own: True/False, or (bool, why)
])
```

A rule a document can't be checked on (a value it needs is missing) is skipped, not failed.
`required` is the rule for a value that must be there. A rule that raises is reported as a rule
that couldn't run, not as a failed document.

## Document types

```python
from assay_sdk.documents import classify_document

classify_document(assay_case, expected="invoice", predicted=pipeline.doc_type, confidence=pipeline.type_confidence)
```

A check, `document_type`, per document ("Invoice" and "invoice " are the same type). The report
adds up the run's pairs into a confusion matrix: which type was taken for which, and precision
and recall per type. A wrong type usually makes the fields read from it wrong too: for errors
reported from production (`/v1/errors`), Assay's failure analysis traces those back to the type.

## Splitting a file into its documents

```python
from assay_sdk.documents import score_split

score_split(assay_case, expected=[(1, 2), (3, 3), (4, 6)], predicted=pipeline.documents)
# or first pages: score_split(assay_case, [1, 3, 4], [1, 3], page_count=6)
```

A check, `split`, per file: it passes when every document starts and ends on the right page, and
otherwise says how it went wrong:

- "pages 3-6 came out as one document, which is 2" (merged)
- "pages 1-3 is one document, cut into 2"
- "the document starting on page 3 was split at page 4" (a boundary a page or two off)

The report gives the share of files split right (and of those holding several documents),
precision and recall over documents (right when their first and last pages are), and over the
pages a new document starts on. On the dashboard, **Document splitting straight-through**
(`split_stp`) is the share of files holding several documents that split right.

## Confidence: when is a value safe to approve without review?

Give the extractor's confidence per field (and per type), and the report says whether it means
anything:

```python
score_document(assay_case, expected, extracted, SCHEMA, confidence={"total": 0.93, "invoice_date": 0.99})
```

```toml
[documents]
auto_approve = 0.9    # your pipeline skips review at or above this
target = 0.99         # the accuracy a suggested threshold must reach (default)
```

```
Confidence   9 values · calibration error 0.199 (was 0.088) · says 97.7% on average, right 77.8%: overconfident
             no threshold reaches 99.0% right over 10 values or more
             at your auto_approve 0.9: 100% approved, 2 wrong values among them (was 1), of 2 wrong in all: they'd skip review
```

- **Calibration error:** the gap between the confidence it states and how often it's right,
  averaged over ten bands of confidence. Overconfident means it's sure of values it gets wrong.
- **A threshold to approve at:** the lowest confidence whose values at or above it are right at
  least `target` of the time, over 10 values or more, with the share it would approve. The lower
  end of its 95% interval is shown too: 20 right out of 20 can't show 99%.
- **At your threshold:** what it approves, and how many wrong values are among them: the ones
  that would reach output without anyone looking.

## OCR: the text read from a page against what it says

```python
from assay_sdk.documents import score_ocr

score_ocr(assay_case, expected=TRANSCRIPTS["17-p1"], read=ocr_text, page=1, max_cer=0.02, max_digit_errors=0)
```

A check per page, `ocr page 1`, with three rates:

- **characters wrong:** edits (insertions, deletions, substitutions) over the page's characters,
  the character error rate;
- **words wrong:** the same over words;
- **digits wrong:** the digits on their own, since a misread digit is a wrong amount, date or
  account number while a misread letter in a sentence rarely matters. `max_digit_errors=0` fails a
  page on a single one.

Spacing doesn't count; case does, unless `case=False`. It fails over `max_cer` (default 5%). The
page is compared line by line, so a line read twice, dropped or out of order is counted as that,
and a long page is scored quickly. The report sums the rates over every page, against the
baseline, and shows the worst pages with a line that went wrong:

```
OCR          1 page · characters wrong 2.2% (was 0%) · words wrong 11.1% (was 0%) · digits wrong 8.3% (was 0%) · 1 over the limit
             tests/test_pages.py::test_page ocr page 1: 2.2%, e.g. 'Total: 1,284.56 EUR' for 'Total: 1,234.56 EUR'
```

**Reading order** is scored apart from the text. Each line read is matched to the page's line it
is, and the order score is the share of lines in the longest run that keeps the page's order: a
two-column page read across the columns scores low. With the lines put back in order, the
character error rate says what the reading got wrong, order aside, so text read right in the
wrong order isn't counted as text read wrong. `min_order=0.9` fails a page read out of order.

```
OCR          1 page · characters wrong 52.9% (was 0%) · words wrong 50.0% (was 0%)
             reading order 75.0% of lines (was 100%) · with them put back in order, characters wrong 0%
```

## Tables: structure as well as cells

```python
from assay_sdk.documents import score_table, Money

score_table(assay_case, expected=[["Item", "Qty", "Amount"], ["Widget", "2", "10.00"], ["Bolt", "5", "2.50"]],
            extracted=pipeline.tables[0], name="items", cells={"Amount": Money()})
```

A check per table, `table: items`. With `header` (the default) the first row names the columns,
and columns are matched by name, so columns and rows in another order are the same table. It
says what happened to the structure: a column missing, one that isn't there, two merged into one
("columns 'Item' and 'Qty' merged into one"), rows missing or made up, and the cells that are
wrong. The cells score is one number, the F1 of the cells right over the correct table's and
those read: a lost column and a garbled cell both lower it. `cells` is the type of every cell, or
per column.

## Spot checks: what reached published output

The **escape rate** is how often a wrong value got past automation and review into published
output. Only checking published values afterwards can say: sample some, have a person verify
them, and send each:

```python
from assay_sdk.documents import spot_check, Money

spot_check("doc-2", "total", published="1,284.56", correct="1234.56", spec=Money(),
           auto_approved=True, checked_by="sam")
```

Each is a check of the run `spot-checks` against the production document. The dashboard's
**Escape rate** is the share checked that were wrong, by segment, document type, field, and the
way it went out (`reviewed` or `auto-approved`), so it says which of the two lets more through.
It's a sample: its n says how far to trust it.

## Where on the page a value was read

```python
from assay_sdk.documents import score_locations

score_locations(assay_case, expected={"total": {"page": 1, "bbox": [412, 690, 520, 708]}},
                extracted=pipeline.locations, min_iou=0.5)
```

A check per field, `location: total`: right when it's on the same page and its box overlaps the
correct one by at least `min_iou` (intersection over union: the overlap's area over both boxes'
together). Boxes are `[x0, y0, x1, y1]`, or `box="xywh"` for x, y, width, height, in the same
units on both sides. A value read from the right place but mistyped is a wrong value; one read
from the wrong place (the subtotal for the total) is usually both.

## A value that isn't on the page was made up

```python
from assay_sdk.documents import check_rules, appears_in

check_rules(run, extracted, [appears_in(ocr_text, SCHEMA)])
```

A rule: each extracted value appears in the document's own text, read by its type, so 1234.56 is
found as "1,234.56 EUR" and 2026-03-04 as "4 March 2026". A value that's nowhere in the text was
invented, or read from another document. It needs no correct values, so it runs on every
production document, where invented values are otherwise invisible: "not in the document's text:
total '1284.56'".

## In the report

`assay test` and `pytest --assay` add a Documents block, against the baseline:

```
Documents    3 · all fields correct 2/3 (66.7%, was 100%) · weighted field accuracy 95.8% (was 100%)
                precision  recall
  invoice_date      66.7%   66.7%   1 wrong
```

It lists the fields with errors, or whose recall changed, with precision (of the values
extracted, how many were right), recall (of the values the documents have, how many were
extracted right), and the kinds of error. The PR comment has it in a line, with the fields of
lowest recall.

## On the dashboard

Once the checks reach the server (`assay test --upload`, or `pytest --assay --assay-upload`, or
sent by the pipeline itself), these are measured over time, overall and by document type and
segment, with the usual expected range and alerts ([Measures](measures.md)):

| Measure | From |
|---|---|
| Severity-weighted field accuracy (`field_accuracy`), also by field | `score_document` |
| Document splitting straight-through (`split_stp`) | `score_split` |
| OCR characters wrong (`ocr_cer`), OCR digits wrong (`ocr_digit_error_rate`), OCR reading order (`ocr_reading_order`) | `score_ocr` |
| Fields read from the right place (`location_accuracy`), also by field | `score_locations` |
| Table cells right (`table_cell_f1`) | `score_table` |
| Escape rate (`escape_rate`), also by the way a value went out | `spot_check` |

Each is unmeasured until its first check arrives, and `GET /v1/coverage` says which call it's waiting on. A document's type is its run's task: record the pipeline's runs as
`assay.run("invoice", ...)` for the slices to be document types. Under the `assay_case` fixture
the task is the test's name.
