"""`assay evals audit`: what each evaluator costs to keep, code checks and judges apart.

A code check (an assertion, a reference answer, a pattern) is cheap to build and keep. A judge needs
100 or so labels, calibration against them, and upkeep every week or so, since prompts, models and
what users ask move under it. This lists every evaluator in recent runs and, for each judge, what
it's missing: labels, a recent calibration, a trust label that holds, and whether a code check
would do its job (most of its failures are about length or format).
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from sqlalchemy import select

from assay import store

LABELS = 100  # labels a judge needs before its scores are worth much
WEEK = 7  # days: a judge not calibrated since is due
FORMAT = re.compile(r"\b(too (long|short|verbose|wordy)|length|word count|\d+ words|characters|format|formatt|json|"
                    r"markdown|bullet|heading|table|capital|uppercase|lowercase|emoji|concise|brevity|list)\b", re.I)


def is_judge(r) -> bool:
    return bool(getattr(r, "judge_model", None) or getattr(r, "judge_prompt", None)
                or "judge" in (r.evaluator or "").lower())


def audit(engine, tenant: str, days: float, golden_items: Dict[str, int]) -> dict:
    from assay.local import trust
    t = store.eval_results
    since = datetime.utcnow() - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(select(t).where((t.c.tenant == tenant) & (t.c.ts >= since))).all()
    code, judges = defaultdict(int), defaultdict(list)
    for r in rows:
        f = r.field or "result"
        if is_judge(r):
            judges[f].append(r)
        else:
            code[f] += 1
    tr = trust(engine, tenant, [r for rs in judges.values() for r in rs])
    out = []
    for f, rs in sorted(judges.items()):
        x = tr.get(f) or {"state": "none"}
        labels = x.get("n") or golden_items.get(f) or 0
        failed = [r for r in rs if r.status == "fail"]
        about_format = sum(1 for r in failed if FORMAT.search(r.reason or ""))
        issues = []
        if labels < LABELS:
            issues.append(f"{labels} labels; a judge needs {LABELS} or so before its scores mean much "
                          "(assay golden suggest picks what to label)")
        if x["state"] == "none":
            issues.append("never calibrated against people (assay calibrate)")
        elif x.get("age") is not None and x["age"] > WEEK:
            issues.append(f"not calibrated in {x['age']} days: a judge needs upkeep every week or so")
        if x["state"] in ("regressed", "other_judge", "topic"):
            issues.append("its trust label doesn't hold: " + {"regressed": "the last calibration regressed",
                                                                  "other_judge": "calibrated for another judge",
                                                                  "topic": "it tells topics apart, not good answers "
                                                                           "from bad ones within a topic"}[x["state"]])
        if len(failed) >= 3 and about_format / len(failed) >= 0.5:
            issues.append(f"{about_format} of its {len(failed)} failures are about length or format: a code check "
                          "(a word limit, a pattern, valid JSON) would do that without the upkeep")
        out.append({"field": f, "results": len(rs), "failures": len(failed), "labels": labels,
                    "calibrated_days_ago": x.get("age"), "trust": x["state"], "issues": issues,
                    "models": sorted({r.judge_model for r in rs if getattr(r, "judge_model", None)})})
    return {"days": days, "code_checks": len(code), "code_results": sum(code.values()), "judges": out,
            "suite": suite(engine, tenant, rows)}


def suite(engine, tenant: str, rows) -> dict:
    """What the CI suite costs per run: its size, the evaluators' time, the model cost (the app's own calls
    and the judges'), the share of cases a judge has to read, and the most expensive cases."""
    t, st = store.agent_trajectories, store.agent_steps
    runs = defaultdict(list)
    for r in rows:
        runs[r.run_id].append(r)
    if not runs:
        return {"runs": 0}
    with engine.connect() as conn:
        latest = max(runs, key=lambda k: max(r.ts for r in runs[k]))
        heads = conn.execute(select(t.c.trajectory_id, t.c.run_id, t.c.case_id).where(
            (t.c.tenant == tenant) & t.c.run_id.in_(list(runs)))).all()
        case_of = {h.trajectory_id: (h.run_id, h.case_id or h.trajectory_id) for h in heads}
        app_cost = defaultdict(float)
        ids = list(case_of)
        for i in range(0, len(ids), 500):
            for s in conn.execute(select(st.c.trajectory_id, st.c.cost_usd).where(
                    (st.c.tenant == tenant) & st.c.trajectory_id.in_(ids[i:i + 500]))):
                if s.cost_usd:
                    app_cost[case_of[s.trajectory_id]] += s.cost_usd
    per_run = []
    for run, rs in runs.items():
        cases = {r.case_id for r in rs}
        judged = {r.case_id for r in rs if is_judge(r)}
        per_run.append({"run": run, "cases": len(cases), "judged_cases": len(judged),
                        "evaluator_seconds": sum(r.duration_ms or 0 for r in rs) / 1000,
                        "judge_cost": sum(r.cost_usd or 0 for r in rs),
                        "app_cost": sum(v for (rn, _), v in app_cost.items() if rn == run)})
    last = next(x for x in per_run if x["run"] == latest)
    cost = defaultdict(float)
    for r in runs[latest]:
        cost[r.case_id] += r.cost_usd or 0
    for (rn, case), v in app_cost.items():
        if rn == latest:
            cost[case] += v
    avg = lambda k: sum(x[k] for x in per_run) / len(per_run)
    return {"runs": len(per_run), "latest": last, "average_cost": avg("judge_cost") + avg("app_cost"),
            "judged_share": last["judged_cases"] / last["cases"] if last["cases"] else None,
            "costliest": [{"case": c, "cost_usd": v} for c, v in sorted(cost.items(), key=lambda kv: -kv[1])[:5] if v]}


def suite_text(s: dict) -> List[str]:
    if not s.get("runs"):
        return []
    x = s["latest"]
    lines = ["", f"The suite, latest run {x['run']}: {x['cases']} cases, {x['judged_cases']} read by a judge "
                 f"({s['judged_share']:.0%}), {x['evaluator_seconds']:.1f} s in evaluators, "
                 f"${x['app_cost'] + x['judge_cost']:.4f} in model calls (${x['app_cost']:.4f} the app's, "
                 f"${x['judge_cost']:.4f} the judges'). Over {s['runs']} run{'s' * (s['runs'] != 1)}: "
                 f"${s['average_cost']:.4f} a run."]
    if s["costliest"]:
        lines.append("  Costliest cases: " + ", ".join(f"{c['case']} ${c['cost_usd']:.4f}" for c in s["costliest"]))
    if s["judged_share"] and s["judged_share"] > 0.5:
        lines.append("  Most cases need a judge: CI runs often, so check deterministically where you can (assay triage).")
    return lines


def text(a: dict) -> str:
    j = a["judges"]
    head = (f"Evaluators in the last {a['days']:g} days: {a['code_checks']} code check{'s' * (a['code_checks'] != 1)}, "
            f"{len(j)} judge{'s' * (len(j) != 1)}.")
    if not j:
        return "\n".join([head + " No judges: nothing to label, calibrate or keep up."] + suite_text(a.get("suite") or {}))
    lines = [head, "", f"  {'judge':<24} {'labels':>8}  {'calibrated':<14} trust"]
    for x in j:
        when = "never" if x["calibrated_days_ago"] is None else \
            "today" if x["calibrated_days_ago"] == 0 else f"{x['calibrated_days_ago']} days ago"
        lines.append(f"  {x['field'][:24]:<24} {str(x['labels']) + '/' + str(LABELS):>8}  {when:<14} {x['trust']}")
    due = [(x["field"], i) for x in j for i in x["issues"]]
    if due:
        lines.append("")
        lines += [f"  - {f}: {i}" for f, i in due]
    else:
        lines += ["", "Every judge has its labels, a calibration this week, and a trust label that holds."]
    return "\n".join(lines + suite_text(a.get("suite") or {}))


def cli(root: Path, days: float, fmt: str) -> int:
    from assay import calibrate, local
    engine, _ = local._open(root)
    if engine is None:
        print("No test runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    golden = {}
    try:
        ccfg = local._calib_cfg(root)
        items = calibrate.load_golden(root / ccfg["golden"])
        if ccfg.get("field"):
            golden[ccfg["field"]] = len(items)
    except Exception:
        pass
    a = audit(engine, local.TENANT, days, golden)
    print(json.dumps(a, indent=1) if fmt == "json" else text(a))
    return 0
