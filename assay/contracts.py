"""Path contracts: rules about the steps a document may take, checked on every document.

A diff between two workflows can't tell a harmless change from a dangerous
one: search → order → retry → respond is fine, search → delete → respond is
not. So instead of diffing paths, Assay checks each document's path against
rules someone agreed to:

- must_include   step            every finished document ran it
- never          step            no document runs it
- before         step, other     when both run, step runs first
- only_after     step, other     step runs only once other has run
- max_runs       step, max_runs  at most this many runs of step per document (retries)
- allowed_steps  steps           nothing outside this list runs
- requires_approval  step        step runs only once it's approved (an approval step for it
                                 whose decision is "approved"; a later rejection takes it back)
- claim          claim, needs and/or state
                                 an agent run whose answer claims something (claim, a regular
                                 expression) must have the evidence for it: a successful call of
                                 the tool `needs`, and/or recorded state that says so (state:
                                 {"name": "order:*", "field": "status", "is": "refunded"}). The
                                 model proposes; the run's own record verifies. Agent runs only.

A contract can be scoped with `when` (the document must match every listed
attribute) and `unless` (a document matching any listed attribute is exempt),
over segment, document_type and processing_mode. One break is enough to open
an alert: there's no baseline to learn and no sample size to wait for.

Changes that break no contract but move traffic (a new step, a branch whose
share jumps) are reported as shifts, judged against sampling noise, for a
person to look at rather than to page anyone.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.models import Window
from assay.rootcause import order_steps

START, END = "(received)", "(done)"
MAX_DOCS = 20000  # documents checked per window
EXAMPLES = 20  # violating documents kept per contract
PAD = timedelta(days=3)  # read steps from before the window, so a document's path is whole
SETTLE = timedelta(hours=1)  # without document records, a path idle this long counts as finished
SCOPE_FIELDS = ("segment", "document_type", "processing_mode")

# kind → (fields it needs, sentence template)
KINDS = {
    "must_include": (("step",), "Every document runs {step}"),
    "never": (("step",), "{step} never runs"),
    "before": (("step", "other"), "{step} runs before {other}"),
    "only_after": (("step", "other"), "{step} runs only after {other}"),
    "max_runs": (("step", "max_runs"), "{step} runs at most {max_runs}× per document"),
    "allowed_steps": (("steps",), "Only these steps run: {steps}"),
    "requires_approval": (("step",), "{step} runs only once it's approved"),
    "claim": (("claim",), "Claiming “{claim}” needs {evidence}"),
}
APPROVAL = "approval:"  # an approval in a path: approval:<action>, its decision in the args
SEVERITIES = ("critical", "warning")


# ---------- contracts ----------

def validate(c: dict) -> Optional[str]:
    """Why a contract can't be saved, or None."""
    kind = c.get("kind")
    if kind not in KINDS:
        return f"Unknown kind '{kind}'. Use one of: {', '.join(KINDS)}."
    missing = [f for f in KINDS[kind][0] if not c.get(f)]
    if missing:
        return f"A {kind} contract needs {', '.join(missing)}."
    if kind in ("before", "only_after") and c["step"] == c["other"]:
        return "step and other must be different steps."
    if kind == "claim":
        import re
        try:
            re.compile(c["claim"])
        except re.error as exc:
            return f"claim isn't a regular expression: {exc}."
        st = c.get("state")
        if not (c.get("needs") or c.get("step")) and not st:
            return "A claim contract needs the evidence: needs (a tool that must succeed), state, or both."
        if st is not None and not (isinstance(st, dict) and st.get("name") and st.get("field") and "is" in st):
            return 'state is {"name": "order:*", "field": "status", "is": "refunded"}.'
        if (c.get("claim_in") or "answer") not in ("answer", "any"):
            return "claim_in is answer (the default) or any (the model's own text too, e.g. a self-review)."
    if kind == "max_runs" and int(c["max_runs"]) < 1:
        return "max_runs must be at least 1."
    if c.get("severity", "critical") not in SEVERITIES:
        return f"severity is one of: {', '.join(SEVERITIES)}."
    if c.get("where") is not None and not isinstance(c["where"], dict):
        return "where is an object of argument conditions, e.g. {\"confirmed\": {\"not\": true}}."
    if c.get("same") and c["kind"] not in ("only_after", "before"):
        return "same applies to only_after and before contracts."
    for scope in ("when", "unless"):
        bad = [k for k in (c.get(scope) or {}) if k not in SCOPE_FIELDS]
        if bad:
            return f"{scope} can use {', '.join(SCOPE_FIELDS)}, not {', '.join(bad)}."
    return None


def _evidence(c: dict) -> str:
    parts = []
    if c.get("needs") or c.get("step"):
        parts.append(f"a successful {c.get('needs') or c.get('step')}")
    st = c.get("state")
    if st:
        parts.append(f"{st['name']} {st['field']} {st['is']!r}")
    return " and ".join(parts)


def describe(c: dict) -> str:
    text = KINDS[c["kind"]][1].format(step=c.get("step"), other=c.get("other"), max_runs=c.get("max_runs"),
                                      steps=", ".join(c.get("steps") or []), claim=c.get("claim"),
                                      evidence=_evidence(c) if c["kind"] == "claim" else "")
    scope = lambda s: "; ".join(f"{k} is {' or '.join(map(str, v))}" for k, v in (s or {}).items())
    if c.get("where"):
        conds = [f"{k} is not {v['not']!r}" if isinstance(v, dict) and "not" in v else
                 f"{k} is one of {v['in']!r}" if isinstance(v, dict) and "in" in v else f"{k} is {v!r}"
                 for k, v in c["where"].items()]
        text = text.replace(str(c.get("step")), f"{c.get('step')} (where {' and '.join(conds)})", 1)
    if c.get("same") and c["kind"] in ("only_after", "before"):
        text += f" with the same {', '.join(c['same'])}"
    if c.get("identical") and c["kind"] == "max_runs":
        text += " with identical arguments"
    if c.get("when"):
        text += f", when {scope(c['when'])}"
    if c.get("unless"):
        text += f", unless {scope(c['unless'])}"
    return text


def load(engine: Engine, source: str) -> List[dict]:
    """Contracts that apply to a source: its own and the "*" ones."""
    t = store.path_contracts
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(or_(t.c.source == source, t.c.source == "*")).order_by(t.c.id)).all()
    return [as_dict(r) for r in rows]


def as_dict(r) -> dict:
    d = dict(r._mapping)
    d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
    d["label"] = describe(d)
    return d


def applies(c: dict, doc) -> Optional[bool]:
    """Whether a contract covers a document; None if a scoped contract can't tell (no record)."""
    if not c.get("when") and not c.get("unless"):
        return True
    if doc is None:
        return None
    if any(getattr(doc, k, None) not in v for k, v in (c.get("when") or {}).items()):
        return False
    return not any(getattr(doc, k, None) in v for k, v in (c.get("unless") or {}).items())


class Step(str):
    """A step name that also carries the step's arguments (a tool call's), so contracts
    can say "delete_order only where confirmed is true". Compares and hashes as its name."""
    args: dict
    server: Optional[str]

    def __new__(cls, name: str, args: Optional[dict] = None, server: Optional[str] = None):
        obj = super().__new__(cls, name)
        obj.args = args or {}
        obj.server = server
        return obj

    @property
    def fullname(self) -> str:
        return f"{self.server}.{self}" if self.server else str(self)


def _args(item) -> dict:
    return getattr(item, "args", None) or {}


def _where(cond: Optional[dict], args: dict) -> bool:
    """{"confirmed": true}, {"confirmed": {"not": true}}, {"region": {"in": ["eu", "uk"]}}."""
    for k, v in (cond or {}).items():
        got = args.get(k)
        if isinstance(v, dict) and "not" in v:
            if got == v["not"]:
                return False
        elif isinstance(v, dict) and "in" in v:
            if got not in v["in"]:
                return False
        elif got != v:
            return False
    return True


def _hits(c: dict, path: List[str]) -> List[int]:
    target = c.get("step")
    return [i for i, s in enumerate(path)
            if (s == target or getattr(s, "fullname", s) == target) and _where(c.get("where"), _args(s))]


def _args_text(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])


def claim_breaks(c: dict, traj: dict) -> Optional[dict]:
    """How an agent run breaks a claim contract: {"stage", "at", "seq", "detail"}, or None."""
    import fnmatch
    import re
    pat = re.compile(c["claim"], re.I)
    steps = traj.get("steps") or []
    said = [(s["seq"], s.get("text")) for s in steps if s["kind"] == "answer" or
            (c.get("claim_in") == "any" and s["kind"] == "reason")]
    if traj.get("answer") and not any(t == traj["answer"] for _, t in said):
        said.append((None, traj["answer"]))
    hit = next(((seq, m.group(0)) for seq, t in said if t for m in [pat.search(t)] if m), None)
    if hit is None:
        return None
    seq, words = hit
    tool = c.get("needs") or c.get("step")
    problems = []
    if tool:
        calls = [s for s in steps if s["kind"] == "tool" and fnmatch.fnmatch(s.get("name") or "", tool)]
        ok = [s for s in calls if not s.get("error")]
        if not calls:
            problems.append(f"{tool} was never called")
        elif not ok:
            problems.append(f"{calls[-1]['name']} was called but failed ({str(calls[-1]['error'])[:120]})")
    st = c.get("state")
    if st:
        states = [s for s in steps if s["kind"] == "state" and fnmatch.fnmatch(s.get("name") or "", st["name"])]
        if not states:
            problems.append(f"no state was recorded for {st['name']}")
        else:
            last = states[-1]
            value = (last.get("result") or {}).get(st["field"]) if isinstance(last.get("result"), dict) else None
            if value != st["is"]:
                problems.append(f"{last['name']} {st['field']} is {value!r}, not {st['is']!r}")
    if not problems:
        return None
    return {"stage": tool or st["name"], "at": None, "seq": seq,
            "detail": f"claimed “{words}” in the {'answer' if seq is None or any(s['seq'] == seq and s['kind'] == 'answer' for s in steps) else 'model text'}, "
                      f"but {' and '.join(problems)}"}


def breaks(c: dict, path: List[str], finished: bool = True, traj: Optional[dict] = None) -> Optional[dict]:
    """How a path breaks a contract: {"stage", "at", "detail"}, or None if it keeps it.
    A claim contract needs the whole agent run (traj); without it, it can't be judged here.

    `at` is the index in the path where it broke (None for a step that never
    ran). must_include is only judged once the document is finished, so a
    document still in flight isn't counted as having skipped anything. Path
    items may be Steps carrying arguments, which `where`, `same` and
    `identical` look at.
    """
    kind, step = c["kind"], c.get("step")
    if kind == "claim":
        return claim_breaks(c, traj) if traj is not None else None
    first = {}
    for i, s in enumerate(path):
        first.setdefault(s, i)
    hits = _hits(c, path) if step else []
    if kind == "must_include":
        if finished and not hits:
            return {"stage": step, "at": None, "detail": f"finished without running {step}"}
    elif kind == "never":
        if hits:
            i = hits[0]
            a = _args_text(_args(path[i]))
            return {"stage": step, "at": i, "detail": f"ran {step} (step {i + 1}{', ' + a if a else ''})"}
    elif kind == "before":
        other = c["other"]
        if hits and other in first and first[other] < hits[0]:
            return {"stage": other, "at": first[other],
                    "detail": f"ran {other} (step {first[other] + 1}) before {step} (step {hits[0] + 1})"}
    elif kind == "only_after":
        other, same = c["other"], c.get("same") or []
        for i in hits:
            ok = any(path[j] == other and all(_args(path[j]).get(k) == _args(path[i]).get(k) for k in same)
                     for j in range(i))
            if not ok:
                earlier = any(path[j] == other for j in range(i))
                why = ("after " + other + " with a different " + ", ".join(same)) if earlier and same else \
                    ("before" if other in first else "without") + " " + other
                a = _args_text({k: _args(path[i]).get(k) for k in same}) if same else ""
                return {"stage": step, "at": i, "detail": f"ran {step} (step {i + 1}{', ' + a if a else ''}) {why}"}
    elif kind == "max_runs":
        limit = int(c["max_runs"])
        if c.get("identical"):
            seen = defaultdict(list)
            for i in hits:
                seen[json.dumps(_args(path[i]), sort_keys=True, default=str)].append(i)
            worst = max(seen.values(), key=len, default=[])
            if len(worst) > limit:
                a = _args_text(_args(path[worst[0]]))
                return {"stage": step, "at": worst[limit],
                        "detail": f"ran {step} {len(worst)} times with the same arguments ({a}; limit {limit})"}
        elif len(hits) > limit:
            return {"stage": step, "at": hits[limit], "detail": f"ran {step} {len(hits)} times (limit {limit})"}
    elif kind == "requires_approval":
        decision = None
        for i, s in enumerate(path):
            if s == APPROVAL + step:
                decision = _args(s).get("decision")
            elif i in hits and decision != "approved":
                why = "without an approval" if decision is None else f"after it was {decision}, not approved"
                a = _args_text(_args(s))
                return {"stage": step, "at": i, "detail": f"ran {step} (step {i + 1}{', ' + a if a else ''}) {why}"}
    elif kind == "allowed_steps":
        allowed = set(c["steps"])
        extra = [(i, s) for i, s in enumerate(path) if s not in allowed and not s.startswith(APPROVAL)]
        if extra:
            names = list(dict.fromkeys(s for _, s in extra))
            return {"stage": names[0], "at": extra[0][0], "detail": f"ran {', '.join(names)}, not an allowed step"}
    return None


# ---------- paths ----------

def paths(source, window: Window) -> Tuple[Dict[str, List[str]], Dict[str, object], set]:
    """Each document's path (steps in order) for documents with a step in the window,
    their document records, and which of them are finished."""
    padded = Window(window.start - PAD, window.end)
    by_doc = defaultdict(list)
    active = set()
    for r in source.stage_runs(padded) or []:
        by_doc[r.document_id].append(r)
        if r.started_at is None or r.started_at >= window.start:
            active.add(r.document_id)
    ids = [d for d in by_doc if d in active][:MAX_DOCS]
    docs = {d.document_id: d for d in (source.documents(padded) or [])}
    out, finished = {}, set()
    for d in ids:
        runs = order_steps(by_doc[d])
        out[d] = [Step(r.stage, (r.outputs or {}).get("_args")) for r in runs]
        rec = docs.get(d)
        if rec is not None:
            # Finished within the window: a backfilled window mustn't see completions from its future.
            if rec.completed_at and rec.completed_at <= window.end:
                finished.add(d)
        else:
            last = max((r.finished_at or r.started_at for r in runs if (r.finished_at or r.started_at)), default=None)
            if last is not None and last <= window.end - SETTLE:
                finished.add(d)
    return out, docs, finished


def edges_of(path: List[str]) -> List[Tuple[str, str]]:
    """Moves between steps, from received to done. A repeated step (a retry) isn't a move."""
    out, prev = [], START
    for s in path:
        if s != prev:
            out.append((prev, s))
        prev = s
    out.append((prev, END))
    return out


# ---------- checking ----------

def check(source, window: Window, contracts: List[dict], doc_paths=None) -> dict:
    """Every contract against every document in the window."""
    ps, docs, finished = doc_paths or paths(source, window)
    report, violating = [], set()
    for c in contracts:
        judged, examples, count, where = 0, [], 0, Counter()
        if c["kind"] == "claim":  # judged on agent runs (their answer and state), not on paths
            report.append({**c, "label": c.get("label") or describe(c), "judged": 0, "violations": 0, "rate": None,
                           "stages": {}, "examples": [], "state": "unjudged"})
            continue
        for d, path in ps.items():
            scope = applies(c, docs.get(d))
            if not scope:
                continue
            if c["kind"] == "must_include" and d not in finished:
                continue
            judged += 1
            b = breaks(c, path, finished=True)
            if b:
                count += 1
                violating.add(d)
                where[b["stage"]] += 1
                if len(examples) < EXAMPLES:
                    examples.append({"document_id": d, "path": path, **b})
        report.append({**c, "label": c.get("label") or describe(c), "judged": judged, "violations": count,
                       "rate": count / judged if judged else None, "stages": dict(where), "examples": examples,
                       "state": "unjudged" if not judged else "broken" if count else "kept"})
    broken = [r for r in report if r["violations"]]
    return {"window": [window.start.isoformat(), window.end.isoformat()], "documents": len(ps),
            "contracts": report, "broken": len(broken),
            "violating_documents": len(violating)}


def check_document(source, document_id: str, contracts: List[dict]) -> Optional[List[dict]]:
    """The contracts one document breaks, with where."""
    detail = source.document_detail(document_id)
    if detail is None:
        return None
    doc, runs = detail[0], order_steps(detail[1])
    path = [Step(r.stage, (r.outputs or {}).get("_args")) for r in runs]
    out = []
    for c in contracts:
        if not applies(c, doc):
            continue
        b = breaks(c, path, finished=bool(getattr(doc, "completed_at", None)))
        if b:
            out.append({"contract_id": c.get("id"), "label": c.get("label") or describe(c), "kind": c["kind"],
                        "severity": c.get("severity", "critical"), **b})
    return out


def annotate(graph: dict, report: dict) -> dict:
    """Mark the workflow graph's steps and moves with the contracts broken there."""
    by_stage = defaultdict(list)
    for c in report["contracts"]:
        for stage, n in c["stages"].items():
            by_stage[stage].append({"contract_id": c.get("id"), "label": c["label"], "kind": c["kind"],
                                    "severity": c.get("severity", "critical"), "documents": n})
    bad_edges = Counter()
    for c in report["contracts"]:
        for e in c["examples"]:
            if e["at"] is None:
                continue
            prev = e["path"][e["at"] - 1] if e["at"] > 0 else START
            if prev != e["stage"]:
                bad_edges[(prev, e["stage"])] += 1
    for n in graph["nodes"]:
        v = by_stage.get(n["id"], [])
        n["contract_violations"] = v
        if any(x["severity"] == "critical" for x in v):
            n["health"] = "bad"
        elif v and n.get("health") == "ok":
            n["health"] = "warn"
    for e in graph["edges"]:
        e["violations"] = bad_edges.get((e["from"], e["to"]), 0)
    # A step that exists only in violating documents may be missing from the node list: add it.
    known = {n["id"] for n in graph["nodes"]}
    for stage, v in by_stage.items():
        if stage not in known:
            graph.setdefault("missing_steps", []).append({"id": stage, "contract_violations": v})
    graph["contracts"] = {"broken": report["broken"], "total": len(report["contracts"])}
    return graph


# ---------- shifts ----------

def _z(x1: int, n1: int, x2: int, n2: int) -> float:
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0.0
    return (x2 / n2 - x1 / n1) / se if se else 0.0


def shifts(source, window: Window, min_docs: int = 30, min_change: float = 0.02) -> dict:
    """How documents' paths moved between the previous window and this one.

    New steps and new moves are listed whenever the previous window had enough
    documents to have seen them; a move's share must change by more than 3
    standard errors and `min_change` to count. Not an alert: a shift isn't a
    break, it's something to look at (and, if it's fine, to write a contract for).
    """
    cur_p, _, _ = paths(source, window)
    prev_p, _, _ = paths(source, window.previous())
    n_cur, n_prev = len(cur_p), len(prev_p)
    out = {"documents": n_cur, "previous_documents": n_prev, "shifts": []}
    if n_cur < min_docs or n_prev < min_docs:
        out["note"] = f"Needs {min_docs} documents in this window and the one before to compare paths."
        return out

    def count(ps):
        edges, steps = Counter(), Counter()
        for path in ps.values():
            edges.update(set(edges_of(path)))
            steps.update(set(path))
        return edges, steps

    ce, cs = count(cur_p)
    pe, ps_ = count(prev_p)
    found = []
    for s in cs:
        if s not in ps_:
            found.append({"kind": "new_step", "step": s, "documents": cs[s], "share": cs[s] / n_cur,
                          "previous_share": 0.0, "message": f"{s} is new: {cs[s]:,} documents ran it, none before."})
    for s in ps_:
        if s not in cs and ps_[s] / n_prev >= 0.05:
            found.append({"kind": "gone_step", "step": s, "documents": 0, "share": 0.0,
                          "previous_share": ps_[s] / n_prev,
                          "message": f"{s} stopped running: {ps_[s] / n_prev:.0%} of documents ran it before."})
    for e in set(ce) | set(pe):
        x2, x1 = ce.get(e, 0), pe.get(e, 0)
        a, b = e
        label = f"{a} → {b}"
        if x1 == 0 and (a == START or a in ps_) and (b == END or b in ps_):
            found.append({"kind": "new_move", "from": a, "to": b, "documents": x2, "share": x2 / n_cur,
                          "previous_share": 0.0,
                          "message": f"New path {label}: {x2:,} documents ({x2 / n_cur:.1%}), none before."})
            continue
        if x1 == 0:  # an endpoint is new: listed as a new step above
            continue
        z, diff = _z(x1, n_prev, x2, n_cur), x2 / n_cur - x1 / n_prev
        if abs(z) >= 3 and abs(diff) >= min_change:
            found.append({"kind": "share", "from": a, "to": b, "documents": x2, "share": x2 / n_cur,
                          "previous_share": x1 / n_prev, "z": round(z, 1),
                          "message": f"{label} went from {x1 / n_prev:.1%} to {x2 / n_cur:.1%} of documents."})
    rank = {"new_step": 0, "new_move": 1, "gone_step": 2, "share": 3}
    found.sort(key=lambda f: (rank[f["kind"]], -abs(f["share"] - f["previous_share"])))
    out["shifts"] = found
    return out


# ---------- suggestions ----------

def suggest(source, window: Window, existing: Iterable[dict] = (), min_docs: int = 50) -> dict:
    """Contracts that the paths seen in the window already keep, for a person to confirm.

    Steps nearly every finished document runs → must_include; steps that
    always run in the same order → before (adjacent ones only, so the list
    stays short); optional steps → only_after the step that always precedes
    them; steps that repeat → max_runs at the most seen; and the list of steps
    seen → allowed_steps. Each comes with how many documents keep it.
    """
    ps, docs, finished = paths(source, window)
    existing = list(existing)
    # Learn from documents that keep the contracts already agreed, so a break isn't suggested as normal.
    done = [ps[d] for d in finished
            if not any(applies(c, docs.get(d)) and breaks(c, ps[d]) for c in existing)]
    if len(done) < min_docs:
        return {"documents": len(done), "suggestions": [],
                "note": f"Needs {min_docs} finished documents to suggest contracts; this window has {len(done)}."}
    n = len(done)
    has = Counter()
    pos = defaultdict(list)
    most = Counter()
    for path in done:
        has.update(set(path))
        for s, k in Counter(path).items():
            most[s] = max(most[s], k)
        seen = set()
        for i, s in enumerate(path):
            if s not in seen:
                pos[s].append(i / max(len(path) - 1, 1))
                seen.add(s)
    order = sorted(has, key=lambda s: sorted(pos[s])[len(pos[s]) // 2])
    core = [s for s in order if has[s] / n >= 0.995]

    def keeps(c):
        return sum(1 for p in done if breaks(c, p) is None)

    out = []
    for s in core:
        out.append({"kind": "must_include", "step": s})
    for a, b in zip(core, core[1:]):
        out.append({"kind": "before", "step": a, "other": b})
    for s in order:
        if s in core or has[s] < 5:
            continue
        # The latest core step that always ran before this one.
        with_s = [p for p in done if s in p]
        prior = [c for c in core if all(c in p and p.index(c) < p.index(s) for p in with_s)]
        if prior:
            out.append({"kind": "only_after", "step": s, "other": prior[-1]})
    for s, k in most.items():
        if k > 1:
            out.append({"kind": "max_runs", "step": s, "max_runs": k})
    out.append({"kind": "allowed_steps", "steps": order})

    same = lambda a, b: a["kind"] == b["kind"] and a.get("step") == b.get("step") and \
        a.get("other") == b.get("other") and (a["kind"] != "allowed_steps" or bool(b.get("steps")))
    result = []
    for c in out:
        if any(same(c, e) for e in existing):
            continue
        k = keeps(c)
        result.append({**c, "severity": "critical" if c["kind"] == "must_include" else "warning",
                       "label": describe(c), "documents": n, "kept_by": k, "kept_rate": k / n})
    return {"documents": n, "suggestions": result}


def save(engine: Engine, source: str, body: dict, contract_id: Optional[int] = None) -> dict:
    t = store.path_contracts
    fields = {k: body.get(k) for k in ("kind", "step", "other", "max_runs", "steps", "when", "unless", "note",
                                       "where", "same", "identical", "claim", "state", "claim_in")}
    fields["step"] = body.get("step") or body.get("needs")  # a claim's evidence tool
    fields["severity"] = body.get("severity") or "critical"
    with engine.begin() as conn:
        if contract_id is None:
            contract_id = conn.execute(t.insert().values(source=source, updated_at=datetime.utcnow(), **fields)
                                       ).inserted_primary_key[0]
        else:
            close_alerts(conn, contract_id)
            conn.execute(t.update().where(t.c.id == contract_id).values(source=source, updated_at=datetime.utcnow(),
                                                                         **fields))
        return as_dict(conn.execute(select(t).where(t.c.id == contract_id)).first())


def close_alerts(conn, contract_id: int) -> None:
    """Resolve a contract's open alert when the rule changes or goes: it was raised under the old rule."""
    a = store.alerts
    conn.execute(a.update().where(and_(a.c.kind == "contract", a.c.slice_value == str(contract_id),
                                       a.c.state == "open"))
                 .values(state="resolved", resolved_at=datetime.utcnow()))
