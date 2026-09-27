"""Document extraction, summed over a run: from the checks assay_sdk.documents records.

Per field, precision (of the values extracted, how many were right) and recall (of the values the
documents have, how many were extracted right), from each check's counts: a wrong value is a
false positive and a false negative, a missing one a false negative, an invented one a false
positive. Per run, the share of documents with every field correct, and the weighted share of
fields right. Per rule, how many documents it held on.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List, Optional

EVALUATOR = "assay.documents@1"


def _raw(r) -> dict:
    try:
        return json.loads(r.raw_output or "{}")
    except (TypeError, ValueError):
        return {}


def summarize(rows: List) -> Optional[dict]:
    """{"documents", "all_correct", "checked", "accuracy", "fields": {field: {...}}, "rules": {...}}, or
    None when the run has no document checks."""
    mine = [r for r in rows if r.evaluator == EVALUATOR]
    if not mine:
        return None
    fields: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    rules: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    docs, acc = [], []
    for r in mine:
        raw = _raw(r)
        kind = raw.get("kind")
        if kind == "document":
            docs.append(r.status == "pass")
            if raw.get("accuracy") is not None:
                acc.append(float(raw["accuracy"]))
        elif kind == "rule":
            if r.status in ("pass", "fail"):
                rules[r.field[len("rule: "):] if r.field.startswith("rule: ") else r.field][0] += r.status == "pass"
                rules[r.field[len("rule: "):] if r.field.startswith("rule: ") else r.field][1] += 1
        elif r.status in ("pass", "fail"):
            f = fields[r.field]
            for k in ("tp", "fp", "fn"):
                f[k] += float(raw.get(k) or 0)
            f[kind or "unknown"] += 1
            f["n"] += 1
    out = {}
    for name, f in fields.items():
        tp, fp, fn = f["tp"], f["fp"], f["fn"]
        out[name] = {"precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
                     "n": int(f["n"]), **{k: int(f[k]) for k in ("correct", "wrong", "missing", "invented") if f[k]}}
    return {"documents": len({r.case_id for r in mine}), "checked": len(docs), "all_correct": sum(docs),
            "accuracy": sum(acc) / len(acc) if acc else None, "fields": dict(sorted(out.items())),
            "rules": {k: {"held": v[0], "checked": v[1]} for k, v in sorted(rules.items())}}


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0%}" if v in (0, 1) else f"{v:.1%}"


def lines(now: dict, before: Optional[dict] = None) -> List[str]:
    """The report's Documents block."""
    share = now["all_correct"] / now["checked"] if now["checked"] else None
    was = before["all_correct"] / before["checked"] if before and before["checked"] else None
    head = (f"Documents    {now['documents']} · all fields correct {now['all_correct']}/{now['checked']} "
            f"({_pct(share)}{f', was {_pct(was)}' if was is not None and was != share else ''})")
    if now["accuracy"] is not None:
        prev = (before or {}).get("accuracy")
        head += f" · weighted field accuracy {_pct(now['accuracy'])}" + (
            f" (was {_pct(prev)})" if prev is not None and abs(prev - now["accuracy"]) >= 0.0005 else "")
    out = [head]
    rows = [(k, v) for k, v in now["fields"].items() if v.get("wrong") or v.get("missing") or v.get("invented")
            or (before and (before["fields"].get(k) or {}).get("recall") not in (None, v.get("recall")))]
    if rows:
        width = max(len(k) for k, _ in rows)
        out.append(f"  {'':<{width}}  precision  recall")
        for k, v in rows[:15]:
            errs = ", ".join(f"{v[e]} {e}" for e in ("wrong", "missing", "invented") if v.get(e))
            out.append(f"  {k:<{width}}  {_pct(v['precision']):>9}  {_pct(v['recall']):>6}" + (f"   {errs}" if errs else ""))
        if len(rows) > 15:
            out.append(f"  … and {len(rows) - 15} more fields")
    broken = {k: v for k, v in now["rules"].items() if v["held"] < v["checked"]}
    for k, v in broken.items():
        out.append(f"Rule         {k}: held on {v['held']} of {v['checked']}")
    return out


def markdown(now: dict, before: Optional[dict] = None) -> str:
    """One line for the PR comment, as text (the caller escapes it)."""
    share = now["all_correct"] / now["checked"] if now["checked"] else None
    was = before["all_correct"] / before["checked"] if before and before["checked"] else None
    s = f"all fields correct {now['all_correct']}/{now['checked']} ({_pct(share)}"
    s += f", was {_pct(was)})" if was is not None and was != share else ")"
    if now["accuracy"] is not None:
        s += f" · weighted field accuracy {_pct(now['accuracy'])}"
    worst = sorted(((k, v) for k, v in now["fields"].items() if v["recall"] is not None and v["recall"] < 1),
                   key=lambda kv: kv[1]["recall"])[:3]
    if worst:
        s += " · lowest recall: " + ", ".join(f"{k} {_pct(v['recall'])}" for k, v in worst)
    return s
