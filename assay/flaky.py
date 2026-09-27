"""Nondeterminism: pass rates per check, flakiness, and when to rerun instead of block.

Run the same case five times and get PASS PASS FAIL PASS PASS. Is that a
regression? Four of five is consistent with a true pass rate anywhere from
about 28% to 99%, so it depends on how reliably the case passed before. This
module answers it per check (a case, field and evaluator), from the attempts
in a run and in its baseline:

  got_worse     the pass rate dropped beyond chance (one-sided Fisher exact
                test, Benjamini-Hochberg across all checks, so 5,000 checks
                don't produce 250 false alarms), or the check collapsed:
                every attempt passed before and every one fails now, three
                or more each. Fisher's test is cautious with few attempts,
                and a capability that's gone shouldn't wait on it
  needs_reruns  plausibly worse, but too few attempts to tell: rerun, don't block.
                Corrected for the number of checks too: in a suite of 50, some
                check drops a little by chance on almost every run, and 8/8 →
                7/8 is no evidence of anything
  flaky         both outcomes seen, and no worse than chance
  improved      the pass rate rose beyond chance
  stable_pass / stable_fail
  errored       every attempt errored, so nothing was judged

A flaky check also says what varies: the output itself (the model or
pipeline is nondeterministic), only the verdict on the same output (the
evaluator), or attempts that errored (infrastructure).

The run-level decision uses pass rates, not single outcomes: a flaky check
counts as 0.8, not as a pass one run and a fail the next. The noise that
comes from attempts is estimated from the attempts themselves, so no
separate identical baseline runs are needed.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import fmean, stdev
from typing import Dict, List, Optional, Tuple

MAX_ATTEMPTS = 20  # stop asking for reruns past this many attempts per check
Q_LEVEL = 0.05  # false-discovery rate for "got worse" / "improved"
PLAUSIBLE = 0.25  # a drop with q up to this is worth rerunning rather than ignoring
COLLAPSE = 3  # attempts on each side for all-passed → all-failing to count as got worse on its own


# ---------- exact statistics, no dependencies ----------

def _binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0 if k == 0 else 0.0
    if p >= 1:
        return 1.0 if k == n else 0.0
    return math.exp(math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                    + k * math.log(p) + (n - k) * math.log1p(-p))


def _binom_cdf(k: int, n: int, p: float) -> float:
    return min(1.0, sum(_binom_pmf(i, n, p) for i in range(0, k + 1)))


def _solve(f, target: float) -> float:
    """p in [0, 1] with f(p) = target, for f decreasing in p."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def interval(passes: int, n: int, alpha: float = 0.05) -> Tuple[Optional[float], Optional[float]]:
    """Exact (Clopper-Pearson) interval for a pass rate."""
    if n == 0:
        return None, None
    # P(X >= passes | lo) = alpha/2, i.e. P(X <= passes-1 | lo) = 1 - alpha/2; P(X <= passes | hi) = alpha/2.
    lo = 0.0 if passes == 0 else _solve(lambda p: _binom_cdf(passes - 1, n, p), 1 - alpha / 2)
    hi = 1.0 if passes == n else _solve(lambda p: _binom_cdf(passes, n, p), alpha / 2)
    return lo, hi


def fisher_lower(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p that the second group's pass rate is lower.
    a/b: first group's passes/fails; c/d: second group's."""
    n1, n2 = a + b, c + d
    k, n = a + c, n1 + n2
    if n1 == 0 or n2 == 0:
        return 1.0
    denom = math.comb(n, k)
    return min(1.0, sum(math.comb(n2, x) * math.comb(n1, k - x) for x in range(max(0, k - n1), c + 1)) / denom)


def bh(pvalues: List[float]) -> List[float]:
    """Benjamini-Hochberg adjusted p-values (q-values), in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [0.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


# Two-sided 95% points of Student's t, by degrees of freedom (1-30); past 30, the normal's.
_T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
         2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
         2.045, 2.042]


def paired_change(before: List[float], after: List[float]) -> Tuple[float, float, float]:
    """Mean change per check with a paired t interval (95%). The check is the unit: 50 attempts of one
    task say a lot about that task and nothing about the others. A percentile bootstrap of five
    differences is far too narrow; t widens it by what five can't tell you."""
    d = [a - b for b, a in zip(before, after)]
    point, n = fmean(d), len(d)
    half = (_T975[n - 2] if n - 1 <= len(_T975) else 1.96) * stdev(d) / math.sqrt(n)
    return point, point - half, point + half


def reruns_needed(passes: int, n: int, base_passes: int, base_n: int, cap: int = MAX_ATTEMPTS) -> int:
    """More attempts after which, if the observed rate held, the drop would be clear."""
    rate = passes / n if n else 0.0
    for m in range(n + 1, cap + 1):
        c = round(rate * m)
        if fisher_lower(base_passes, base_n - base_passes, c, m - c) <= 0.05:
            return max(2, m - n)
    return max(0, cap - n)


# ---------- checks ----------

def check_key(r) -> tuple:
    return (r.case_id, r.field or "", r.evaluator or "")


def attempts_by_check(rows) -> Dict[tuple, List]:
    out = defaultdict(list)
    for r in rows:
        out[check_key(r)].append(r)
    return out


def _counts(rows) -> Tuple[int, int, int]:
    p = sum(1 for r in rows if r.status == "pass")
    f = sum(1 for r in rows if r.status == "fail")
    return p, f, len(rows) - p - f


def flake_source(rows) -> Optional[str]:
    """What varies between attempts of a check that had both outcomes."""
    if any(r.status == "error" for r in rows):
        return "infrastructure"
    outputs = {r.actual for r in rows}
    return "output" if len(outputs) > 1 else "evaluator"


def assess(candidate: Dict[tuple, List], baseline: Dict[tuple, List]) -> Dict[tuple, dict]:
    """Every check's state, pass rate and interval, against the baseline."""
    out, worse_i, better_i = {}, [], []
    for key, rows in candidate.items():
        c, f, e = _counts(rows)
        n = c + f
        base = baseline.get(key, [])
        a, b, _ = _counts(base)
        nb = a + b
        lo, hi = interval(c, n)
        x = {"passed": c, "failed": f, "errored": e, "attempts": n, "rate": c / n if n else None,
             "low": lo, "high": hi, "base_passed": a if nb else None, "base_attempts": nb or None,
             "base_rate": a / nb if nb else None, "p_worse": None, "q_worse": None, "outcomes":
             "".join("●" if r.status == "pass" else "○" if r.status == "fail" else "×"
                     for r in sorted(rows, key=lambda r: (r.attempt or 0, r.ts)))}
        if n and nb:
            if c / n < a / nb:
                x["p_worse"] = fisher_lower(a, b, c, f)
                worse_i.append(key)
            elif c / n > a / nb:
                x["p_better"] = fisher_lower(c, f, a, b)
                better_i.append(key)
        mixed = (0 < c < n) or (nb and 0 < a < nb)
        x["flake"] = flake_source(rows + base) if mixed or (e and c) else None
        out[key] = x
    # Every check that could have moved counts towards the correction, not just the ones that did.
    m = sum(1 for x in out.values() if x["attempts"] and x["base_attempts"])
    for keys, p, q in ((worse_i, "p_worse", "q_worse"), (better_i, "p_better", "q_better")):
        ps = [out[k][p] for k in keys]
        for k, qv in zip(keys, bh(ps + [1.0] * (m - len(ps)))):
            out[k][q] = qv
    for key, x in out.items():
        n = x["attempts"]
        if not n:
            x["state"] = "errored"
        elif x.get("q_worse") is not None and (x["q_worse"] <= Q_LEVEL or (
                x["passed"] == 0 and x["base_passed"] == x["base_attempts"] and min(n, x["base_attempts"]) >= COLLAPSE)):
            x["state"] = "got_worse"
        elif x.get("q_better") is not None and x["q_better"] <= Q_LEVEL:
            x["state"] = "improved"
        elif x.get("q_worse") is not None and n < MAX_ATTEMPTS and (
                x["q_worse"] <= PLAUSIBLE or (x["passed"] == 0 and x["base_passed"] == x["base_attempts"])):
            # Corrected for the number of checks, or every attempt passed before and every one fails now:
            # a whole capability gone isn't waved through as chance because the suite is large.
            x["state"] = "needs_reruns"
            x["reruns"] = reruns_needed(x["passed"], n, x["base_passed"], x["base_attempts"])
        elif x["flake"]:
            x["state"] = "flaky"
        else:
            x["state"] = "stable_pass" if x["passed"] == n else "stable_fail" if x["passed"] == 0 else "flaky"
        # Relative to the baseline: newly failing, failing as before, or flaky either way.
        if x["state"] == "flaky":
            x["since"] = "flaky"
        elif x["base_rate"] is None or x["passed"] == n:
            x["since"] = None
        elif x["rate"] < x["base_rate"]:
            x["since"] = "new"
        else:
            x["since"] = "persisting"
    return out


STATES = {"got_worse": "Got worse", "needs_reruns": "Needs reruns", "flaky": "Flaky", "improved": "Improved",
          "stable_fail": "Failing", "stable_pass": "Passing", "errored": "Couldn't be judged"}


def pooled(checks: List[dict]) -> Optional[dict]:
    """A cause's checks together: baseline vs candidate passes, and whether the drop is beyond chance.
    Three attempts per case can't prove much; 55 cases that all went from 3/3 to 0/3 can."""
    both = [x for x in checks if x.get("base_attempts") and x.get("attempts")]
    if not both:
        return None
    a = sum(x["base_passed"] for x in both)
    nb = sum(x["base_attempts"] for x in both)
    c = sum(x["passed"] for x in both)
    n = sum(x["attempts"] for x in both)
    return {"checks": len(both), "base_passed": a, "base_attempts": nb, "passed": c, "attempts": n,
            "p": fisher_lower(a, nb - a, c, n - c)}


# ---------- the run ----------

ROLES = {
    "accepted": "decided: accepted as intended, or not a problem",
    "intended": "look like an intended change nobody has accepted yet",
    "evaluator": "fail because of the evaluator, not the output: fix the checks",
    "evaluator_input": "were judged on data that doesn't match the trace: fix what the evaluator is given",
    "infrastructure": "failed on infrastructure: rerun them once it's fixed",
}


def summarize(states: Dict[tuple, dict], tolerance: float = 0.01, roles: Optional[Dict[tuple, str]] = None,
              max_list: int = 500) -> dict:
    """Run-level pass rate change with its interval, the noise attempts add, and a decision.

    `roles` comes from the failure causes: checks failing because of the evaluator, on
    infrastructure, or in a change someone accepted don't count against the release;
    an intended change nobody has accepted yet holds it. Everything else counts by its
    pass rate, so a flaky check is 0.8, not a pass one run and a fail the next."""
    roles = roles or {}
    counted = {k: x for k, x in states.items() if k not in roles}
    paired = [(x["base_rate"], x["rate"]) for x in counted.values()
              if x["base_rate"] is not None and x["rate"] is not None]
    rates = [x["rate"] for x in counted.values() if x["rate"] is not None]
    counts, role_counts = defaultdict(int), defaultdict(int)
    for k, x in counted.items():  # states of the checks that count; the rest are in role_counts
        counts[x["state"]] += 1
    for r in roles.values():
        role_counts[r] += 1
    change = None
    if len(paired) >= 2:
        point, lo, hi = paired_change([b for b, _ in paired], [c for _, c in paired])
        change = {"point": point, "low": lo, "high": hi, "checks": len(paired)}
    # How much the run's pass rate moves from attempt noise alone: the spread of the mean of
    # per-check Bernoulli outcomes. Two runs differing by less than this are the same.
    var = sum((x["rate"] * (1 - x["rate"])) / max(x["attempts"], 1) for x in counted.values() if x["rate"] is not None)
    noise = 1.96 * math.sqrt(2 * var) / len(rates) if rates else None

    def item(k, x, why=None):
        return dict(case_id=k[0], field=k[1] or None, evaluator=k[2] or None, why=why, **{
            f: x.get(f) for f in ("passed", "attempts", "rate", "low", "high", "base_passed", "base_attempts",
                                  "base_rate", "q_worse", "p_worse", "flake", "reruns", "outcomes", "state")})

    worse = sorted((item(k, x) for k, x in counted.items() if x["state"] == "got_worse"),
                   key=lambda i: (i["q_worse"] or 1, i["case_id"]))
    reruns = sorted((item(k, x, "plausibly worse, too few attempts to tell") for k, x in counted.items()
                     if x["state"] == "needs_reruns"), key=lambda i: (i["p_worse"] or 1, i["case_id"]))
    reruns += [item(k, x, "failed on infrastructure") | {"reruns": x["attempts"] or 1}
               for k, x in states.items() if roles.get(k) == "infrastructure" and x["passed"] < x["attempts"]]
    flaky = sorted((item(k, x) for k, x in states.items() if x["state"] == "flaky"),
                   key=lambda i: (i["rate"] if i["rate"] is not None else 1, i["case_id"]))
    base_flaky = sum(1 for x in states.values() if x["base_attempts"] and 0 < (x["base_passed"] or 0) < x["base_attempts"])
    counted_worse = len(worse)
    more = sum(r["reruns"] or 0 for r in reruns)

    reasons = []
    if change and change["high"] < -tolerance:
        outcome = "rollback"
        reasons.append(f"Leaving out evaluator, infrastructure and decided failures, the pass rate is lower by "
                       f"{-change['point']:.2%} (95% interval {-change['high']:.2%} to {-change['low']:.2%}): beyond "
                       f"the {tolerance:.2%} tolerance even at best.")
    elif worse:
        outcome = "hold"
        reasons.append(f"{counted_worse:,} checks got worse beyond chance (false-discovery rate {Q_LEVEL:.0%}), "
                       "allowing for flakiness.")
    elif role_counts["intended"]:
        outcome = "hold"
        reasons.append(f"{role_counts['intended']:,} checks {ROLES['intended']}. Accept it, or mark it a problem.")
    elif reruns:
        outcome = "rerun"
        reasons.append(f"{len(reruns):,} checks can't be judged yet. Rerun them ({more:,} more attempts in all) "
                       "instead of blocking.")
    else:
        outcome = "advance"
        # Not proven worse, but not proven fine either. Said, not blocked: holding on it would hold
        # nearly every run of a small or flaky suite, since a 1% drop can't be ruled out.
        if change and change["low"] < -tolerance:
            reasons.append(f"No check got worse beyond chance, and the pass rate isn't proven lower than the "
                           f"tolerance allows. It could be lower by up to {-change['low']:.2%}: more attempts or "
                           "more cases narrow it.")
        else:
            reasons.append("No check got worse beyond chance, and the pass rate is within tolerance.")
    if outcome in ("rollback", "hold") and role_counts["intended"] and not reasons[0].endswith("problem."):
        reasons.append(f"{role_counts['intended']:,} checks {ROLES['intended']}.")
    if reruns and outcome != "rerun":
        reasons.append(f"{len(reruns):,} checks need reruns ({more:,} attempts) before they can be judged.")
    for r in ("evaluator", "evaluator_input", "accepted"):
        if role_counts[r]:
            reasons.append(f"Not counted: {role_counts[r]:,} checks {ROLES[r]}.")
    all_flaky = sum(1 for x in states.values() if x["state"] == "flaky")
    if all_flaky:
        reasons.append(f"{all_flaky:,} flaky checks count by their pass rate and don't block"
                       f"{f' ({base_flaky:,} were flaky before)' if base_flaky else ''}.")
    return {"outcome": outcome, "reasons": reasons, "tolerance": tolerance, "checks": len(states),
            "counted": len(counted), "states": {s: counts.get(s, 0) for s in STATES},
            "roles": dict(role_counts), "change": change, "noise": noise,
            "pass_rate": sum(rates) / len(rates) if rates else None,
            "base_flaky": base_flaky, "got_worse": worse[:max_list], "reruns": reruns[:max_list],
            "flaky": flaky[:max_list]}
