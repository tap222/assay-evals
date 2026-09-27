"""Judge calibration: does your judge's score track a person's, and does it still after a change?

A judge can return valid JSON, in range, every time, and still be wrong: rank a great answer
below a poor one, call everything a 3, or give the same answer a 2 and a 4 on two tries.
Checking that takes a golden set: outputs a person scored, spanning terrible to great. This
runs your judge over it, several times per item, and says:

  ranking      Spearman rank correlation with the labels (and its 95% interval, which says how
               much a set this size can tell you), the share of pairs in the right order, and
               the ordering violations themselves: labeled 5, judged below one labeled 2
  agreement    exact and within one, and label × judge
  bias         lenient or harsh on average, per label, and whether it squashes the scale
  consistency  how much each item's score swings across repeats, and which flip pass/fail
  validity     answers that weren't a verdict, timeouts: counted apart, never as scores

Each calibration is stored, and compared with the last one that passed: overall and per tag
(a question type, say), since improving one kind of item often breaks another. It regressed
when a rank correlation dropped beyond chance (a paired bootstrap over the items both runs
judged) by at least min_drop, or when a new ordering violation is two or more label points
apart. Smaller moves are reported, not failed.

golden.jsonl, one item a line:

  {"id": "q17", "input": "...", "output": "...", "score": 4, "by": "sam", "tags": ["behavioral"]}
  {"id": "q18", "input": "...", "output": "...", "labels": [{"by": "sam", "score": 2}, {"by": "ana", "score": 3}]}

An item's label is the median of its labels. Items two people labeled say how much people agree:
the ceiling for any judge. A label can carry a critique: why the person scored it so, the raw
material a judge is built from (its few-shot examples, its rubric).

Splits keep a judge from being measured on what it memorized (assay golden split): train is what a
judge may learn from (assay_sdk.golden_examples), dev what calibration reports on while the judge
is being worked on, test held out for a final check (assay calibrate --final). A judge whose source
contains an item of the split it's measured on, or that read that split's examples, has leaked:
the calibration fails, since its numbers would say nothing about unseen data.

Three more things say whether a judge can be trusted:

  variants     versions of an item's output with what should happen to its score: a paraphrase
               should keep it ("same"), a subtly broken answer should lose it ("lower"):
               {"id": "q17", ..., "variants": [{"output": "...", "expect": "same", "note": "paraphrase"}]}
  second judge another judge over the same items (second_judge): how often they agree, and the
               items they disagree on, which is usually where the rubric is ambiguous
  catch rate   of the answers people called bad, how many the judge failed. Judges confirm good
               answers far more reliably than they catch bad ones, and a score says nothing of it
  bias probes  whether the judge's gap from people follows a surface feature: length, citations,
               hedging, formatting, or an item written by a model of the judge's own family
               (give items "model": "claude-sonnet-5")
  drift        the same judge (its code, and the models that answered) over the same items,
               agreeing with people less than it did: the provider changed the model under its
               name. Only a calibration run on a schedule, with nothing else changed, catches it.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Callable, Dict, List, Optional, Tuple

BOOTSTRAP, SEED = 1000, 0
MIN_TAG_ITEMS = 8  # fewer can't say whether a tag got worse
DEFAULTS = {"golden": "golden.jsonl", "judge": None, "repeat": 5, "score_range": [1, 5], "label_range": None,
            "threshold": None, "concurrency": 8, "min_drop": 0.05, "field": None, "second_judge": None,
            "split": None, "group_by": "tags"}
GROUP_BY = ("tags", "input", "none")
MIN_GROUP_ITEMS, MIN_GROUPS = 3, 3  # a group check needs this many groups of this many judged items
TOPIC = 0.3  # within-group Spearman below this, where groups alone reach this, tracks the group
SPLITS = ("train", "dev", "test")
EXPECTS = ("same", "lower", "higher")


class CalibrationError(ValueError):
    pass


# ---------- the golden set ----------

def load_golden(path: Path) -> List[dict]:
    if not path.exists():
        raise CalibrationError(f"No golden set at {path}. `assay golden add CASE --score N` starts one from a "
                               f"recorded run, or write it: one JSON object a line, with input, output and score.")
    items, seen = [], set()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except ValueError:
            raise CalibrationError(f"{path.name}, line {n}: not JSON.")
        if not isinstance(x, dict) or "output" not in x:
            raise CalibrationError(f"{path.name}, line {n}: needs at least output, and a score or labels.")
        labels = list(x.get("labels") or [])
        if x.get("score") is not None:
            labels.insert(0, {"by": x.get("by") or "unknown", "score": x["score"],
                              **({"critique": x["critique"]} if x.get("critique") else {})})
        labels = [lab for lab in labels if isinstance(lab, dict) and isinstance(lab.get("score"), (int, float))
                  and not isinstance(lab.get("score"), bool)]
        if not labels:
            raise CalibrationError(f"{path.name}, line {n}: no score. Give score (and by), or labels.")
        x["id"] = str(x.get("id") or f"item-{n}")
        if x["id"] in seen:
            raise CalibrationError(f"{path.name}, line {n}: id {x['id']!r} is there twice.")
        seen.add(x["id"])
        x["labels"], x["label"] = labels, median(lab["score"] for lab in labels)
        x["tags"] = [str(t) for t in x.get("tags") or []]
        vs = x.get("variants") or []
        if not isinstance(vs, list) or any(not isinstance(v, dict) or "output" not in v or v.get("expect") not in EXPECTS
                                           for v in vs):
            raise CalibrationError(f"{path.name}, line {n}: each variant needs output, and expect: same, lower or "
                                   f"higher (what should happen to the score).")
        x["variants"] = vs
        if x.get("split") is not None and x["split"] not in SPLITS:
            raise CalibrationError(f"{path.name}, line {n}: split is train, dev or test, not {x['split']!r}.")
        x["critiques"] = [lab["critique"] for lab in labels if lab.get("critique")]
        items.append(x)
    if not items:
        raise CalibrationError(f"{path.name} is empty.")
    return items


def save_golden(path: Path, items: List[dict]) -> None:
    out = []
    for x in items:
        x = {k: v for k, v in x.items() if k not in ("label", "critiques")}
        labs = x.pop("labels", [])
        x.pop("score", None), x.pop("by", None), x.pop("critique", None)
        if len(labs) == 1 and set(labs[0]) <= {"score", "by", "critique"}:
            x["score"], x["by"] = labs[0]["score"], labs[0].get("by")
            if labs[0].get("critique"):
                x["critique"] = labs[0]["critique"]
        else:
            x["labels"] = labs
        out.append(json.dumps(x, ensure_ascii=False, default=str))
    path.write_text("\n".join(out) + "\n")


def assign_splits(items: List[dict], train: float = 0.2, dev: float = 0.4, seed: int = 0,
                  by_group: Optional[str] = None) -> Dict[str, int]:
    """Give every item without a split one: train, dev or test, stratified by label so each split
    spans poor to great. Items that have one keep it. {split: items}.

    With `by_group` ("tags" or "input"), whole groups go to one split, so a judge that learned from
    train is measured on topics (or inputs) it hasn't seen. An unsplit item joins the split most of
    its group already has."""
    rnd = random.Random(seed)
    if by_group:
        return _assign_groups(items, train, dev, rnd, by_group)
    by: Dict[int, List[dict]] = defaultdict(list)
    for x in items:
        if not x.get("split"):
            by[int(round(x["label"]))].append(x)
    for level, xs in sorted(by.items()):
        rnd.shuffle(xs)
        n = len(xs)
        a, b = round(n * train), round(n * (train + dev))
        for i, x in enumerate(xs):
            x["split"] = "train" if i < a else "dev" if i < b else "test"
    return dict(Counter(x.get("split") or "none" for x in items))


def group_of(x: dict, by: str) -> Optional[str]:
    """The group an item belongs to: its tags together, or its input. None when it has none."""
    if by == "tags":
        return "+".join(sorted(x.get("tags") or [])) or None
    if by == "input":
        return _norm_text(x.get("input")) or None
    return None


def _assign_groups(items: List[dict], train: float, dev: float, rnd: random.Random, by_group: str) -> Dict[str, int]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for x in items:
        groups[group_of(x, by_group) or f"item:{x['id']}"].append(x)
    fresh = []
    for g, xs in sorted(groups.items()):
        had = Counter(x["split"] for x in xs if x.get("split"))
        if had:
            for x in xs:
                x["split"] = x.get("split") or had.most_common(1)[0][0]
        else:
            fresh.append(xs)
    rnd.shuffle(fresh)
    total, done = sum(len(xs) for xs in fresh), 0
    for xs in fresh:  # by the share of items placed so far, so a big group doesn't skew the split
        at = done / total if total else 0
        split = "train" if at < train else "dev" if at < train + dev else "test"
        for x in xs:
            x["split"] = split
        done += len(xs)
    return dict(Counter(x.get("split") or "none" for x in items))


def select_split(items: List[dict], split: Optional[str]) -> List[dict]:
    """The items calibration reports on: a split, or all of them when none are split."""
    if not split or split == "all" or not any(x.get("split") for x in items):
        return items
    return [x for x in items if x.get("split") == split]


def leaks(judge: Callable, items: List[dict], split: Optional[str]) -> List[str]:
    """How a judge could have seen the items it's measured on: their text in its source, or its
    reading that split's examples (assay_sdk.golden_examples)."""
    import inspect
    from assay_sdk import golden
    out = []
    if split and split in golden.requested():
        out.append(f"the judge read the {split} split's examples (golden_examples(split={split!r}))")
    try:
        src = _norm_text(Path(inspect.getsourcefile(judge)).read_text())
    except (TypeError, OSError):
        return out
    for x in items:
        t = _norm_text(x.get("output"))
        if len(t) >= 30 and t in src:
            out.append(f"item {x['id']}'s output is in the judge's source")
            if len(out) >= 5:
                break
    return out


def _norm_text(v) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", str(v or "")).strip().lower()


def digest(items: List[dict]) -> str:
    return hashlib.sha256(json.dumps([[x["id"], x["label"]] for x in items]).encode()).hexdigest()[:12]


def coverage(items: List[dict], label_range: Tuple[float, float]) -> dict:
    """Labels per level, labelers, and how much labelers agree where two labeled the same item."""
    lo, hi = label_range
    levels = list(range(int(math.ceil(lo)), int(math.floor(hi)) + 1))
    per = Counter(int(round(x["label"])) for x in items)
    by = Counter(lab.get("by") or "unknown" for x in items for lab in x["labels"])
    pairs = [(x["labels"][0]["score"], x["labels"][1]["score"]) for x in items if len(x["labels"]) >= 2]
    agree = None
    if pairs:
        agree = {"items": len(pairs), "exact": sum(a == b for a, b in pairs) / len(pairs),
                 "within_one": sum(abs(a - b) <= 1 for a, b in pairs) / len(pairs),
                 "spearman": spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= 3 else None}
    tags = Counter(t for x in items for t in x["tags"])
    variants = Counter(v["expect"] for x in items for v in x.get("variants") or [])
    splits = Counter(x.get("split") for x in items if x.get("split"))
    return {"items": len(items), "levels": {lv: per.get(lv, 0) for lv in levels},
            "missing": [lv for lv in levels if not per.get(lv)], "labelers": dict(by.most_common()),
            "labeler_agreement": agree, "tags": dict(tags.most_common()), "variants": dict(variants),
            "splits": dict(splits), "critiques": sum(1 for x in items if x.get("critiques"))}


# ---------- statistics ----------

def _ranks(xs: List[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a: List[float], b: List[float]) -> Optional[float]:
    """Rank correlation, ties averaged; None when either side doesn't vary."""
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = mean(ra), mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def _interval(values: List[Optional[float]]) -> Optional[Tuple[float, float]]:
    vs = sorted(v for v in values if v is not None)
    if len(vs) < BOOTSTRAP // 2:
        return None
    return vs[int(0.025 * len(vs))], vs[int(0.975 * len(vs)) - 1]


def bootstrap_rho(labels: List[float], judged: List[float]) -> Optional[Tuple[float, float]]:
    rnd, n = random.Random(SEED), len(labels)
    if n < 5:
        return None
    out = []
    for _ in range(BOOTSTRAP):
        idx = [rnd.randrange(n) for _ in range(n)]
        out.append(spearman([labels[i] for i in idx], [judged[i] for i in idx]))
    return _interval(out)


def group_check(judged: List[dict], by: str) -> Optional[dict]:
    """Does the judge rank answers, or recognize their group? A golden set whose tags differ in
    typical quality (refunds rated low, greetings high) gives a judge that only knows the topic a
    healthy overall Spearman. Ranked within each group it's near zero. None without enough groups.

      within    Spearman over the items centred on their group's mean (label and judge alike):
                how well it ranks answers of the same group
      baseline  Spearman of the label against the other items' mean label in its group: what
                knowing the group alone reaches
      topic     groups alone reach TOPIC or more, the judge ranks within groups below TOPIC, and
                its overall number is at least 0.2 above that: it tracks the group, not the answer
    """
    if by not in ("tags", "input"):
        return None
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in judged:
        g = group_of(r, by)
        if g:
            groups[g].append(r)
    groups = {g: rs for g, rs in groups.items() if len(rs) >= MIN_GROUP_ITEMS}
    if len(groups) < MIN_GROUPS:
        return None
    rows = [r for rs in groups.values() for r in rs]
    base = []
    for rs in groups.values():
        total = sum(r["label"] for r in rs)
        base += [(total - r["label"]) / (len(rs) - 1) for r in rs]

    def within(parts: List[List[dict]]) -> Optional[float]:
        cl, cj = [], []
        for rs in parts:
            ml, mj = mean(r["label"] for r in rs), mean(r["judged"] for r in rs)
            cl += [r["label"] - ml for r in rs]
            cj += [r["judged"] - mj for r in rs]
        if len(set(cl)) > 1 and len(set(cj)) == 1:
            return 0.0  # the same score for every answer of a group: it ranks nothing within one
        return spearman(cl, cj)
    rho = spearman([r["label"] for r in rows], [r["judged"] for r in rows])
    rho_base = spearman([r["label"] for r in rows], base)
    w = within(list(groups.values()))
    rnd, names = random.Random(SEED), sorted(groups)
    boot = [within([groups[rnd.choice(names)] for _ in names]) for _ in range(BOOTSTRAP)]  # groups are the unit
    topic = bool(rho is not None and rho_base is not None and w is not None
                 and rho_base >= TOPIC and w < TOPIC and rho - w >= 0.2)
    return {"by": by, "groups": len(groups), "items": len(rows), "spearman": rho, "baseline": rho_base,
            "within": w, "within_interval": _interval(boot), "topic": topic}


def paired_drop(labels: List[float], before: List[float], now: List[float]) -> Optional[Tuple[float, float]]:
    """95% interval of rho(now) - rho(before) over the same items, resampled together."""
    rnd, n = random.Random(SEED), len(labels)
    if n < 5:
        return None
    out = []
    for _ in range(BOOTSTRAP):
        idx = [rnd.randrange(n) for _ in range(n)]
        la = [labels[i] for i in idx]
        a, b = spearman(la, [before[i] for i in idx]), spearman(la, [now[i] for i in idx])
        out.append(None if a is None or b is None else b - a)
    return _interval(out)


def violations(items: List[dict]) -> Tuple[List[dict], int]:
    """Pairs a person ordered one way and the judge the other: ([{"high", "low", "gap"}], comparable pairs)."""
    judged = [x for x in items if x.get("judged") is not None]
    out, comparable = [], 0
    for i, a in enumerate(judged):
        for b in judged[i + 1:]:
            if a["label"] == b["label"]:
                continue
            hi, lo = (a, b) if a["label"] > b["label"] else (b, a)
            comparable += 1
            if hi["judged"] < lo["judged"]:
                out.append({"high": hi["id"], "low": lo["id"], "gap": hi["label"] - lo["label"],
                            "high_label": hi["label"], "high_judged": hi["judged"],
                            "low_label": lo["label"], "low_judged": lo["judged"]})
    return sorted(out, key=lambda v: (-v["gap"], v["high_judged"] - v["low_judged"])), comparable


# ---------- running the judge ----------

def load_judge(spec: str, root: Path) -> Callable:
    """"evals/judges.py:helpfulness" or "evals.judges:helpfulness"."""
    if not spec or ":" not in spec:
        raise CalibrationError("[calibrate] judge: the function to calibrate, as \"evals/judges.py:helpfulness\" "
                               "or \"evals.judges:helpfulness\". It's called as judge(input, output).")
    where, name = spec.rsplit(":", 1)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        if where.endswith(".py"):
            s = importlib.util.spec_from_file_location(Path(where).stem, root / where)
            if s is None or not (root / where).exists():
                raise CalibrationError(f"[calibrate] judge: no file {where}.")
            mod = importlib.util.module_from_spec(s)
            s.loader.exec_module(mod)
        else:
            mod = importlib.import_module(where)
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"[calibrate] judge: couldn't load {where}: {type(exc).__name__}: {exc}")
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise CalibrationError(f"[calibrate] judge: {where} has no function {name}.")
    return fn


# ---------- rates, and what the judge rewards besides quality ----------

def wilson(k: int, n: int) -> Optional[Tuple[float, float]]:
    if not n:
        return None
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _binom_tail(k: int, n: int) -> float:
    """P(X >= k) for X ~ Binomial(n, 1/2): a one-sided sign test."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


FAMILIES = (("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
            ("chatgpt", "openai"), ("gemini", "google"), ("gemma", "google"), ("llama", "meta"),
            ("mistral", "mistral"), ("mixtral", "mistral"), ("qwen", "alibaba"), ("deepseek", "deepseek"),
            ("grok", "xai"), ("command", "cohere"))


def family(model: Optional[str]) -> Optional[str]:
    """The model family a model name belongs to: claude-sonnet-5 → anthropic."""
    if not model:
        return None
    m = model.lower().split("/")[-1]
    return next((f for prefix, f in FAMILIES if m.startswith(prefix)), None)


CITATION = __import__("re").compile(r"\[\d+\]|https?://|\baccording to\b|\bsource[s]?:|\(\w[\w .&-]*,? (19|20)\d\d\)", 2)
HEDGE = __import__("re").compile(r"\b(might|may|possibly|perhaps|probably|likely|it seems|i'?m not (sure|certain)|"
                                 r"i am not (sure|certain)|not sure|uncertain)\b", 2)
FORMAT = __import__("re").compile(r"^\s*(#{1,6} |[-*] |\d+[.)] )", 8)


def surface(text: Any) -> dict:
    """The surface features a judge may reward besides quality."""
    t = text if isinstance(text, str) else json.dumps(text, default=str)
    return {"words": len(t.split()), "citations": bool(CITATION.search(t)), "hedges": bool(HEDGE.search(t)),
            "formatted": len(FORMAT.findall(t)) >= 2}


def _boot_diff(a: List[float], b: List[float]) -> Optional[Tuple[float, float]]:
    rnd = random.Random(SEED)
    if len(a) < 3 or len(b) < 3:
        return None
    out = [mean(rnd.choice(a) for _ in a) - mean(rnd.choice(b) for _ in b) for _ in range(BOOTSTRAP)]
    return _interval(out)


def probes(judged: List[dict], judge_models: List[str], span: float) -> List[dict]:
    """How the judge's gap from people (judged - label) follows each surface feature. Only what's
    beyond chance is reported."""
    if len(judged) < 6:
        return []
    gap = [r["judged"] - r["label"] for r in judged]
    feats = [surface(r["output"]) for r in judged]
    out = []
    rho = spearman([f["words"] for f in feats], gap)
    if rho is not None and abs(rho) >= 0.3:
        iv = None
        rnd, n = random.Random(SEED), len(gap)
        boots = []
        for _ in range(BOOTSTRAP):
            idx = [rnd.randrange(n) for _ in range(n)]
            boots.append(spearman([feats[i]["words"] for i in idx], [gap[i] for i in idx]))
        iv = _interval(boots)
        if iv and (iv[0] > 0 or iv[1] < 0):
            out.append({"feature": "length", "effect": rho, "text": f"{'longer' if rho > 0 else 'shorter'} answers "
                        f"score higher than people scored them (Spearman {rho:.2f} between length and the gap)"})
    fams = {family(m) for m in judge_models} - {None}
    labels = {"citations": "answers with citations or links", "hedges": "answers that hedge (might, not sure)",
              "formatted": "answers with headers or bullets", "family": "answers written by the judge's own family"}
    split = {k: [f[k] for f in feats] for k in ("citations", "hedges", "formatted")}
    if fams:
        split["family"] = [family(r.get("model")) in fams if r.get("model") else None for r in judged]
    for k, has in split.items():
        yes = [g for g, h in zip(gap, has) if h is True]
        no = [g for g, h in zip(gap, has) if h is False]
        iv = _boot_diff(yes, no)
        if iv and (iv[0] > 0 or iv[1] < 0) and abs(mean(yes) - mean(no)) >= 0.1 * span:
            d = mean(yes) - mean(no)
            out.append({"feature": k, "effect": d, "text": f"{labels[k]} score {abs(d):.1f} {'more' if d > 0 else 'less'} "
                        f"than people scored them, compared with the rest ({len(yes)} vs {len(no)} items)"})
    return out


def fingerprint(judge: Callable) -> Optional[str]:
    """The judge's code, as a digest of its source file: an edited rubric is another judge."""
    import inspect
    try:
        return hashlib.sha256(Path(inspect.getsourcefile(judge)).read_bytes()).hexdigest()[:12]
    except (TypeError, OSError):
        return None


def _to_labels(s: float, score_range, label_range) -> float:
    (a, b), (c, d) = score_range, label_range
    return s if (a, b) == (c, d) else c + (s - a) * (d - c) / (b - a)


def run_judge(judge: Callable, items: List[dict], cfg: dict, rt=None):
    """Every item `repeat` times through an EvalRuntime: {id: [Result]}, and the runtime's report."""
    from assay_sdk import EvalRuntime, Sample
    rt = rt or EvalRuntime(concurrency=int(cfg["concurrency"]), retries=2)
    samples = [Sample(x.get("input"), x["output"], id=f"{x['id']}#{k}", **(x.get("args") or {}))
               for x in items for k in range(int(cfg["repeat"]))]
    samples += [Sample(x.get("input"), v["output"], id=f"{x['id']}~{j}#{k}", **(x.get("args") or {}))
                for x in items for j, v in enumerate(x.get("variants") or []) for k in range(int(cfg["repeat"]))]
    lo, hi = cfg["score_range"]
    report = rt.run(judge, samples, score_range=(lo, hi), threshold=cfg["threshold"] if cfg["threshold"] is not None
                    else (lo + hi) / 2)
    by: Dict[str, list] = defaultdict(list)
    for r in report.results:
        by[str(r.id).rsplit("#", 1)[0]].append(r)
    return by, report


# ---------- the analysis ----------

def analyze(items: List[dict], results: Dict[str, list], cfg: dict) -> dict:
    score_range = tuple(cfg["score_range"])
    label_range = tuple(cfg["label_range"] or cfg["score_range"])
    lo, hi = score_range
    threshold = cfg["threshold"] if cfg["threshold"] is not None else (lo + hi) / 2
    invalid, first_error = Counter(), None
    rows = []
    for x in items:
        rs = results.get(x["id"], [])
        scores = [r.result.score for r in rs if r.result is not None and r.result.valid and r.result.score is not None]
        for r in rs:
            if r.result is None or not r.result.valid:
                invalid[r.kind] += 1
                first_error = first_error or f"{x['id']}: {r.error}"
        conv = [_to_labels(s, score_range, label_range) for s in scores]
        rows.append({**x, "scores": scores, "judged": median(conv) if conv else None,
                     "spread": (max(conv) - min(conv)) if len(conv) > 1 else 0.0 if conv else None,
                     "sd": pstdev(conv) if len(conv) > 1 else 0.0 if conv else None,
                     "flips": len({s >= threshold for s in scores}) > 1})
    judged = [r for r in rows if r["judged"] is not None]
    L, J = [r["label"] for r in judged], [r["judged"] for r in judged]
    span = label_range[1] - label_range[0]
    variants = []
    for r in judged:
        for j, v in enumerate(r.get("variants") or []):
            vs = [_to_labels(x.result.score, score_range, label_range) for x in results.get(f"{r['id']}~{j}", [])
                  if x.result is not None and x.result.valid and x.result.score is not None]
            if not vs:
                continue
            vm = median(vs)
            noise = max(r["spread"] or 0, max(vs) - min(vs), 0.1 * span)
            ok = abs(vm - r["judged"]) <= noise if v["expect"] == "same" else \
                vm < r["judged"] - noise / 2 if v["expect"] == "lower" else vm > r["judged"] + noise / 2
            variants.append({"id": r["id"], "variant": j, "expect": v["expect"], "note": v.get("note"),
                             "original": r["judged"], "judged": vm, "ok": ok})
    models = sorted({x.result.judge_model for rs in results.values() for x in rs
                     if x.result is not None and getattr(x.result, "judge_model", None)})
    rho = spearman(L, J)
    viol, comparable = violations(judged)
    levels = sorted({int(round(r["label"])) for r in judged})
    per_label = {lv: mean(r["judged"] for r in judged if int(round(r["label"])) == lv) for lv in levels}
    matrix = defaultdict(Counter)
    for r in judged:
        matrix[int(round(r["label"]))][int(round(r["judged"]))] += 1
    sd_l = pstdev(L) if len(L) > 1 else 0
    tags = {}
    for t in sorted({t for r in judged for t in r["tags"]}):
        mine = [r for r in judged if t in r["tags"]]
        tags[t] = {"n": len(mine), "spearman": spearman([r["label"] for r in mine], [r["judged"] for r in mine])}
    return {
        "items": rows, "n": len(items), "judged": len(judged), "invalid": dict(invalid), "first_error": first_error,
        "spearman": rho, "interval": bootstrap_rho(L, J),
        "pairs": {"comparable": comparable, "right": comparable - len(viol), "violations": viol},
        "agreement": {"exact": mean(round(j) == round(lab) for j, lab in zip(J, L)) if J else None,
                      "within_one": mean(abs(j - lab) <= 1 for j, lab in zip(J, L)) if J else None},
        "bias": mean(j - lab for j, lab in zip(J, L)) if J else None, "per_label": per_label,
        "squashed": bool(sd_l and len(J) > 1 and pstdev(J) / sd_l < 0.5),
        "matrix": {k: dict(v) for k, v in matrix.items()},
        "consistency": {"mean_spread": mean(r["spread"] for r in judged) if judged else None,
                        "flips": [r["id"] for r in judged if r["flips"]],
                        "widest": sorted(judged, key=lambda r: -(r["spread"] or 0))[:3]},
        "tags": tags, "coverage": coverage(items, label_range), "label_range": label_range,
        "variants": variants, "models": models, "golden_digest": digest(items),
        "catch": _catch(judged, _to_labels(threshold, score_range, label_range)),
        "probes": probes(judged, models, span),
        "groups": group_check(judged, cfg.get("group_by") or "tags"),
    }


def _catch(judged: List[dict], thr: float) -> dict:
    """Of the answers people called bad (label below the pass mark), how many the judge failed; of the
    good, how many it passed."""
    bad = [r for r in judged if r["label"] < thr]
    good = [r for r in judged if r["label"] >= thr]
    caught = [r["id"] for r in bad if r["judged"] < thr]
    passed = [r["id"] for r in good if r["judged"] >= thr]
    return {"threshold": thr, "bad": len(bad), "caught": caught, "missed": [r["id"] for r in bad if r["id"] not in caught],
            "good": len(good), "confirmed": len(passed), "tnr": len(caught) / len(bad) if bad else None,
            "tpr": len(passed) / len(good) if good else None, "tnr_interval": wilson(len(caught), len(bad))}


def between_judges(a: dict, b: dict) -> dict:
    """Two judges over the same items: how often they agree, and where they don't (by 2+ points)."""
    x = {r["id"]: r for r in a["items"] if r.get("judged") is not None}
    y = {r["id"]: r for r in b["items"] if r.get("judged") is not None}
    ids = [i for i in x if i in y]
    A, B = [x[i]["judged"] for i in ids], [y[i]["judged"] for i in ids]
    apart = sorted(({"id": i, "label": x[i]["label"], "first": x[i]["judged"], "second": y[i]["judged"]}
                    for i in ids if abs(x[i]["judged"] - y[i]["judged"]) >= 2), key=lambda d: -abs(d["first"] - d["second"]))
    return {"items": len(ids), "spearman": spearman(A, B),
            "exact": mean(round(p) == round(q) for p, q in zip(A, B)) if ids else None,
            "within_one": mean(abs(p - q) <= 1 for p, q in zip(A, B)) if ids else None,
            "apart": apart, "second_spearman": b["spearman"], "first_spearman": a["spearman"]}


def compare(now: dict, before: dict, cfg: dict) -> dict:
    """Against the baseline calibration: overall and per tag, over the items both judged."""
    def judged(a):
        return {r["id"]: r for r in a["items"] if r.get("judged") is not None}
    a, b = judged(before), judged(now)
    common = [i for i in b if i in a and a[i]["label"] == b[i]["label"]]
    min_drop = float(cfg["min_drop"])
    out = {"items": len(common), "overall": None, "tags": {}, "new_violations": [], "regressed": False}

    def test(ids):
        if len(ids) < 5:
            return None
        L = [b[i]["label"] for i in ids]
        was, now_ = spearman(L, [a[i]["judged"] for i in ids]), spearman(L, [b[i]["judged"] for i in ids])
        iv = paired_drop(L, [a[i]["judged"] for i in ids], [b[i]["judged"] for i in ids])
        worse = bool(was is not None and now_ is not None and iv and iv[1] < 0 and was - now_ >= min_drop)
        return {"before": was, "now": now_, "interval": iv, "n": len(ids), "worse": worse}
    out["overall"] = test(common)
    for t in sorted({t for i in common for t in b[i]["tags"]}):
        ids = [i for i in common if t in b[i]["tags"]]
        if len(ids) >= MIN_TAG_ITEMS:
            out["tags"][t] = test(ids)
    old = {(v["high"], v["low"]) for v in violations([a[i] for i in common])[0]}
    out["new_violations"] = [v for v in violations([b[i] for i in common])[0] if (v["high"], v["low"]) not in old]
    c0, c1 = before.get("catch") or {}, now.get("catch") or {}
    common_bad = set(c0.get("caught", []) + c0.get("missed", [])) & set(c1.get("caught", []) + c1.get("missed", []))
    lost = sorted(i for i in common_bad if i in c0.get("caught", []) and i in c1.get("missed", []))
    won = sorted(i for i in common_bad if i in c0.get("missed", []) and i in c1.get("caught", []))
    out["catch"] = {"lost": lost, "won": won, "worse": len(lost) > len(won) and
                    _binom_tail(len(lost), len(lost) + len(won)) < 0.05}
    was_ok = {(v["id"], v["variant"]) for v in before.get("variants") or [] if v["ok"]}
    out["new_variant_failures"] = [v for v in now.get("variants") or [] if not v["ok"] and (v["id"], v["variant"]) in was_ok]
    blind = [v for v in out["new_variant_failures"] if v["expect"] == "lower" and v["judged"] >= v["original"]]
    out["regressed"] = bool((out["overall"] or {}).get("worse") or any((t or {}).get("worse") for t in out["tags"].values())
                            or any(v["gap"] >= 2 for v in out["new_violations"]) or blind or out["catch"]["worse"])
    # Why: the judge changed (its code, its model), or nothing did and the provider's model moved.
    j0, j1 = before.get("judge") or {}, now.get("judge") or {}
    same_items = before.get("golden_digest") == now.get("golden_digest")
    if j0.get("source") and j0.get("source") != j1.get("source"):
        out["cause"] = "the judge's code changed since the baseline"
    elif (before.get("models") or []) != (now.get("models") or []) and before.get("models") and now.get("models"):
        out["cause"] = f"the judge's model changed: {', '.join(before['models'])} → {', '.join(now['models'])}"
    elif out["regressed"] and same_items and j0.get("source") and j0 == j1:
        out["cause"] = ("the same judge (its code and its model's name) over the same items agrees with people less "
                        "than it did: the provider changed the model under its name")
        out["drift"] = True
    return out


# ---------- showing it ----------

def _f(v: Optional[float], nd: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _variant(v: dict) -> str:
    what = {"same": "should stay the same", "lower": "should score lower", "higher": "should score higher"}[v["expect"]]
    return f"{v['id']}{' (' + v['note'] + ')' if v.get('note') else ''}: {v['original']:.1f} → {v['judged']:.1f}, {what}"


def text(run_id: str, spec: str, a: dict, cfg: dict, cmp: Optional[dict], baseline: Optional[str],
         runtime_line: Optional[str], paint=lambda s, c: s, second: Optional[dict] = None) -> str:
    cov = a["coverage"]
    out = [paint("Judge calibration", "bold") + f"  {run_id}", "─" * 44,
           f"{spec} · {a['n']} items · {cfg['repeat']} judgements each"
           + (f" · the {a['split']} split" + (" (held out)" if a["split"] == "test" else "") if a.get("split") else ""),
           ""]
    if a.get("leaks"):
        out += [paint("Leak: the judge has seen what it's measured on, so these numbers say nothing about unseen "
                      "data:", "red")] + [paint(f"  {x}", "red") for x in a["leaks"]] + [""]
    elif not a.get("split"):
        out += [paint("Not split: if the judge learned from these items, this measures memory. `assay golden split` "
                      "assigns train, dev and test.", "dim"), ""]
    labelers = ", ".join(f"{k} ({v})" for k, v in cov["labelers"].items())
    out.append(f"Golden set   {cov['items']} items, labeled by {labelers}")
    out.append("             " + "  ".join(f"{lv}: {n}" for lv, n in cov["levels"].items()))
    if cov["missing"]:
        out.append(paint(f"             nothing labeled {', '.join(map(str, cov['missing']))}: the judge is untested "
                         f"there (`assay golden suggest`)", "yellow"))
    ag = cov["labeler_agreement"]
    if ag:
        out.append(f"             people agree: exact {_pct(ag['exact'])}, within one {_pct(ag['within_one'])} "
                   f"({ag['items']} items labeled twice): the ceiling for any judge")
    out.append("")
    iv = a["interval"]
    out.append(f"Ranking      Spearman {_f(a['spearman'])}" + (f" (95% interval {_f(iv[0])}–{_f(iv[1])})" if iv else
                                                                  " (too few items for an interval)"))
    p = a["pairs"]
    if p["comparable"]:
        out.append(f"             pairs in the right order: {_pct(p['right'] / p['comparable'])} "
                   f"({p['right']:,} of {p['comparable']:,})")
    far = [v for v in p["violations"] if v["gap"] >= 2]
    if far:
        out.append(paint(f"             {len(far)} ordering violation{'s' * (len(far) != 1)} two or more apart:", "yellow"))
        out += [f"               {v['high']} labeled {v['high_label']:g}, judged {v['high_judged']:.1f}  <  "
                f"{v['low']} labeled {v['low_label']:g}, judged {v['low_judged']:.1f}" for v in far[:5]]
    ag2 = a["agreement"]
    out.append(f"Agreement    exact {_pct(ag2['exact'])}, within one {_pct(ag2['within_one'])}")
    ct = a.get("catch") or {}
    if ct.get("bad"):
        iv = ct["tnr_interval"]
        weak = ct["tnr"] < 0.7
        line = (f"Catch rate   fails {len(ct['caught'])} of the {ct['bad']} answers people called bad "
                f"({_pct(ct['tnr'])}, 95% interval {_pct(iv[0])}–{_pct(iv[1])}); passes {ct['confirmed']} of "
                f"{ct['good']} good ones")
        out.append(paint(line + ": it confirms good answers but lets bad ones through", "yellow") if weak else line)
        if weak and ct["missed"]:
            out.append(paint(f"             missed: {', '.join(ct['missed'][:6])}", "dim"))
    for p in a.get("probes") or []:
        out.append(paint(f"Bias probe   {p['text']}", "yellow"))
    if a["bias"] is not None:
        lean = "lenient" if a["bias"] > 0.25 else "harsh" if a["bias"] < -0.25 else "no lean"
        worst = max(a["per_label"].items(), key=lambda kv: abs(kv[1] - kv[0]), default=None)
        out.append(f"Bias         {a['bias']:+.2f} ({lean})" + (f" · labeled {worst[0]} → judged {worst[1]:.1f} on "
                                                                 f"average" if worst else "")
                   + (" · squashes the scale toward the middle" if a["squashed"] else ""))
    c = a["consistency"]
    if c["mean_spread"] is not None:
        line = f"Consistency  mean spread {c['mean_spread']:.2f} across {cfg['repeat']} judgements"
        if c["flips"]:
            line += f" · {len(c['flips'])} flip pass/fail: {', '.join(c['flips'][:5])}"
        out.append(line)
    bad = sum(a["invalid"].values())
    out.append(f"Validity     {a['judged']} of {a['n']} items judged" + (
        "; not verdicts: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in a["invalid"].items()) if bad else ""))
    if bad and a.get("first_error"):
        out.append(paint(f"             e.g. {a['first_error'][:200]}", "dim"))
    if a["tags"]:
        out.append("By tag       " + "   ".join(f"{t} {_f(v['spearman'])} (n={v['n']})" for t, v in a["tags"].items()))
    g = a.get("groups")
    if g:
        what = "tag" if g["by"] == "tags" else "input"
        iv = g["within_interval"]
        line = (f"Within {what + 's':<6}Spearman {_f(g['within'])}" + (f" (95% interval {_f(iv[0])}–{_f(iv[1])}, "
                f"{g['groups']} {what}s resampled)" if iv else "") + f" · knowing only each {what}'s average "
                f"label: {_f(g['baseline'])}")
        out.append(line)
        if g["topic"]:
            out.append(paint(f"             it tracks the {'topic' if what == 'tag' else 'input'}, not the answer: "
                             f"{_f(g['spearman'])} overall comes from telling {what}s apart. Among answers of the "
                             f"same {what} it ranks at {_f(g['within'])}", "yellow"))
    vs = a.get("variants") or []
    if vs:
        ok = sum(v["ok"] for v in vs)
        parts = []
        for e, words in (("same", "paraphrases kept their score"), ("lower", "broken versions scored lower"),
                         ("higher", "improved versions scored higher")):
            mine = [v for v in vs if v["expect"] == e]
            if mine:
                parts.append(f"{words} {sum(v['ok'] for v in mine)}/{len(mine)}")
        out.append(f"Variants     {ok} of {len(vs)} as expected: " + ", ".join(parts))
        out += [paint(f"               {_variant(v)}", "yellow") for v in vs if not v["ok"]][:5]
    if second:
        s = second
        out += ["", f"Second judge {s['spec']} over the same {s['items']} items",
                f"             agrees with the first: Spearman {_f(s['spearman'])}, exact {_pct(s['exact'])}, "
                f"within one {_pct(s['within_one'])}",
                f"             with people: first {_f(s['first_spearman'])}, second {_f(s['second_spearman'])}"]
        if s["apart"]:
            out.append(paint(f"             {len(s['apart'])} item{'s' * (len(s['apart']) != 1)} they disagree on by 2+ "
                             f"points (the rubric is likely ambiguous there):", "yellow"))
            out += [f"               {d['id']}: labeled {d['label']:g}, first {d['first']:.1f}, second {d['second']:.1f}"
                    for d in s["apart"][:5]]
    if a["matrix"]:
        cols = sorted({j for row in a["matrix"].values() for j in row})
        out += ["", "Label × judge  " + " ".join(f"{j:>3}" for j in cols)]
        for lv in sorted(a["matrix"]):
            out.append(f"  {lv:>3}         " + " ".join(f"{a['matrix'][lv].get(j, 0) or '.':>3}" for j in cols))
    if runtime_line:
        out += ["", paint(runtime_line, "dim")]
    out.append("")
    if cmp is None:
        out.append(paint("Failed: a leak.", "red") if a.get("leaks") else
                   "No earlier calibration to compare with: this one is the baseline.")
        return "\n".join(out)
    out.append(f"Compared with {baseline} ({cmp['items']} items in both)")
    rows = [("overall", cmp["overall"])] + list(cmp["tags"].items())
    for name, t in rows:
        if not t:
            continue
        iv = t["interval"]
        chance = "beyond chance" if t["worse"] else "within chance" if (t["before"] or 0) > (t["now"] or 0) else ""
        mark = paint("✗ ", "red") if t["worse"] else "  "
        out.append(f"{mark}{name:<14} Spearman {_f(t['before'])} → {_f(t['now'])}"
                   + (f" ({t['now'] - t['before']:+.2f}{', ' + chance if chance else ''})"
                      if t["before"] is not None and t["now"] is not None else ""))
    far = [v for v in cmp["new_violations"] if v["gap"] >= 2]
    if far:
        out.append(paint(f"✗ {len(far)} new ordering violation{'s' * (len(far) != 1)} two or more apart:", "red"))
        out += [f"    {v['high']} (labeled {v['high_label']:g}) now judged below {v['low']} (labeled {v['low_label']:g})"
                for v in far[:5]]
    near = len(cmp["new_violations"]) - len(far)
    if near:
        out.append(paint(f"  {near} new near-miss{'es' * (near != 1)} one label apart (not failing)", "dim"))
    cc = cmp.get("catch") or {}
    if cc.get("lost"):
        out.append(paint(f"{'✗ ' if cc['worse'] else '  '}{len(cc['lost'])} bad answer{'s' * (len(cc['lost']) != 1)} it "
                         f"caught before now pass{'es' * (len(cc['lost']) == 1)}: {', '.join(cc['lost'][:6])}"
                         + (" (beyond chance)" if cc["worse"] else ""), "red" if cc["worse"] else "dim"))
    nv = cmp.get("new_variant_failures") or []
    if nv:
        out.append(paint(f"✗ {len(nv)} variant{'s' * (len(nv) != 1)} that behaved before no longer do:", "red"))
        out += [f"    {_variant(v)}" for v in nv[:5]]
    if cmp.get("cause"):
        out.append(paint(f"Why: {cmp['cause']}.", "yellow" if cmp.get("drift") else "dim"))
    out += ["", paint("Regressed.", "red") if cmp["regressed"] else paint("Calibrated as before.", "green")]
    if a.get("leaks"):
        out.append(paint("Failed: a leak.", "red"))
    return "\n".join(out)


def as_json(a: dict, cmp: Optional[dict]) -> dict:
    keep = {k: v for k, v in a.items() if k not in ("items",)}
    keep["consistency"] = {**a["consistency"], "widest": [r["id"] for r in a["consistency"]["widest"]]}
    keep["items"] = [{k: r.get(k) for k in ("id", "label", "judged", "scores", "spread", "flips", "tags")}
                     for r in a["items"]]
    return {"calibration": keep, "compared": cmp}


def new_id() -> str:
    return datetime.utcnow().strftime("c-%Y%m%d-%H%M%S-%f")[:-3]
