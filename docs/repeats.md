[Documentation](README.md)

# Repeated attempts: chance or a regression

Run an agent twice on the same task, with the same code, model and prompt, and it can take a
different path and end somewhere else. A pass rate that moved on a pull request isn't evidence
on its own: an unchanged agent moves too. With `repeat = 8` (or `assay test --repeat 8`), each
case runs several times and Assay decides from the attempts which of three things happened:

| What happened | Example | Result | Exit |
|---|---|---|---|
| Chance: what an unchanged agent does | one task of five, 8/8 → 7/8 | flaky, not blocking | 0 |
| Could be worse, too few attempts to tell | one task of five, 8/8 → 4/8 | inconclusive: rerun with more attempts | 3 |
| A regression | one task, 8/8 → 0/8 | failed | 1 |

`pytest --assay` exits 6 for inconclusive rather than 3. The GitHub Action fails the job on
inconclusive unless `fail-on-inconclusive: false`: too little data blocks a merge, but it's
never reported as a regression.

## Two questions, two gates

A suite average answers one question and hides another. One task going 8/8 → 0/8 among seven
moves the average by 14 points, which a small suite's interval can't tell from noise. The
capability is still gone. So there are two gates, and either can fail the run:

- **Did the suite get broadly worse?** The change in pass rate, per check, with a paired t
  interval over the checks. The check is the unit: 50 attempts of one task say a lot about that
  task and nothing about the others. It fails when even the optimistic end of the interval is
  lower than `tolerance` (default 0.01) allows.
- **Did something that worked stop working?** Each check's pass rate against its baseline's: a
  one-sided Fisher exact test, corrected for the number of checks (Benjamini–Hochberg at 5%).
  Or the check collapsed: every attempt passed before and every one fails now, three or more
  each. Fisher's test is cautious with few attempts, and a capability that's gone shouldn't
  wait on it.

## Why the number of checks matters

Watch 50 tasks and one of them will dip on almost every run by chance. Without a correction,
a rule like "rerun anything that dropped" fires on nearly every unchanged pull request, and
people learn to click re-run until it goes green. So "could be worse" is corrected too (at
25%), and the correction counts every check that could have moved, not only the ones that did.

Measured on simulated unchanged pull requests (8 attempts per check, per-check pass rates
between 60% and 100%, 400 runs each):

| Tasks | Advance | Needs reruns | Blocked |
|---|---|---|---|
| 5 | 97% | 2% | 1% |
| 10 | 96% | 2% | 2% |
| 50 | 98% | 1% | 2% |

With one task collapsed from 8/8 to 0/8, the run is blocked every time, among 7 or 50 tasks.

## What it can't tell you

- **Moderate drops in a small suite.** A 30-point drop across all of seven tasks at 8 attempts
  passes about one run in nine: seven tasks can't rule out chance. At 50 tasks it's caught
  every time. More cases help more than more attempts.
- **One attempt.** With `repeat = 1` there's nothing to tell chance by, so a case that passed
  before and fails now is a regression. Repeat the cases that can vary.
- **"Not proven worse" isn't "proven fine".** When the interval still reaches past the
  tolerance, the reasons say so ("could be lower by up to 6%"), without blocking.

## Is it the AI or the judge?

A check graded by an LLM judge can vary for two reasons: the AI answered differently, or the
judge gave a similar answer a different verdict. Repeated attempts alone can't separate them.
Ask the judge again about the same answer, with `evaluate(..., rejudge=3)`, and Assay can:

```
? 1 check with an unstable judge: the same answer, judged again, got another verdict
  test_refund_tone  helpful  100% → 50%, verdict flipped on 3 of 8 answers judged again (could be worse)
  More attempts of the AI won't settle these. Tighten the rubric, check it deterministically, or `assay calibrate` the judge.
```

A check is **judge unstable** when it would be flaky or need reruns, and the judge split on at
least one answer it was asked about more than once. Each attempt's result is the majority of
its judgements, so a judge that wavers on a borderline answer moves the pass rate less too.

| What happened | Result | Exit |
|---|---|---|
| Varies, no worse than chance, and the judge split | judge unstable, not blocking | 0 |
| Could be worse, and the judge split | judge unstable, inconclusive: fix the judge, then rerun | 3 (6 for pytest) |
| Got worse beyond chance, the judge split or not | a regression | 1 |

Nothing is loosened: a check that could be worse is still inconclusive, and a collapse (8/8 →
0/8) is still a regression. What changes is the advice. "Rerun with more attempts" doesn't help
when the judge is what varies. A judge that agreed with itself every time leaves the check
flaky, or needing reruns, as before. `assay calibrate` measures a judge's consistency on a
labeled set ahead of time; `rejudge` measures it on this run's answers.

## Judging a saved run again

`rejudge=` asks the judge again during the run. `assay rejudge` asks it again afterwards, about the
answers a run already saved, with no calls to the AI:

```
assay rejudge --judge evals/judges.py:helpful --times 3
Re-judged 24 saved answers from t-20261017-101500-120 with evals/judges.py:helpful, 3 times each. No calls to the AI.

Saved verdicts came from claude-haiku-5-5 / helpful@2.
Agrees with the saved verdicts  21/24 (88%), Cohen's kappa 0.71
  pass → fail 2   fail → pass 1
Splits with itself  2 of 24 answers, asked 3 times
```

- **The same judge, frozen** (model and prompt), measures its own noise. A model swap that moved
  fewer verdicts than the judge moves on its own hasn't shown anything.
- **A new judge or rubric** is tried on the same answers before switching to it. Cohen's kappa
  discounts the agreement two judges get by chance; `--min-agreement 0.9` exits 1 below that, for
  CI.
- **What it needs:** checks recorded with what the judge saw, `evaluate(judge, ..., inputs={"question":
  q, "answer": a})`. The judge is called as `judge(**inputs)`. `--run` picks a run (default: the
  last), `--field` one check, `--format json` the whole result.
- Answers it couldn't give a verdict on are counted apart, never as flips.

## Code checks first, then judged checks

When a run has both, the report gives them a line each:

```
Code checks    40/40 passing
Judged checks  12/15 passing · 3 failing
Code checks held: what moved is judged. Check the judge before the AI (`assay rejudge`).
```

A code check (an exact value, a tool that must or mustn't be called, a schema) has no judge's
noise in it, so after a model swap it's the first signal to read. A check counts as judged when it
was recorded with `judge_model` or `judge_prompt`, or its evaluator's name says judge. When code
checks moved, the note says the AI's behavior changed, whatever the judges say.

## Cases that broke, and cases that were fixed

The run-level change is a paired interval on pass rates. The report also counts each case in both
runs as one pass or fail, and how many went each way:

```
Paired cases
  40 cases in both runs: 6 pass → fail, 1 fail → pass (exact McNemar p = 0.062, within chance; ...)
```

A case passes when each of its checks passes on most of its attempts (more than half: a tie isn't
most). The exact McNemar test asks whether more broke than were fixed beyond chance; cases that
didn't change say nothing either way. It's shown, not a gate: the two gates above decide the run.

## More attempts only where they're needed

"Inconclusive: rerun with more attempts" leaves the rerun to you, and a rerun repeats every case,
including the ones that were already clear. `assay test --rerun-unsure` (or `rerun_unsure = true`
under `[test]`) does it in the same run, for the cases that need it:

```
assay test --repeat 6 --rerun-unsure
1 case could be worse, too few attempts to tell: 6 more attempts of it (12 each).
5 cases · 6 attempts each, up to 12 for the 1 case that needed more · compared with ...
```

After the first attempts, only the cases that could be worse run again, a batch (`repeat`
attempts) at a time. Each one stops at the first batch where it's clear: chance, or worse. Clear
cases never run again, so the spend goes to the ambiguous ones.

- **Peeking is paid for.** Every look at the same check is another chance of a false alarm, so
  the bar for "got worse" is split across the looks (5% after one, 2.5% after two, ...), on top
  of the correction for the number of checks.
- **It never passes a case because it ran out.** A check that could be worse stops asking for
  reruns at 20 attempts. The loop stops at 19, so a case that's still unclear stays
  inconclusive, exit 3.
- **The baseline limits it.** More attempts now can't make up for few attempts before: against
  a 6-attempt baseline, 50% can't be told from chance. Give the baseline more attempts
  (`assay test --repeat 16` once) when cases often end inconclusive.
- **Judge-unstable checks aren't rerun.** More attempts of the AI won't settle a judge that
  changes its verdict on the same answer.

With pytest, the cases to run come through `ASSAY_RERUN_CASES` and the plugin picks them. Another
command gets the same variable, a JSON list of case names; one that ignores it runs every case
again, which costs more but stays correct.

## Two cases that aren't left to chance

- **A drop that follows the model.** If a check fails on exactly the model a call was routed
  to and passes on the other, that's a cause, not chance, however few the attempts: a
  regression.
- **The baseline doesn't drift down.** Each case's baseline is its last passing run, but a
  case whose pass rate dropped within chance keeps its old one. Otherwise a few noisy runs in a
  row could walk it from 8/8 to 4/8 without ever failing.

The release call on the server (`GET /v1/evals/runs/{run}/stability`) uses the same states:
see [Release gates](release-gates.md#nondeterminism-flaky-checks-and-rerun-instead-of-block).
The verdict each check gets is in [Results you can trust](verdicts.md).
