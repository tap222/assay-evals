"""Analysis of errors and warnings logged by tests compared to their baseline runs.

Identifies:
1. New error templates logged by tests that never logged them before, linked to changed functions.
2. Error rate increases where an existing error template occurs in significantly more tests.
3. Leaves out templates logged at similar rates on the baseline to avoid noisy reports.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from assay_sdk.logged_errors import template_log


def logged_errors_by_case(engine, tenant: str, run_id: str) -> Dict[str, List[dict]]:
    """{case_id: [log_step_dicts]} for a run from SQLite store."""
    from collections import defaultdict
    from sqlalchemy import select
    from assay import store
    t = store.eval_results
    st = store.agent_steps
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(t.c.case_id, st.c.kind, st.c.name, st.c.args, st.c.result, st.c.error, st.c.text)
                .join(st, (st.c.tenant == t.c.tenant) & (st.c.trajectory_id == t.c.document_id))
                .where((t.c.tenant == tenant) & (t.c.run_id == run_id) & (st.c.kind == "log"))
            ).all()
    except Exception:
        return {}
    by_case = defaultdict(list)
    for r in rows:
        args = r.args or {}
        by_case[r.case_id].append({
            "kind": r.kind,
            "name": r.name,
            "args": args,
            "result": r.result,
            "error": r.error,
            "text": r.text,
            "level": args.get("level") or "ERROR",
            "template": args.get("template") or r.error,
            "file": args.get("file"),
            "line": args.get("line"),
            "function": args.get("function"),
            "message": r.text or r.error,
        })
    return dict(by_case)
    """Extracts all log steps from an iterable of steps/events."""
    out = []
    for s in steps_or_events:
        if s.get("kind") == "log":
            args = s.get("args") or {}
            tmpl = s.get("template") or args.get("template") or template_log(s.get("text") or s.get("error") or "")
            level = s.get("level") or args.get("level") or "ERROR"
            file = s.get("file") or args.get("file")
            line = s.get("line") or args.get("line")
            fn = s.get("function") or args.get("function")
            msg = s.get("text") or s.get("message") or s.get("error") or tmpl
            out.append({
                "template": tmpl,
                "level": str(level).upper(),
                "message": msg,
                "file": file,
                "line": line,
                "function": fn,
            })
    return out


def _find_origin(locations: List[Tuple[Optional[str], Optional[int], Optional[str]]],
                 changed_files: Optional[Set[str]] = None,
                 changed_functions: Optional[Dict[str, Set[str]]] = None) -> Optional[str]:
    """Finds the most likely source origin (changed file and function) for a logged error."""
    if not locations:
        return None
    # 1. Prefer locations that match changed functions or files
    for file, line, fn in locations:
        if not file:
            continue
        rel = os.path.relpath(file) if os.path.isabs(file) else file
        if changed_functions and (rel in changed_functions or file in changed_functions):
            fns = changed_functions.get(rel) or changed_functions.get(file) or set()
            if fn and fn in fns:
                return f"{rel}:{fn}"
        if changed_files and (rel in changed_files or file in changed_files):
            return f"{rel}:{fn}" if fn else f"{rel}:{line}" if line else rel

    # 2. Fall back to the first non-internal location
    for file, line, fn in locations:
        if not file or "site-packages" in file or "pytest" in file or "assay" in file:
            continue
        rel = os.path.relpath(file) if os.path.isabs(file) else file
        return f"{rel}:{fn}" if fn else f"{rel}:{line}" if line else rel

    # 3. Fall back to any location
    file, line, fn = locations[0]
    if file:
        rel = os.path.relpath(file) if os.path.isabs(file) else file
        return f"{rel}:{fn}" if fn else f"{rel}:{line}" if line else rel
    return None


def compare_logged_errors(
    current_cases: Dict[str, List[dict]],
    baseline_cases: Dict[str, List[dict]],
    changed_files: Optional[Set[str]] = None,
    changed_functions: Optional[Dict[str, Set[str]]] = None,
    rise_multiplier: float = 3.0,
    min_rise_tests: int = 2,
) -> List[dict]:
    """Compares candidate run errors against baseline run errors across test cases.

    current_cases: {case_id: [log_step_dicts]}
    baseline_cases: {case_id: [log_step_dicts]}
    """
    now_templates: Dict[str, dict] = defaultdict(lambda: {"cases": set(), "samples": [], "locations": [], "levels": set()})
    for case_id, logs in current_cases.items():
        for l in logs:
            tmpl = l.get("template") or template_log(l.get("message") or "")
            if not tmpl:
                continue
            d = now_templates[tmpl]
            d["cases"].add(case_id)
            if l.get("message"):
                d["samples"].append(l["message"])
            d["locations"].append((l.get("file"), l.get("line"), l.get("function")))
            d["levels"].add(l.get("level") or "ERROR")

    base_templates: Dict[str, dict] = defaultdict(lambda: {"cases": set(), "count": 0})
    for case_id, logs in baseline_cases.items():
        for l in logs:
            tmpl = l.get("template") or template_log(l.get("message") or "")
            if not tmpl:
                continue
            d = base_templates[tmpl]
            d["cases"].add(case_id)
            d["count"] += 1

    out: List[dict] = []
    for tmpl in sorted(now_templates):
        now_data = now_templates[tmpl]
        now_cases = sorted(now_data["cases"])
        level = "ERROR" if "ERROR" in now_data["levels"] or "CRITICAL" in now_data["levels"] else "WARNING"
        origin = _find_origin(now_data["locations"], changed_files, changed_functions)

        if tmpl not in base_templates:
            # 1. A template no test logged before
            cases_str = ", ".join(now_cases[:3]) + (f", and {len(now_cases) - 3} more" if len(now_cases) > 3 else "")
            origin_str = f" (from {origin})" if origin else ""
            text = f"new error logged: “{tmpl}” in {cases_str}{origin_str}"
            out.append({
                "kind": "logged_error",
                "subtype": "new",
                "template": tmpl,
                "level": level,
                "cases": now_cases,
                "cases_count": len(now_cases),
                "origin": origin,
                "sample": now_data["samples"][0] if now_data["samples"] else tmpl,
                "text": text,
                "detail": origin or f"in {len(now_cases)} tests",
                "key": f"logged_error:new:{tmpl}",
            })
        else:
            # 2. A familiar one, many times more often
            base_count = len(base_templates[tmpl]["cases"])
            now_count = len(now_cases)
            if now_count >= rise_multiplier * base_count and (now_count - base_count) >= min_rise_tests:
                cases_str = ", ".join(now_cases[:3]) + (f", and {len(now_cases) - 3} more" if len(now_cases) > 3 else "")
                text = f"error logged much more often: “{tmpl}” in {now_count} tests (was in {base_count} on baseline)"
                out.append({
                    "kind": "logged_error",
                    "subtype": "rise",
                    "template": tmpl,
                    "level": level,
                    "cases": now_cases,
                    "cases_count": now_count,
                    "base_cases_count": base_count,
                    "origin": origin,
                    "sample": now_data["samples"][0] if now_data["samples"] else tmpl,
                    "text": text,
                    "detail": f"{now_count} tests vs {base_count} on baseline",
                    "key": f"logged_error:rise:{tmpl}",
                })
            # 3. Familiar error logged at roughly the same rate is left out!

    return out
