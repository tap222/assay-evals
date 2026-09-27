[Assay](../README.md) › [Documentation](README.md)

# Measures, cost and alerting

## What you get

| View | Answers |
|---|---|
| **Overview** | What's broken right now? Open anomalies, SLO state, and the slices that moved beyond noise since the last run. Refreshes every minute. |
| **Workflow** | The pipeline as a graph inferred from traffic: each step's health, errors and broken path contracts; the contracts and how each is holding up; suggested contracts; and path shifts. |
| **Learn** | Production traces scored without labels, clustered into patterns, drafted into test cases for review, and suites with the loop's coverage, time to test, and recurrences. |
| **Agents** | An agent run's pass rate per check against the baseline, where failing runs first went wrong, efficiency (steps, repeats, tool errors, tokens, cost), runs that got longer, and every trajectory. Trace shows one step by step against its reference. |
| **Failures** | Reported errors or an evaluation run, grouped into causes: each with its kind, confidence, evidence, what sets it apart from passes, and examples. Accept intended changes, confirm or dismiss the rest. |
| **Cost** | Fully loaded cost per document and per page, stacked by component over time; cost by document type, segment or mode; AI spend by model with the fallback share; the rate card. |
| **Measures** | Each measure over time, with the expected range it's judged against, any SLO line, a breakdown of every slice, and an SLO editor. |
| **Prompts** | Every prompt version: documents, error rate, fallback, latency, cost, a verdict against the previous version (adjusted for document mix), what changed, and a diff. |
| **Errors** | Where reported wrong values start: by step in pipeline order, by verdict, field, document type, segment and model; recent errors with their diagnosis; a report form. |
| **Trace** | Why was *this* document slow, lost or wrong? A timeline of every stage and model call, each step's values side by side with wrong ones marked, and problems flagged. |
| **Alerts** | Pending, open and resolved alerts, with how long each lasted. |
| **Release gates** | Every advance, hold or rollback decision, with its lineage. |
| **Connect** | Setup snippets, what your data can answer measure by measure, and one-click backfill. |

Every alert has an **Investigate** link to its slice. `#measures/<id>` and
`#trace/<document_id>` are shareable links.

## Measures

| Group | Measures |
|---|---|
| **Operational health** | `document_volume`, `stage_failure_rate`, `call_error_rate`, `call_latency_p95`, `time_to_complete_p90`, `input_mix_drift` |
| **Cost** | `cost_per_document`, `cost_per_page`, `total_spend`, `human_touch_rate`, `cost_coverage` (see Cost below) |
| **Pipeline integrity** | `fallback_attribution` (does each call record which model tier answered, and why), `model_mismatch` (served ≠ declared), `revision_coverage`, `noop_stage_rate` (stages that report success without doing work), `source_positions` (values a reviewer can click through to), `handoff_loss` (finished documents missing downstream) |
| **Errors** | `reported_error_rate`, `errors_by_origin`, `prompt_error_rate` (see Error analysis and Prompt versions) |
| **Document quality** | `ocr_cer`, `ocr_digit_error_rate`, `ocr_reading_order`, `location_accuracy`, `table_cell_f1`: from OCR, locations and tables scored with `assay_sdk.documents` ([Document extraction](documents.md)) |
| **Accuracy** | `field_accuracy`: fields scored against their correct values ([Document extraction](documents.md)), weighted by what an error costs, by document type, segment and field. `split_stp`: the share of files holding several documents split right (`score_split`). `escape_rate`: wrong values in published output, from spot checks (`spot_check`). `superseded_value_rate`: listed as *unmeasured* until links between documents can be ingested |

Every measure reports an overall row plus one row per slice value. A missing dimension is
kept as an `(unrecorded)` slice. A source that can't provide the data makes a measure
*unmeasured*, never 0.

`input_mix_drift` is the population stability index against the previous window of equal
length, split into each category's contribution. It tells a moving accuracy number apart
from a moving population.

Adding a measure means writing a class in `assay/measures/` with a `compute(source, window)`
and registering it in `assay/measures/__init__.py`.

## Cost

The price of a model call is the number everyone quotes, and usually the smallest part of what
a document costs. Assay prices every component it can see and says which ones it can't:

| Component | Priced from |
|---|---|
| AI inference | `cost_usd` on calls answered by the model they declared |
| AI inference, estimated | unpriced calls, at the median price of priced calls to the same model at the same stage (else the same model). Always its own line, never mixed into measured cost |
| Fallback escalation | calls answered by a fallback tier (`resolving_layer` isn't primary) or by a different model than declared |
| Human review | review `minutes` × the reviewer rate, or `cost_usd` as recorded |
| Rework | rework `minutes` × the rework rate, or `cost_usd` as recorded |
| Platform | per-document + per-page rates × `page_count` |

The **rate card** holds prices the pipeline can't record itself: reviewer and rework cost per
hour, and platform cost per document and per page. It is set per source, in the Cost tab or with
`PUT /v1/cost/rates`. Nothing is guessed silently:
- Unpriced calls with nothing to estimate from make AI cost a stated floor.
- No review records means people cost is shown as *missing*, not zero.
- Review minutes without a rate are counted and reported.

`cost_per_document` is broken down by component, document type, segment, mode and stage, and
alerts when any of them rises beyond its normal range. For example, the demo's fallback
escalation opens an alert on the *Fallback escalation* component. The band uses the slice's
real standard error from per-document costs, so the heavy tail of expensive documents doesn't
page anyone. `total_spend` is for reporting: it tracks volume, so it's charted but never
raises anomalies. `/v1/cost/breakdown?by=document_type` returns the component mix per
category, computed live.

## Alerting

After every run, each measured slice is checked two ways:

- **Anomaly:** the value leaves the range learned from that slice's last 8 runs. The band's
  half-width is the largest of: 3× the robust run-to-run spread, 3× the sampling error at
  this sample size (binomial for rates, Poisson for counts), and a minimum width. So small
  slices get wide bands and a flat history never produces a zero-width band.
  Measures with a direction alert only when they get worse. Volume and drift alert either way.
- **SLO:** the value is on the wrong side of a target. Targets can apply to the overall
  value, to one slice, or to *every* slice of a dimension, for example "no customer loses
  more than 5% at the handoff". The most specific target wins.

A condition seen once is **pending** and notifies nobody. It **opens** after 2 consecutive
runs (`ASSAY_ALERT_AFTER_RUNS`) and **resolves** on the first run where it no longer holds.
Slices under 30 (`ASSAY_ALERT_MIN_N`) are never judged. Resolving works the same way: an alert
closes only after 2 clear runs in a row. While open, an anomaly is judged against the range it
opened with, so a sustained shift stays open instead of quietly becoming the new normal.
Band widths come from each slice's normal noise in history, not from the current run, because
an incident often increases the spread too.

Notifications go to `ASSAY_WEBHOOK_URL`: Slack format by default, or structured JSON with
`ASSAY_WEBHOOK_FORMAT=json`. Messages link straight to the measure when `ASSAY_PUBLIC_URL`
is set.
