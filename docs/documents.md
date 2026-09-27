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

The **Severity-weighted field accuracy** measure (`field_accuracy`) is computed from the same
checks once they reach the server (`assay test --upload`, or `pytest --assay --assay-upload`):
overall, and by document type, segment and field, with the usual expected range and alerts
([Measures](measures.md)). A document's type is its run's task: record the pipeline's runs as
`assay.run("invoice", ...)` for the slices to be document types. Under the `assay_case` fixture
the task is the test's name.
