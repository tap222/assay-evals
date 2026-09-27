"""Document extraction, summed over a run: from the checks assay_sdk.documents records.

Per field, precision (of the values extracted, how many were right) and recall (of the values the
documents have, how many were extracted right), from each check's counts: a wrong value is a
false positive and a false negative, a missing one a false negative, an invented one a false
positive. Per run, the share of documents with every field correct, and the weighted share of
fields right. Per rule, how many documents it held on.

Document types: a confusion matrix, and precision and recall per type. Splitting: files split
right (every document on the right pages), documents right, and precision and recall over the
pages a new document starts on. Confidence, where the extractor gives one: whether a confident
value is a right one (expected calibration error), the lowest threshold whose auto-approved
values reach the target accuracy, and at the threshold you use, how many wrong values it lets
through.
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
    docs, acc, confident = [], [], []
    confusion: Dict[tuple, int] = defaultdict(int)
    split, ocr, where = defaultdict(int), defaultdict(int), defaultdict(float)
    worst_pages, ious = [], []
    tables = defaultdict(int)
    table_notes = []
    for r in mine:
        raw = _raw(r)
        kind = raw.get("kind")
        if raw.get("confidence") is not None and r.status in ("pass", "fail"):
            confident.append((float(raw["confidence"]), r.status == "pass"))
        if kind == "ocr":
            for k in ("chars", "char_errors", "words", "word_errors", "digits", "digit_errors"):
                ocr[k] += int(raw.get(k) or 0)
            ocr["pages"] += 1
            ocr["failed"] += r.status == "fail"
            if raw.get("order") is not None:
                ocr["order_num"] += raw["order"] * (raw.get("chars") or 1)
                ocr["order_den"] += raw.get("chars") or 1
                ocr["free_errors"] += int(raw.get("order_free_errors") or 0)
                ocr["free_chars"] += int(raw.get("chars") or 0)
            if raw.get("chars"):
                worst_pages.append((raw["char_errors"] / raw["chars"], r.case_id, r.field, raw.get("worst") or []))
            continue
        if kind == "table":
            tables["n"] += 1
            tables["right"] += r.status == "pass"
            tables["shape_right"] += bool(raw.get("shape_right"))
            for k in ("cells", "cells_read", "cells_right"):
                tables[k] += int(raw.get(k) or 0)
            if r.status == "fail" and getattr(r, "reason", None):
                table_notes.append(f"{r.field.split(': ', 1)[-1]} ({r.case_id}): {r.reason}")
            continue
        if kind == "location":
            where["n"] += 1
            where["right"] += r.status == "pass"
            where["wrong_page"] += raw.get("page_right") is False
            if raw.get("iou") is not None:
                ious.append(float(raw["iou"]))
            continue
        if kind == "classification":
            confusion[(raw.get("expected"), raw.get("predicted") or "(none)")] += 1
        elif kind == "split":
            split["files"] += 1
            split["right"] += r.status == "pass"
            if (raw.get("documents") or 0) > 1:
                split["multi"] += 1
                split["multi_right"] += r.status == "pass"
            for k in ("tp", "fp", "fn"):
                split[k] += int(raw.get(k) or 0)
                split["b_" + k] += int((raw.get("boundaries") or {}).get(k) or 0)
        elif kind == "document":
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
            "rules": {k: {"held": v[0], "checked": v[1]} for k, v in sorted(rules.items())},
            "types": _types(confusion), "split": _split(split), "confidence": [list(x) for x in confident],
            "tables": {"n": tables["n"], "right": tables["right"], "shape_right": tables["shape_right"],
                       "precision": _ratio(tables["cells_right"], tables["cells_read"]),
                       "recall": _ratio(tables["cells_right"], tables["cells"]),
                       "f1": _ratio(2 * tables["cells_right"], tables["cells"] + tables["cells_read"]),
                       "notes": table_notes[:3]} if tables["n"] else None,
            "ocr": _ocr(ocr, worst_pages), "locations": {"n": int(where["n"]), "right": int(where["right"]),
                                                          "wrong_page": int(where["wrong_page"]),
                                                          "mean_iou": sum(ious) / len(ious) if ious else None}
            if where["n"] else None}


def _ocr(o: Dict[str, int], worst: list) -> Optional[dict]:
    if not o.get("pages"):
        return None
    return {"pages": o["pages"], "failed": o["failed"], "cer": _ratio(o["char_errors"], o["chars"]),
            "wer": _ratio(o["word_errors"], o["words"]), "digit_error_rate": _ratio(o["digit_errors"], o["digits"]),
            "digit_errors": o["digit_errors"],
            "order": _ratio(o.get("order_num", 0), o.get("order_den", 0)),
            "order_free_cer": _ratio(o.get("free_errors", 0), o.get("free_chars", 0)) if o.get("order_den") else None,
            "worst": [[c, case, field, lines] for c, case, field, lines in sorted(worst, key=lambda x: -x[0])[:3] if c]}


def _ratio(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def _types(confusion: Dict[tuple, int]) -> Optional[dict]:
    if not confusion:
        return None
    n = sum(confusion.values())
    right = sum(v for (e, p), v in confusion.items() if e == p)
    labels = sorted({e for e, _ in confusion} | {p for _, p in confusion if p != "(none)"})
    per = {}
    for t in labels:
        tp = confusion.get((t, t), 0)
        pred = sum(v for (e, p), v in confusion.items() if p == t)
        real = sum(v for (e, p), v in confusion.items() if e == t)
        per[t] = {"precision": _ratio(tp, pred), "recall": _ratio(tp, real), "n": real}
    mistakes = sorted(((e, p, v) for (e, p), v in confusion.items() if e != p), key=lambda x: (-x[2], x[0], x[1]))
    return {"n": n, "right": right, "per_type": per, "mistakes": [list(m) for m in mistakes],
            "matrix": [[e, p, v] for (e, p), v in sorted(confusion.items())]}


def _split(s: Dict[str, int]) -> Optional[dict]:
    if not s.get("files"):
        return None
    return {"files": s["files"], "right": s["right"], "multi": s["multi"], "multi_right": s["multi_right"],
            "precision": _ratio(s["tp"], s["tp"] + s["fp"]), "recall": _ratio(s["tp"], s["tp"] + s["fn"]),
            "boundary_precision": _ratio(s["b_tp"], s["b_tp"] + s["b_fp"]),
            "boundary_recall": _ratio(s["b_tp"], s["b_tp"] + s["b_fn"])}


MIN_APPROVED = 10  # fewer auto-approved values than this can't say a threshold is safe
BINS = 10


def confidence(pairs: List[list], target: float = 0.99, threshold: Optional[float] = None) -> Optional[dict]:
    """Whether confidence tracks correctness: expected calibration error (the gap between confidence
    and accuracy, averaged over ten bins, weighted by their values), the lowest threshold whose
    values at or above it are right at least `target` of the time (with the lower end of its 95%
    interval, since 99% of a few dozen is shaky), and at `threshold`, what it approves and how many
    of those are wrong: the values that would skip review."""
    from assay.calibrate import wilson
    pairs = [(max(0.0, min(1.0, c)), ok) for c, ok in pairs]
    if not pairs:
        return None
    n = len(pairs)
    bins: Dict[int, List[tuple]] = defaultdict(list)
    for c, ok in pairs:
        bins[min(BINS - 1, int(c * BINS))].append((c, ok))
    ece = sum(abs(sum(ok for _, ok in b) / len(b) - sum(c for c, _ in b) / len(b)) * len(b) / n for b in bins.values())
    mean_conf, accuracy = sum(c for c, _ in pairs) / n, sum(ok for _, ok in pairs) / n
    best = None
    for t in sorted({c for c, _ in pairs}):
        above = [ok for c, ok in pairs if c >= t]
        if len(above) >= MIN_APPROVED and sum(above) / len(above) >= target:
            lo, _ = wilson(sum(above), len(above))
            best = {"threshold": t, "approved": len(above) / n, "accuracy": sum(above) / len(above), "low": lo}
            break
    at = None
    if threshold is not None:
        above = [ok for c, ok in pairs if c >= threshold]
        at = {"threshold": threshold, "approved": len(above) / n, "wrong": len(above) - sum(above),
              "accuracy": _ratio(sum(above), len(above)), "wrong_total": n - sum(ok for _, ok in pairs)}
    return {"n": n, "ece": ece, "mean_confidence": mean_conf, "accuracy": accuracy, "suggested": best,
            "target": target, "at": at}


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0%}" if v in (0, 1) else f"{v:.1%}"


def lines(now: dict, before: Optional[dict] = None, cfg: Optional[dict] = None) -> List[str]:
    """The report's Documents block."""
    out = _field_lines(now, before) if now["checked"] or now["fields"] else []
    broken = {k: v for k, v in now["rules"].items() if v["held"] < v["checked"]}
    for k, v in broken.items():
        out.append(f"Rule         {k}: held on {v['held']} of {v['checked']}")
    out += _type_lines(now.get("types"), (before or {}).get("types"))
    out += _split_lines(now.get("split"), (before or {}).get("split"))
    out += _confidence_lines(now, before, cfg)
    out += _ocr_lines(now.get("ocr"), (before or {}).get("ocr"))
    tb, btb = now.get("tables"), (before or {}).get("tables")
    if tb:
        share, was = _ratio(tb["right"], tb["n"]), _ratio(btb["right"], btb["n"]) if btb else None
        out.append(f"Tables       {tb['n']} · right {tb['right']}/{tb['n']} ({_pct(share)}{_was(share, was)}) · "
                   f"structure right {tb['shape_right']}/{tb['n']} · cells right: F1 {_pct(tb['f1'])}"
                   f"{_paren_was(tb['f1'], (btb or {}).get('f1'))} · precision {_pct(tb['precision'])}, recall "
                   f"{_pct(tb['recall'])}")
        out += [f"             {n}" for n in tb["notes"][:2]]
    loc, bl = now.get("locations"), (before or {}).get("locations")
    if loc:
        share, was = _ratio(loc["right"], loc["n"]), _ratio(bl["right"], bl["n"]) if bl else None
        out.append(f"Locations    {loc['n']} field{'s' * (loc['n'] != 1)} · right page and box {loc['right']}/{loc['n']} ({_pct(share)}"
                   f"{_was(share, was)})" + (f" · {loc['wrong_page']} on the wrong page" if loc["wrong_page"] else "")
                   + (f" · mean overlap {loc['mean_iou']:.2f}" if loc["mean_iou"] is not None else ""))
    return out


def _ocr_lines(o: Optional[dict], b: Optional[dict]) -> List[str]:
    if not o:
        return []
    rate = lambda k, label: f"{label} {_pct(o[k])}" + (f" (was {_pct(b[k])})" if b and b.get(k) is not None
                                                          and o[k] is not None and abs(b[k] - o[k]) >= 0.0005 else "")
    out = [f"OCR          {o['pages']} page{'s' * (o['pages'] != 1)} · " + " · ".join(
        rate(k, label) for k, label in (("cer", "characters wrong"), ("wer", "words wrong"),
                                        ("digit_error_rate", "digits wrong")) if o[k] is not None)
           + (f" · {o['failed']} over the limit" if o["failed"] else "")]
    if o.get("order") is not None and (o["order"] < 1 or (b and (b.get("order") or 1) < 1)):
        out.append(f"             reading order {_pct(o['order'])} of lines" + _paren_was(o["order"], (b or {}).get("order"))
                   + (f" · with them put back in order, characters wrong {_pct(o['order_free_cer'])}"
                      if o.get("order_free_cer") is not None else ""))
    for c, case, field, lines in o["worst"][:2]:
        out.append(f"             {case} {field}: {_pct(c)}" + (f", e.g. {lines[0]}" if lines else ""))
    return out


def _field_lines(now: dict, before: Optional[dict]) -> List[str]:
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
    return out


def _was(now: Optional[float], was: Optional[float]) -> str:
    return f", was {_pct(was)}" if was is not None and now is not None and abs(was - now) >= 0.0005 else ""


def _paren_was(now: Optional[float], was: Optional[float]) -> str:
    return f" (was {_pct(was)})" if was is not None and now is not None and abs(was - now) >= 0.0005 else ""


def _type_lines(t: Optional[dict], b: Optional[dict]) -> List[str]:
    if not t:
        return []
    share, was = _ratio(t["right"], t["n"]), _ratio(b["right"], b["n"]) if b else None
    out = [f"Types        {t['n']} classified · right {t['right']}/{t['n']} ({_pct(share)}{_was(share, was)})"]
    if t["mistakes"]:
        out.append("             " + " · ".join(f"{e} → {p} {v}" for e, p, v in t["mistakes"][:5]))
        bad = [(k, v) for k, v in t["per_type"].items() if (v["precision"] or 0) < 1 or (v["recall"] or 0) < 1]
        width = max(len(k) for k, _ in bad)
        out.append(f"  {'':<{width}}  precision  recall")
        out += [f"  {k:<{width}}  {_pct(v['precision']):>9}  {_pct(v['recall']):>6}" for k, v in bad[:10]]
    return out


def _split_lines(s: Optional[dict], b: Optional[dict]) -> List[str]:
    if not s:
        return []
    share, was = _ratio(s["right"], s["files"]), _ratio(b["right"], b["files"]) if b else None
    line = f"Splitting    {s['files']} files · split right {s['right']}/{s['files']} ({_pct(share)}{_was(share, was)})"
    if s["multi"] and s["multi"] != s["files"]:
        line += f" · with several documents {s['multi_right']}/{s['multi']}"
    return [line, f"             documents right: precision {_pct(s['precision'])}, recall {_pct(s['recall'])} · "
                  f"where a document starts: precision {_pct(s['boundary_precision'])}, recall "
                  f"{_pct(s['boundary_recall'])}"]


def _confidence_lines(now: dict, before: Optional[dict], cfg: Optional[dict]) -> List[str]:
    cfg = cfg or {}
    c = confidence(now.get("confidence") or [], cfg.get("target", 0.99), cfg.get("auto_approve"))
    if not c:
        return []
    b = confidence((before or {}).get("confidence") or [], cfg.get("target", 0.99), cfg.get("auto_approve"))
    lean = "overconfident" if c["mean_confidence"] > c["accuracy"] + 0.05 else \
        "underconfident" if c["mean_confidence"] < c["accuracy"] - 0.05 else "about right"
    out = [f"Confidence   {c['n']} values · calibration error {c['ece']:.3f}"
           + (f" (was {b['ece']:.3f})" if b and abs(b["ece"] - c["ece"]) >= 0.0005 else "")
           + f" · says {_pct(c['mean_confidence'])} on average, right {_pct(c['accuracy'])}: {lean}"]
    s = c["suggested"]
    if s:
        out.append(f"             auto-approve at {s['threshold']:g} or more: {_pct(s['approved'])} of values, "
                   f"{_pct(s['accuracy'])} right (95% interval from {_pct(s['low'])}), the target {_pct(c['target'])}")
    else:
        out.append(f"             no threshold reaches {_pct(c['target'])} right over {MIN_APPROVED} values or more")
    a = c["at"]
    if a:
        was = (b or {}).get("at") or {}
        out.append(f"             at your auto_approve {a['threshold']:g}: {_pct(a['approved'])} approved, "
                   f"{a['wrong']} wrong value{'s' * (a['wrong'] != 1)} among them"
                   + (f" (was {was['wrong']})" if was and was.get("wrong") != a["wrong"] else "")
                   + (f", of {a['wrong_total']} wrong in all: they'd skip review" if a["wrong"] else ""))
    return out


def markdown(now: dict, before: Optional[dict] = None) -> str:
    """One line for the PR comment, as text (the caller escapes it)."""
    parts = []
    t, bt = now.get("types"), (before or {}).get("types")
    if t:
        share = _ratio(t["right"], t["n"])
        parts.append(f"types right {t['right']}/{t['n']} ({_pct(share)}{_was(share, _ratio(bt['right'], bt['n']) if bt else None)})")
    sp, bs = now.get("split"), (before or {}).get("split")
    if sp:
        share = _ratio(sp["right"], sp["files"])
        parts.append(f"files split right {sp['right']}/{sp['files']} ({_pct(share)}"
                     f"{_was(share, _ratio(bs['right'], bs['files']) if bs else None)})")
    o, bo = now.get("ocr"), (before or {}).get("ocr")
    if o and o["cer"] is not None:
        parts.append(f"OCR characters wrong {_pct(o['cer'])}{_was(o['cer'], (bo or {}).get('cer'))}")
    if not now["checked"]:
        return " · ".join(parts)
    share = now["all_correct"] / now["checked"] if now["checked"] else None
    was = before["all_correct"] / before["checked"] if before and before["checked"] else None
    s = f"all fields correct {now['all_correct']}/{now['checked']} ({_pct(share)}"
    s += f", was {_pct(was)})" if was is not None and was != share else ")"
    s = " · ".join([s] + parts)
    if now["accuracy"] is not None:
        s += f" · weighted field accuracy {_pct(now['accuracy'])}"
    worst = sorted(((k, v) for k, v in now["fields"].items() if v["recall"] is not None and v["recall"] < 1),
                   key=lambda kv: kv[1]["recall"])[:3]
    if worst:
        s += " · lowest recall: " + ", ".join(f"{k} {_pct(v['recall'])}" for k, v in worst)
    return s
