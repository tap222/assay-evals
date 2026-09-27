[Assay](../README.md) › [Documentation](README.md)

# Test your AI app locally: it's pytest (no server, no account)

Your AI tests are pytest tests: `tests/ai/test_support.py`, `test_tool_selection.py`,
`test_security.py`, `test_document_extraction.py`. Each test records what the agent did, fails
when the run breaks a rule or misses what the test expects, and, with `--assay`, is compared
with its own last passing run:

```bash
pip install assay-server pytest     # assay-server brings the SDK, assay-evals, and its pytest plugin
assay init                          # assay.toml, and tests/ai/test_support.py with an example agent
pytest --assay tests/ai             # exit code 1 when something regressed
```

```
    def test_no_refund_before_delivery(assay_case):
        assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-18"}}], answer="hasn't arrived")
        support_agent(assay_case, "Can I get a refund for O-18?", "O-18")
>       assert_not_called(assay_case, "refund")
E       AssertionError: refund shouldn't have been called, but was: refund(order_id='O-18', amount=12.0) (step 1).

==================================== assay =====================================
2 cases · 1 attempt each · compared with each case's last passing run (2 of 2 cases have one, from 1 run)

✗ tests/ai/test_support.py  1/2

⚠ 1 case regressed (3 checks)

1. tests/ai/test_support.py::test_no_refund_before_delivery  Answer, Your asserts, Tool usage
   Wrong tool: Called refund(order_id='O-18', amount=12.0), which the reference doesn't expect.

Failed.
```

Plain `pytest` works too: red or green, with Assay's checks. `--assay` adds the comparison
with each test's last passing run, and decides the exit code: 0 nothing got worse, 1 a
regression, 6 inconclusive (nothing got worse, but some results couldn't be judged, or some
checks could be worse and need more attempts to tell). A test
that failed in its baseline too doesn't fail the session; a failing test that doesn't take
the fixture does, as always. It works with `pytest -n` (xdist) and `-k`: running a subset only
moves the baselines of the tests it ran. `--assay-baseline RUN` compares with one run instead,
and `--assay-upload` sends the run to a server.

`assay test` does the same from outside pytest (or around any command that records with the
SDK), and adds `--repeat N` for flaky cases and `--junit report.xml` for CI. Its exit codes are
0, 1, 2 (setup problem) and 3 (inconclusive).

- **Your tests:** with pytest, take the `assay_case` fixture (it comes with `assay-evals`)
  and set `command = "pytest -q tests/ai"`. Each test is then a case, and pytest's own
  pass/fail is the answer. With `assay-server` installed, a test also fails when its run
  fails Assay's checks: its expectations, and the contracts and PII rules in `assay.toml`.
  So `pytest` alone goes red on an unsafe tool call, with the reason (`[pytest] checks =
  false` turns that off). `assay_sdk.testing` has assertions for the test body:

  ```python
  from assay_sdk.testing import assert_called, assert_not_called, assert_max_steps

  def test_refund(assay_case):
      reply = my_agent("Refund O-17", run=assay_case)   # records steps: run.call, run.answer, ...
      assert_called(assay_case, "get_order", order_id="O-17")
      assert_not_called(assay_case, "delete_order")
      assert_max_steps(assay_case, 6)
      assert "27.61" in reply
  ```

  They fail with what the run did, e.g. "Expected a call to get_order(order_id='O-17');
  get_order was called with get_order(order_id='O-18')". There are also
  `assert_called_before`, `assert_answer_contains` and `assert_no_pii`.

  Without pytest, record each case with `assay.run(..., test="<case>")` and
  `run.expect(...)`. For pipelines, send field results with
  `run.check("invoice_date", "pass" | "fail", expected=..., actual=...)`.
- **Safety rules:** go in `assay.toml` as path contracts, e.g. `never delete_order`, or
  `refund only_after get_order`. Every agent run is checked against them.
- **PII:** personal data (email, card, IBAN, SSN, phone) in a tool's arguments fails the PII
  check. A tool that needs it is allowed it in `assay.toml`, under `[pii]`:
  `allow = { send_receipt = ["email"] }`.
- **Baseline:** kept per case: each case's last passing run. A failing run never becomes
  a baseline, and running a subset (`assay test -- pytest tests/ai/test_security.py`) only
  moves the baselines of the cases it ran. If your suite has known failures, `assay accept`
  makes the latest run their baseline, and acknowledges its failures for two weeks
  ([below](#acknowledging-a-failure)): later runs fail only on what got worse. A case whose pass
  rate dropped within chance passes but keeps its old baseline, so a few noisy runs in a row
  can't walk it from 8/8 down to 4/8.
- **Report:** with pytest, the report opens with each test file (`✗ tests/ai/test_tools.py
  9/10`), then each check. `assay test --junit report.xml` writes JUnit XML for CI: a
  regression is a failure, a known failure is skipped, and a flaky test passes.
- **Flakiness:** `repeat = 3` (or `assay test --repeat 3`) runs each case several times, and
  each check is judged by its pass rate against its baseline's, corrected for how many checks
  there are. 8/8 → 7/8 is what an unchanged agent does: flaky, not blocking. A drop that could be
  real but can't be told yet (8/8 → 4/8 on one of five tasks) is inconclusive, exit 3, with the
  number of attempts that would settle it. A drop beyond chance, or a check that passed every
  attempt before and fails every one now (three or more each), is a regression, exit 1. A drop
  that follows the model a call was routed to is a regression too, however few the attempts.
  With one attempt there's nothing to tell chance by: a pass that became a failure is a
  regression.
- **Where things live:** everything goes in `.assay/` (recordings, the store, the baseline),
  which ignores itself in git. `assay test -- pytest -q tests/ai` overrides the command.

## Can a judged number be trusted?

Every judged check in the report says whether its judge was calibrated against people, how well,
and whether for the judge that scored this run (see [Judge calibration](calibration.md#every-judged-number-says-whether-it-can-be-trusted)):

```
Judges
  helpful      calibrated 3 days ago: Spearman 0.82 on 60 items (claude-opus-5)
  consistency  not calibrated: its scores haven't been checked against people (`assay calibrate`)
```

Two more warnings sit next to judged scores:

- **The same family.** When the judge and the model that wrote the answers are from the same
  family (Anthropic judging Claude, OpenAI judging GPT), the label says so: judges favor their
  own family.
- **A score that rose with the surface.** When a judged score rose from the baseline while the
  answers got notably longer, more formatted or more cited, the report says part of the rise may
  be the judge's taste, not quality, and points to calibration's bias probes. It doesn't fail the
  run.

## Noise floors and coin flips

A judged case can score 5/5 on one run and 2/5 on the next with nothing changed. Comparing only
pass rates misses a real drop that still passes, and alarms on noise that crosses the threshold.
So each judged check has a **noise floor**: the scores it showed in its last 10 runs where it
passed every attempt (or its baseline's). A drop counts only when it clears that floor:

```
⚠ 1 check still passing, but scored below its noise floor
  tests/ai/test_support.py::test_refund  helpful: 0.61, below the 0.8–0.9 it scores when nothing is wrong (6 scores)

· 2 scores lower, within the noise (not failing): test_warranty 0.83 in 0.8–0.9, test_returns 4 in 4–5
```

A score below the floor fails the run like any regression. One inside it is listed, dim, so
nobody chases it. With fewer than 3 scores there's no floor yet: repeat the case
(`assay test --repeat 5`) to measure one, which is also the way to try a case before it joins
the suite.

**Coin flips** are cases that give different answers from the same system: attempts in one run
that split between passing and failing, or whose scores span half the check's range. A pass or a
fail from them says little:

```
~ 1 coin flip: different answers from the same system, so a pass or a fail says little
  tests/ai/test_support.py::test_tone  helpful: passed 2/5, scores 0.2–0.9
  Tighten its rubric or its expected output, or check it deterministically if it can be.
```

They don't fail the run. They're the cases worth rewriting: a tighter rubric, a fixed output
format, or a deterministic check in place of the judge.

## When the judge or the model changes

A comparison with the baseline assumes only your AI changed. Two things break that.

**A new judge.** Moving the judge to another model, or editing its rubric, changes the measure.
A drop could then be the judge, not your app. Each result records which judge it was: its model
(from an `assay_sdk.Judge` answer on its own, or `judge_model=` on `evaluate()` and
`run.check()`) and its prompt (`judge_prompt="helpful@3"`). The built-in judge records its model
and a fingerprint of its rubric. A failing check whose judge differs from its baseline's isn't
reported as a regression:

```
? 3 failing checks judged by a different judge than their baseline: not compared, since a drop could be the judge, not the AI
  claude-opus-5 · helpful@3 → claude-fable-5-1 · helpful@3: q1 Helpful, q7 Helpful, q9 Helpful
  They're the baseline from this run on. `assay calibrate` with the new judge checks it agrees with people first.
```

The run's results then become the baseline, so the next run compares new judge with new judge.
[`assay calibrate`](calibration.md) is how to check the new judge before trusting it.

**Routing.** When more than one model serves (a router, a fallback, an A/B), each attempt is
tied to the model its calls went to. The report then shows how each model did, and a failure that
lines up with the model says so, instead of looking like chance:

```
By model
  claude-opus-5  38/40 cases
  gpt-5-mini     30/40 cases

1. refund  Answer
   fails only on gpt-5-mini (0/2), passes on claude-opus-5 (2/2): the model it was routed to, not chance
```

Otherwise, each model's pass rate is shown next to the baseline's (`claude-opus-5 passed 3/3 (3/3
before) · gpt-5-mini passed 0/2 (not in the baseline)`). A flaky check whose passes and failures
follow the model is explained the same way, and so is `assay diff`. Routing is part of the app,
so a regression that comes from routing still fails the run; the report says where it came from.

## Acknowledging a failure

"I already know about this one" is an acknowledgement, not a mute. It covers one check of one
case, it ends on its own, and it speaks up again as soon as the failure has something new to
say.

```
assay ack test_refund_policy consistency --reason "judge disagrees, #412" --for 14d
Acknowledged tests/ai/test_support.py::test_refund_policy Consistency, until 2026-10-10: quiet while its
score stays within 2–3 (from 6 scores) and it fails the way it does now (Reasoning · low score).
Written to assay.acks.toml: commit it, so CI and reviewers see it.
```

With no check named, every check the case fails is acknowledged. `behavior.cost_usd`,
`behavior.context_tokens`, `behavior.retrieved_context` and the other behavior numbers can be
acknowledged too.

**What it remembers is a band, not a score.** A judge that scores a case 2 or 3 on its own
would wake anyone keyed on "3". So the acknowledgement stores the case's score over its last
10 runs (lowest to highest), how many attempts passed, and how it failed: the kind of check and
the mechanism (`Output quality · Wrong answer`, `Security · Personal data leaked (email)`,
`Your asserts · AssertionError`), never the reason's raw text.

**Quiet until worse.** While it holds, the check doesn't fail the run and is one line in the
report (`· 3 checks acknowledged (2 cases), quiet until worse`). It fails the run again, saying
why, when:

- its score falls below the band. A dip inside the band is the noise it already showed;
- fewer attempts pass than did, beyond chance;
- it fails a different way: the same case going from a wrong answer to an unsafe action is a
  new failure, even at the same score. For a judge's score, the kind comes from the judge: a
  `category` in its verdict (`evaluate()` reads it), `run.check(..., category="grounding")`, or
  the built-in judge's (grounding, contradiction, incomplete, policy_refusal, unworkable,
  inefficient). So a grounding miss that becomes a policy refusal wakes it at the same score. A
  judge that names no category is "low score", and only the band wakes it;
- for a behavior number, it goes past the highest value it showed, by more than the metric's
  minimum.

A different check failing on the same case was never acknowledged, so it fails as usual. A
flaky check is still reported as flaky.

**It ends.** At `--for` (14 days by default, 90 at most: nothing is acknowledged for ever), the
report says `Acknowledgement ended` and the failure is reported as it would be without one. When
the check passes in a later run, the acknowledgement is spent, so a failure that comes back is
news. `assay acks` lists each one: quiet, worse, expiring soon, or ended. `assay acks --prune`
removes the ended ones.

**It's in the repository.** `assay.acks.toml` sits next to `assay.toml`, so CI applies it and a
reviewer sees who acknowledged what, why, and until when. In CI on a pull request, an
acknowledgement the PR adds or extends counts as loosening the checks. It's listed, and held
back unless the `assay-policy-change` label accepts it, so a PR can't acknowledge away its own
regression.
