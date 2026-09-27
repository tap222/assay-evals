"""One verdict per check: not everything that isn't a pass is a failure.

  PASS             judged, and passed every attempt
  FAIL             judged, and failed
  FLAKY            passes some attempts and fails others, the way it did before
  INCONCLUSIVE     plausibly worse, but too few attempts to tell (see assay/flaky.py)
  INVALID          couldn't be judged: the evaluator answered, but not with a verdict
                   (unparseable, off its schema, a score that isn't a number). Never a 0.
  TIMEOUT          couldn't be judged: the evaluator timed out
  RATE_LIMITED     couldn't be judged: the evaluator was rate limited
  EVALUATOR_ERROR  couldn't be judged: the evaluator failed, or was given data that
                   doesn't match the trace (assay/audit.py)
  INFRA_ERROR      couldn't be judged: a 5xx or connection error, or a timeout or rate limit
                   the evaluator reported only in its reason
  MISSING          no result arrived: the evaluator reported on this case in the baseline,
                   or on most of this run's cases, but not on this one here (its job didn't
                   run, or dropped it). An evaluator that stopped running altogether shows
                   up this way too, against the baseline.

A check is a case, field and evaluator (flaky.check_key); its attempts decide it. Only
FAIL says anything bad about the AI. The error verdicts and MISSING say the evaluation
didn't happen; INCONCLUSIVE says it hasn't settled.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional

from assay import flaky
from assay.failures import INFRA_REASON

VERDICTS = {
    "PASS": "passed", "FAIL": "failed", "FLAKY": "flaky", "INCONCLUSIVE": "inconclusive",
    "INVALID": "invalid result", "TIMEOUT": "timed out", "RATE_LIMITED": "rate limited",
    "EVALUATOR_ERROR": "evaluator error", "INFRA_ERROR": "infrastructure error", "MISSING": "missing",
}
NOT_JUDGED = ("INVALID", "TIMEOUT", "RATE_LIMITED", "EVALUATOR_ERROR", "INFRA_ERROR", "MISSING")  # didn't happen
# A result's error_kind (schema.ERROR_KINDS), when the evaluator said why it couldn't judge.
BY_KIND = {"invalid": "INVALID", "timeout": "TIMEOUT", "rate_limited": "RATE_LIMITED", "unavailable": "INFRA_ERROR",
           "error": "EVALUATOR_ERROR"}


def _not_judged(errors: list) -> tuple:
    """(verdict, reason) for attempts that couldn't be judged: what they said, else what the reason suggests."""
    kinds = [getattr(r, "error_kind", None) for r in errors]
    reason = next((r.reason for r in errors if r.reason), None) or "the check couldn't run"
    kind = next((k for k in kinds if k), None)
    if kind:
        return BY_KIND.get(kind, "EVALUATOR_ERROR"), reason
    return ("INFRA_ERROR" if INFRA_REASON.search(reason) else "EVALUATOR_ERROR"), reason
MISSING_COVERAGE = 0.5  # an evaluator is expected on every case once it has reported on this share
# Metrics a test calls in its own body (assay_sdk.frameworks): a test that doesn't call one doesn't use
# it, so how many cases it covered says nothing. A case that had it in its baseline still does.
INLINE = ("deepeval:", "ragas:")


def of_check(state: dict, rows: list, findings: Optional[List[str]] = None) -> tuple:
    """(verdict, reason) for one check, from its state (flaky.assess) and its attempts."""
    if findings:
        return "EVALUATOR_ERROR", f"judged on data that doesn't match the trace: {findings[0]}"
    errors = [r for r in rows if r.status == "error"]
    if state["state"] == "errored":  # no attempt was judged
        return _not_judged(errors)
    if state["state"] == "needs_reruns":
        return "INCONCLUSIVE", (f"passed {state['passed']} of {state['attempts']} attempts, "
                                f"{state['base_passed']} of {state['base_attempts']} before: "
                                f"{state.get('reruns') or 'more'} more attempts to tell")
    if state["state"] == "flaky" or (state.get("flake") and 0 < state["passed"] < state["attempts"]):
        return "FLAKY", f"passed {state['passed']} of {state['attempts']} attempts"
    judged = [r for r in rows if r.status != "error"]
    note = f" ({len(errors)} more attempt{'s' * (len(errors) != 1)} couldn't run)" if errors else ""
    if state["passed"] == state["attempts"]:
        return "PASS", (note.strip(" ()") or None)
    reason = next((r.reason for r in judged if r.status == "fail" and r.reason), None)
    if reason is None:
        bad = next(r for r in judged if r.status == "fail")
        reason = f"expected {bad.expected}, got {bad.actual}" if bad.expected or bad.actual else "failed"
    return "FAIL", reason + note


def _external(rows: list) -> tuple:
    """({evaluator: cases it reported on}, {evaluator: its fields}) for evaluators other than Assay's
    own, whose checks are made for every run they apply to."""
    by_eval: Dict[str, set] = defaultdict(set)
    fields: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.evaluator and not r.evaluator.startswith("assay."):
            by_eval[r.evaluator].add(r.case_id)
            fields[r.evaluator][r.field] += 1
    return by_eval, fields


def missing(rows: list, base_rows: Optional[list] = None) -> List[dict]:
    """Checks that never arrived, per evaluator: the cases of this run it reported on in the
    baseline but not now, and, for one that reported on most of this run's cases, the rest."""
    cases = {r.case_id for r in rows}
    by_eval, fields = _external(rows)
    base_eval, base_fields = _external(base_rows or [])
    # A new version of an evaluator (faithfulness@2 → @3) replaces the old one: not missing.
    now_names = {e.split("@")[0] for e in by_eval}
    out, seen = [], set()

    def add(ev, case, field, reason):
        if (case, ev) not in seen:
            seen.add((case, ev))
            out.append({"case_id": case, "field": field, "evaluator": ev, "verdict": "MISSING", "reason": reason})
    for ev, before in sorted(base_eval.items()):
        if ev not in by_eval and ev.split("@")[0] in now_names:
            continue
        expected, got = before & cases, by_eval.get(ev, set())
        field = (fields.get(ev) or base_fields[ev]).most_common(1)[0][0]
        for case in sorted(expected - got):
            add(ev, case, field, f"{ev} reported on {len(expected)} of these cases in the baseline, none in this run"
                if not got else f"{ev} reported on this case in the baseline, not in this run")
    for ev, covered in sorted(by_eval.items()):
        if ev.startswith(INLINE):
            continue
        if len(covered) >= MISSING_COVERAGE * len(cases) and covered != cases:
            field = fields[ev].most_common(1)[0][0]
            for case in sorted(cases - covered):
                add(ev, case, field, f"{ev} reported on {len(covered)} of {len(cases)} cases, not this one")
    return out


# A failing check's cause (assay/failures.py) can say it isn't about the AI: the same judgement
# the release call makes (flaky.ROLES).
BY_ROLE = {"evaluator": "EVALUATOR_ERROR", "evaluator_input": "EVALUATOR_ERROR", "infrastructure": "INFRA_ERROR",
           "intended": "INCONCLUSIVE", "accepted": "PASS"}


def compute(rows: list, states: Dict[tuple, dict], audited: Dict[str, List[str]],
            roles: Optional[Dict[tuple, str]] = None, causes: Optional[Dict[tuple, str]] = None,
            base_rows: Optional[list] = None) -> dict:
    """Every check's verdict in a run, and how many of each. roles/causes: per check, what its
    failure cause makes of it, and the cause's name."""
    roles, causes = roles or {}, causes or {}
    checks = []
    for key, attempts in flaky.attempts_by_check(rows).items():
        found = next((audited[r.result_id] for r in attempts if r.result_id in audited), None)
        verdict, reason = of_check(states[key], attempts, found)
        if verdict in ("FAIL", "FLAKY") and roles.get(key) in BY_ROLE:
            verdict = BY_ROLE[roles[key]]
            why = causes.get(key) or reason
            reason = {"INCONCLUSIVE": f"looks like an intended change nobody has accepted yet: {why}",
                      "PASS": f"accepted: {why}"}.get(verdict, why)
        last = attempts[-1]  # what the evaluator returned, and how many tries it took, for a look at why
        checks.append({"case_id": key[0], "field": key[1] or None, "evaluator": key[2] or None,
                       "verdict": verdict, "reason": reason, "attempts": len(attempts),
                       **({"tries": last.tries} if getattr(last, "tries", None) else {}),
                       **({"raw_output": last.raw_output} if getattr(last, "raw_output", None) else {})})
    checks += missing(rows, base_rows)
    counts = Counter(c["verdict"] for c in checks)
    return {"counts": {v: counts.get(v, 0) for v in VERDICTS}, "checks": checks}
