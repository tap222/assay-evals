"""Document extraction, summed over a run: from the checks assay_sdk.documents records.

Per field, precision (of the values extracted, how many were right) and recall (of the values the
documents have, how many were extracted right), from each check's counts: a wrong value is a
false positive and a false negative, a missing one a false negative, an invented one a false
positive. Per run, the share of documents with every field correct, the weighted share of
fields right, and precision, recall and F1 over cells (each field one, each line-item cell one),
pooled over the documents. How wrong and invented values were made up: format (the right value
in the wrong shape), inferred (in the document, not as this field), fabricated (nowhere in it),
out of the values extracted. Fields extracted that the schema doesn't score, by name. Line items
complete (no row missing, made up or duplicated). Tables: TEDS beside the cells' F1. Per rule,
how many documents it held on.

Document types: a confusion matrix, and precision and recall per type. Splitting: files split
right (every document on the right pages), documents right, precision and recall over the pages a
new document starts on, panoptic quality, and the pages a reviewer must move to put it right (in
minutes and money with [documents] seconds_per_drag and rework_per_hour). Confidence, where the extractor gives one: whether a confident
value is a right one (expected calibration error), the lowest threshold whose auto-approved
values reach the target accuracy, and at the threshold you use, how many wrong values it lets
through.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

EVALUATOR = "assay.documents@1"
_MADE_UP = ("format", "inferred", "fabricated")
TABLE_TOLERANCE = 0.02  # the default gate on line items: fails when row F1 is surely more than this below
GATE_KEYS = ("max_errors", "min_accuracy", "min_precision", "min_recall", "min_f1", "max_drop")
Z = 1.96


def values_to_show(target: float) -> int:
    """The fewest values, all right, whose 95% interval (Wilson) clears `target`: n / (n + z^2)."""
    import math
    return math.ceil(Z * Z * target / (1 - target)) if target < 1 else 0


def _whole(now: dict) -> Dict[str, dict]:
    """The gates' names for the run as a whole: `document` (every field right) and `critical`
    (the critical values), beside the fields."""
    out = {}
    if now.get("checked"):
        out["document"] = {"accuracy": now["all_correct"] / now["checked"], "n": now["checked"],
                           "errors": now["checked"] - now["all_correct"]}
    st = now.get("stability")
    if st:
        out["stability"] = {"accuracy": st["same"] / st["fields"], "n": st["fields"],
                            "errors": st["fields"] - st["same"]}
    for name, sl in (now.get("slices") or {}).items():
        out[f"document[{name}]"] = {"accuracy": sl["zero_errors"], "n": sl["n"],
                                    "errors": sl["n"] - round(sl["zero_errors"] * sl["n"])}
    c = now.get("critical")
    if c:
        out["critical"] = {"accuracy": c["right"] / c["values"], "errors": c["values"] - c["right"], "n": c["values"]}
    return out


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
    confused, words_confused = defaultdict(int), defaultdict(int)
    rankings: Dict[str, dict] = {}
    facet_docs: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    slice_cases: Dict[tuple, Dict[str, bool]] = defaultdict(dict)  # per slice: case -> every field right
    repeats: Dict[tuple, List[tuple]] = defaultdict(list)  # (case, field) -> [(value, passed)] over attempts
    worst_pages, ious = [], []
    tables, cells, made_up, unscored = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    items, teds, crit = defaultdict(int), [], defaultdict(int)
    table_notes = []
    for r in mine:
        raw = _raw(r)
        kind = raw.get("kind")
        if raw.get("confidence") is not None and r.status in ("pass", "fail"):
            confident.append((float(raw["confidence"]), r.status == "pass"))
        if kind == "ocr":
            for k in ("chars", "char_errors", "words", "word_errors", "digits", "digit_errors", "letters",
                      "letter_errors"):
                ocr[k] += int(raw.get(k) or 0)
            for a, b, n in raw.get("confusions") or ():
                confused[(a, b)] += n
            for a, b, n in raw.get("word_confusions") or ():
                words_confused[(a, b)] += n
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
        if kind == "ocr_rank":  # engines ranked against corrected text: not ground truth, kept apart
            rankings[r.field] = {**raw, "agrees": r.status == "pass"}
            continue
        if kind == "table":
            tables["n"] += 1
            tables["right"] += r.status == "pass"
            tables["shape_right"] += bool(raw.get("shape_right"))
            for k in ("cells", "cells_read", "cells_right"):
                tables[k] += int(raw.get(k) or 0)
            if raw.get("teds") is not None:
                teds.append(float(raw["teds"]))
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
            if raw.get("panoptic"):
                split["scored"] += 1
                for k in ("iou", "tp", "fp", "fn"):
                    split["pq_" + k] += raw["panoptic"].get(k) or 0
                split["drags"] += int(raw.get("drags") or 0)
                split["pages"] += int(raw.get("pages") or 0)
        elif kind == "document":
            docs.append(r.status == "pass")
            for k, v in (raw.get("facets") or {}).items():
                if k == "template":
                    continue  # an id, one slice per supplier: too many to read; template_seen is the slice
                d = facet_docs[(k, v)]
                d["n"] += 1
                d["zero"] += r.status == "pass"
                slice_cases[(k, v)][r.case_id] = r.status == "pass"
                if raw.get("accuracy") is not None:
                    d["acc"] += float(raw["accuracy"])
                    d["acc_n"] += 1
                for c in ("tp", "fp", "fn"):
                    d[c] += int((raw.get("cells") or {}).get(c) or 0)
            if raw.get("critical_correct") is not None:
                crit["documents"] += 1
                crit["documents_right"] += bool(raw["critical_correct"])
            if raw.get("accuracy") is not None:
                acc.append(float(raw["accuracy"]))
            for k in ("tp", "fp", "fn"):
                cells[k] += int((raw.get("cells") or {}).get(k) or 0)
            for k in raw.get("unscored") or ():
                unscored[k] += 1
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
            f.setdefault("per_case", defaultdict(list))[r.case_id].append(float(raw.get("share", r.status == "pass")))
            if "value" in raw and not raw.get("part_of"):
                repeats[(r.case_id, r.field)].append((raw["value"], r.status == "pass"))
            f["table"] = f["table"] or "rows" in raw
            if raw.get("critical"):
                f["critical"] = 1
                crit["values"] += 1
                crit["right"] += r.status == "pass"
            if raw.get("made_up"):
                f[raw["made_up"]] += 1
            if "rows" in raw:  # a table of line items: complete when no row is missing, made up or repeated
                items["n"] += 1
                gaps = [int(raw.get(k) or 0) for k in ("rows_missing", "rows_invented", "rows_duplicated")]
                items["complete"] += not any(gaps)
                for k, g in zip(("missing", "invented", "duplicated"), gaps):
                    items[k] += g
            if (not raw.get("part_of") or raw.get("grouped")) and "rows" not in raw and "members" not in raw \
                    and (raw.get("tp") or raw.get("fp")):
                made_up["values"] += 1
                made_up["grounded"] += bool(raw.get("grounded"))
                if raw.get("made_up"):
                    made_up[raw["made_up"]] += 1
    out = {}
    for name, f in fields.items():
        per_case = {c: sum(v) / len(v) for c, v in (f.pop("per_case", None) or {}).items()}
        tp, fp, fn = f["tp"], f["fp"], f["fn"]
        out[name] = {"precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
                     "f1": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None,
                     "errors": int(f["wrong"] + f["missing"] + f["invented"]), "table": bool(f["table"]),
                     "accuracy": f["correct"] / f["n"] if f["n"] else None, "critical": bool(f["critical"]),
                     "per_case": per_case,
                     "n": int(f["n"]), **{k: int(f[k]) for k in ("correct", "wrong", "missing", "invented", *_MADE_UP)
                                          if f[k]}}
    return {"documents": len({r.case_id for r in mine}), "checked": len(docs), "all_correct": sum(docs),
            "accuracy": sum(acc) / len(acc) if acc else None, "fields": dict(sorted(out.items())),
            "cells": {"precision": _ratio(cells["tp"], cells["tp"] + cells["fp"]),
                      "recall": _ratio(cells["tp"], cells["tp"] + cells["fn"]),
                      "f1": _ratio(2 * cells["tp"], 2 * cells["tp"] + cells["fp"] + cells["fn"])}
            if cells["tp"] + cells["fp"] + cells["fn"] else None,
            "unscored": dict(sorted(unscored.items(), key=lambda kv: (-kv[1], kv[0]))),
            "ocr_rankings": rankings or None,
            "stability": _stability(repeats),
            "slices": {f"{k}={v}": {"n": int(d["n"]), "zero_errors": d["zero"] / d["n"],
                                    "accuracy": d["acc"] / d["acc_n"] if d["acc_n"] else None,
                                    "cell_f1": _ratio(2 * d["tp"], 2 * d["tp"] + d["fp"] + d["fn"]),
                                    "cases": slice_cases[(k, v)]}
                       for (k, v), d in sorted(facet_docs.items())} or None,
            "made_up": {k: made_up[k] for k in ("values", "grounded", *_MADE_UP)} if made_up["values"] else None,
            "rules": {k: {"held": v[0], "checked": v[1]} for k, v in sorted(rules.items())},
            "types": _types(confusion), "split": _split(split), "confidence": [list(x) for x in confident],
            "tables": {"n": tables["n"], "right": tables["right"], "shape_right": tables["shape_right"],
                       "precision": _ratio(tables["cells_right"], tables["cells_read"]),
                       "recall": _ratio(tables["cells_right"], tables["cells"]),
                       "f1": _ratio(2 * tables["cells_right"], tables["cells"] + tables["cells_read"]),
                       "teds": sum(teds) / len(teds) if teds else None,
                       "notes": table_notes[:3]} if tables["n"] else None,
            "line_items": dict(items) if items["n"] else None,
            "critical": dict(crit) if crit["values"] else None,
            "ocr": _ocr(ocr, worst_pages, confused, words_confused),
            "locations": {"n": int(where["n"]), "right": int(where["right"]), "wrong_page": int(where["wrong_page"]),
                          "mean_iou": sum(ious) / len(ious) if ious else None} if where["n"] else None}


def paired_drop(now: Optional[Dict[str, float]], before: Optional[Dict[str, float]]
                ) -> Optional[Tuple[float, float, float, int]]:
    """How far a per-document score fell, on the documents in both runs: (drop, low, high, n), a
    paired t interval (95%) on each document's before minus now. The document is the unit, so a
    long table doesn't count for more than a short one. None with fewer than two documents."""
    from assay.flaky import paired_change
    common = sorted(set(now or {}) & set(before or {}))
    if len(common) < 2:
        return None
    point, lo, hi = paired_change([now[c] for c in common], [before[c] for c in common])  # before - now
    return point, lo, hi, len(common)


def slice_changes(now: Optional[dict], before: Optional[dict], alpha: float = 0.05) -> Dict[str, dict]:
    """Per slice, whether its documents with zero errors fell beyond chance: an exact one-sided
    McNemar test on the documents in both runs (right before and wrong now, against the reverse),
    Benjamini-Hochberg across the slices so many slices don't make false alarms.
    {slice: {"drop", "was", "p", "q", "worse", "lost", "gained", "n"}}."""
    from assay.flaky import _binom_cdf, bh
    out = {}
    for name, s in (now or {}).items():
        b = (before or {}).get(name)
        if not b or not b.get("cases") or not s.get("cases"):
            continue
        common = set(s["cases"]) & set(b["cases"])
        lost = sum(1 for c in common if b["cases"][c] and not s["cases"][c])
        gained = sum(1 for c in common if s["cases"][c] and not b["cases"][c])
        p = 1.0 if not lost else 1 - _binom_cdf(lost - 1, lost + gained, 0.5)  # P(X >= lost)
        out[name] = {"drop": b["zero_errors"] - s["zero_errors"], "was": b["zero_errors"], "p": p,
                     "lost": lost, "gained": gained, "n": len(common)}
    names = sorted(out)
    for name, q in zip(names, bh([out[n]["p"] for n in names])):
        out[name].update(q=q, worse=q <= alpha and out[name]["drop"] > 0)
    return out


def check_gates(now: Optional[dict], before: Optional[dict], gates: Optional[dict]) -> List[dict]:
    """Per-field gates, beside the per-case regressions: an average can stay plausible while one
    field collapses, and some fields can't afford a single error. [documents.gates] in assay.toml:

        tax_number = { max_errors = 0 }     # one wrong value fails the run, baseline or not
        line_items = { max_drop = 0.02 }    # row F1 may fall at most 2 points below the baseline

    max_errors (values wrong, missing or invented; for line items, documents with a row wrong),
    min_accuracy (the share right), min_precision, min_recall, min_f1, and max_drop: the drop from
    the baseline tolerated, tested on the documents in both runs (a paired t interval on each
    document's F1). It fails when even the optimistic end of the interval is a bigger drop, and
    warns (could be worse: add documents) when only the pessimistic end is, as release gates do. `document` gates the documents with every field right, `critical` the critical
    values (score_document critical=): critical = { min_accuracy = 0.999 }, and
    `document[facet=value]` one slice of documents (score_document facets=):
    "document[template_seen=unseen]" = { min_accuracy = 0.9 }. `stability` gates the fields with the
    same value on every attempt (assay test --repeat): stability = { min_accuracy = 0.99 }. Every line-items
    table is gated at max_drop = TABLE_TOLERANCE unless configured (`line_items = {}` turns it off).
    A configured field this run didn't score fails: a gate can't pass on nothing.
    [{"field", "rule", "value", "limit", "was", "passed", "why", "default"}]."""
    if not now or not (now.get("fields") or now.get("checked")):
        return []
    fields, prev = {**now["fields"], **_whole(now)}, {**((before or {}).get("fields") or {}), **_whole(before or {})}
    rules = {k: dict(v) for k, v in (gates or {}).items()}
    defaults = {k for k, v in fields.items() if v.get("table") and k not in rules}
    rules.update({k: {"max_drop": TABLE_TOLERANCE} for k in defaults})
    out = []
    for name, rule in sorted(rules.items()):
        f = fields.get(name)
        if f is None:
            out.append({"field": name, "rule": "scored", "value": None, "limit": None, "was": None, "passed": False,
                        "why": f"{name}: not scored in this run; a gate can't pass on nothing", "default": False})
            continue
        for key, limit in rule.items():
            g = {"field": name, "rule": key, "limit": limit, "was": None, "default": name in defaults}
            if key == "max_errors":
                g.update(value=f["errors"], passed=f["errors"] <= limit,
                         why=f"{name}: {f['errors']} wrong, missing or invented, at most {int(limit)} allowed")
            elif key == "max_drop":
                was = (prev.get(name) or {}).get("f1")
                if was is None or f.get("f1") is None:
                    continue  # nothing to compare with yet
                d = paired_drop(f.get("per_case"), (prev.get(name) or {}).get("per_case"))
                if d is None:
                    continue  # fewer than two documents in both runs: nothing to test
                point, lo, hi, n = d
                surely, maybe = lo > limit + 1e-9, hi > limit + 1e-9
                g.update(value=f["f1"], was=was, passed=not surely, unsure=maybe and not surely, drop=point,
                         interval=(lo, hi), n=n,
                         why=f"{name}: F1 {_pct(f['f1'])}, was {_pct(was)}; per document down {point * 100:.1f} "
                             f"points (95% interval {lo * 100:.1f} to {hi * 100:.1f}, {n} documents), "
                             + (f"surely more than the {limit * 100:g} allowed" if surely else
                                f"could be more than the {limit * 100:g} allowed: add documents to tell" if maybe
                                else f"within the {limit * 100:g} allowed"))
            else:
                metric = key[len("min_"):]
                v = f.get(metric)
                if v is None:
                    continue  # nothing extracted, or nothing to extract: not checkable
                g.update(value=v, passed=v >= limit - 1e-9,
                         why=f"{name}: {metric} {_pct(v, 1 if limit < 0.999 else 2)}, at least "
                             f"{_pct(limit, 1 if limit < 0.999 else 2)} required")
            out.append(g)
    return out


def _gate_lines(now: dict, before: Optional[dict], cfg: Optional[dict]) -> List[str]:
    gates = check_gates(now, before, (cfg or {}).get("gates"))
    if not gates:
        return []
    bad = [g for g in gates if g["passed"] is False]
    unsure = [g for g in gates if g.get("unsure")]
    head = f"Gates        {len(gates) - len(bad)} of {len(gates)} held"
    return [head + (":" if bad or unsure else "")] + [f"  failed: {g['why']}" for g in bad] + \
        [f"  unsure: {g['why']}" for g in unsure]


def _stability(repeats: Dict[tuple, List[tuple]]) -> Optional[dict]:
    """The same document extracted several times (assay test --repeat): fields with the same value
    every attempt, compared on what the value means (canonical). A field wrong every time, but
    differently each time, never flips pass to fail, so flakiness can't see it; it's named here."""
    many = {k: v for k, v in repeats.items() if len(v) > 1}
    if not many:
        return None
    same = {k for k, v in many.items() if len({x for x, _ in v}) == 1}
    cases = defaultdict(list)
    for (case, _), v in many.items():
        cases[case].append(len({x for x, _ in v}) == 1)
    moving = [[case, field, sorted({x for x, _ in v})] for (case, field), v in sorted(many.items())
              if (case, field) not in same and not any(ok for _, ok in v)]
    return {"attempts": max(len(v) for v in many.values()), "fields": len(many), "same": len(same),
            "documents": len(cases), "documents_same": sum(all(v) for v in cases.values()),
            "wrong_and_moving": moving[:20], "wrong_and_moving_n": len(moving),
            "flipping": sum(1 for (k, v) in many.items() if k not in same and any(ok for _, ok in v)
                            and not all(ok for _, ok in v))}


def _ocr(o: Dict[str, int], worst: list, confused: Optional[dict] = None,
         words_confused: Optional[dict] = None) -> Optional[dict]:
    if not o.get("pages"):
        return None
    top = lambda d: [[a, b, n] for (a, b), n in sorted((d or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:40]]
    return {"pages": o["pages"], "failed": o["failed"], "cer": _ratio(o["char_errors"], o["chars"]),
            "wer": _ratio(o["word_errors"], o["words"]), "digit_error_rate": _ratio(o["digit_errors"], o["digits"]),
            "letter_error_rate": _ratio(o.get("letter_errors", 0), o.get("letters", 0)),
            "digit_errors": o["digit_errors"], "confusions": top(confused), "word_confusions": top(words_confused),
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
            "boundary_recall": _ratio(s["b_tp"], s["b_tp"] + s["b_fn"]),
            **({"pq": _ratio(s["pq_iou"], s["pq_tp"] + 0.5 * s["pq_fp"] + 0.5 * s["pq_fn"]),
                "drags": int(s["drags"]), "pages": int(s["pages"]), "scored": int(s["scored"])}
               if s.get("scored") else {})}


MIN_APPROVED = 10  # fewer auto-approved values than this can't say a threshold is safe
BINS = 10
THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99)  # the risk-coverage table's rows
BAND = 0.9  # the practical test: do values stated at 0.90 or more come out right that often?


def risk_coverage(pairs: List[tuple]) -> Dict[str, float]:
    """Selective prediction: approve values from the most confident down; at each point the share
    approved (coverage) and the share of those wrong (risk). AURC is the mean risk over the curve
    (lower is better); the best possible, for the same accuracy, puts every wrong value last.
    Values tied on confidence are approved together."""
    n = len(pairs)
    if not n:
        return {"aurc": 0.0, "best": 0.0}
    by = defaultdict(lambda: [0, 0])
    for c, ok in pairs:
        by[c][0] += 1
        by[c][1] += not ok
    area, seen, wrong = 0.0, 0, 0
    for c in sorted(by, reverse=True):
        k, w = by[c]
        seen, wrong = seen + k, wrong + w
        area += k * wrong / seen
    right = sum(ok for _, ok in pairs)
    best = sum(max(0, k - right) / k for k in range(1, n + 1))
    return {"aurc": area / n, "best": best / n}


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
    curve = []
    for t in sorted(set(THRESHOLDS) | ({threshold} if threshold is not None else set()), reverse=True):
        above = [(c, ok) for c, ok in pairs if c >= t]
        right = sum(ok for _, ok in above)
        curve.append({"threshold": t, "approved": len(above) / n, "n": len(above), "wrong": len(above) - right,
                      "accuracy": _ratio(right, len(above)), "interval": wilson(right, len(above))})
    band_t = threshold if threshold is not None else BAND
    band = [(c, ok) for c, ok in pairs if c >= band_t]
    band_out = {"threshold": band_t, "n": len(band), "says": sum(c for c, _ in band) / len(band),
                "right": sum(ok for _, ok in band) / len(band),
                "interval": wilson(sum(ok for _, ok in band), len(band))} if band else None
    return {"n": n, "ece": ece, "mean_confidence": mean_conf, "accuracy": accuracy, "suggested": best,
            "target": target, "at": at, "curve": curve, "band": band_out, **risk_coverage(pairs)}


def _pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v:.0%}" if v in (0, 1) else f"{v:.{digits}%}"


def lines(now: dict, before: Optional[dict] = None, cfg: Optional[dict] = None) -> List[str]:
    """The report's Documents block."""
    out = _field_lines(now, before, cfg) if now["checked"] or now["fields"] else []
    out += _gate_lines(now, before, cfg)
    out += _slice_lines(now.get("slices"), (before or {}).get("slices"))
    out += _stability_lines(now.get("stability"), (before or {}).get("stability"))
    broken = {k: v for k, v in now["rules"].items() if v["held"] < v["checked"]}
    for k, v in broken.items():
        out.append(f"Rule         {k}: held on {v['held']} of {v['checked']}")
    out += _type_lines(now.get("types"), (before or {}).get("types"))
    out += _split_lines(now.get("split"), (before or {}).get("split"), cfg)
    out += _confidence_lines(now, before, cfg)
    out += _ocr_lines(now.get("ocr"), (before or {}).get("ocr"))
    out += _ranking_lines(now.get("ocr_rankings"), (before or {}).get("ocr_rankings"))
    tb, btb = now.get("tables"), (before or {}).get("tables")
    if tb:
        share, was = _ratio(tb["right"], tb["n"]), _ratio(btb["right"], btb["n"]) if btb else None
        out.append(f"Tables       {tb['n']} · right {tb['right']}/{tb['n']} ({_pct(share)}{_was(share, was)}) · "
                   f"structure right {tb['shape_right']}/{tb['n']} · cells right: F1 {_pct(tb['f1'])}"
                   f"{_paren_was(tb['f1'], (btb or {}).get('f1'))} · precision {_pct(tb['precision'])}, recall "
                   f"{_pct(tb['recall'])}"
                   + (f" · TEDS {_pct(tb['teds'])}{_paren_was(tb['teds'], (btb or {}).get('teds'))}"
                      if tb.get("teds") is not None else ""))
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
                                        ("digit_error_rate", "digits wrong"), ("letter_error_rate", "letters wrong"))
        if o.get(k) is not None)
           + (f" · {o['failed']} over the limit" if o["failed"] else "")]
    if o.get("order") is not None and (o["order"] < 1 or (b and (b.get("order") or 1) < 1)):
        out.append(f"             reading order {_pct(o['order'])} of lines" + _paren_was(o["order"], (b or {}).get("order"))
                   + (f" · with them put back in order, characters wrong {_pct(o['order_free_cer'])}"
                      if o.get("order_free_cer") is not None else ""))
    for c, case, field, lines in o["worst"][:2]:
        out.append(f"             {case} {field}: {_pct(c)}" + (f", e.g. {lines[0]}" if lines else ""))
    out += confusion_lines(o.get("confusions"), (b or {}).get("confusions") if b else None, "read as")
    out += confusion_lines(o.get("word_confusions"), (b or {}).get("word_confusions") if b else None, "words read as")
    return out


def _stability_lines(s: Optional[dict], b: Optional[dict]) -> List[str]:
    if not s:
        return []
    share, was = s["same"] / s["fields"], (b["same"] / b["fields"]) if b and b.get("fields") else None
    out = [f"Stability    {s['attempts']} attempts · fields with the same value every time {_pct(share)} "
           f"({s['same']}/{s['fields']}){_was(share, was)} · documents fully repeatable "
           f"{s['documents_same']}/{s['documents']}"]
    if s["wrong_and_moving_n"]:
        case, field, values = s["wrong_and_moving"][0]
        out.append(f"             {s['wrong_and_moving_n']} wrong every time and never the same way, which pass/fail "
                   f"can't see as flaky: e.g. {field} in {case}: {', '.join(repr(v) for v in values[:4])}")
    return out


def _slice_lines(now: Optional[dict], before: Optional[dict]) -> List[str]:
    """Each facet's slices (score_document facets=): documents with zero errors, field accuracy
    and cell F1, against the baseline's; the ones that got worse first, since an average that
    held can hide a slice that didn't."""
    if not now:
        return []
    ch = slice_changes(now, before)
    rows = []
    for name, s in now.items():
        was = ((before or {}).get(name) or {}).get("zero_errors")
        drop = was - s["zero_errors"] if was is not None else 0.0
        rows.append((-drop, name, s, was))
    rows.sort(key=lambda r: (not ch.get(r[1], {}).get("worse"), r[0], r[1]))  # worse beyond chance first
    worse = [r for r in rows if ch.get(r[1], {}).get("worse")]
    out = [f"Slices       {len(rows)} by facet · documents with zero errors"
           + (f" · worse beyond chance: {', '.join(r[1] for r in worse[:3])}" if worse else "")]
    width = max(len(r[1]) for r in rows[:12])
    for d, name, s, was in rows[:12]:
        c = ch.get(name) or {}
        verdict = "" if -d <= 0 or not c else \
            f", down {-d * 100:.1f} points: worse beyond chance, p {c['q']:.3f}" if c.get("worse") else \
            f", down {-d * 100:.1f} points, within chance (p {c['q']:.2f}, {c['lost']} of {c['n']} documents)"
        change = "" if was is None or abs(was - s["zero_errors"]) < 0.0005 else f" (was {_pct(was)}{verdict})"
        out.append(f"  {name:<{width}}  {s['n']:>4} · zero errors {_pct(s['zero_errors'])}{change}"
                   + (f" · field accuracy {_pct(s['accuracy'])}" if s.get("accuracy") is not None else "")
                   + (f" · cell F1 {_pct(s['cell_f1'])}" if s.get("cell_f1") is not None else ""))
    if len(rows) > 12:
        out.append(f"  ... and {len(rows) - 12} more")
    return out


def _ranking_lines(now: Optional[dict], before: Optional[dict]) -> List[str]:
    """OCR engines ranked without labels (rank_ocr), and on the labelled pages, whether that ranking
    holds: how far to trust it on the rest."""
    out = []
    for name, r in sorted((now or {}).items()):
        order = ", ".join(f"{e} {_pct(r['scores'][e])}" for e in r["order"])
        line = (f"OCR ranking  {len(r['order'])} engines · {r['pages']} page{'s' * (r['pages'] != 1)} · against text "
                f"corrected by {', '.join(r['correctors'])} (no labels): {order}")
        out.append(line if name == "ocr ranking" else f"{line} ({name})")
        if r.get("labelled"):
            best = max(r["truth"], key=r["truth"].get)
            out.append(f"             on the {r['labelled']} labelled page{'s' * (r['labelled'] != 1)}: "
                       + ("the same best engine" if best == r["order"][0] else f"{best} is best, not {r['order'][0]}")
                       + (f" · Kendall tau {r['kendall']:.2f}" if r.get("kendall") is not None else "")
                       + (f" · NDCG {r['ndcg']:.2f}" if r.get("ndcg") is not None else ""))
        b = (before or {}).get(name)
        if b and b.get("order") != r["order"]:
            out.append(f"             the order was {', '.join(b['order'])}")
    return out


def confusion_diff(now: Optional[list], before: Optional[list], min_change: int = 2) -> Dict[str, list]:
    """Confusions against the baseline's: new (not there before), more (up by min_change or more),
    fewer, and gone. Each [on the page, read as, now, before]."""
    a = {(x, y): n for x, y, n in now or ()}
    b = {(x, y): n for x, y, n in before or ()}
    out: Dict[str, list] = {"new": [], "more": [], "fewer": [], "gone": []}
    for k in sorted(set(a) | set(b), key=lambda k: -abs(a.get(k, 0) - b.get(k, 0))):
        n, m = a.get(k, 0), b.get(k, 0)
        kind = "new" if not m else "gone" if not n else "more" if n - m >= min_change else \
            "fewer" if m - n >= min_change else None
        if kind:
            out[kind].append([k[0], k[1], n, m])
    return out


def _show(a: str, b: str) -> str:
    return f"{a!r} as {b!r}" if a and b else f"{a!r} lost" if a else f"{b!r} added"


def confusion_lines(now: Optional[list], before: Optional[list], label: str) -> List[str]:
    """The most common confusions, and, against a baseline, what changed: a confusion a new version
    brought, or one it fixed."""
    if not now and not before:
        return []
    out = []
    if now:
        out.append(f"             {label}: " + ", ".join(f"{_show(a, b)} {n}" for a, b, n in now[:5]))
    if before is not None:
        d = confusion_diff(now, before)
        parts = [f"new {', '.join(f'{_show(a, b)} {n}' for a, b, n, _ in d['new'][:3])}" if d["new"] else "",
                 f"more {', '.join(f'{_show(a, b)} {n} (was {m})' for a, b, n, m in d['more'][:3])}"
                 if d["more"] else "",
                 f"fixed {', '.join(f'{_show(a, b)} (was {m})' for a, b, _, m in d['gone'][:3])}" if d["gone"] else ""]
        parts = [p for p in parts if p]
        if parts:
            out.append("               since the baseline: " + "; ".join(parts))
    return out


def _critical_lines(now: dict, before: Optional[dict], cfg: Optional[dict]) -> List[str]:
    """The critical fields: their accuracy with its 95% interval, the documents with all of them
    right, and whether the run has enough values to show the target a gate sets."""
    from assay.calibrate import wilson
    c, b = now.get("critical"), (before or {}).get("critical")
    if not c:
        return []
    names = [k for k, v in now["fields"].items() if v.get("critical")]
    acc, lo_hi = c["right"] / c["values"], wilson(c["right"], c["values"])
    was = b["right"] / b["values"] if b and b.get("values") else None
    line = (f"  critical fields ({', '.join(names[:5])}{', ...' if len(names) > 5 else ''}): {c['right']:,} of "
            f"{c['values']:,} right ({_pct(acc, 2)}, 95% interval {_pct(lo_hi[0], 2)} to {_pct(lo_hi[1], 2)}"
            + (f", was {_pct(was, 2)}" if was is not None and abs(was - acc) >= 0.00005 else "") + ")")
    if c.get("documents"):
        line += f" · documents with all of them right {c['documents_right']}/{c['documents']}"
    out = [line]
    target = (((cfg or {}).get("gates") or {}).get("critical") or {}).get("min_accuracy")
    if target and lo_hi[0] < target:
        need, low = values_to_show(target), c["values"] / (c["values"] + Z * Z)
        out.append(f"    {_pct(target, 2)} can't be shown with {c['values']:,} values: "
                   + (f"even all right, the interval's low end would be {_pct(low, 2)}; "
                      f"it takes {need:,} in a row" if c["values"] < need else
                      f"the interval's low end is {_pct(lo_hi[0], 2)}")
                   + ". The gate checks the share right; this is how far to trust it.")
    return out


def _field_lines(now: dict, before: Optional[dict], cfg: Optional[dict] = None) -> List[str]:
    share = now["all_correct"] / now["checked"] if now["checked"] else None
    was = before["all_correct"] / before["checked"] if before and before["checked"] else None
    head = (f"Documents    {now['documents']} · all fields correct {now['all_correct']}/{now['checked']} "
            f"({_pct(share)}{f', was {_pct(was)}' if was is not None and was != share else ''})")
    if now["accuracy"] is not None:
        prev = (before or {}).get("accuracy")
        head += f" · weighted field accuracy {_pct(now['accuracy'])}" + (
            f" (was {_pct(prev)})" if prev is not None and abs(prev - now["accuracy"]) >= 0.0005 else "")
    c, bc = now.get("cells"), (before or {}).get("cells")
    if c:
        head += f" · cell F1 {_pct(c['f1'])}" + (
            f" (was {_pct(bc['f1'])})" if bc and abs(bc["f1"] - c["f1"]) >= 0.0005 else "") + \
            f", precision {_pct(c['precision'])}, recall {_pct(c['recall'])}"
    out = [head]
    out += _critical_lines(now, before, cfg)
    m, bm = now.get("made_up"), (before or {}).get("made_up")
    if m and (any(m[k] for k in _MADE_UP) or bm and any(bm[k] for k in _MADE_UP)):
        parts = [f"{m[k]} {k}" + (f" (was {bm[k]})" if bm and bm[k] != m[k] else "") for k in _MADE_UP
                 if m[k] or (bm and bm[k]) or k != "format" and m["grounded"]]
        out.append(f"  made up, of {m['values']} values extracted: " + ", ".join(parts)
                   + ("" if m["grounded"] else " (inferred and fabricated need the text: score_document(..., text=))"))
    li, bli = now.get("line_items"), (before or {}).get("line_items")
    if li and (li["complete"] < li["n"] or bli and bli["complete"] < bli["n"]):
        gaps = [f"{li[k]} row{'s' * (li[k] != 1)} {k}" for k in ("missing", "invented", "duplicated") if li.get(k)]
        changed = bli and (bli["complete"], bli["n"]) != (li["complete"], li["n"])
        out.append(f"  line items complete {li['complete']}/{li['n']}"
                   + (f" (was {bli['complete']}/{bli['n']})" if changed else "") + (": " + ", ".join(gaps) if gaps else ""))
    if now.get("unscored"):
        names = list(now["unscored"].items())
        out.append("  extracted but not in the schema, so not scored: "
                   + ", ".join(f"{k} ({n} document{'s' if n > 1 else ''})" for k, n in names[:6])
                   + (f" and {len(names) - 6} more" if len(names) > 6 else ""))
    rows = [(k, v) for k, v in now["fields"].items() if v.get("wrong") or v.get("missing") or v.get("invented")
            or (before and (before["fields"].get(k) or {}).get("recall") not in (None, v.get("recall")))]
    if rows:
        width = max(len(k) for k, _ in rows)
        out.append(f"  {'':<{width}}  precision  recall")
        for k, v in rows[:15]:
            errs = ", ".join(f"{v[e]} {e}" for e in ("wrong", "missing", "invented", *_MADE_UP) if v.get(e))
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


def drag_cost(drags: int, cfg: Optional[dict]) -> str:
    """ "about 4 minutes by hand ($2.00)", from [documents] seconds_per_drag and rework_per_hour."""
    sec = (cfg or {}).get("seconds_per_drag")
    if not sec or not drags:
        return ""
    minutes = drags * sec / 60
    rate = (cfg or {}).get("rework_per_hour")
    return (f", about {minutes:,.0f} minute{'s' * (round(minutes) != 1)} by hand" if minutes >= 1
            else f", about {drags * sec:,.0f} seconds by hand") + (f" (${minutes / 60 * rate:,.2f})" if rate else "")


def _split_lines(s: Optional[dict], b: Optional[dict], cfg: Optional[dict] = None) -> List[str]:
    if not s:
        return []
    share, was = _ratio(s["right"], s["files"]), _ratio(b["right"], b["files"]) if b else None
    line = f"Splitting    {s['files']} files · split right {s['right']}/{s['files']} ({_pct(share)}{_was(share, was)})"
    if s["multi"] and s["multi"] != s["files"]:
        line += f" · with several documents {s['multi_right']}/{s['multi']}"
    out = [line, f"             documents right: precision {_pct(s['precision'])}, recall {_pct(s['recall'])} · "
                 f"where a document starts: precision {_pct(s['boundary_precision'])}, recall "
                 f"{_pct(s['boundary_recall'])}"]
    if s.get("pq") is not None:
        bpq, bd = (b or {}).get("pq"), (b or {}).get("drags")
        out.append(f"             panoptic quality {_pct(s['pq'])}{_paren_was(s['pq'], bpq)} · pages to move by hand "
                   f"{s['drags']} of {s['pages']}" + (f" (was {bd})" if bd is not None and bd != s["drags"] else "")
                   + drag_cost(s["drags"], cfg))
    return out


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
    bd = c["band"]
    if bd:
        lo, hi = bd["interval"]
        lean = "overconfident" if hi < bd["says"] else "underconfident" if lo > bd["says"] else "about right"
        out.append(f"             stated {bd['threshold']:g} or more: {bd['n']} values, says {_pct(bd['says'])} on "
                   f"average, right {_pct(bd['right'])} (95% interval {_pct(lo)} to {_pct(hi)}): {lean}"
                   + (f"; {100 - bd['n']} more to reach the 100 a check needs" if bd["n"] < 100 else ""))
    was_aurc = f" (was {b['aurc']:.3f})" if b and abs(b["aurc"] - c["aurc"]) >= 0.0005 else ""
    out.append(f"             risk-coverage: AURC {c['aurc']:.3f}{was_aurc}, the best possible {c['best']:.3f} "
               "(every wrong value least confident); approving from the most confident down:")
    out.append(f"             {'threshold':>9}  {'approved':>8}  {'right':>6}  {'wrong through':>13}")
    for r in c["curve"]:
        row = f"{r['threshold']:>9g}  {_pct(r['approved']):>8}  {_pct(r['accuracy']):>6}  {r['wrong']:>13}"
        out.append("             " + row + ("   yours" if r["threshold"] == cfg.get("auto_approve") else ""))
    a = c["at"]
    if a:
        was = (b or {}).get("at") or {}
        out.append(f"             at your auto_approve {a['threshold']:g}: {_pct(a['approved'])} approved, "
                   f"{a['wrong']} wrong value{'s' * (a['wrong'] != 1)} among them"
                   + (f" (was {was['wrong']})" if was and was.get("wrong") != a["wrong"] else "")
                   + (f", of {a['wrong_total']} wrong in all: they'd skip review" if a["wrong"] else ""))
    return out


def markdown(now: dict, before: Optional[dict] = None, cfg: Optional[dict] = None) -> str:
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
        if bo:
            new = confusion_diff(o.get("confusions"), bo.get("confusions"))["new"]
            if new:
                parts.append("new OCR confusions: " + ", ".join(f"{_show(a, b)} {n}" for a, b, n, _ in new[:3]))
    if not now["checked"]:
        return " · ".join(parts)
    share = now["all_correct"] / now["checked"] if now["checked"] else None
    was = before["all_correct"] / before["checked"] if before and before["checked"] else None
    s = f"all fields correct {now['all_correct']}/{now['checked']} ({_pct(share)}"
    s += f", was {_pct(was)})" if was is not None and was != share else ")"
    s = " · ".join([s] + parts)
    if now["accuracy"] is not None:
        s += f" · weighted field accuracy {_pct(now['accuracy'])}"
    if now.get("cells"):
        s += f" · cell F1 {_pct(now['cells']['f1'])}"
    m = now.get("made_up")
    if m and any(m[k] for k in _MADE_UP):
        s += " · made up: " + ", ".join(f"{m[k]} {k}" for k in _MADE_UP if m[k])
    worst = sorted(((k, v) for k, v in now["fields"].items() if v["recall"] is not None and v["recall"] < 1),
                   key=lambda kv: kv[1]["recall"])[:3]
    if worst:
        s += " · lowest recall: " + ", ".join(f"{k} {_pct(v['recall'])}" for k, v in worst)
    ch = slice_changes(now.get("slices"), (before or {}).get("slices"))
    worse = sorted((k for k, c in ch.items() if c["worse"]), key=lambda k: (ch[k]["q"], k))  # clearest first
    st = now.get("stability")
    if st and st["same"] < st["fields"]:
        s += f" · same value every attempt {_pct(st['same'] / st['fields'])}"
    if worse:
        s += " · slices worse beyond chance: " + ", ".join(worse[:3])
    bad = [g for g in check_gates(now, before, (cfg or {}).get("gates")) if g["passed"] is False]
    if bad:
        s += " · gates failed: " + "; ".join(g["why"] for g in bad[:3])
    return s
