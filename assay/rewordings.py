"""Rewordings: the same request in other words should get the same behavior.

A test passes on its exact wording ("Can I get a refund for O-18?") and the agent breaks on
another ("I want my money back for O-18"). Tests written as rewordings of one another say so:
with pytest, `assay_sdk.testing.rewordings(...)` runs the test once per wording; without it, a
run's tags say it (`rewording_of`, `wording`, `wording_index`; index 0 is the original).

After a test run, each rewording gets a check of its own, `rewording`, per attempt: did it do
something the original wording did? It fails when it takes a path (tools, approvals and
resources, in order) the original never took in a passing attempt, or fails where the original
passed every time. Against all of the original's attempts, not the one with the same number: an
agent that takes one of two paths at random would otherwise differ from itself. When the
original never passed there's nothing to be consistent with, and nothing is judged.

It's an ordinary check from then on: compared with its baseline, flaky across attempts, a new
rewording that already behaves differently is a new failure. So a PR that makes the agent
depend on the exact wording fails, and a wording added from production is checked against the
original the first time it runs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional

from sqlalchemy import and_, select

from assay import agents, diff, ingest, store

FIELD, EVALUATOR = "rewording", "assay.rewording@1"


def _tags(engine, tenant: str, ids: List[str]) -> Dict[str, dict]:
    t, out = store.runs, {}
    with engine.connect() as conn:
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(t.c.run_id, t.c.tags).where(and_(t.c.tenant == tenant,
                                                                          t.c.run_id.in_(ids[i:i + 500])))):
                out[r.run_id] = r.tags or {}
    return out


def _outcomes(engine, tenant: str, run_id: str) -> Dict[tuple, dict]:
    """(case, attempt): whether every judged check passed, and which failed. Errors aren't failures."""
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.case_id, t.c.attempt, t.c.field, t.c.status).where(
            and_(t.c.tenant == tenant, t.c.run_id == run_id))).all()
    out: Dict[tuple, dict] = defaultdict(lambda: {"judged": 0, "failed": []})
    for r in rows:
        if r.field == FIELD or r.status not in ("pass", "fail"):
            continue
        x = out[(r.case_id, r.attempt or 0)]
        x["judged"] += 1
        if r.status == "fail":
            x["failed"].append(r.field or "result")
    return out


def check(engine, tenant: str, run_id: str) -> int:
    """Write each rewording's `rewording` check for this test run. Returns how many were written."""
    from assay.local import _label
    from assay.sources.events import EventsSource
    heads = agents.run_trajectories(engine, tenant, run_id)
    tags = _tags(engine, tenant, [h["trajectory_id"] for h in heads])
    groups: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for h in heads:
        tg = tags.get(h["trajectory_id"]) or {}
        if not tg.get("rewording_of") or not h["case_id"]:
            continue
        m = groups[str(tg["rewording_of"])].setdefault(h["case_id"], {
            "index": int(tg.get("wording_index") or 0), "wording": tg.get("wording"), "attempts": {}})
        m["attempts"][h["attempt"] or 0] = h
    groups = {g: ms for g, ms in groups.items() if len(ms) > 1 and any(m["index"] == 0 for m in ms.values())}
    if not groups:
        return 0
    trajs = EventsSource(engine, tenant).trajectories(
        [h["trajectory_id"] for ms in groups.values() for m in ms.values() for h in m["attempts"].values()])
    outcomes = _outcomes(engine, tenant, run_id)

    def state(case: str, h: dict) -> Optional[dict]:
        o = outcomes.get((case, h["attempt"] or 0))
        traj = trajs.get(h["trajectory_id"])
        if not o or not o["judged"] or traj is None:
            return None
        return {"ok": not o["failed"], "failed": o["failed"], "flow": diff.flow(traj)}

    rows = []
    for g, members in groups.items():
        original_case, original = next((c, m) for c, m in members.items() if m["index"] == 0)
        said = f"the original wording ({original['wording']!r})" if original.get("wording") else "the original wording"
        judged = [o for o in (state(original_case, h) for h in original["attempts"].values()) if o]
        passing = [o for o in judged if o["ok"]]
        if not passing:
            continue  # the original never passed: nothing to be consistent with
        paths = Counter(o["flow"] for o in passing)
        usual = paths.most_common(1)[0][0]
        always = len(passing) == len(judged)
        for case, m in members.items():
            if case == original_case:
                continue
            for attempt, h in sorted(m["attempts"].items()):
                x = state(case, h)
                if x is None:
                    continue
                problems = []
                if not x["ok"] and always:
                    problems.append(f"fails where {said} passes: {', '.join(_label(f) for f in x['failed'][:3])}")
                if x["flow"] not in paths:
                    problems.append(f"does something else than {said}: "
                                    f"{diff._describe(diff.flow_change(usual, x['flow']))}")
                rows += agents.result_rows(tenant, run_id, {**h, "case_id": case}, [{
                    "field": FIELD, "status": "fail" if problems else "pass",
                    "expected": " → ".join(usual) or "(no calls)", "actual": " → ".join(x["flow"]) or "(no calls)",
                    "reason": "; ".join(problems)[:2000] or None}], EVALUATOR)
    if rows:
        ingest.upsert(engine, store.eval_results, rows, "result_id")
    return len(rows)
