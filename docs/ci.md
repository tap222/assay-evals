[Assay](../README.md) › [Documentation](README.md)

# In CI: a verdict on every pull request

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

The action compares each test with its last passing run *on the default branch*: pushes to
`main` save `.assay/`'s baselines to the Actions cache, and a pull request restores its base
branch's latest. It then posts one comment on the PR, and edits that comment on later pushes:

```
## AI regression detected
**48 cases** · 45 passed · 2 regressed · 1 flaky · 3 improved

- `test_refund_policy` → Safety: Called refund before get_order; Tool usage: …
```

The same summary goes on the job's summary page, and is in `.assay/summary.md` after every
run. Its inputs are `command`, `install` (default `pip install assay-server pytest`),
`timeout`, `comment`, `fail-on-inconclusive`, `token`, and `trusted-policy`, `config` and
`policy-label` (see Security, below). Its `result` output is `passed`, `regressed`,
`inconclusive` or `error`. A fork's PR gets a read-only token, so there the comment is
skipped with a warning and the job doesn't fail over it. Outside the action,
`assay pr-comment` posts the summary itself (it needs `GITHUB_TOKEN`).

- **Rerun what failed:** `pytest --assay --assay-rerun failed` (or `assay test --failed`)
  runs only the tests that didn't pass last time: regressions, new failures, flaky tests,
  tests that need more attempts to tell, tests that couldn't be judged, known failures, and acknowledged ones. If none are left, that counts as a pass.
- **Dropped tests fail the PR.** A test the default branch runs that a pull request no longer
  runs (deleted, skipped, or filtered out of the command) counts as loosening the checks, until
  the `assay-policy-change` label accepts it ([Security](security.md)). A rerun of what failed
  doesn't count.
- **Timeouts: a hung run doesn't hang CI.** An async evaluation that runs every case and then
  never returns shouldn't keep a job running until GitHub kills it at six hours.
  `timeout = 900` in `assay.toml` (or `assay test --timeout 900`, `pytest --assay
  --assay-timeout 900`, the `ASSAY_TIMEOUT` variable, or the action's `timeout` input) stops a
  run that takes longer, along with everything it started. What it recorded is still judged,
  and the PR comment still gets written. A test that never returned is reported as never
  finished and fails the run. If every case had already finished, the report says the process
  hung on the way out, and the result stands. `0` means no limit.
- **A process that won't exit:** with `pytest --assay`, a session that finished but whose
  process is still running 30 seconds later (a thread or event loop that never stopped) exits
  with the session's own result (`ASSAY_EXIT_GRACE` sets the wait, `0` turns it off).

## Nightly: failures with nothing changed on your side

The models you call change underneath you. A provider updates a model under its name, or tests
a variant on some of your traffic, and what passed last week fails this week with no change of
yours. A pull request never sees that; a scheduled run does:

```yaml
on:
  schedule: [{cron: "0 5 * * *"}]   # every morning, on the default branch
jobs:
  assay:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: tap222/assay-evals@main
```

Every `assay test` run records what it ran: the commit, a digest of uncommitted changes, of
`assay.toml`, and of the prompt and model versions its results record. When a run regresses and
its cases' baselines ran exactly the same, the report says so:

```
Nothing on your side changed since these cases' baseline: the same commit (3f9a2c1), no uncommitted changes, the same assay.toml and the same prompt versions. The model underneath changed, or a service a tool calls did.
```

That's the difference between "we broke it" and "it broke under us", and it decides who looks
first.
