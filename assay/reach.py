"""Analysis of real-world resources touched by tests (commands, non-local sockets, outside file writes).

Compares candidate test runs against baseline test runs to flag any real system access
that the baseline tests never touched:
  - Commands started (e.g. scp, rsync, psql)
  - Non-local socket connections (e.g. db.prod.internal:5432)
  - File writes outside the repository and temporary folders
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def is_real_touch_step(step_name: str) -> bool:
    """True if tool step represents touching a real system (command, non-local socket, outside write)."""
    return bool(step_name and (
        step_name.startswith("CMD ") or
        step_name.startswith("SOCKET ") or
        step_name.startswith("FILE write ")
    ))


def extract_real_touches_by_case(cases_dict: Dict[str, dict]) -> Dict[str, Set[str]]:
    """{case_id: set(touch_names)} from trajectories/cases."""
    out: Dict[str, Set[str]] = defaultdict(set)
    for case_id, traj in cases_dict.items():
        for s in traj.get("steps") or []:
            name = s.get("name") or ""
            if is_real_touch_step(name):
                out[case_id].add(name)
    return dict(out)


def real_touches_from_db(engine, tenant: str, run_id: str) -> Dict[str, Set[str]]:
    """Extracts {case_id: set(touch_names)} from SQLite store."""
    from assay import store
    from sqlalchemy import select
    t = store.eval_results
    st = store.agent_steps
    out: Dict[str, Set[str]] = defaultdict(set)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(t.c.case_id, st.c.name)
                .join(st, (st.c.tenant == t.c.tenant) & (st.c.trajectory_id == t.c.document_id))
                .where((t.c.tenant == tenant) & (t.c.run_id == run_id) & (st.c.kind == "tool"))
            ).all()
        for r in rows:
            if is_real_touch_step(r.name):
                out[r.case_id].add(r.name)
    except Exception:
        pass
    return dict(out)


def compare_real_touches(
    current_touches_by_case: Dict[str, Set[str]],
    baseline_touches_by_case: Dict[str, Set[str]],
) -> List[dict]:
    """Compares candidate run real touches against baseline run touches.

    Anything the candidate tests touched that the baseline tests never touched
    is flagged as reaching something real.
    """
    base_all: Set[str] = set()
    for touches in baseline_touches_by_case.values():
        base_all.update(touches)

    now_by_touch: Dict[str, Set[str]] = defaultdict(set)
    for case_id, touches in current_touches_by_case.items():
        for t in touches:
            now_by_touch[t].add(case_id)

    out: List[dict] = []
    for touch in sorted(now_by_touch):
        if touch not in base_all:
            cases = sorted(now_by_touch[touch])
            cases_str = ", ".join(cases[:3]) + (f", and {len(cases) - 3} more" if len(cases) > 3 else "")
            
            # Format readable description based on category
            if touch.startswith("CMD "):
                cmd = touch[4:]
                desc = f"command {cmd} run in {cases_str}"
            elif touch.startswith("SOCKET "):
                host_port = touch[7:]
                desc = f"connection to {host_port} in {cases_str}"
            elif touch.startswith("FILE write "):
                path = touch[11:]
                desc = f"file written to {path} in {cases_str}"
            else:
                desc = f"{touch} in {cases_str}"

            text = f"tests reached something real: {desc}"
            out.append({
                "kind": "touched_real",
                "touch": touch,
                "cases": cases,
                "cases_count": len(cases),
                "text": text,
                "detail": f"not touched on baseline ({len(cases)} test{'s' * (len(cases) != 1)})",
                "key": f"touched_real:{touch}",
            })

    return out
