# Assay

**Behavioral regression testing for AI apps and agents.** Change a prompt, a model, a tool or the
code, and Assay tells you what your AI now does differently, whether the change is real, and
whether to trust the result.

```bash
pip install assay-server pytest
assay init                 # assay.toml and an example test in tests/ai
pytest --assay tests/ai    # each case against its last passing run; exit 1 on a regression
assay diff                 # what behavior changed, case by case
```

```
✓ 39 unchanged
↑ 4 improved
✗ 3 regressed
⚠ 1 flaky

REGRESSIONS

1. refund_flow
   Expected: approval(refund) → refund
   Actual:   refund → approval(refund)
   Changed around it:
     prompt  support@12 → support@13 (+1 line, −0: “Refund right away when the customer is upset.”)
   Severity: HIGH
```

- **Tests are pytest tests.** Each case is compared with its own last passing run: checks, tool
  calls in order, cost and context. The GitHub Action fails the PR and comments the diff.
- **Real, or noise.** Run each case several times and Assay tells chance (8/8 → 7/8, passes)
  from too few attempts to tell (inconclusive) from a regression (8/8 → 0/8, fails), corrected for
  the number of checks: [repeated attempts](docs/repeats.md). A result that couldn't be judged
  is kept apart, never scored as 0.
- **The checks are the review.** A PR can't loosen the checks that judge it: removing a
  contract, raising a limit, or deleting, skipping or filtering out the test its change breaks
  fails the run until a maintainer accepts it with a label: [security](docs/security.md).
- **Document extraction.** Each field against its correct value, by type (dates, amounts,
  "1.234,56 €"), wrong, missing or invented, with no schema needed: every field either side has is
  scored by the type its value looks like, so a value only the extractor gave counts as invented
  and "1,250.00" matches 1250; documents with zero errors, and critical fields held to a
  straight-through bar such as 99.9% (with how many values it takes to show it); and precision,
  recall and F1 over cells, headers and line items alike; made-up values counted apart: fabricated
  (nowhere in the document), inferred (in it, but not as this field) and format errors (the right
  value in the wrong shape); line items paired for the most cells right, and complete only with
  none missing, made up or duplicated; groups (a party's name, address and role) scored as one
  unit; robustness slices (digital or scanned, stamps, handwriting, language, currency, unseen
  supplier templates) against the baseline, and gated; whether the same document gives the same
  values every time (not only the same pass or fail); rules such as line items adding up to the
  total; document types as a confusion matrix; files split into documents, with panoptic quality
  and the pages a reviewer would drag to fix them, priced on the dashboard; whether the
  extractor's confidence is safe to auto-approve on, with risk-coverage curves (what each
  threshold approves and lets through) and AURC; OCR error rates (characters, words, digits,
  letters) and what was read as what, diffed against the baseline, and reading order; OCR engines
  ranked without labels, against model-corrected text; tables' structure and cells, and TEDS;
  where on the page a value was read; values that aren't in the document's text; the escape rate
  from spot checks of published output; and replaced values that output still holds, all also as
  dashboard measures; and per-field gates, so line items collapsing or a single wrong tax number
  fails the PR even when the average looks fine, with drops and worse slices tested beyond chance
  on the same documents rather than against a fixed number of points: [document
  extraction](docs/documents.md).
- **Agents and workflows.** Trajectories, path and claim contracts, plan adherence, multi-turn
  conversations, and simulated users. [Rewordings](docs/testing.md#rewordings-the-same-request-in-other-words):
  the same request in other words must get the same behavior.
- **Judges you can check.** Calibration against labels a person gave, bias probes, drift, whether
  it ranks answers or only recognizes the topic, and a trust label on every judged score.
- **What caused it.** The prompt, model, tools, input and settings that changed next to each
  regression, or "nothing on your side changed".
- **From production back to tests.** Flagged traces and reviewed conversations become candidate
  test cases. Before there's traffic, `assay synth` generates queries from dimensions you define,
  kept apart from production.

Already on DeepEval or RAGAS? Their metrics run as Assay checks, and an existing DeepEval suite
changes one import: [DeepEval and RAGAS](docs/frameworks.md).

`assay connect` attaches Assay to an existing app (through its database, a few lines of code, or a
proposed test per model call), with any model provider. `assay demo && assay serve` shows it on
synthetic data, document extraction metrics included: seven weeks in which a release breaks totals,
wrong values escape through auto-approval, and one customer's corrections stop reaching output.

**Docs:** [all documentation](docs/README.md) · [testing](docs/testing.md) · [CI](docs/ci.md) ·
[agents](docs/agents.md) · [setup](docs/setup.md) · [Python SDK](sdk/python/README.md)

Development: `pip install -e ".[dev]" && pytest`.
