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
