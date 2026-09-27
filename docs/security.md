[Assay](../README.md) › [Documentation](README.md)

# Security: the agent's, and the pipeline's

**What the checks catch in the agent:**

| Check | Fails when the agent |
|---|---|
| **Safety** (contracts) | calls a tool it must never call, or out of order, or with arguments a rule forbids (`never delete_order`, `refund only_after get_order`, `where`) |
| **Approval** | takes an action that needs sign-off without it (`requires_approval`, `must_get_approval_before`) |
| **Claims** | says it did something its own record doesn't support: "refunded" with no successful refund call, or with the order's recorded status still "delivered" (`claim` contracts) |
| **PII** | sends personal data to a tool that isn't allowed it, or says personal data in its answer that the request didn't give (someone else's email, card or IBAN; the user's own is fine). `[pii] allow_in_answer` lists kinds an answer may carry. It needs the request recorded (`input=`) |
| **Prompt injection** | obeys instructions that reached it through a tool or resource result ("ignore previous instructions", "you are now", "call delete_account"): after the injected text it calls a tool the text named, breaks a contract, or makes a call its case or plan didn't expect. An agent that reads the text and carries on passes |

**What a pull request can and can't do to the evaluation.** The tests are code, and CI runs
them, as with any test suite. What Assay adds:

- **A PR can't loosen the checks that judge it.** On a pull request, the action reads the base
  branch's `assay.toml` (`trusted-policy`, on by default), and the contracts, `[pii]`,
  `[behavior]`, `[pytest] checks` and `tolerance` are held to it. The PR can tighten them: an
  added contract applies at once. Loosening them (removing a contract, turning a check off,
  allowing a tool more personal data, raising a limit, acknowledging a failure in
  `assay.acks.toml` or extending an acknowledgement) is listed, isn't applied, and fails the
  run. The PR comment says so: "Checks weakened: this PR loosens the checks that judge it". A
  maintainer accepts the change with the `assay-policy-change` label (add `labeled` to the
  workflow's `pull_request` types, so labelling re-runs it). Outside the action, set
  `ASSAY_POLICY` to the trusted `assay.toml` and `ASSAY_POLICY_CHANGE=accepted` to accept.
- **A PR can't drop the test its change breaks.** Deleting a failing test, skipping it, or
  filtering it out of the command (`-k "not refund"`) leaves a suite that passes. So the default
  branch's last run records which cases make up the suite, and on a pull request a case of it
  that didn't run counts as loosening the checks: "stops running 1 test case the base branch
  runs: test_cancel". It fails the run until the label accepts it, like removing a contract. A
  rerun of what failed (`--assay-rerun failed`) leaves the rest out on purpose and isn't counted.
  Outside a pull request it's one line, not a failure: running a subset is how you work on one
  file.
- **Who did what.** With SSO, people sign in with the company's identity provider and get a role
  from their groups; every change, sign-in and refusal is in the audit log
  ([API and authentication](api.md#single-sign-on-people-and-roles)).
- **No code in the config.** `assay.toml` is TOML, and contracts are rules, not code: no
  inline scripts, no expressions, no regular expressions.
- **A fork's PR gets nothing to steal.** The action runs on `pull_request`, where a fork's PR
  gets a read-only token and no secrets (so no `ANTHROPIC_API_KEY` either). The action stops
  under `pull_request_target`, which would run the PR's code with write access and secrets.
- **Baselines can't be poisoned.** Only pushes to the default branch save them; a PR restores.
- **The PR comment is text.** Test names and failure messages come from the PR, so they go in
  escaped: no `@`-mentions, links, images or HTML.
- **The judge doesn't take personal data out.** Traces are redacted before they're sent to the
  model API (`[judge] redact`, `ASSAY_JUDGE_REDACT`), and the trace is marked as data, so
  instructions inside it don't steer the score.

What it doesn't catch: a judge whose code the PR changed. A changed judge is noticed when its
results say which judge they were: the model (read from a `Judge` answer) and `judge_prompt`
("rubric@3"). A PR that edits a rubric and keeps its version is judged by the new rubric
without a word. Version your rubrics, and review changes to judge code (a `CODEOWNERS` entry for
`evals/` does it). The same goes for the workflow file itself: a PR can edit
`.github/workflows/`, so make Assay's job a required check in branch protection.

What it doesn't do: sandbox the tests. They run on the CI runner, like any test suite; run
untrusted code only where a read-only token and no secrets are all there is to reach.
