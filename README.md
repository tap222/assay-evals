<h1 align="center">Assay</h1>

<p align="center">
  <strong>Your prompt change broke 3 cases. Assay tells you which ones, what changed, and whether it's real or noise.</strong><br>
  Behavioral regression testing for AI apps and agents, as a pytest plugin and a PR check.
</p>

<p align="center">
  <a href="https://pypi.org/project/assay-server/"><img src="https://img.shields.io/pypi/v/assay-server?label=pypi&color=blue" alt="assay-server on PyPI"></a>
  <a href="https://pypi.org/project/assay-server/"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python versions"></a>
  <a href="https://pepy.tech/project/assay-server"><img src="https://img.shields.io/pepy/dt/assay-server?label=downloads" alt="Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/tap222/assay-evals" alt="License: MIT"></a>
  <a href="https://github.com/tap222/assay-evals/stargazers"><img src="https://img.shields.io/github/stars/tap222/assay-evals?style=flat" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="#quickstart"><strong>Quickstart</strong></a> ·
  <a href="examples/prompt-regression"><strong>1-minute demo</strong></a> ·
  <a href="#features"><strong>Features</strong></a> ·
  <a href="#document-extraction"><strong>Document extraction</strong></a> ·
  <a href="#ci-and-pull-requests"><strong>CI</strong></a> ·
  <a href="#cli-reference"><strong>CLI</strong></a> ·
  <a href="docs/README.md"><strong>Docs</strong></a>
</p>

---

## Contents

- [Why Assay](#why-assay)
- [Try it in a minute](#try-it-in-a-minute)
- [Quickstart](#quickstart)
- [Features](#features)
- [Document extraction](#document-extraction)
- [CI and pull requests](#ci-and-pull-requests)
- [Integrations](#integrations)
- [Demo](#demo)
- [CLI reference](#cli-reference)
- [Documentation](#documentation)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Why Assay

AI output changes when anything around it changes, and a green test suite doesn't tell you what
changed or whether it matters. Assay compares every test case with its own last passing run, then
answers three questions on every change:

1. **What does the AI do differently?** Case by case: checks, tool calls in order, cost and context.
2. **Is the change real, or noise?** Repeated attempts separate chance from a true regression.
3. **Can the result be trusted?** Checks a pull request can't loosen, and judges calibrated
   against people's labels.

## Try it in a minute

No API key and no account. One line added to a prompt makes an agent skip an approval before a
refund, and Assay catches it:

```bash
pip install assay-server pytest
git clone https://github.com/tap222/assay-evals && cd assay-evals/examples/prompt-regression
./demo.sh
```

See [examples/prompt-regression](examples/prompt-regression) for what it shows.

## Quickstart

```bash
pip install assay-server pytest
assay init                 # assay.toml and an example test in tests/ai
pytest --assay tests/ai    # each case against its last passing run; exit 1 on a regression
assay diff                 # what behavior changed, case by case
```

A test is a pytest test that takes the `assay_case` fixture:

```python
from assay_sdk.testing import assert_called, assert_not_called, assert_max_steps

def test_refund(assay_case):
    reply = my_agent("Refund O-17", run=assay_case)
    assert_called(assay_case, "get_order", order_id="O-17")
    assert_not_called(assay_case, "delete_order")
    assert_max_steps(assay_case, 6)
    assert "27.61" in reply
```

`assay diff` shows what changed and what changed around it:

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

Exit codes for `pytest --assay`: `0` nothing got worse, `1` a regression, `6` inconclusive
(nothing got worse, but some results couldn't be judged or need more attempts to tell).

## Features

| Area | What you get | Docs |
|---|---|---|
| **Regression tests** | Tests are pytest tests. Each case is compared with its own last passing run: checks, tool calls in order, cost and context. | [Testing](docs/testing.md) |
| **Behavior diff** | What changed between two versions, with the flow before and after and a severity. | [Diff](docs/diff.md) |
| **Real change or noise** | Run each case several times and Assay separates chance (8/8 → 7/8, passes) from too few attempts to tell (inconclusive) from a regression (8/8 → 0/8, fails), corrected for the number of checks. A result that couldn't be judged is kept apart, never scored as 0. | [Repeated attempts](docs/repeats.md) |
| **Checks can't be loosened** | A PR that removes a contract, raises a limit, or deletes, skips or filters out the test its change breaks fails the run until a maintainer accepts it with a label. | [Security](docs/security.md) |
| **Document extraction** | Field, line-item, table, OCR, splitting and confidence metrics, with per-field gates. See [below](#document-extraction). | [Documents](docs/documents.md) |
| **Agents and workflows** | Trajectories, path and claim contracts, plan adherence, multi-turn conversations, simulated users, and [rewordings](docs/testing.md#rewordings-the-same-request-in-other-words): the same request in other words must get the same behavior. | [Agents](docs/agents.md) |
| **Judges you can check** | Calibration against labels a person gave, bias probes, drift, whether a judge ranks answers or only recognizes the topic, and a trust label on every judged score. | [Calibration](docs/calibration.md) |
| **What caused it** | The prompt, model, tools, input and settings that changed next to each regression, or "nothing on your side changed". | [Failures](docs/failures.md) |
| **Production back to tests** | Flagged traces and reviewed conversations become candidate test cases. Before there's traffic, `assay synth` generates queries from dimensions you define, kept apart from production. | [Learning](docs/learning.md), [Synthetic](docs/synthetic.md) |

## Document extraction

Scores each field against its correct value, and gates releases field by field so a single bad
field fails the PR even when the average looks fine. Full guide: [docs/documents.md](docs/documents.md).

### Fields

- **Typed matching with no schema needed.** Dates, amounts and numbers are compared by the type
  their values look like, so `"1,250.00"` matches `1250` and `"1.234,56 €"` is read correctly.
- **Wrong, missing or invented.** A value only the extractor gave counts as invented.
- **Made-up values counted apart:** fabricated (nowhere in the document), inferred (in it, but not
  as this field) and format errors (the right value in the wrong shape).
- **Groups scored as one unit**, such as a party's name, address and role.
- **Precision, recall and F1** over cells, headers and line items alike.
- **Values not in the document's text**, and where on the page a value was read.

### Documents and critical fields

- **Documents with zero errors.**
- **Straight-through bar for critical fields**, such as 99.9%, with how many values it takes to show it.
- **Rules**, such as line items adding up to the total.
- **Document types** as a confusion matrix.

### Line items and tables

- **Line items paired for the most cells right.** Complete only with none missing, made up or duplicated.
- **Table structure and cells**, with TEDS.

### OCR

- **Error rates** for characters, words, digits and letters, and what was read as what, diffed
  against the baseline.
- **Reading order.**
- **Engines ranked without labels**, against model-corrected text.

### Splitting

- **Files split into documents**, scored with panoptic quality.
- **Rework:** the pages a reviewer would drag to fix a split, priced on the dashboard.

### Confidence and routing

- **Is the extractor's confidence safe to auto-approve on?** Risk-coverage curves show what each
  threshold approves and lets through, with AURC.

### Robustness and stability

- **Robustness slices:** digital or scanned, stamps, handwriting, language, currency, and unseen
  supplier templates, compared with the baseline and gated.
- **Stability:** whether the same document gives the same values every time, not only the same
  pass or fail.

### Gates and production

- **Per-field gates**, so line items collapsing or a single wrong tax number fails the PR.
- **Tested beyond chance:** drops and worse slices are tested on the same documents, not against a
  fixed number of points.
- **Escape rate** from spot checks of published output, and **replaced values** that output still holds.
- Every metric is also a dashboard measure.

## CI and pull requests

The GitHub Action runs your tests, compares each one with its last passing run on the default
branch, and posts the diff as a PR comment. It fails the PR on a regression.

```yaml
# .github/workflows/ai-tests.yml
on: { push: { branches: [main] }, pull_request: {} }
permissions: { contents: read, pull-requests: write }
jobs:
  ai-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: tap222/assay-evals@main
        with:
          command: pytest --assay tests/ai
```

Inputs, timeouts, rerunning only what failed, and nightly runs: [docs/ci.md](docs/ci.md).

## Integrations

- **DeepEval and RAGAS.** Their metrics run as Assay checks, and an existing DeepEval suite changes
  one import. See [DeepEval and RAGAS](docs/frameworks.md).
- **Existing apps.** `assay connect` attaches Assay to an app you already have, through its
  database, a few lines of code, or a proposed test per model call. See [Setup](docs/setup.md).
- **Any model provider.**
- **Python SDK.** See [sdk/python](sdk/python/README.md).

## Demo

```bash
assay demo && assay serve
```

This loads synthetic data and opens the dashboard, document extraction metrics included. It covers
seven weeks in which a release breaks totals, wrong values escape through auto-approval, and one
customer's corrections stop reaching output.

## CLI reference

| Command | What it does |
|---|---|
| `assay init` | Set up local testing: `assay.toml` and a runnable example |
| `assay test` | Run your tests with recording, check every run, and compare with the baseline (`--repeat N`, `--junit`, `--failed`) |
| `assay diff` | What behavior changed between two runs |
| `assay accept` | Make the latest run the baseline |
| `assay ack` | Acknowledge a failing check until it gets worse |
| `assay pr-comment` | Post the latest run's summary on the pull request |
| `assay connect` | Attach Assay to an existing pipeline |
| `assay synth` | Generate synthetic queries from dimensions you define |
| `assay golden` | Manage the golden set a judge is calibrated against |
| `assay triage` | For each failure category: fix the prompt, add a code check, or add a judge |
| `assay evals` | Audit what each evaluator costs, and which could run as guardrails |
| `assay redact` | Check redaction and replay traces with personal data replaced |
| `assay serve` | Run the API and dashboard |
| `assay demo` | Load a synthetic demo tenant with 7 weeks of runs |

Run `assay --help` for every command and option.

## Documentation

| Guide | Covers |
|---|---|
| [Testing](docs/testing.md) | `pytest --assay`, the `assay_case` fixture, assertions, baselines, flakiness |
| [Behavior diff](docs/diff.md) | What changed between two versions, with a severity |
| [Repeated attempts](docs/repeats.md) | Chance, too few attempts, or a regression |
| [CI](docs/ci.md) | The GitHub Action, PR comments, reruns, timeouts |
| [Security](docs/security.md) | What the checks catch, and what a PR can't do to the evaluation |
| [Agents](docs/agents.md) | Trajectories, plan adherence, the LLM judge, conversations, MCP |
| [Document extraction](docs/documents.md) | Every document metric and gate |
| [Judge calibration](docs/calibration.md) | Golden sets and whether a judge tracks them |
| [Setup](docs/setup.md) | Install, connect, go live, deploy |

The full list is in [docs/README.md](docs/README.md). What's not built yet: [docs/roadmap.md](docs/roadmap.md).

## Development

```bash
pip install -e ".[dev]"
pytest
```

Where things live in this repository: [docs/layout.md](docs/layout.md).

## Contributing

Issues and pull requests are welcome at [github.com/tap222/assay-evals](https://github.com/tap222/assay-evals/issues).
Run `pytest` before opening a PR, and update this README in the same PR as any feature change.

## License

MIT. See [LICENSE](LICENSE).
