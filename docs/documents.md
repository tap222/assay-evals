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

## How a value was made up

Not every made-up value is the same, and they're counted apart. Give `score_document` the
document's text (its OCR) and each wrong or invented value says which kind it is:

| Kind | What it is | Example |
|---|---|---|
| format | the right information in the wrong shape | 03/04 for 04/03; 1,250.00 for 1,250,000.00; "Doe, Jane" for "Jane Doe" |
| inferred | a guess from context: the value is in the document, just not as this field | governing law "California", because California is mentioned a lot, when the contract says New York; the seller's name as the buyer |
| fabricated | the value is nowhere in the document | a county that no page names; a parcel number on a deed that has none |

```python
s = score_document(run, expected, extracted, DEED_SCHEMA, text=ocr_text)
s.made_up                         # {"format": 2, "inferred": 2, "fabricated": 2}
s.fields["grantee"].note          # "jane roe, not john doe; inferred: it's in the document, but not as this field"
```

Inferred values are the ones to watch on recording documents such as deeds: a county, or which
party is the grantor and which the grantee, look right to a reviewer because the words are
there. They're read by type, like `appears_in`, so 1234.56 is found as "1,234.56" and a date as
"4 March 2026". Format errors are told apart without the text; inferred and fabricated need it.
Missing values weren't made up and aren't sorted. Line-item cells aren't sorted either, so their
tables aren't counted in these shares.

Zero is a value, not an empty one. Some benchmarks treat 0 and "" as the same; that hides the
difference between a field left out and one read wrong. Here 0 extracted where the document has
nothing is invented, and nothing extracted where it says 0 is missing.

## Values match by type, not by spelling

Without a schema, `score_document(run, expected, extracted)` scores every field either side has,
each by the type its correct value looks like (`infer_schema`): "1250" and "1,250.00" are the same
number, "2026-03-04" and "4 March 2026" the same date, "$5" an amount; nested objects become
dotted fields (`vendor.name`), lists of objects line items. A field only the extractor gave is
**invented**, not ignored. Digits with a leading zero ("02139") are an identifier, compared as
text. Declare a schema for anything the guess can't know: weights, `day_first`, tolerances, the
line items' `key`.

With a schema, fields the extractor gave that it doesn't list aren't scored, but they're named:
`DocumentScore.unscored`, and in the report "extracted but not in the schema, so not scored:
po_number (2 documents)".

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

Rows are matched row to row whatever their order, as DocILE scores them: every correct row
against every extracted one, field by field, then the pairing with the most cells right in all (a
maximum matching, not first come first served, which can pair two rows that share a lot and
strand the rest). With `key` (a cell that names the row, like its description), a row pairs only
with one of the same key. A row is right when all its cells are.
`line_items` is scored by its rows: precision and recall over rows, and its share right is the
row F1. Missing rows, invented rows and the wrong cells are named ("1 row(s) invented; amount
wrong in 1 row(s), e.g. Widget: '100.00', not '1000.00'"). Each column is a check of its own
too (`line_items.amount`), which says which column breaks; it isn't counted again in the
document's accuracy. Its precision and recall are over cells: a cell of a matched row counts as a
field does, a cell in a missing row is missing, and one in an invented row is invented.

**Complete** means every line item captured, none left out, made up or repeated. A row that
repeats one already matched is counted as **duplicated**, apart from rows made up; the report
says "line items complete 2/3: 1 row missing, 1 row duplicated".

## Groups: fields that belong together

```python
from assay_sdk.documents import Group, LineItems, Text

SCHEMA = {"grantor": Group({"name": Text(), "address": Text(), "role": Text()}),
          "grantees": LineItems({"name": Text(), "address": Text(), "role": Text()}, key="name")}
```

A group is scored as one unit, as KIEval scores grouped information: the grantor is right only
when name, address and role all are ("role wrong (2 of 3 right)"). Each part is a check of its
own too (`grantor.role`), and it counts in cell F1 and in made-up values, so a role given to the
wrong party shows up as inferred. In the weighted accuracy the group counts once, by its
`weight`. For several parties, use line items: each row is a group, right when all its cells are.

## The whole document

`document` is one more check: **All fields correct**, the share of documents that need no
correction. This is the number that governs automation: one wrong field and a person touches the
document. It's much stricter than field accuracy. In the demo, fields are 97.5% right but only
85.2% of documents have zero errors. The weighted share of fields right (each field by its `weight`, line items by their
row F1) is the document's accuracy.

Beside it, one unweighted number with a single definition for headers and line items, as
ExtractBench scores: the document flattened into cells, each field one and each line-item cell
one, with precision (of the cells extracted, the share right), recall (of the cells the document
has, the share extracted right) and F1 over them. A cell empty in both isn't counted.

```python
s = score_document(None, expected, extracted, SCHEMA)
s.cells       # e.g. {"tp": 4, "fp": 6, "fn": 3}
s.precision   # 0.4
s.recall      # 0.571
s.f1          # 0.471
```

Use the weighted accuracy when some fields cost more to get wrong; use cell F1 to compare
extractors or runs on one scale, however many line items a document has.

## Robustness slices: where regressions hide

An average can hold while one kind of document falls apart: scanned pages, stamps over the text,
a language, or a supplier layout the model was never tuned on. Say what each document is like:

```python
score_document(assay_case, expected, extracted, SCHEMA, facets={
    "source": "scanned",          # or digital
    "quality": "skewed",          # clean, noisy, low-resolution, ...
    "stamps": True, "handwriting": False,
    "language": "de", "currency": "EUR",
    "template": "acme-v3",        # the supplier's layout
    "template_seen": False,       # a layout the model wasn't built or tuned on
})
```

The report compares every slice with the baseline, the ones worse beyond chance first:

```
Documents    40 · all fields correct 32/40 (80.0%, was 100%) · weighted field accuracy 93.3% ...
Slices       4 by facet · documents with zero errors · worse beyond chance: template_seen=unseen
  template_seen=unseen     8 · zero errors 0% (was 100%, down 100.0 points: worse beyond chance, p 0.016) · ...
  source=digital          20 · zero errors 80.0% (was 100%, down 20.0 points, within chance (p 0.08, 4 of 20 documents)) · ...
  ...
```

A slice is called worse only when the drop is beyond chance: on the documents in both runs, an
exact McNemar test (those right before and wrong now, against the reverse), with
Benjamini-Hochberg across the slices so that many slices don't make false alarms. A drop that
could be chance is shown, with its p and how many documents it rests on: four documents all
flipping is 1 in 16 by luck, not proof. The PR comment names the slices worse beyond chance. `template_seen` is yours to set, since only you know what the
model was built or tuned on. The template id itself isn't a slice (one per supplier is too many
to read); `template_seen` and `template_new` are. Gate a slice like any field:

```toml
[documents.gates]
"document[template_seen=unseen]" = { min_accuracy = 0.9 }
```

**In production**, send the same facets with each document (`POST /v1/events/documents`,
`"facets": {"source": "scanned", "template": "acme-v3"}`). The dashboard's accuracy measures are
sliced by every facet that was sent, and no others (a facet nobody sends isn't an "unrecorded"
slice). From `template`, Assay adds **`template_new`**: yes for a template's first 30 days, from
its first document, so a supplier's new layout arriving in production is a slice of its own.

## Stability: the same document, extracted again

`assay test --repeat 3` runs every case three times. Pass/fail noise is handled as for any
check (a check that flips is flaky, one whose pass rate fell beyond chance is a regression). But
a field can be unstable without ever flipping: wrong every time, and wrong a different way each
time. So each field's value is compared across attempts, on what it means (`canonical`: "1,250.00"
and 1250 are one value, "4 March 2026" and "2026-03-04" one date, line items whatever their
order):

```
Stability    4 attempts · fields with the same value every time 90.0% (9/10) · documents fully repeatable 4/5
             1 wrong every time and never the same way, which pass/fail can't see as flaky: e.g. total in tests/test_rep.py::test_3: '100.00', '101.00', '102.00', '103.00'
```

Gate it like any field, `stability = { min_accuracy = 0.99 }`, and on the dashboard it's
**Same value every attempt** (`value_stability`), by segment, document type, field and facet.

## Critical fields and straight-through processing

Not every field stops a document. Name the ones that do:

```python
score_document(assay_case, expected, extracted, SCHEMA, critical=["tax_number", "total"])
```

The document check then also says whether every critical field was right
(`DocumentScore.critical_correct`): the document could go straight through, whatever the
low-stakes fields say. The report adds:

```
  critical fields (tax_number, total): 1,000 of 1,000 right (100%, 95% interval 99.62% to 100%) · documents with all of them right 500/500
    99.90% can't be shown with 1,000 values: even all right, the interval's low end would be 99.62%; it takes 3,838 in a row. The gate checks the share right; this is how far to trust it.
```

The bar often quoted for straight-through processing is 99.9% on critical financial fields. Set
it as a gate, with one for documents with zero errors:

```toml
[documents.gates]
critical = { min_accuracy = 0.999 }   # the critical values, over the run
document = { min_accuracy = 0.95 }    # documents with every field right
```

A share is only as good as its sample: 99.9% can't be shown by fewer than 3,838 values in a row,
all right (`values_to_show`). The gate checks the share; the report says when the run is too
small to back it, so a green gate on 200 documents isn't read as proof.

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

Each file is scored the ways the page stream segmentation literature does:

| Score | What it counts |
|---|---|
| Where a document starts | precision and recall over the pages a new document starts on |
| Documents right | precision and recall over documents, right only when their exact pages match |
| Panoptic quality (`pq`) | documents matched when they share over half their pages, each match weighted by how much (IoU), over the matches plus half the documents unmatched on either side. A comparison of six metrics on the WooIR dataset found it the most fitting for this task. `sq` is the matches' mean IoU, `rq` the F1 of matching |
| Pages to move by hand (`drags`) | the fewest pages a reviewer must drag to put the split right: MNDD, minimum number of drags and drops (Mungmeeprued et al., DocEng 2022). Each correct document is kept as the predicted one it shares most pages with, one to one, and every other page moves once, to a new document too. The same count as the paper's reference code (TABME, `num_of_swaps`), which tries every pairing; here the best one is found directly (the Hungarian method), so large files are fast |

Boundaries a page off and merges score very differently on these: pages 1-10 cut in half has
half its boundaries wrong, no document right, a panoptic quality of 0 (half the pages isn't over
half), and 5 pages to drag back.

What a wrong split costs a reviewer, in time and money:

```toml
[documents]
seconds_per_drag = 20      # one page dragged to where it belongs
rework_per_hour = 36       # USD
```

```
Splitting    2 files · split right 0/2 (0%, was 100%)
             documents right: precision 33.3%, recall 16.7% · where a document starts: precision 100%, recall 25.0%
             panoptic quality 37.0% (was 100%) · pages to move by hand 4 of 12 (was 0), about 1 minute by hand ($0.80)
```

On the dashboard: **Document splitting straight-through** (`split_stp`), the share of files
holding several documents that split right; **Splitting panoptic quality** (`split_pq`); and
**Pages moved by hand** (`split_drag_rate`), the share of pages a reviewer would drag, by segment
and document type; and under Cost, **Split rework cost per file** (`split_rework_cost`): the pages
to drag x `seconds_per_drag` x `rework_per_hour` (else `review_per_hour`), all on the rate card.
It's an estimate from scored files, kept apart from `cost_per_document`, where recorded rework
minutes already count.

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

### Selective prediction: what each threshold approves, and what gets through

Confidence is for routing: values above a threshold skip review, the rest go to a person. The
question is less "are the numbers calibrated" than "does it rank right values above wrong ones",
and what each threshold trades: automation (coverage) against wrong values let through (risk).

```
             stated 0.9 or more: 20 values, says 97.0% on average, right 80.0% (95% interval 58.4% to 91.9%): overconfident; 80 more to reach the 100 a check needs
             risk-coverage: AURC 0.150, the best possible 0.006 (every wrong value least confident); approving from the most confident down:
             threshold  approved   right  wrong through
                  0.99        0%       —              0
                  0.95     50.0%   80.0%              4
                   0.9     50.0%   80.0%              4   yours
                   0.5      100%   90.0%              4
```

- **The 0.90+ check:** the values stated at 0.90 or more (or at your `auto_approve`): what they
  say on average against how often they're right, with a 95% interval. Overconfident when the
  interval sits below what's stated. A check needs about 100 values; the line says how many more.
- **The risk-coverage table:** at each threshold, the share of values approved, the share of those
  right, and the wrong values that would go through unreviewed. Your `auto_approve` is marked.
- **AURC:** the area under the risk-coverage curve, one number for the ranking: approving from the
  most confident down, the mean share wrong among those approved. Lower is better. It's shown
  beside the best possible for the same accuracy (every wrong value least confident), since AURC
  also rises with the error rate itself.

Calibration and ranking can disagree. Above, the extractor is underconfident overall (says 78.5%,
right 90%) and overconfident exactly where it matters: every wrong value was stated at 0.97.

On the dashboard, under Confidence: **Risk-coverage (AURC)** (`confidence_aurc`), **Wrong at
0.90+ confidence** (`confident_error_rate`) and **Confidence calibration error**
(`confidence_ece`), by segment, document type and field.

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
OCR          1 page · characters wrong 2.2% (was 0%) · words wrong 11.1% (was 0%) · digits wrong 8.3% (was 0%) · letters wrong 0% · 1 over the limit
             tests/test_pages.py::test_page ocr page 1: 2.2%, e.g. 'Total: 1,284.56 EUR' for 'Total: 1,234.56 EUR'
             read as: '3' as '8' 1
               since the baseline: new '3' as '8' 1
             words read as: '1,234.56' as '1,284.56' 1
               since the baseline: new '1,234.56' as '1,284.56' 1
```

Digits and letters are counted apart: a wrong digit is a wrong amount, a wrong letter a wrong
name or code. **What was read as what** is kept per page and summed over the run: characters
("l" as "1", "0" as "O", "rn" as "m", "i" lost) and whole words ("modern" as "modem"). Against
the baseline, the report says which confusions a new version brought, which grew, and which it
fixed ("since the baseline: new 'l' as '1' 9; fixed 'S' as '5' (was 3)"), and the PR comment
names the new ones. A change of OCR engine or preprocessing shows up as the confusions it
changes, not only as a rate that moved. Lines only moved, not misread, add no confusions.
`OcrScore.confusions` and `word_confusions` hold them per page.

**Reading order** is scored apart from the text. Each line read is matched to the page's line it
is, and the order score is the share of lines in the longest run that keeps the page's order: a
two-column page read across the columns scores low. With the lines put back in order, the
character error rate says what the reading got wrong, order aside, so text read right in the
wrong order isn't counted as text read wrong. `min_order=0.9` fails a page read out of order.

```
OCR          1 page · characters wrong 52.9% (was 0%) · words wrong 50.0% (was 0%)
             reading order 75.0% of lines (was 100%) · with them put back in order, characters wrong 0%
```

## OCR without labels: ranking engines against corrected text

Choosing between OCR engines (or checking a new version) usually needs pages someone typed out.
Without them, DocOCR-Eval's approach ranks engines against what a model says each page most
likely reads: each engine's text is corrected by one or more models, and the engines are scored
by how close they came (ANLS: 1 minus edits over the longer text, 0 below 0.5), averaged over
the correctors so no one model's taste decides.

```python
from assay_sdk import Judge
from assay_sdk.documents import correct_ocr, rank_ocr

reads = {"tesseract": tesseract_pages, "azure": azure_pages}        # {engine: {page: text}}
correctors = {"claude": Judge("anthropic", "claude-opus-5-5"),
              "gpt": Judge("openai", "gpt-5")}
corrected = {name: {p: correct_ocr(reads["azure"][p], judge, image=png[p]) for p in reads["azure"]}
             for name, judge in correctors.items()}
rank_ocr(assay_case, reads, corrected, truth=labelled_pages)           # truth: the few pages you have
```

```
OCR ranking  3 engines · 40 pages · against text corrected by claude, gpt (no labels): azure 95.4%, tesseract-5 91.2%, tesseract-4 88.0%
             on the 10 labelled pages: the same best engine · Kendall tau 1.00 · NDCG 1.00
```

- **`correct_ocr(read, corrector, image=None)`** asks a model to correct only OCR errors (misread
  characters, broken words), keeping the line breaks, the page's own spelling and anything it
  can't verify. With the page image (Claude), it re-reads doubtful text. The default corrector
  is Claude (`claude-opus-5-5`), with server-side fallbacks on; any `Judge` works. Each page
  corrected is a model call you pay for. A refusal, an empty answer or a correction cut off at
  `max_tokens` raises `OcrCorrectionError` rather than scoring against a partial page.
- **`rank_ocr(run, engines, corrected, truth=None)`** scores and orders the engines. With the
  labelled pages you do have, it checks the ranking against theirs (Kendall tau, NDCG, the same
  best engine): that says how far to trust it on the rest, and the check fails when the best
  engine differs.
- **An engine is never scored against its own corrections:** a model that's both a candidate and
  a corrector would be flattered, so an engine named as a corrector, or given as
  `same_model={"gpt-ocr": "gpt"}`, is refused.
- It's kept apart from `score_ocr`: nothing scored against a model's reading counts as OCR
  accuracy, in the report or on the dashboard.

This is a simplified version of the paper's method: one correction prompt, where the paper first
diagnoses each block (character noise, tokenization, semantic consistency) and corrects from the
text or from a re-read of the image depending on what it found. The paper reports that the best
correction strategy varies across document collections, so check the ranking on a few labelled
pages before relying on it.

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
per column. Rows are paired to get the most cells right, and a repeated row is named duplicated.

Beside it is **TEDS** (tree edit distance similarity), the standard table score: both tables as
trees (the table, its rows, their cells, the header included), and 1 minus the edits turning one
into the other over the larger's size. A cell renamed costs its text's normalized edit distance,
so "10.00" read as "10.0O" costs less than a cell lost. `TableScore.teds_structure` is TEDS-S,
structure only. `teds(expected, extracted)` scores two tables directly. Cells don't span rows or
columns here: tables are lists of rows.

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

## Superseded values: a later document replaced them; did output follow?

A corrected invoice, an amended contract, a credit note: a later document changes values an
earlier one gave. If output still holds the old value, unmarked, whatever reads it takes it as
current. Nothing in the pipeline's own records says so, so check it: for a later document and the
one it replaces, compare what output holds now.

```python
from assay_sdk.documents import superseded_values, Money, Date

superseded_values("inv-17", "inv-17-corrected", old=extracted_17, new=extracted_17b,
                  output=published["inv-17"], flagged=held_for_review, schema=SCHEMA, link="replaces")
# {"total": "escaped", "po_number": "updated"}
```

Each field the later document changed is one of:

| Outcome | Output holds |
|---|---|
| updated | the new value (or nothing, when the new document dropped it) |
| flagged | the old value, marked as superseded or held for review (`flagged`: the fields, or True for all) |
| escaped | the old value, or another wrong one, unmarked |

Fields it left the same aren't counted. Each is sent as a check of the run `superseded`, against
the earlier document ("output holds the old value ('1234.56'); inv-17-corrected replaces it with
'1,200.00'"), and the dashboard's **Superseded values reaching output**
(`superseded_value_rate`) is the share escaped, by segment, document type, field, and whether the
later document `replaces` or `amends` the earlier one.

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

## Gates: fields that can't average out

A pull request fails when a document check that passed now fails. That misses a field that was
already imperfect and then collapses: `line_items` fails on a document with one row wrong, so
when a new model gets 2 of 10 rows right instead of 9, the check is "still failing", not a
regression, and the headers keep the average plausible (80% weighted field accuracy with line
items at 20%). And some fields can't afford one error at all: a wrong tax number is an
accounting error, not a lost point.

Gates are per field, beside the per-case regressions:

```toml
[documents.gates]
tax_number = { max_errors = 0 }                   # one wrong value fails the run, baseline or not
total      = { max_errors = 0, min_recall = 0.99 }
line_items = { max_drop = 0.02 }                  # row F1 surely no more than 2 points below the baseline's
vendor     = { min_precision = 0.95 }
```

| Rule | Fails when |
|---|---|
| `max_errors` | more values than this are wrong, missing or invented in the run (for line items: documents with a row wrong) |
| `min_accuracy` | the share right is below this; `document` gates documents with every field right, `critical` the critical values |
| `min_precision`, `min_recall`, `min_f1` | the field's precision, recall or F1 over the run is below this |
| `max_drop` | the field's F1 is surely more than this below the baseline's: tested on the documents in both runs (below). Skipped until there is a baseline |

**`max_drop` is tested, not compared:** each document in both runs is scored before and now, and
a paired t interval (95%) on the change says how far it fell. The document is the unit, so one
long table doesn't outweigh the rest. The gate fails when even the optimistic end of the
interval is a bigger drop than allowed, and warns ("unsure: ... could be more than the 2
allowed: add documents to tell") when only the pessimistic end is, as release gates do. A drop
on two documents out of six won't fail a PR; the same drop on every document will.

**Line items are gated by default:** every line-items table at `max_drop = 0.02`, so a collapse
fails the run with nothing configured. Give it a rule of your own to change that, or
`line_items = {}` to turn it off. A configured field the run didn't score fails: a gate can't pass
on nothing. Weights (`Text(weight=3)`) say what an error costs in the average; gates say which
errors the average mustn't hide.

```
Gates        0 of 1 held:
  failed: line_items: F1 20.0%, was 90.0%; per document down 70.0 points (95% interval 70.0 to 70.0, 4 documents), surely more than the 2 allowed
...
1 document gate failed: line_items: F1 20.0%, was 90.0%; per document down 70.0 points (95% interval 70.0 to 70.0, 4 documents), surely more than the 2 allowed.
Failed.
```

The PR comment says which gates failed.

## In the report

`assay test` and `pytest --assay` add a Documents block, against the baseline:

```
Documents    3 · all fields correct 2/3 (66.7%, was 100%) · weighted field accuracy 95.8% (was 100%) · cell F1 94.1% (was 100%), precision 94.1%, recall 94.1%
  made up, of 9 values extracted: 1 format (was 0) (inferred and fabricated need the text: score_document(..., text=))
                precision  recall
  invoice_date      66.7%   66.7%   1 wrong, 1 format
```

With the document's text, the made-up line counts inferred and fabricated values too.

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
| Documents with zero errors (`document_accuracy`) | `score_document` |
| Same value every attempt (`value_stability`), also by field | `score_document`, under `assay test --repeat` |
| Documents right on critical fields (`critical_document_accuracy`), critical field accuracy (`critical_field_accuracy`, also by field) | `score_document`, with `critical=` |
| Severity-weighted field accuracy (`field_accuracy`), also by field | `score_document` |
| Field cells right (`field_cell_f1`): F1 over header and line-item cells | `score_document` |
| Fabricated values (`fabricated_value_rate`), inferred values (`inferred_value_rate`), format errors (`format_error_rate`): each a share of the values extracted, also by field | `score_document`, with `text=` for the first two |
| Document splitting straight-through (`split_stp`), panoptic quality (`split_pq`), pages moved by hand (`split_drag_rate`) | `score_split` |
| OCR characters wrong (`ocr_cer`), digits wrong (`ocr_digit_error_rate`), letters wrong (`ocr_letter_error_rate`), reading order (`ocr_reading_order`) | `score_ocr` |
| Fields read from the right place (`location_accuracy`), also by field | `score_locations` |
| Table cells right (`table_cell_f1`), table similarity (`table_teds`) | `score_table` |
| Escape rate (`escape_rate`), also by the way a value went out | `spot_check` |
| Superseded values reaching output (`superseded_value_rate`), also by field and link | `superseded_values` |
| Risk-coverage (`confidence_aurc`), wrong at 0.90+ confidence (`confident_error_rate`), calibration error (`confidence_ece`), also by field | `score_document`, with `confidence=` |

Each is unmeasured until its first check arrives, and `GET /v1/coverage` says which call it's waiting on. A document's type is its run's task: record the pipeline's runs as
`assay.run("invoice", ...)` for the slices to be document types. Under the `assay_case` fixture
the task is the test's name.
