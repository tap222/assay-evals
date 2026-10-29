[Assay](../README.md) › [Documentation](README.md)

# The change report: `assay changes`

The question a pull request needs answered: what did this change actually do, and is it safe to
merge? `assay changes` answers with one verdict and at most three things to look at, against the
base branch. It works on any repository; with Assay tests, their results are part of it.

```bash
pytest --assay --cov=myapp --cov-report=json     # or: assay test, with jest --coverage
assay changes --base main
```

```
NOT SAFE TO MERGE: 1 THING BROKE OR STOPPED BEING CHECKED
  1. refund_flow: refund ran before its approval (HIGH · changed: prompt refund@3 → refund@4)
  2. not run by any test: app/client.py: upload_run, lines 120-126 (outside calls)
  3. .github/workflows/release.yml: doesn't run on pull requests, only on push, tags: its first run is after the merge
Also: 2 more untested changes, 1 more new write to check; 1 flaky, as before. `assay changes --all` lists everything.
(against main, 6 changed files; tests: run 20261018-1 vs each case's last passing run; 41 of 50 changed lines ran in a test)
```

The verdict is one of three:

- **Not safe to merge:** a test regressed, or a test was weakened so it checks less.
- **Check N things before merging:** nothing broke, but something wasn't checked or looks wrong.
- **Nothing to check:** every changed line ran in a test, and CI used every deploy change.

Everything found is ranked into one list, and the first three are shown:

1. **Regressed:** a case that passed on its baseline and fails now, from the last `assay test`
   or `pytest --assay` run, with what changed next to it (a prompt, a model, a tool's definition).
2. **Weakened:** an assertion loosened or removed ([the guard](security.md)).
3. **Test runner configuration changes:** changes to files that decide how tests run (`conftest.py`,
   `pytest.ini`, `setup.cfg`, `tox.ini`, Jest/Vitest configs, `pyproject.toml` pytest options, `package.json` test scripts,
   or CI test workflow commands), with changed lines for confirmation.
4. **Tests reached something real:** commands started (`scp`, `rsync`, `psql`), non-local socket connections,
   or file writes outside the repository and temp folders that the base branch's tests didn't touch.
5. **New writes to check:** a placeholder saved, money left empty, a sent value that never reached
   a row ([database writes](../sdk/js/README.md#new-writes-checked)).
6. **Changed lines no test ran**, money, messages, logins, database writes and outside calls first.
7. **Deploy and dependency changes no CI job checked.**
8. **Changed, still passing:** a case that takes another path now, though its checks pass.
9. **The rest:** untested lines of lower risk, and expected values the change rewrote.

One change seen in many cases is one item: "refund ran before its approval (refund_a, and 4 more
cases)". Flaky tests are a count, not items: they did what they did before. Test results from a run on
another commit are left out, and the report says so: results from other code would mislead.
`--format json` gives the whole report for a coding agent to act on, and `--format markdown` the
pull-request version. Nothing here fails the run: `assay test` still decides that.

## Marking an item intended

Some items are the change you meant: a release workflow you'll run by hand, a debug branch no
test needs. Mark one once, with a reason, and it's left out of later reports:

```bash
assay changes --all                                         # every item, numbered
assay changes --intended 4 --reason "release workflow, run by hand before tagging"
```

The mark goes in `assay.intended.toml` next to `assay.toml`. Commit it with the change, so CI
sees it and a reviewer reads the reason in the pull request. Each mark matches only what was
flagged: the same unrun lines, the same file contents, the same behavior change. If that changes
again, the item comes back. Regressions and weakened tests can't be marked here: `assay accept`,
`assay ack` and the policy label are for those.

Track which flags get marked intended and identify kinds people have learned to ignore:

```bash
assay changes --intended-stats
```

## Changed lines no test ran

The changed lines come from `git diff` against the merge base with the base branch, uncommitted
changes and new files included. Whether a test ran them comes from the coverage your test run
left, the first of these that's found (or `--coverage FILE`):

| File | Written by |
|---|---|
| `coverage.json` | coverage.py: `pytest --cov --cov-report=json`, or `coverage json` |
| `.coverage` | coverage.py's data file (needs `coverage` installed) |
| `coverage/coverage-final.json` | Jest or Vitest with `--coverage` (istanbul) |
| `coverage.out`, `cover.out` | Go: `go test -coverprofile=coverage.out ./...` (each module's path in `go.mod` maps it to the repository) |
| `coverage/lcov.info`, `lcov.info` | lcov, which Rust, Ruby, Java and most other languages can write |

Lines coverage doesn't count, like comments and blank lines, are left out, and so are test
files. Unrun lines are grouped by the function they're in. A file no test ran at all is one item,
and so is a changed file of a measured language that coverage never saw.

Each item is ranked by what its unrun lines do: **money** (charges, refunds, prices) first, then
**messages** (email, SMS, webhooks), **logins** (passwords, access tokens, permissions),
**database writes** and **outside calls** (HTTP), then the rest. At most three items are shown;
`--all` lists every one.

Why lines and not functions: replaying this repository's history, a changed function no test ran
would have flagged 1 of 9 bugs later fixed by hand. The upload bug sat in a function tests did run,
just not on the line that sent the request ([P0 findings](p0-findings.md)).

## Deploy and dependency changes no CI job checked

Four of those nine bugs were in files coverage can't see: a requirements file, an ignore file, a
workflow and a version pin. A change to one of these is listed only when no job in a workflow that
runs on pull requests used it:

| Changed | Counts as checked when a pull-request job |
|---|---|
| A workflow | is that workflow (a release workflow that runs on tags isn't) |
| `action.yml` | uses the action (`uses: ./`) |
| `requirements*.txt`, `pyproject.toml`, `setup.py`, lock files | installs or builds from it (`pip install -r`, `pip install .`, `uv sync`, `poetry install`, `python -m build`) |
| `package.json`, lock files, `.npmrc`, `.nvmrc` | installs or runs in its directory (`npm ci`, `yarn`, `pnpm install`) |
| `Dockerfile`, `.dockerignore`, compose files | builds an image |
| `vercel.json`, `.vercelignore`, `netlify.toml`, `fly.toml`, `Procfile`, `.python-version` | deploys with Vercel, Netlify, Fly or Render |

A manifest whose changed lines are only its version or description isn't listed, and neither is
an `action.yml` whose inputs changed but not its steps. Files under `examples/`, `docs/` and
`tests/` aren't deployed, so they're skipped. A preview deploy run by a hosting provider's own
GitHub app, outside the workflows, isn't seen.

Replayed over this repository's history, the rule lists all four of those bugs. Of the 52 changes
that touched such a file, it lists 26. Most are from before pull requests ran CI here, when nothing
checked these files. Since then it lists 7 changes, 2 of them the bugs; most of the others change
release workflows, which only run after a merge.

## In the pull request

With the GitHub Action, `changes: true` opens the PR comment with the change report, above the
test run's details, and adds it to the job summary.
Run your tests under coverage in `command`, and check out with `fetch-depth: 0` so the merge
base is known:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: tap222/assay-evals@v1
  with:
    command: pytest --assay --cov=myapp --cov-report=json
    install: pip install assay-server pytest pytest-cov
    changes: true
```

For any other CI, `assay changes --format markdown` prints the same report.
