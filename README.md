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
  is kept apart, never scored as 0. A PR can't loosen the checks that judge it.
- **Agents and workflows.** Trajectories, path and claim contracts, plan adherence, multi-turn
  conversations, and simulated users.
- **Judges you can check.** Calibration against labels a person gave, bias probes, drift, and a
  trust label on every judged score.
- **What caused it.** The prompt, model, tools, input and settings that changed next to each
  regression, or "nothing on your side changed".
- **From production back to tests.** Flagged traces and reviewed conversations become candidate
  test cases. Before there's traffic, `assay synth` generates queries from dimensions you define,
  kept apart from production.

`assay connect` attaches Assay to an existing app (through its database, a few lines of code, or a
proposed test per model call), with any model provider. `assay demo && assay serve` shows it on
synthetic data.

**Docs:** [all documentation](docs/README.md) · [testing](docs/testing.md) · [CI](docs/ci.md) ·
[agents](docs/agents.md) · [setup](docs/setup.md) · [Python SDK](sdk/python/README.md)

Development: `pip install -e ".[dev]" && pytest`.
