"""Judging a saved run again: the judge's own variance, apart from the AI's.

    assay rejudge --judge evals/judges.py:helpful --times 3

Every check judged through evaluate(..., inputs={...}) keeps what the judge saw. This asks a
judge about those same answers again, with no calls to the AI, and says:

  agreement    how often it gives the verdict that was saved (and Cohen's kappa, which
               discounts the agreement two judges get by chance), with each flip listed
  self         with --times 2 or more: answers it splits on when asked again
  not judged   answers it couldn't give a verdict on: counted apart, never as a flip

The same judge, frozen (model and prompt), measures how much its verdicts move on their own:
a model swap that moved fewer verdicts than that hasn't shown anything. Another judge, or a
new rubric, is tried on the same answers before switching to it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy import select

from assay import store


class RejudgeError(Exception):
    """Nothing to re-judge, or a setup problem; the message says what to do."""


def saved(engine, tenant: str, run_id: str, field: Optional[str] = None) -> List:
    """The run's checks that a judge passed or failed and whose inputs were kept."""
    t = store.eval_results
    q = select(t).where(t.c.tenant == tenant, t.c.run_id == run_id, t.c.status.in_(("pass", "fail")),
                        t.c.inputs.isnot(None))
    if field:
        q = q.where(t.c.field == field)
    with engine.connect() as conn:
        rows = conn.execute(q.order_by(t.c.case_id, t.c.field, t.c.attempt, t.c.ts)).fetchall()
    return [r for r in rows if isinstance(r.inputs, dict) and r.inputs]


def kappa(pairs: List[tuple]) -> Optional[float]:
    """Cohen's kappa over (saved, now) pass/fail pairs; None when chance agreement is total."""
    n = len(pairs)
    if not n:
        return None
    seen = sum(1 for a, b in pairs if a == b) / n
    a1, b1 = sum(1 for a, _ in pairs if a) / n, sum(1 for _, b in pairs if b) / n
    chance = a1 * b1 + (1 - a1) * (1 - b1)
    return None if chance >= 1 else (seen - chance) / (1 - chance)


def rejudge(rows: List, judge: Callable, times: int = 1, threshold: Optional[float] = 0.5) -> dict:
    """Ask `judge` about each saved answer again, `times` times; compare with the saved verdicts."""
    from assay_sdk.evaluation import evaluate
    items, pairs, unjudged, split = [], [], 0, 0
    for r in rows:
        res = evaluate(judge, threshold=threshold, rejudge=times, retries=1, backoff=0.2, judge_kwargs=dict(r.inputs))
        was = r.status == "pass"
        item = {"case_id": r.case_id, "field": r.field, "attempt": r.attempt, "saved": r.status,
                "now": None, "votes": list(res.votes) if res.votes else None, "reason": res.reason,
                "error": None if res.valid else res.error}
        if not res.valid:
            unjudged += 1
        else:
            item["now"] = "pass" if res.status == "PASS" else "fail"
            pairs.append((was, item["now"] == "pass"))
            split += bool(res.votes and 0 < res.votes[0] < res.votes[1])
        items.append(item)
    flips = [i for i in items if i["now"] and i["now"] != i["saved"]]
    return {"answers": len(rows), "judged": len(pairs), "times": times,
            "agree": sum(1 for a, b in pairs if a == b), "kappa": kappa(pairs),
            "pass_to_fail": sum(1 for i in flips if i["saved"] == "pass"),
            "fail_to_pass": sum(1 for i in flips if i["saved"] == "fail"),
            "split": split, "not_judged": unjudged, "flips": flips,
            "errors": [i for i in items if i["error"]],
            "saved_by": sorted({f"{r.judge_model or '?'} / {r.judge_prompt or '?'}" for r in rows
                                if r.judge_model or r.judge_prompt})}


def text(out: dict, run_id: str, spec: str) -> str:
    lines = [f"Re-judged {out['answers']} saved answer{'s' if out['answers'] != 1 else ''} from {run_id} with "
             f"{spec}" + (f", {out['times']} times each" if out["times"] > 1 else "") + ". No calls to the AI.", ""]
    if out["saved_by"]:
        lines.append(f"Saved verdicts came from {', '.join(out['saved_by'])}.")
    if out["judged"]:
        share = out["agree"] / out["judged"]
        k = f", Cohen's kappa {out['kappa']:.2f}" if out["kappa"] is not None else ""
        lines.append(f"Agrees with the saved verdicts  {out['agree']}/{out['judged']} ({share:.0%}){k}")
        lines.append(f"  pass → fail {out['pass_to_fail']}   fail → pass {out['fail_to_pass']}")
    if out["times"] > 1:
        lines.append(f"Splits with itself  {out['split']} of {out['judged']} answers, asked {out['times']} times")
    if out["not_judged"]:
        lines.append(f"Couldn't be judged  {out['not_judged']}: not counted as flips")
    if out["flips"]:
        lines += ["", "Flipped:"]
        for f in out["flips"][:20]:
            where = f"{f['case_id']}  {f['field'] or 'evaluation'}" + (f"  attempt {f['attempt']}" if f["attempt"] else "")
            lines.append(f"  {where}  {f['saved']} → {f['now']}" + (f"  {f['reason'][:100]!r}" if f["reason"] else ""))
        if len(out["flips"]) > 20:
            lines.append(f"  and {len(out['flips']) - 20} more")
    if out["judged"] and out["agree"] < out["judged"]:
        lines += ["", "The same judge, frozen, flipping verdicts is the judge's own noise: a model or prompt change "
                      "that moved fewer verdicts than this hasn't shown anything. Another judge flipping them is "
                      "what switching to it would change."]
    return "\n".join(lines)


def run(root: Path, spec: str, run_id: Optional[str] = None, field: Optional[str] = None, times: int = 1,
        threshold: Optional[float] = 0.5, fmt: str = "text", min_agreement: Optional[float] = None) -> int:
    """`assay rejudge`. 0 done, 1 below --min-agreement, 2 nothing to re-judge or a setup problem."""
    import sys
    from assay import calibrate, local
    home = root / ".assay"
    if not (home / "assay.db").exists():
        print("No runs here yet: run `assay test` first.", file=sys.stderr)
        return 2
    run_id = run_id or local._state(home).get("last")
    if not run_id:
        print("No run to re-judge: run `assay test` first, or give --run.", file=sys.stderr)
        return 2
    try:
        judge = calibrate.load_judge(spec, root)
    except calibrate.CalibrationError as exc:
        print(str(exc).replace("[calibrate] judge", "--judge").replace("judge(input, output)",
                                                                       "judge(**inputs), with what the check saved"),
              file=sys.stderr)
        return 2
    rows = saved(store.make_engine(f"sqlite:///{home / 'assay.db'}"), local.TENANT, run_id, field)
    if not rows:
        print(f"Nothing to re-judge in {run_id}" + (f" for {field}" if field else "") + ": no passed or failed check "
              "kept what its judge saw. Record it with evaluate(..., inputs={\"question\": q, \"answer\": a}).",
              file=sys.stderr)
        return 2
    out = rejudge(rows, judge, max(1, times), threshold)
    print(json.dumps({"run": run_id, **out}, indent=2, default=str) if fmt == "json" else text(out, run_id, spec))
    if min_agreement is not None and out["judged"] and out["agree"] / out["judged"] < min_agreement:
        if fmt != "json":
            print(f"\nBelow --min-agreement {min_agreement:.0%}.")
        return 1
    return 0
