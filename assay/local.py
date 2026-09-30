"""Local testing: `assay init` and `assay test`, with no server and no account.

`assay test` runs your command with the SDK recording to a file, loads what it
recorded into a store under .assay/, checks every run, and compares the result
with the last run that passed:

  - agent runs are checked against their case's expectations (assay.expect) and
    the safety rules in assay.toml (path contracts; see assay/contracts.py);
  - results you send yourself (assay.check) count as they are;
  - a check that passed in the baseline and fails now is a regression, judged
    with its attempts (assay/flaky.py), so a flaky case doesn't block.

The baseline is the last run that passed, not the previous run: one bad run
must not become the thing the next one is compared with. With no baseline
(a fresh clone, CI), every failing check fails the run.

Exit codes: 0 passed, 1 regressions or failures, 2 nothing to check or a setup
problem.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from assay import (agents, audit, behavior, contracts, failures, flaky, ingest, learn, lifecycle, schema, store,
                   verdicts)
from assay.sources.events import EventsSource

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CONFIG = "assay.toml"
HOME = ".assay"
TENANT = "local"
EXAMPLE = "tests/ai/test_support.py"
CHECK_NAMES = {"plan_quality": "Plan quality", "consistency": "Consistency", "completed": "Finished", "answer": "Answer", "tool_calls": "Tool usage", "end_state": "End state",
               "safety": "Safety", "pii": "PII", "efficiency": "Efficiency", "pytest": "Your asserts",
               "plan": "Plan adherence", "injection": "Prompt injection", "max_fragments": "Fragments per query",
               "max_retrieved_tokens": "Retrieved tokens per query", "max_context_tokens": "Prompt size",
               "faithfulness": "Faithfulness", "context_relevance": "Context relevance",
               "max_fixed_context_tokens": "Fixed context per call", "tool_choice": "Tool choice",
               "tool_args": "Tool arguments", "tool_results": "Tool results", "arguments": "Well-formed arguments",
               "claimed_success": "Claimed success", "context_retention": "Context retention",
               "rewording": "Same behavior reworded", "document": "All fields correct"}
PII_EVALUATOR = "assay.pii@1"

CONFIG_TEMPLATE = '''\
# Assay: your AI tests are pytest tests. `pytest --assay` runs them, checks every run, and
# compares each test with its last passing run; `assay test` does the same, with repeats.
# Docs: https://github.com/tap222/assay-evals/tree/main/sdk/python#readme

[test]
command = "pytest -q tests/ai"   # what `assay test` runs
repeat = 1        # attempts per case; 3 or more lets Assay tell a flaky case from a broken one
tolerance = 0.01  # a drop in the pass rate smaller than this doesn't fail the run
timeout = 900     # seconds per attempt; a command still running then is stopped (0: no limit)

# Safety rules every agent run must keep. Kinds: never, must_include, before, only_after,
# max_runs, allowed_steps, requires_approval, claim. `where` narrows a rule to calls with certain
# arguments. A claim needs its evidence: kind = "claim", claim = "refunded", needs = "refund".
[[contracts]]
kind = "never"
step = "delete_order"

[[contracts]]
kind = "requires_approval"   # refund only after run.approval("refund", "approved")
step = "refund"

# Personal data (email, card, IBAN, SSN, phone) in a tool's arguments fails the PII check,
# unless the tool is allowed that kind, e.g. allow = {{ send_receipt = ["email"] }}. So does
# personal data in the answer that the request didn't give: someone else's, not the user's own.
[pii]
check = true
allow = {{}}
answers = true
allow_in_answer = []

# A test fails when its run fails these checks, not only on its own asserts.
[pytest]
checks = true

# Behavior compared with each test's last passing run: a case fails when it costs, takes, grows
# its context, retrieves or offers tools this many times over its baseline (0 turns one off),
# when it stops resolving, or when an approval decision changes. fail = false only reports it.
[behavior]
fail = true
cost_usd = 1.5
seconds = 1.5
context_tokens = 1.5
input_tokens = 1.5
fragments = 1.5          # retrieved fragments one query puts into the prompt (run.retrieve())
retrieved_tokens = 1.5
tools_exposed = 1.5
steps = 1.5
suite = 1.25             # the whole run's totals: every query a little bigger adds up
# max_fragments = 8          # limits, whatever the baseline: fragments per query,
# max_retrieved_tokens = 3000  # tokens of fragments per query,
# max_context_tokens = 8000    # and input per model call,
# max_fixed_context_tokens = 3000  # of which system prompt and tool definitions

# Judge calibration: your judge against golden.jsonl, outputs a person scored (`assay calibrate`).
# [calibrate]
# judge = "evals/judges.py:helpfulness"   # called as judge(input, output)
# repeat = 5
# score_range = [1, 5]
# threshold = 3
# group_by = "tags"          # also ranked within each tag ("input": answers to the same input)

# Document extraction scored with assay_sdk.documents: the confidence at or above which your
# pipeline skips review, so the report says how many wrong values that lets through.
# [documents]
# auto_approve = 0.9
# target = 0.99              # the accuracy a suggested threshold must reach

# Dollars per million tokens (input, output[, cached]): recorded model calls get their cost.
# [prices]
# "claude-opus-5" = [5, 25]

# An LLM judge for what rules can't check: whether each run's plan was a good one, and whether
# its reasoning, tool results and answer agree. A model call per run, so off unless asked
# (`assay test --judge`, `pytest --assay --assay-judge`). Needs `pip install anthropic`.
[judge]
enabled = false
provider = "anthropic"   # or openai, gemini, ollama, openai-compatible (then name the model)
model = "claude-opus-5"
redact = true     # personal data is replaced before the trace is sent to the model API
concurrency = 4   # runs judged at once
# rate_limit = 50   # model calls a minute, shared by all of them (a 429 pauses them all)
# timeout = 120     # seconds a call may take; retries = 3 (after a 429, a 5xx, a timeout, an invalid verdict)
# max_time = 600    # seconds for all the judging; budget_usd = 5: runs left then aren't judged, not failed
# [judge.prices]    # dollars per million tokens, for the estimated cost: input, output[, cached]
# "claude-opus-5" = [5, 25]
'''

EXAMPLE_TEMPLATE = '''\
"""AI tests are pytest tests. This one tests a small support agent that needs no LLM: replace it
with yours, and add files next to this one (test_tool_selection.py, test_security.py, ...).

A test that takes the `assay_case` fixture records its run. The test fails when the run breaks a
rule in assay.toml, or misses what the test expects, as well as on its own asserts.

    pytest tests/ai            # red or green, like any test
    pytest --assay tests/ai    # also compared with each test's last passing run
"""
from assay_sdk.testing import assert_called, assert_max_steps, assert_not_called, expect

ORDERS = {"O-17": {"price": 27.61, "status": "delivered"}, "O-18": {"price": 12.00, "status": "shipped"}}


def get_order(order_id):
    return ORDERS[order_id]


def refund(order_id, amount):
    return {"refunded": amount}


def support_agent(run, message, order_id):
    """Your agent goes here. Record what it does on `run`: run.llm() for a model call (with the
    tools it was offered), run.call() for a tool, run.approval() for a decision to allow an action,
    run.answer() for the reply and run.outcome() for whether it resolved the request."""
    run.llm(model="your-model", tokens_in=850, tokens_out=60, cost_usd=0.0021, tools=["get_order", "refund"])
    order = run.call("get_order", get_order, order_id=order_id)
    if order["status"] != "delivered":
        reply = f"Order {order_id} hasn't arrived yet, so it can't be refunded."
    else:
        run.approval("refund", "approved", by="policy:under-50")
        run.call("refund", refund, order_id=order_id, amount=order["price"])
        reply = f"Refunded ${order['price']:.2f}."
    run.answer(reply)
    run.outcome("resolved")
    return reply


def test_refunds_a_delivered_order(assay_case):
    # Everything the run should do, beyond its answer: checked together when the test ends.
    expect(assay_case).must_call("get_order").must_get_approval_before("refund").max_cost(0.01) \\
        .max_tools_exposed(10).must_resolve()
    reply = support_agent(assay_case, "Refund order O-17 please", "O-17")
    assert_called(assay_case, "refund", order_id="O-17")
    assert_max_steps(assay_case, 6)
    assert "27.61" in reply


def test_no_refund_before_delivery(assay_case):
    # What the run should do: checked by Assay after the test, like the rules in assay.toml.
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-18"}}], answer="hasn't arrived")
    support_agent(assay_case, "Can I get a refund for O-18?", "O-18")
    assert_not_called(assay_case, "refund")
'''


# ---------- files ----------

def ensure_home(root: Path) -> Path:
    """.assay/ holds recordings, the local store and the baseline: none of it belongs in git."""
    home = root / HOME
    home.mkdir(exist_ok=True)
    (home / ".gitignore").write_text("*\n")
    return home


def load_config(root: Path, path: Optional[Path] = None, policy: bool = True) -> dict:
    """assay.toml, checked. With ASSAY_POLICY set (CI, on a pull request), its checks are held to
    that trusted copy's: see with_trusted_policy."""
    path = path or root / CONFIG
    if not path.exists():
        raise SetupError(f"No {CONFIG} here. Run `assay init` first.")
    try:
        cfg = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{CONFIG} isn't valid TOML: {exc}")
    test = cfg.get("test") or {}
    rules = cfg.get("contracts") or []
    for i, c in enumerate(rules, 1):
        problem = contracts.validate(c)
        if problem:
            raise SetupError(f"{CONFIG}, contract {i}: {problem}")
    pii = cfg.get("pii") or {}
    allow = pii.get("allow") or {}
    kinds = set(learn.PII)
    answer_allow = pii.get("allow_in_answer") or []
    if not isinstance(answer_allow, list) or set(answer_allow) - kinds:
        raise SetupError(f"{CONFIG}, [pii] allow_in_answer: a list of kinds from {', '.join(learn.PII)}.")
    for tool, allowed in allow.items():
        if not isinstance(allowed, list) or set(allowed) - kinds:
            raise SetupError(f"{CONFIG}, [pii] allow.{tool}: a list of kinds from {', '.join(learn.PII)}.")
    out = {"command": test.get("command"), "repeat": int(test.get("repeat", 1)),
            "timeout": float(test["timeout"]) if test.get("timeout") else None,
            "tolerance": float(test.get("tolerance", 0.01)), "contracts": rules,
            "pii": {"check": bool(pii.get("check", True)), "allow": {k: set(v) for k, v in allow.items()},
                    "answers": bool(pii.get("answers", True)), "answer_allow": set(answer_allow)},
            "pytest": {"checks": bool((cfg.get("pytest") or {}).get("checks", True))},
            "behavior": _behavior_config(cfg.get("behavior") or {}), "judge": _judge_config(cfg.get("judge") or {}),
            "prices": _prices_config(cfg.get("prices")), "calibrate": _calibrate_config(cfg.get("calibrate") or {}),
            "documents": _documents_config(cfg.get("documents") or {})}
    if out["prices"] and "prices" not in out["judge"]:
        out["judge"]["prices"] = out["prices"]
    from assay import acks
    try:
        out["acks"] = acks.load(path)
    except acks.AckError as exc:
        raise SetupError(str(exc))
    return with_trusted_policy(out) if policy else out


def _documents_config(c: dict) -> dict:
    """[documents]: auto_approve, the confidence at or above which your pipeline skips review;
    target, the accuracy a threshold must reach to be suggested (default 0.99); seconds_per_drag and
    rework_per_hour, what a page moved by hand costs when a split is wrong; and [documents.gates],
    per-field gates (assay/documents.py check_gates)."""
    from assay.documents import GATE_KEYS
    unknown = set(c) - {"auto_approve", "target", "gates", "seconds_per_drag", "rework_per_hour"}
    if unknown:
        raise SetupError(f"{CONFIG}, [documents]: unknown {', '.join(sorted(unknown))}. "
                         "Use auto_approve, target, gates, seconds_per_drag, rework_per_hour.")
    out = {"auto_approve": None, "target": 0.99, "gates": {}, "seconds_per_drag": None, "rework_per_hour": None}
    for k in ("seconds_per_drag", "rework_per_hour"):  # what a page moved by hand costs, for splits
        if k in c:
            v = c[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise SetupError(f"{CONFIG}, [documents] {k}: a positive number, e.g. "
                                 + ("15 (seconds)." if k == "seconds_per_drag" else "40 (USD an hour)."))
            out[k] = float(v)
    gates = c.get("gates") or {}
    if not isinstance(gates, dict):
        raise SetupError(f"{CONFIG}, [documents.gates]: a field per line, e.g. tax_number = {{ max_errors = 0 }}.")
    for field, rule in gates.items():
        if not isinstance(rule, dict):
            raise SetupError(f"{CONFIG}, [documents.gates] {field}: a table of rules, e.g. {{ max_errors = 0 }}.")
        bad = set(rule) - set(GATE_KEYS)
        if bad:
            raise SetupError(f"{CONFIG}, [documents.gates] {field}: unknown {', '.join(sorted(bad))}. "
                             f"Use {', '.join(GATE_KEYS)}.")
        for k, v in rule.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or \
                    (k == "max_errors" and v != int(v)) or (k != "max_errors" and v > 1):
                raise SetupError(f"{CONFIG}, [documents.gates] {field}.{k}: "
                                 + ("a whole number, e.g. 0." if k == "max_errors" else "a share from 0 to 1, e.g. 0.02."))
        out["gates"][field] = {k: float(v) if k != "max_errors" else int(v) for k, v in rule.items()}
    for k in ("auto_approve", "target"):
        if k in c:
            v = c[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise SetupError(f"{CONFIG}, [documents] {k}: a share from 0 to 1, e.g. 0.9.")
            out[k] = float(v)
    return out


def _calibrate_config(c: dict) -> dict:
    from assay.calibrate import DEFAULTS
    unknown = set(c) - set(DEFAULTS)
    if unknown:
        raise SetupError(f"{CONFIG}, [calibrate]: unknown {', '.join(sorted(unknown))}. Use {', '.join(DEFAULTS)}.")
    out = {**DEFAULTS, **c}
    for k in ("score_range", "label_range"):
        v = out[k]
        if v is not None and not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)
                                  and v[0] < v[1]):
            raise SetupError(f"{CONFIG}, [calibrate] {k}: [lowest, highest], e.g. [1, 5].")
    try:
        out["repeat"], out["concurrency"] = int(out["repeat"]), int(out["concurrency"])
        out["min_drop"] = float(out["min_drop"])
    except (TypeError, ValueError):
        raise SetupError(f"{CONFIG}, [calibrate]: repeat, concurrency and min_drop are numbers.")
    if out["repeat"] < 1:
        raise SetupError(f"{CONFIG}, [calibrate] repeat: at least 1 (3 or more shows how much the judge swings).")
    from assay.calibrate import GROUP_BY
    if out["group_by"] not in GROUP_BY:
        raise SetupError(f"{CONFIG}, [calibrate] group_by: one of {', '.join(GROUP_BY)} (what the judge is checked "
                         "within: items of the same tags, or answers to the same input).")
    return out


def _prices_config(p) -> Optional[dict]:
    """[prices]: model = [input, output(, cached)], dollars per million tokens."""
    if p is None:
        return None
    from assay_sdk.runtime import _prices
    try:
        _prices(p if isinstance(p, dict) else {"": p})
    except (TypeError, ValueError):
        raise SetupError(f'{CONFIG}, [prices]: "model" = [input, output] dollars per million tokens, e.g. '
                         f'"claude-opus-5" = [5, 25].')
    return p


# ---------- trusted policy: a pull request can't loosen the checks that judge it ----------
#
# On a pull request, assay.toml comes from the PR, so the PR could delete the contract its own
# change breaks, or turn the PII check off, and pass. In CI (the GitHub Action), ASSAY_POLICY is
# the base branch's assay.toml: the checks (contracts, [pii], [behavior], [pytest] checks,
# tolerance) are held to it. The PR can tighten them; loosening them is reported and fails the
# run, unless ASSAY_POLICY_CHANGE=accepted (the action sets it from a PR label), which runs the
# PR's own checks. How the tests run (command, repeat, timeout, judge) stays the PR's.

POLICY_ENV, POLICY_ACCEPT_ENV = "ASSAY_POLICY", "ASSAY_POLICY_CHANGE"


def _ratio(cfg: dict, key: str) -> float:
    return cfg["behavior"]["ratios"].get(key, behavior.NUMBERS[key][0])


def _looser_ratio(a: float, b: float) -> bool:
    """b lets more through than a (0 turns a check off)."""
    return (b == 0 and a != 0) or (a != 0 and b > a)


def _stricter_ratio(a: float, b: float) -> float:
    return b if a == 0 else a if b == 0 else min(a, b)


def policy_changes(base: dict, pr: dict) -> List[dict]:
    """How a PR's checks differ from the trusted ones: [{"text", "weakens"}]."""
    out = []
    add = lambda text, weakens: out.append({"text": text, "weakens": weakens})
    key = lambda c: json.dumps(c, sort_keys=True, default=str)
    was, now = {key(c): c for c in base["contracts"]}, {key(c): c for c in pr["contracts"]}
    for k in was.keys() - now.keys():
        add(f"removes the contract “{contracts.describe(was[k])}”", True)
    for k in now.keys() - was.keys():
        add(f"adds the contract “{contracts.describe(now[k])}”", False)
    bp, pp = base["pii"], pr["pii"]
    if bp["check"] != pp["check"]:
        add("turns the PII check off" if bp["check"] else "turns the PII check on", bp["check"])
    if bp.get("answers", True) != pp.get("answers", True):
        add("stops checking answers for personal data" if bp.get("answers", True) else
            "checks answers for personal data", bp.get("answers", True))
    for tool in sorted(set(bp["allow"]) | set(pp["allow"])):
        more, less = pp["allow"].get(tool, set()) - bp["allow"].get(tool, set()), \
            bp["allow"].get(tool, set()) - pp["allow"].get(tool, set())
        if more:
            add(f"lets {tool} receive {', '.join(sorted(more))}", True)
        if less:
            add(f"no longer lets {tool} receive {', '.join(sorted(less))}", False)
    more = set(pp.get("answer_allow") or ()) - set(bp.get("answer_allow") or ())
    if more:
        add(f"allows {', '.join(sorted(more))} in answers", True)
    if base["pytest"]["checks"] != pr["pytest"]["checks"]:
        add("stops failing tests on Assay's checks ([pytest] checks = false)" if base["pytest"]["checks"] else
            "fails tests on Assay's checks", base["pytest"]["checks"])
    if base["behavior"]["fail"] != pr["behavior"]["fail"]:
        add("stops failing on behavior regressions ([behavior] fail = false)" if base["behavior"]["fail"] else
            "fails on behavior regressions", base["behavior"]["fail"])
    for k in behavior.NUMBERS:
        a, b = _ratio(base, k), _ratio(pr, k)
        if a != b:
            what = behavior.LABELS.get(k, k)
            add(f"turns the {what.lower()} regression check off" if b == 0 else
                f"lets {what.lower()} grow {b:g}x over the baseline, not {a:g}x" if a else
                f"checks {what.lower()} against the baseline ({b:g}x)", _looser_ratio(a, b))
    a, b = base["behavior"].get("suite", behavior.SUITE_RATIO), pr["behavior"].get("suite", behavior.SUITE_RATIO)
    if a != b:
        add("turns the whole-run totals check off" if b == 0 else
            f"lets the whole run's totals grow {b:g}x over the baseline, not {a:g}x" if a else
            f"checks the whole run's totals against the baseline ({b:g}x)", _looser_ratio(a, b))
    bl, pl = base["behavior"].get("limits") or {}, pr["behavior"].get("limits") or {}
    for k in behavior.LIMITS:
        a, b = bl.get(k), pl.get(k)
        if a == b:
            continue
        what = CHECK_NAMES[k].lower()
        add(f"removes the limit on {what} ({a:g})" if b is None else f"sets a limit on {what} ({b:g})" if a is None
            else f"{'raises' if b > a else 'lowers'} the limit on {what} from {a:g} to {b:g}",
            b is None or (a is not None and b > a))
    was = {(a["case"], a["check"]): a for a in base.get("acks") or []}
    for a in pr.get("acks") or []:
        b = was.get((a["case"], a["check"]))
        if b is None:
            add(f"acknowledges {a['case']} {_label(a['check'])} until {a['until']:%Y-%m-%d} ({a['by']}: "
                f"{a['reason']})", True)
        elif a["until"] > b["until"] or a.get("classes") != b.get("classes") or a.get("band") != b.get("band"):
            add(f"extends or changes the acknowledgement of {a['case']} {_label(a['check'])}", True)
    for k in was.keys() - {(a["case"], a["check"]) for a in pr.get("acks") or []}:
        add(f"removes the acknowledgement of {k[0]} {_label(k[1])}", False)
    if pr["tolerance"] != base["tolerance"]:
        add(f"raises the tolerated pass-rate drop from {base['tolerance']:g} to {pr['tolerance']:g}"
            if pr["tolerance"] > base["tolerance"] else
            f"lowers the tolerated pass-rate drop to {pr['tolerance']:g}", pr["tolerance"] > base["tolerance"])
    return out


def strictest(base: dict, pr: dict) -> dict:
    """The PR's config, with each check at the stricter of the two: its additions count, its
    removals don't."""
    key = lambda c: json.dumps(c, sort_keys=True, default=str)
    rules = list({key(c): c for c in [*base["contracts"], *pr["contracts"]]}.values())
    bp, pp = base["pii"], pr["pii"]
    pii = {"check": bp["check"] or pp["check"], "answers": bp.get("answers", True) or pp.get("answers", True),
           "allow": {t: bp["allow"][t] & pp["allow"][t] for t in bp["allow"].keys() & pp["allow"].keys()},
           "answer_allow": set(bp.get("answer_allow") or ()) & set(pp.get("answer_allow") or ())}
    ratios = {k: _stricter_ratio(_ratio(base, k), _ratio(pr, k)) for k in behavior.NUMBERS}
    mine = {(a["case"], a["check"]) for a in pr.get("acks") or []}
    held = [a for a in base.get("acks") or [] if (a["case"], a["check"]) in mine]  # removals count; additions don't
    return {**pr, "acks": held, "contracts": rules, "pii": pii, "tolerance": min(base["tolerance"], pr["tolerance"]),
            "pytest": {**pr["pytest"], "checks": base["pytest"]["checks"] or pr["pytest"]["checks"]},
            "behavior": {"fail": base["behavior"]["fail"] or pr["behavior"]["fail"], "ratios": ratios,
                         "suite": _stricter_ratio(base["behavior"].get("suite", behavior.SUITE_RATIO),
                                                  pr["behavior"].get("suite", behavior.SUITE_RATIO)),
                         "limits": _stricter_limits(base["behavior"].get("limits") or {},
                                                    pr["behavior"].get("limits") or {})}}


def _stricter_limits(a: dict, b: dict) -> dict:
    return {k: min(v for v in (a.get(k), b.get(k)) if v is not None) for k in behavior.LIMITS
            if a.get(k) is not None or b.get(k) is not None}


def with_trusted_policy(cfg: dict) -> dict:
    """cfg held to the trusted policy in ASSAY_POLICY, with cfg["policy"] saying what differed."""
    path = os.environ.get(POLICY_ENV)
    if not path:
        return cfg
    if not Path(path).exists():  # the base branch has no assay.toml yet: this PR sets Assay up
        return {**cfg, "policy": {"trusted": None, "changes": [], "accepted": False, "weakened": False}}
    trusted = load_config(Path(path).parent, Path(path), policy=False)
    changes = policy_changes(trusted, cfg)
    accepted = os.environ.get(POLICY_ACCEPT_ENV) == "accepted"
    weakened = any(c["weakens"] for c in changes)
    out = cfg if accepted else strictest(trusted, cfg)
    return {**out, "policy": {"trusted": path, "changes": changes, "accepted": accepted,
                              "weakened": weakened and not accepted}}


def policy_lines(policy: Optional[dict]) -> List[str]:
    """The policy section of the report, if the PR changed the checks."""
    if not policy or not policy["changes"]:
        return []
    if policy["weakened"]:
        head = ("This PR loosens the checks that judge it, so they're held to the base branch's. To make the "
                "change, add the assay-policy-change label (ASSAY_POLICY_CHANGE=accepted):")
    elif policy["accepted"]:
        head = "This PR changes the checks, and the change is accepted: its own checks are used."
    else:
        head = "This PR tightens the checks; they apply to this run:"
    return [head, *(f"  {'- ' if c['weakens'] else '+ '}{c['text']}" for c in policy["changes"])]


def judge_cost(judged: dict) -> str:
    """One line: what judging cost, and what was left when it stopped."""
    s = judged["summary"]
    t = s["tokens"]
    cost = f"estimated ${s['cost_usd']:,.2f}" if s["cost_usd"] is not None else \
        "cost unknown (set [prices], or ASSAY_PRICES)"
    line = (f"{_n(s['llm_calls'], 'LLM call')}, {_n(s['retries'], 'retry').replace('retrys', 'retries')}, "
            f"{t['input']:,} tokens in, {t['output']:,} out, {cost}")
    if s["unpriced_calls"] and s["cost_usd"] is not None:
        line += f" ({_n(s['unpriced_calls'], 'call')} not priced)"
    if judged.get("not_run"):
        line += f". {_n(judged['not_run'], 'run')} not judged: {s['stopped']}"
    return line + "."


def _judge_config(j: dict) -> dict:
    from assay import judge
    from assay_sdk.llm import PROVIDERS
    provider = str(j.get("provider") or "anthropic")
    if provider not in PROVIDERS:
        raise SetupError(f"{CONFIG}, [judge] provider: one of {', '.join(PROVIDERS)}.")
    if provider != "anthropic" and not j.get("model"):
        raise SetupError(f"{CONFIG}, [judge]: name the {provider} model to judge with (model = \"...\").")
    limits = {}
    for k, kind in (("concurrency", int), ("retries", int), ("timeout", float), ("rate_limit", float),
                    ("max_time", float), ("budget_usd", float)):
        if j.get(k) is not None:
            try:
                limits[k] = kind(j[k])
            except (TypeError, ValueError):
                raise SetupError(f"{CONFIG}, [judge] {k}: a number, not {j[k]!r}.")
    if j.get("prices") is not None:
        if not isinstance(j["prices"], dict):
            raise SetupError(f'{CONFIG}, [judge.prices]: model = [input, output] dollars per million tokens.')
        limits["prices"] = j["prices"]
    return {"enabled": bool(j.get("enabled", False)), "model": str(j.get("model") or judge.MODEL),
            "redact": bool(j.get("redact", True)), "provider": provider, **limits}


def _behavior_config(b: dict) -> dict:
    unknown = set(b) - set(behavior.NUMBERS) - {"fail", "suite"} - set(behavior.LIMITS)
    if unknown:
        raise SetupError(f"{CONFIG}, [behavior]: unknown {', '.join(sorted(unknown))}. Use fail; ratios for "
                         f"{', '.join(behavior.NUMBERS)} (0 turns one off); suite, the ratio for the whole "
                         f"run's totals; and limits: {', '.join(behavior.LIMITS)}.")
    for k in [*behavior.NUMBERS, "suite", *behavior.LIMITS]:
        if k in b and (isinstance(b[k], bool) or not isinstance(b[k], (int, float)) or b[k] < 0):
            raise SetupError(f"{CONFIG}, [behavior] {k}: a number, not {b[k]!r}.")
    return {"fail": bool(b.get("fail", True)), "ratios": {k: float(v) for k, v in b.items() if k in behavior.NUMBERS},
            "suite": float(b.get("suite", behavior.SUITE_RATIO)),
            "limits": {k: float(b[k]) for k in behavior.LIMITS if k in b}}


DEFAULT_CONFIG = {"command": None, "repeat": 1, "timeout": None, "tolerance": 0.01, "contracts": [],  # no assay.toml
                  "pii": {"check": True, "allow": {}, "answers": True, "answer_allow": set()}, "pytest": {"checks": True},
                  "prices": None, "acks": [], "calibrate": None, "documents": {"auto_approve": None, "target": 0.99}, "behavior": {"fail": True, "ratios": {}, "suite": behavior.SUITE_RATIO, "limits": {}},
                  "judge": {"enabled": False, "model": "claude-opus-5", "redact": True, "provider": "anthropic"}}


def find_config(start: Path) -> dict:
    """assay.toml from `start` or the nearest folder above it; the defaults without one."""
    for folder in (start, *start.parents):
        if (folder / CONFIG).exists():
            return load_config(folder)
    return DEFAULT_CONFIG


def as_trajectory(steps: List[dict], answer: Optional[str]) -> dict:
    """SDK steps (assay_sdk.Run.steps) in the shape the checks read (assay/agents.py)."""
    out = []
    for s in steps:
        kind = "reason" if s["kind"] == "llm" else s["kind"]
        state = s["kind"] == "state"
        resource = kind == "resource"
        out.append({"seq": s["seq"], "kind": kind, "name": s.get("name") or (s["uri"][:128] if resource else None),
                    "parent_seq": s.get("parent_seq"), "server": s.get("server"),
                    "args": {"op": s.get("op") or "update"} if state else
                    {"decision": s.get("decision"), "by": s.get("by")} if kind == "approval" else
                    {"uri": s.get("uri")} if resource else
                    {"steps": s.get("plan")} if kind == "plan" else
                    schema.retrieval_args(s.get("query"), s.get("fragments")) if kind == "retrieval" else s.get("args"),
                    "tokens_in": s.get("tokens_in"), "tools": s.get("tools"), "context": s.get("context"),
                    "tool_schemas": s.get("tool_schemas"), "fault": s.get("fault"), "tool_calls": s.get("tool_calls"),
                    "media": s.get("media"), "settings": s.get("settings"),
                    "result": s.get("value") if state else s.get("fragments") if kind == "retrieval" else
                    s.get("result"), "error": s.get("error"),
                    "text": s.get("text"), "model": s.get("model"),
                    "tokens": (s.get("tokens_in") or 0) + (s.get("tokens_out") or 0) or None,
                    "cost_usd": s.get("cost_usd"), "started_at": None, "finished_at": None})
    return {"steps": out, "answer": answer, "task": None, "status": "completed",
            "started_at": None, "finished_at": None}


def check_run(steps: List[dict], expected: Optional[dict], answer: Optional[str], cfg: dict,
              request: Any = None) -> List[str]:
    """The checks `assay test` makes, on one run held in memory: its case's expectations, the
    contracts, PII and loops. What failed, as "Check: why" lines."""
    traj = as_trajectory(steps, answer)
    ref = None
    if expected:
        ref = {"calls": expected.get("calls") or [], "answer": expected.get("answer"),
               "answer_match": expected.get("answer_match") or "contains", "state": expected.get("state") or [],
               "allow_extra": expected.get("allow_extra") or [], "max_steps": expected.get("max_steps"),
               "checkpoints": expected.get("checkpoints") or [], "split": bool(expected.get("split"))}
    rules = [{"severity": "critical", **c} for c in cfg["contracts"]]
    by_reason: Dict[str, List[str]] = {}  # one line per reason: several checks often share one
    for c in agents.checks_for(traj, ref, rules):
        if c["status"] == "fail":
            by_reason.setdefault(c["reason"], []).append(CHECK_NAMES.get(c["field"], c["field"]))
    if cfg["pii"]["check"]:
        found = pii_findings(traj, cfg["pii"]["allow"], request, cfg["pii"]["answers"], cfg["pii"]["answer_allow"])
        if found:
            by_reason[f"Personal data leaked: {'; '.join(found)}"] = ["PII"]
    for r in behavior.limits(traj, (cfg.get("behavior") or {}).get("limits") or {}):
        if r["status"] == "fail":
            by_reason.setdefault(r["reason"], []).append(CHECK_NAMES[r["field"]])
    return [f"{', '.join(checks)}: {why}" for why, checks in by_reason.items()]


class SetupError(Exception):
    pass


def init(root: Path) -> List[str]:
    """Write assay.toml and the example, leaving anything that already exists alone."""
    ensure_home(root)
    made = []
    if not (root / EXAMPLE).exists():
        (root / EXAMPLE).parent.mkdir(parents=True, exist_ok=True)
        (root / EXAMPLE).write_text(EXAMPLE_TEMPLATE)
        made.append(EXAMPLE)
    if not (root / CONFIG).exists():
        (root / CONFIG).write_text(CONFIG_TEMPLATE.format())
        made.append(CONFIG)
    return made


def _state(home: Path) -> dict:
    try:
        return json.loads((home / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(home: Path, state: dict) -> None:
    (home / "state.json").write_text(json.dumps(state, indent=1))


# ---------- loading a recording ----------

def load_file(engine, path: str, tenant: str) -> Tuple[Dict[str, int], List[str]]:
    """Validate every line of an SDK recording, then ingest it. A file with a bad line loads nothing.
    Returns (counts by event type, problems)."""
    from pydantic import ValidationError
    with open(path, encoding="utf-8") as f:
        lines = [(n, line) for n, line in enumerate(f, 1) if line.strip()]
    events, bad = [], []
    for n, line in lines:
        try:
            events.append(schema.EVENTS.validate_python([json.loads(line)])[0])
        except json.JSONDecodeError as exc:
            bad.append(f"line {n}: not JSON ({exc.msg})")
        except ValidationError as exc:
            err = exc.errors()[0]
            field = ".".join(str(x) for x in err["loc"][2:])
            bad.append(f"line {n}: {field + ': ' if field else ''}{err['msg']}")
    if bad:
        return {}, bad
    by_type: Dict[str, int] = {}
    for i in range(0, len(events), 5000):
        for k, v in schema.ingest(engine, events[i:i + 5000], tenant).items():
            by_type[k] = by_type.get(k, 0) + v
    return by_type, []


# ---------- a test run ----------

TIMED_OUT = 124  # the exit code `timeout` uses


def run_command(command: str, events: Path, run_id: str, repeat: int, timeout: Optional[float] = None,
                rerun_failed: bool = False, prices: Optional[dict] = None) -> List[int]:
    """Run the command once per attempt, with the SDK recording to `events`. Returns the exit codes;
    TIMED_OUT for an attempt stopped at `timeout` seconds (with everything it started)."""
    import signal
    env = {k: v for k, v in os.environ.items() if k != "ASSAY_URL"}  # record locally, never to a server
    env.update(ASSAY_PATH=str(events), ASSAY_TEST_RUN=run_id)
    if prices and not env.get("ASSAY_PRICES"):  # [prices]: model calls are recorded with their cost
        env["ASSAY_PRICES"] = json.dumps(prices)
    if rerun_failed:
        env["ASSAY_RERUN"] = "failed"  # the pytest plugin runs only what didn't pass last time
    codes = []
    for attempt in range(repeat):
        env["ASSAY_TEST_ATTEMPT"] = str(attempt)
        proc = subprocess.Popen(command, shell=True, env=env, start_new_session=True)
        try:
            codes.append(proc.wait(timeout=timeout or None))
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):  # the shell and all it started
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=wait)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
            codes.append(TIMED_OUT)
    return codes


def sync_contracts(engine, rules: List[dict]) -> None:
    """The local source's contracts are exactly those in assay.toml."""
    source = f"events:{TENANT}"
    with engine.begin() as conn:
        conn.execute(delete(store.path_contracts).where(store.path_contracts.c.source == source))
    for c in rules:
        contracts.save(engine, source, c)


def pii_findings(traj: dict, allow: Dict[str, set], request: Any = None, answers: bool = True,
                 answer_allow: Optional[set] = None) -> List[str]:
    """Personal data in the arguments of the run's tool calls, except kinds the tool may receive;
    and, when the request is known, personal data in the answer that the request didn't give
    (someone else's: the user's own email said back to them is fine)."""
    out = []
    for s in traj["steps"]:
        if s["kind"] != "tool" or not s.get("args"):
            continue
        for hit in learn.pii_scan(s["args"]):
            if hit["kind"] not in allow.get(s["name"], ()):
                out.append(f"{hit['kind']} ({hit['sample']}) sent to {s['name']} (step {s['seq']})")
    if answers and request is not None:
        given = {learn.pii_key(k, v) for k, v in learn.pii_matches(request)}
        said = [traj.get("answer")] + [s.get("text") for s in traj["steps"] if s["kind"] == "answer"]
        seen = set()
        for text in filter(None, said):
            for kind, v in learn.pii_matches(text):
                key = learn.pii_key(kind, v)
                if key in given or key in seen or kind in (answer_allow or set()):
                    continue
                seen.add(key)
                out.append(f"{kind} ({learn.pii_sample(v)}) in the answer, which the request didn't give")
    return out


BASELINE = "baseline"  # the per-case baseline, kept as an evaluation run of its own


def dropped(result: dict) -> set:
    """Cases with a check whose pass rate is lower than in its baseline, beyond chance or not."""
    base = result["base_attempts"]
    return {case for (case, f), a in result["attempts"].items()
            if a and base.get((case, f)) and sum(a) / len(a) < sum(base[(case, f)]) / len(base[(case, f)])}


def promote(engine, run_id: str, keep: Optional[set] = None) -> List[str]:
    """Make this run each of its cases' baseline: its results replace those cases' results in the
    baseline, and only theirs, so running a subset leaves every other case's baseline alone. Cases
    in `keep` keep the baseline they have."""
    t = store.eval_results
    rows = [r for r in _rows(engine, run_id) if r.case_id not in (keep or set())]
    cases = sorted({r.case_id for r in rows})
    copies = [{**dict(r._mapping), "run_id": BASELINE, "result_id": ingest._derive(BASELINE, r.result_id)}
              for r in rows]
    m = store.run_metrics
    with engine.connect() as conn:
        mrows = conn.execute(select(m).where((m.c.tenant == TENANT) & (m.c.run_id == run_id))).all()
    mcopies = [{**dict(r._mapping), "run_id": BASELINE, "metric_id": ingest._derive(BASELINE, r.metric_id)}
               for r in mrows]
    with engine.begin() as conn:
        for i in range(0, len(cases), 500):
            for tbl in (t, m):
                conn.execute(tbl.delete().where((tbl.c.tenant == TENANT) & (tbl.c.run_id == BASELINE)
                                                & tbl.c.case_id.in_(cases[i:i + 500])))
        if copies:
            conn.execute(t.insert(), copies)
        if mcopies:
            conn.execute(m.insert(), mcopies)
    return cases


def check_pii(engine, source, run_id: str, pii: dict) -> None:
    """One PII result per agent run, stored like the trajectory checks."""
    heads = agents.run_trajectories(engine, TENANT, run_id)
    trajs = source.trajectories([h["trajectory_id"] for h in heads])
    requests = learn._inputs(engine, TENANT, [h["trajectory_id"] for h in heads])
    rows = []
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is None:
            continue
        found = pii_findings(traj, pii["allow"], (requests.get(h["trajectory_id"]) or {}).get("input"),
                             pii.get("answers", True), pii.get("answer_allow"))
        case = h["case_id"] or h["trajectory_id"]
        rows.append({"tenant": TENANT, "result_id": ingest._derive(run_id, case, "pii", PII_EVALUATOR, h["attempt"]),
                     "run_id": run_id, "case_id": case, "document_id": h["trajectory_id"],
                     "evaluator": PII_EVALUATOR, "attempt": h["attempt"], "ts": h["started_at"],
                     "lineage": h["lineage"], "score": None, "field": "pii",
                     "status": "fail" if found else "pass", "expected": "no personal data in tool arguments or answers",
                     "actual": "; ".join(found)[:300] or "none",
                     "reason": f"Personal data leaked: {'; '.join(found)}"[:2000] if found else None})
    ingest.upsert(engine, store.eval_results, rows, "result_id")


def check_limits(engine, source, run_id: str, limits: dict) -> None:
    """The [behavior] limits, one result per agent run and limit, stored like the trajectory checks."""
    heads = agents.run_trajectories(engine, TENANT, run_id)
    trajs = source.trajectories([h["trajectory_id"] for h in heads])
    rows = []
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is None:
            continue
        case = h["case_id"] or h["trajectory_id"]
        for r in behavior.limits(traj, limits):
            rows.append({"tenant": TENANT, "run_id": run_id, "case_id": case, "document_id": h["trajectory_id"],
                         "result_id": ingest._derive(run_id, case, r["field"], behavior.LIMIT_EVALUATOR, h["attempt"]),
                         "evaluator": behavior.LIMIT_EVALUATOR, "attempt": h["attempt"], "ts": h["started_at"],
                         "lineage": h["lineage"], "score": None, "field": r["field"], "status": r["status"],
                         "expected": r["expected"], "actual": r["actual"], "reason": r["reason"]})
    ingest.upsert(engine, store.eval_results, rows, "result_id")


def case_behavior(engine, run_id: str, tenant: str = TENANT) -> Dict[str, dict]:
    """Per case: its behavior over its attempts (assay/behavior.py)."""
    m = store.run_metrics
    with engine.connect() as conn:
        rows = conn.execute(select(m.c.case_id, m.c.metrics).where((m.c.tenant == tenant) & (m.c.run_id == run_id))).all()
    by = defaultdict(list)
    for r in rows:
        by[r.case_id].append(r.metrics)
    return {c: behavior.combine(ms) for c, ms in by.items()}


def evaluate(engine, run_id: str, baseline: Optional[str], tolerance: float,
             pii: Optional[dict] = None, behavior_cfg: Optional[dict] = None,
             abandoned_why: Optional[str] = None, acks: Optional[List[dict]] = None) -> Optional[dict]:
    """Check the run and compare it with the baseline. None if the run recorded nothing to check.
    For a run whose command has exited: what it left open is closed and evaluated first."""
    source = EventsSource(engine, TENANT)
    heads = agents.run_trajectories(engine, TENANT, run_id)
    left_open = 0
    if heads:
        # The command has exited: a run it left open will never end. Say so, instead of skipping it.
        left_open = lifecycle.abandon(engine, tenant=TENANT,
                                      ids=[h["trajectory_id"] for h in heads if h["status"] == "running"])
        lifecycle.evaluate(engine, lifecycle.pending(engine, TENANT, [h["trajectory_id"] for h in heads]),
                           abandoned_why=abandoned_why or "the command exited first")
        if pii and pii["check"]:
            check_pii(engine, source, run_id, pii)
        if (behavior_cfg or {}).get("limits"):
            check_limits(engine, source, run_id, behavior_cfg["limits"])
    out = compare(engine, run_id, baseline, tolerance, behavior_cfg, acks=acks)
    return out and {**out, "left_open": left_open}


def compare(engine, run_id: str, baseline: Optional[str], tolerance: float, behavior_cfg: Optional[dict] = None,
            tenant: str = TENANT, source=None, acks: Optional[List[dict]] = None) -> Optional[dict]:
    """Compare a run's results with the baseline's, changing nothing: what `assay test`, `assay
    diff` and the server's diff read. None if the run has no results. acks: assay.acks.toml's
    acknowledgements (assay/acks.py), decided for this run into result["acks"]."""
    source = source or EventsSource(engine, tenant)
    # "" means no baseline: failures.evaluation would otherwise pick the run before this one.
    a = failures.evaluation(engine, source, tenant, run_id, baseline or "", tolerance)
    if a is None:
        return None
    # Results whose evaluator was given the wrong data (assay/audit.py) say nothing about the AI:
    # they're listed on their own and left out of every count below.
    rows, base_rows = _rows(engine, run_id, tenant), _rows(engine, baseline, tenant) if baseline else []
    ran = {r.case_id for r in rows}
    base_rows = [r for r in base_rows if r.case_id in ran]  # a subset is compared on its own cases
    found = audit.audit_rows(engine, tenant, rows)
    not_judged = [c for c in a["verdicts"]["checks"] if c["verdict"] in verdicts.NOT_JUDGED]
    skip = {(c["case_id"], c["field"] or "", c["evaluator"] or "") for c in not_judged}
    # Listed apart, and out of every count: judged on the wrong data, or not judged at all.
    rows = [r for r in rows if r.result_id not in found and flaky.check_key(r) not in skip]
    base_rows = [r for r in base_rows if r.result_id not in audit.audit_rows(engine, tenant, base_rows)]
    out = {"stability": a["stability"], "fields": field_rates(rows, base_rows), "failing": failing(rows),
           "attempts": attempts(rows), "base_attempts": attempts(base_rows),
           "not_judged": not_judged, **_behavior_changes(engine, run_id, baseline, ran, behavior_cfg, tenant),
           "judge_changed": judge_changes(rows, base_rows), **routing(engine, tenant, rows, base_rows)}
    from assay import acks as acks_
    acks_.apply(engine, tenant, run_id, out, acks or [])
    skip = set(out["judge_changed"]) | set((out.get("acks") or {}).get("quiet") or {})
    out.update(scores(engine, tenant, run_id, rows, base_rows, skip))
    out["trust"] = trust(engine, tenant, rows)
    out["surface"] = surface_shift(engine, tenant, rows, base_rows) if baseline else []
    out["kinds"] = failure_kinds(rows, base_rows)
    out["setup"] = setup_changes(engine, tenant, rows, base_rows) if baseline else {}
    out["fixed_context"] = fixed_context(engine, tenant, rows, base_rows)
    from assay import documents  # extraction scored per field (assay_sdk.documents): precision, recall, all correct
    out["documents"], out["documents_before"] = documents.summarize(rows), documents.summarize(base_rows)
    return out


def _behavior_changes(engine, run_id: str, baseline: Optional[str], ran: set, cfg: Optional[dict],
                      tenant: str = TENANT) -> dict:
    """{"behavior": cases whose behavior got worse than their baseline's, [{"case_id", "changes"}],
    "behavior_compared": the cases that could be compared}."""
    if not baseline:
        return {"behavior": [], "behavior_compared": [], "behavior_suite": []}
    ratios = (cfg or {}).get("ratios") or {}
    now, before = case_behavior(engine, run_id, tenant), case_behavior(engine, baseline, tenant)
    compared = sorted(ran & set(now) & set(before))
    worse = [{"case_id": case, "changes": ch} for case in compared
             if (ch := behavior.compare(now[case], before[case], ratios))]
    ratio = (cfg or {}).get("suite", behavior.SUITE_RATIO)
    suite = behavior.suite_totals(now, before, compared, ratio)
    return {"behavior": worse, "behavior_compared": compared, "behavior_suite": suite,
            "_suite": (now, before, compared, ratio)}  # for acknowledgements to take their cases out


def _rows(engine, run_id: str, tenant: str = TENANT) -> list:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t).where((t.c.tenant == tenant) & (t.c.run_id == run_id))).all()


# ---------- scores: the noise floor, and cases that are a coin flip ----------

FLOOR_RUNS = 10  # recent runs the check passed in: the scores it shows when nothing is wrong
FLOOR_MIN = 3  # fewer scores than this aren't a floor


def _judged_scores(rs) -> Dict[Tuple[str, str], List[float]]:
    out: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for r in rs:
        if r.status in ("pass", "fail") and r.score is not None:
            out[(r.case_id, r.field or "result")].append(float(r.score))
    return out


def noise_floors(engine, tenant: str, keys: set, run_id: str, base_rows: list) -> Dict[Tuple[str, str], dict]:
    """Per (case, field): its scores in the last FLOOR_RUNS runs where it passed every attempt, or its
    baseline's when there's no such history. {"min", "max", "median", "n"}."""
    t = store.eval_results
    cases = sorted({k[0] for k in keys})
    hist: Dict[Tuple[str, str], Dict[str, list]] = defaultdict(lambda: defaultdict(list))
    with engine.connect() as conn:
        for i in range(0, len(cases), 500):
            for r in conn.execute(select(t.c.run_id, t.c.case_id, t.c.field, t.c.status, t.c.score, t.c.ts).where(
                    (t.c.tenant == tenant) & t.c.case_id.in_(cases[i:i + 500]) & (t.c.run_id != run_id)
                    & (t.c.run_id != BASELINE) & t.c.status.in_(("pass", "fail")))):
                k = (r.case_id, r.field or "result")
                if k in keys:
                    hist[k][r.run_id].append(r)
    base = _judged_scores(base_rows)
    out = {}
    for k in keys:
        runs = [rs for rs in hist[k].values() if all(r.status == "pass" for r in rs)]
        runs.sort(key=lambda rs: max(r.ts for r in rs))
        vals = [float(r.score) for rs in runs[-FLOOR_RUNS:] for r in rs if r.score is not None] or base.get(k, [])
        if vals:
            out[k] = {"min": min(vals), "max": max(vals), "median": median(vals), "n": len(vals)}
    return out


def scores(engine, tenant: str, run_id: str, rows: list, base_rows: list, skip: set) -> dict:
    """{"score_regressions": a check still passing, scored below the floor it showed when nothing
    was wrong; "within_noise": lower, inside it; "no_floor": lower, with too few scores to tell;
    "coin_flips": cases that give different answers on the same system}."""
    now = _judged_scores(rows)
    passing = {k for k, a in attempts(rows).items() if all(a)}
    keys = {k for k in now if k in passing and k not in skip}
    floors = noise_floors(engine, tenant, keys, run_id, base_rows) if keys else {}
    out = {"score_regressions": [], "within_noise": [], "no_floor": [], "coin_flips": []}
    for k in sorted(keys):
        f, m = floors.get(k), median(now[k])
        if f is None or m >= f["median"]:
            continue
        item = {"case_id": k[0], "field": k[1], "now": m, "floor": f}
        out["no_floor" if f["n"] < FLOOR_MIN else "score_regressions" if m < f["min"] else "within_noise"].append(item)
    # A coin flip: attempts of the same system that disagree with each other a lot.
    spans: Dict[str, List[float]] = defaultdict(list)
    for (case, field), v in now.items():
        spans[field] += v
    for k, a in sorted(attempts(rows).items()):
        passed, n = sum(a), len(a)
        split = n >= 3 and 0 < passed < n and min(passed, n - passed) >= max(1, 0.2 * n)
        v = now.get(k) or []
        lo, hi = (min(spans[k[1]]), max(spans[k[1]])) if spans.get(k[1]) else (0, 0)
        wide = len(v) >= 3 and hi > lo and (max(v) - min(v)) >= 0.5 * (hi - lo)
        if split or wide:
            out["coin_flips"].append({"case_id": k[0], "field": k[1], "passed": passed, "n": n,
                                      "scores": (min(v), max(v)) if v else None})
    return out


def _floor(f: dict) -> str:
    return f"{f['min']:g}–{f['max']:g}" if f["min"] != f["max"] else f"{f['min']:g}"


# ---------- what changed around a case: its prompts, models and tools ----------

def _setups(engine, tenant: str, docs: List[str]) -> Dict[str, dict]:
    """Per run: the prompt versions its model calls used, the models, and the tools they were offered."""
    st = store.agent_steps
    out: Dict[str, dict] = defaultdict(lambda: {"prompts": set(), "models": set(), "tools": set(), "calls": []})
    ids = sorted({d for d in docs if d})
    with engine.connect() as conn:
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(st.c.trajectory_id, st.c.prompt, st.c.model, st.c.tools, st.c.context,
                                         st.c.media, st.c.settings).where(
                    (st.c.tenant == tenant) & st.c.trajectory_id.in_(ids[i:i + 500]) & (st.c.kind == "reason"))):
                s = out[r.trajectory_id]
                if r.prompt:
                    s["prompts"].add(r.prompt)
                if r.model:
                    s["models"].add(r.model)
                s["tools"] |= set(r.tools or [])
                s["calls"].append({"context": r.context or {}, "media": r.media or {}, "settings": r.settings or {}})
    return out


def _inputs_of(calls: List[dict]) -> dict:
    """A case's model calls, summed up: the median system prompt and tool definitions per call, the
    media per call, and the settings most calls used."""
    med = lambda k: median(c["context"].get(k) or 0 for c in calls) if calls else 0
    per = [(c["media"].get("images") or 0) + (c["media"].get("videos") or 0) for c in calls]
    common = lambda key: Counter(c["media"].get(key) for c in calls if c["media"].get(key)).most_common(1)
    settings: Dict[str, Any] = {}
    for k in sorted({k for c in calls for k in c["settings"]}):
        vals = Counter(json.dumps(c["settings"].get(k)) for c in calls if k in c["settings"])
        settings[k] = json.loads(vals.most_common(1)[0][0])
    return {"system": med("system"), "tools": med("tools"), "known": any(c["context"] for c in calls),
            "media": {"per_call": max(per, default=0), "size": (common("size") or [(None,)])[0][0],
                      "detail": (common("detail") or [(None,)])[0][0]} if any(per) else None,
            "settings": settings}


def fixed_context(engine, tenant: str, rows: list, base_rows: list) -> Optional[dict]:
    """The fixed context per model call, this run and its baseline: the system prompt and tool
    definitions every call starts with, whatever it was asked. The input nobody measures."""
    def of(rs):
        docs = {r.document_id for r in rs if r.document_id}
        calls = [c for s in _setups(engine, tenant, list(docs)).values() for c in s["calls"] if c["context"]]
        if not calls:
            return None
        sys_ = median(c["context"].get("system") or 0 for c in calls)
        tools = median(c["context"].get("tools") or 0 for c in calls)
        return {"system": round(sys_), "tools": round(tools), "fixed": round(sys_ + tools), "calls": len(calls)}
    now = of(rows)
    return {"now": now, "before": of(base_rows) if base_rows else None} if now else None


def fixed_context_text(f: dict) -> str:
    n, b = f["now"], f.get("before")
    line = f"{n['fixed']:,} tokens (system prompt {n['system']:,}, tool definitions {n['tools']:,})"
    if b and b["fixed"] != n["fixed"]:
        line += f", {b['fixed']:,} before ({n['fixed'] - b['fixed']:+,})"
    return line


def _input_changes(a: dict, b: dict) -> List[dict]:
    out = []
    if a["known"] and b["known"]:
        parts = {k: (a[k], b[k]) for k in ("system", "tools")
                 if abs(b[k] - a[k]) >= max(200, 0.2 * a[k]) and a[k] != b[k]}
        if parts:
            out.append({"what": "context", "parts": {k: [round(x), round(y)] for k, (x, y) in parts.items()}})
    if a["settings"] and b["settings"] and a["settings"] != b["settings"]:
        ch = {k: [a["settings"].get(k), b["settings"].get(k)] for k in sorted(a["settings"].keys() | b["settings"].keys())
              if a["settings"].get(k) != b["settings"].get(k)}
        out.append({"what": "settings", "changed": ch})
    if (a["media"] or b["media"]) and a["media"] != b["media"]:
        out.append({"what": "media", "before": a["media"], "now": b["media"]})
    return out


def _versions(prompts: set) -> Dict[str, set]:
    by: Dict[str, set] = defaultdict(set)
    for p in prompts:
        pid, _, ver = p.partition("@")
        by[pid].add(ver or "(unversioned)")
    return by


def setup_changes(engine, tenant: str, rows: list, base_rows: list) -> dict:
    """{"cases": {case: [change]}, "everywhere": [change], "now": {case: setup}}: each case's prompt
    versions, models and offered tools against its baseline run's. A change every compared case
    shares is said once, in "everywhere"."""
    docs_now, docs_before = defaultdict(set), defaultdict(set)
    for r in rows:
        docs_now[r.case_id].add(r.document_id)
    for r in base_rows:
        docs_before[r.case_id].add(r.document_id)
    setups = _setups(engine, tenant, [d for ds in [*docs_now.values(), *docs_before.values()] for d in ds])

    def union(ds):
        u = {"prompts": set(), "models": set(), "tools": set()}
        calls = []
        for d in ds:
            for k in u:
                u[k] |= setups[d][k] if d in setups else set()
            calls += setups[d]["calls"] if d in setups else []
        return {**u, "inputs": _inputs_of(calls)}
    now = {c: union(ds) for c, ds in docs_now.items()}
    before = {c: union(ds) for c, ds in docs_before.items()}
    cases: Dict[str, List[dict]] = {}
    for c in now.keys() & before.keys():
        a, b = before[c], now[c]
        if not any(a.values()) or not any(b.values()):
            continue
        ch = []
        va, vb = _versions(a["prompts"]), _versions(b["prompts"])
        for pid in sorted(va.keys() | vb.keys()):
            if va.get(pid) != vb.get(pid):
                ch.append({"what": "prompt", "id": pid, "before": sorted(va.get(pid, [])), "now": sorted(vb.get(pid, []))})
        if a["models"] != b["models"] and a["models"] and b["models"]:
            ch.append({"what": "model", "before": sorted(a["models"]), "now": sorted(b["models"])})
        if a["tools"] != b["tools"]:
            ch.append({"what": "tools", "added": sorted(b["tools"] - a["tools"]), "removed": sorted(a["tools"] - b["tools"])})
        ch += _input_changes(a["inputs"], b["inputs"])
        if ch:
            cases[c] = ch
    compared = [c for c in now.keys() & before.keys() if any(now[c].values()) and any(before[c].values())]
    key = lambda x: json.dumps(x, sort_keys=True)
    counts = Counter(key(x) for chs in cases.values() for x in chs)
    everywhere = [json.loads(k) for k, n in counts.items() if n == len(compared) and n >= 2]
    shared = {key(x) for x in everywhere}
    cases = {c: [x for x in chs if key(x) not in shared] for c, chs in cases.items()}
    for x in [*everywhere, *(x for chs in cases.values() for x in chs)]:
        if x["what"] == "prompt" and len(x["before"]) == 1 and len(x["now"]) == 1 and "diff" not in x:
            x["diff"] = _prompt_diff(engine, tenant, x["id"], x["before"][0], x["now"][0])
    return {"cases": {c: chs for c, chs in cases.items() if chs}, "everywhere": everywhere,
            "now": {c: {k: sorted(v) for k, v in s.items() if k != "inputs"} for c, s in now.items()}}


def _prompt_diff(engine, tenant: str, pid: str, a: str, b: str) -> Optional[dict]:
    from assay import prompts
    d = prompts.diff(engine, tenant, pid, a, b)
    if not d or not d.get("available"):
        return None
    lines = [x for x in d["diff"] if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    added = [x[1:].strip() for x in lines if x.startswith("+")]
    return {"added": len(added), "removed": sum(1 for x in lines if x.startswith("-")),
            "first": next((x for x in added if x), None), "note": d.get("note")}


def change_text(x: dict) -> str:
    """prompt   support_agent@12 → support_agent@13 (+2 lines, −1: "Refund right away …")"""
    if x["what"] == "prompt":
        was = ", ".join(f"{x['id']}@{v}" for v in x["before"]) or "(not used)"
        now = ", ".join(f"{x['id']}@{v}" for v in x["now"]) or "(not used)"
        d = x.get("diff")
        tail = ""
        if d:
            tail = f" (+{d['added']} line{'s' * (d['added'] != 1)}, −{d['removed']}" + \
                   (f": “{d['first'][:70]}{'…' if len(d['first']) > 70 else ''}”" if d.get("first") else "") + ")"
            if d.get("note"):
                tail += f" — {d['note']}"
        return f"prompt  {was} → {now}{tail}"
    if x["what"] == "model":
        return f"model   {', '.join(x['before'])} → {', '.join(x['now'])}"
    if x["what"] == "context":
        names = {"system": "system prompt", "tools": "tool definitions"}
        return "context " + "; ".join(f"{names[k]} {a:,} → {b:,} tokens ({b - a:+,})" for k, (a, b) in x["parts"].items()) \
            + " per call"
    if x["what"] == "settings":
        return "settings " + ", ".join(f"{k} {a if a is not None else '(unset)'} → {b if b is not None else '(unset)'}"
                                        for k, (a, b) in x["changed"].items())
    if x["what"] == "media":
        def m(v):
            if not v:
                return "none"
            return f"{v['per_call']} per call" + (f" at {v['size']}" if v.get("size") else "") + \
                (f", detail {v['detail']}" if v.get("detail") else "")
        return f"media   {m(x['before'])} → {m(x['now'])}"
    parts = ([f"+{t}" for t in x["added"]] + [f"−{t}" for t in x["removed"]])
    return f"tools   offered {' '.join(parts)}"


def blame(setup: dict, regressed: List[str]) -> List[str]:
    """When the regressions line up with a prompt version: which, and how the others did."""
    now = setup.get("now") or {}
    if not regressed or not now:
        return []
    bad = set(regressed)
    out = []
    changed = {(x["id"], v) for chs in [setup.get("everywhere") or [], *(setup.get("cases") or {}).values()]
               for x in chs if x["what"] == "prompt" for v in x["now"]}
    for pid, ver in sorted(changed):
        on = {c for c, s in now.items() if f"{pid}@{ver}" in s["prompts"] or (ver == "(unversioned)" and pid in s["prompts"])}
        off = {c for c, s in now.items() if any(p.startswith(f"{pid}@") for p in s["prompts"])} - on
        hit = bad & on
        if hit and len(hit) == len(bad & (on | off)) and on != set(now):
            rest = f"; the {_n(len(off), 'case')} still on another version {'all pass' if not (bad & off) else 'pass'}" \
                if off else ""
            out.append(f"{len(hit)} of {len(bad & (on | off))} regressions use {pid}@{ver}{rest}")
    return out


# ---------- what kinds of failure ----------

def failure_kinds(rows: list, base_rows: list) -> dict:
    """Failures by the kind their evaluator named (fabricated, contradicts_source, ...): {"now", "before"}.
    A hallucination that fabricates needs another fix than one that contradicts its source."""
    from assay.judge import LEGACY
    def count(rs):
        return dict(Counter(LEGACY.get(r.category, r.category) for r in rs
                            if r.status == "fail" and getattr(r, "category", None)).most_common())
    now = count(rows)
    return {"now": now, "before": count(base_rows), "compared": bool(base_rows)} if now else {}


def kinds_text(k: dict) -> str:
    before = k.get("before") or {}
    return " · ".join(f"{n} {kind.replace('_', ' ')}" + (f" ({before.get(kind, 0)} before)" if k.get("compared") else "")
                      for kind, n in k["now"].items())


# ---------- a score that rose with the answers' surface ----------

def _answers(engine, tenant: str, ids: List[str]) -> Dict[str, str]:
    t = store.agent_trajectories
    out = {}
    ids = sorted({i for i in ids if i})
    with engine.connect() as conn:
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(t.c.trajectory_id, t.c.answer).where(
                    (t.c.tenant == tenant) & t.c.trajectory_id.in_(ids[i:i + 500]))):
                if r.answer:
                    out[r.trajectory_id] = r.answer
    return out


def surface_shift(engine, tenant: str, rows: list, base_rows: list) -> List[dict]:
    """Judged fields whose scores rose while the answers got longer, more formatted or more cited
    than the baseline's: part of the rise may be what the judge rewards besides quality."""
    from assay.calibrate import surface
    judged = lambda rs: [r for r in rs if r.score is not None and r.status in ("pass", "fail")]
    now, before = judged(rows), judged(base_rows)
    if not now or not before:
        return []
    texts = _answers(engine, tenant, [r.document_id for r in [*now, *before]])
    out = []
    for field in sorted({r.field or "result" for r in now}):
        a = [r for r in before if (r.field or "result") == field]
        b = [r for r in now if (r.field or "result") == field]
        cases = {r.case_id for r in a} & {r.case_id for r in b}
        if len(cases) < 3:
            continue
        per = lambda rs: {c: median(r.score for r in rs if r.case_id == c) for c in cases}
        rise = mean(v - per(a)[c] for c, v in per(b).items())
        fa = [surface(texts[r.document_id]) for r in a if r.case_id in cases and r.document_id in texts]
        fb = [surface(texts[r.document_id]) for r in b if r.case_id in cases and r.document_id in texts]
        if rise <= 0 or len(fa) < 3 or len(fb) < 3:
            continue
        words = median(f["words"] for f in fb) / max(1, median(f["words"] for f in fa))
        share = lambda fs, k: sum(f[k] for f in fs) / len(fs)
        why = []
        if words >= 1.25:
            why.append(f"the answers are {words - 1:.0%} longer")
        for k, what in (("formatted", "formatted with headers or bullets"), ("citations", "citing sources")):
            if share(fb, k) - share(fa, k) >= 0.25:
                why.append(f"{share(fb, k):.0%} are {what} ({share(fa, k):.0%} before)")
        if why:
            out.append({"field": field, "rise": rise, "cases": len(cases), "why": why,
                        "text": f"{_label(field)} rose {rise:.2f} on average over {len(cases)} cases, and "
                                f"{' and '.join(why)} than the baseline's: part of the rise may be what the judge "
                                f"rewards besides quality. Check it with calibration's bias probes."})
    return out


# ---------- can a judged number be trusted: its calibration ----------

STALE_DAYS = 30


def trust(engine, tenant: str, rows: list) -> Dict[str, dict]:
    """Per judged field (one with scores): whether its judge was calibrated against people, when,
    how well, and for the same judge that scored this run."""
    from assay.calibrate import family
    fields: Dict[str, set] = defaultdict(set)
    docs: Dict[str, set] = defaultdict(set)
    for r in rows:
        if r.score is not None and r.status in ("pass", "fail"):
            fields[r.field or "result"].update({r.judge_model} if getattr(r, "judge_model", None) else set())
            if r.document_id:
                docs[r.field or "result"].add(r.document_id)
    if not fields:
        return {}
    served = serving_models(engine, tenant, [d for ds in docs.values() for d in ds])

    def same_family(f, models):
        judges = {family(m) for m in models} - {None}
        answered = {family(m) for d in docs[f] for m in (served.get(d) or "").split(" + ") if m} - {None}
        both = judges & answered
        return sorted(both)[0] if both else None
    c = store.calibrations
    with engine.connect() as conn:
        cals = conn.execute(select(c.c.run_id, c.c.created_at, c.c.passed, c.c.result).where(c.c.tenant == tenant)
                            .order_by(c.c.created_at)).all()
    latest: Dict[str, Any] = {}
    for x in cals:
        f = (x.result.get("calibration") or {}).get("field")
        if f:
            latest[f] = x
    out = {}
    for f, models in sorted(fields.items()):
        x = latest.get(f)
        fam = same_family(f, models)
        if x is None:
            out[f] = {"state": "none", "same_family": fam}
            continue
        cal = x.result["calibration"]
        age = (datetime.utcnow() - x.created_at).days
        cal_models = set(cal.get("models") or [])
        groups = cal.get("groups") or {}
        state = "regressed" if not x.passed else "other_judge" if models and cal_models and models != cal_models \
            else "topic" if groups.get("topic") else "stale" if age > STALE_DAYS else "ok"
        out[f] = {"state": state, "age": age, "spearman": cal.get("spearman"), "n": cal.get("n"), "groups": groups,
                  "models": sorted(cal_models), "now": sorted(models), "run_id": x.run_id, "same_family": fam}
    return out


def trust_text(field: str, t: dict) -> str:
    fam = (f"; the judge and the answers are both {t['same_family']} models, and judges favor their own family"
           if t.get("same_family") else "")
    return _trust_text(t) + fam


def _trust_text(t: dict) -> str:
    if t["state"] == "none":
        return "not calibrated: its scores haven't been checked against people (`assay calibrate`)"
    when = "today" if t["age"] == 0 else f"{t['age']} day{'s' * (t['age'] != 1)} ago"
    how = f"Spearman {t['spearman']:.2f} on {t['n']} items" if t.get("spearman") is not None else f"{t['n']} items"
    base = f"calibrated {when}: {how}" + (f" ({', '.join(t['models'])})" if t["models"] else "")
    return {"ok": base,
            "stale": f"{base}; over {STALE_DAYS} days old, and a provider can change a model under its name",
            "regressed": f"its last calibration regressed ({t['run_id']}): don't lean on these scores",
            "other_judge": f"calibrated for {', '.join(t['models'])}, but judged by {', '.join(t['now'])} this run: "
                           f"not calibrated for that judge",
            "topic": _topic_text(base, t.get("groups") or {})}[t["state"]]


def _topic_text(base: str, g: dict) -> str:
    what = "tag" if g.get("by") == "tags" else "input"
    within = g.get("within")
    return (f"{base}, but it tracks the {'topic' if what == 'tag' else 'input'}, not the answer: "
            f"Spearman {within:.2f} among answers of the same {what}" if within is not None else base)


def trust_lines(result: dict) -> List[str]:
    t = result.get("trust") or {}
    if not t:
        return []
    w = max(len(_label(f)) for f in t)
    tone = {"ok": "dim", "none": "yellow", "stale": "yellow", "regressed": "red", "other_judge": "yellow", "topic": "yellow"}
    return [_paint("Judges", "bold")] + [_paint(f"  {_label(f):<{w}}  {trust_text(f, x)}",
                                                "yellow" if x.get("same_family") and x["state"] == "ok" else tone[x["state"]])
                                         for f, x in t.items()] + [""]


# ---------- which judge, and which model served ----------

def _judge_id(r) -> Optional[str]:
    m, p = getattr(r, "judge_model", None), getattr(r, "judge_prompt", None)
    return None if not (m or p) else " · ".join(x for x in (m, p) if x)


def judge_changes(rows: list, base_rows: list) -> Dict[Tuple[str, str], dict]:
    """(case, field) whose judge (model or prompt) differs from the baseline's: {"before", "now"}.
    Results that didn't say which judge they were are left alone."""
    def ids(rs):
        by: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
        for r in rs:
            j = _judge_id(r)
            if j:
                by[(r.case_id, r.field or "result")][j] += 1
        return {k: c.most_common(1)[0][0] for k, c in by.items()}
    now, before = ids(rows), ids(base_rows)
    return {k: {"before": before[k], "now": now[k]} for k in now.keys() & before.keys() if now[k] != before[k]}


def serving_models(engine, tenant: str, trajectories: List[str]) -> Dict[str, str]:
    """The model(s) that served each run, from its model calls: "claude-opus-5", or "a + b"."""
    st = store.agent_steps
    got: Dict[str, set] = defaultdict(set)
    ids = sorted({t for t in trajectories if t})
    with engine.connect() as conn:
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(st.c.trajectory_id, st.c.model).where(
                    (st.c.tenant == tenant) & st.c.trajectory_id.in_(ids[i:i + 500]) & (st.c.kind == "reason")
                    & st.c.model.isnot(None))):
                got[r.trajectory_id].add(r.model)
    return {t: " + ".join(sorted(ms)) for t, ms in got.items()}


def routing(engine, tenant: str, rows: list, base_rows: list) -> dict:
    """With more than one model serving: {"routing": {(case, field): {"now": {model: [passed, n]},
    "before": {...}}}, "models": {model: [cases passing on it, cases]}}. Empty with one model."""
    served = serving_models(engine, tenant, [r.document_id for r in [*rows, *base_rows]])
    if len(set(served.values())) < 2:
        return {"routing": {}, "models": {}}

    def split(rs):
        out: Dict[Tuple[str, str], Dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in rs:
            m = served.get(r.document_id)
            if m and r.status != "error":
                x = out[(r.case_id, r.field or "result")][m]
                x[0] += r.status == "pass"
                x[1] += 1
        return out
    now, before = split(rows), split(base_rows)
    keys = [k for k in now if len(set(now[k]) | set(before.get(k, {}))) > 1]
    cases: Dict[str, Dict[str, bool]] = defaultdict(dict)
    for (case, _), ms in now.items():
        for m, (p, n) in ms.items():
            cases[m][case] = cases[m].get(case, True) and p == n
    return {"routing": {k: {"now": {m: list(v) for m, v in now[k].items()},
                            "before": {m: list(v) for m, v in before.get(k, {}).items()}} for k in keys},
            "models": {m: [sum(c.values()), len(c)] for m, c in sorted(cases.items())}}


def routing_line(r: dict) -> str:
    """claude-opus-5 passed 3/3 (3/3 before) · gpt-5-mini passed 0/2 (not in the baseline)"""
    parts = []
    for m, (p, n) in sorted(r["now"].items(), key=lambda kv: -kv[1][0] / kv[1][1]):
        b = r["before"].get(m)
        parts.append(f"{m} passed {p}/{n}" + (f" ({b[0]}/{b[1]} before)" if b else " (not in the baseline)"))
    return " · ".join(parts)


def routed(r: dict) -> Optional[str]:
    """When passing and failing line up with the model: "fails only on gpt-5-mini (0/3), passes on ..."."""
    bad = {m: v for m, v in r["now"].items() if v[0] == 0}
    good = {m: v for m, v in r["now"].items() if v[0] == v[1]}
    if bad and good and len(bad) + len(good) == len(r["now"]):
        fmt = lambda d: ", ".join(f"{m} ({p}/{n})" for m, (p, n) in sorted(d.items()))
        return f"fails only on {fmt(bad)}, passes on {fmt(good)}: the model it was routed to, not chance"
    return None


def attempts(rows: list) -> Dict[Tuple[str, str], List[bool]]:
    """Per (case, field): whether each judged attempt passed. An attempt that couldn't run (an
    evaluator or infrastructure error) isn't a failure: it's listed with what wasn't judged."""
    out = defaultdict(list)
    for r in rows:
        if r.status != "error":
            out[(r.case_id, r.field or "result")].append(r.status == "pass")
    return dict(out)


def field_rates(rows: list, base_rows: list) -> List[dict]:
    """Per check (answer, tool_calls, ... or your own field): cases passing on every attempt."""
    def rates(rs):
        by = defaultdict(dict)
        for (case, field), a in attempts(rs).items():
            by[field][case] = all(a)
        return {f: (sum(cases.values()), len(cases)) for f, cases in by.items()}
    cur, base = rates(rows), rates(base_rows)
    order = [k for k in CHECK_NAMES if k in cur] + sorted(k for k in cur if k not in CHECK_NAMES)
    return [{"field": f, "label": CHECK_NAMES.get(f, f), "passed": cur[f][0], "total": cur[f][1],
             "base_passed": base[f][0] if f in base else None, "base_total": base[f][1] if f in base else None}
            for f in order]


def failing(rows: list) -> Dict[Tuple[str, str], dict]:
    """The first failing attempt of each (case, field), with why."""
    out = {}
    for r in rows:
        key = (r.case_id, r.field or "result")
        if r.status == "fail" and key not in out:
            out[key] = {"reason": r.reason, "expected": r.expected, "actual": r.actual}
    return out


def classify(result: dict, has_baseline: bool) -> dict:
    """Sort each failing check: a problem (a regression, a new case that fails, or with no baseline
    any failure), flaky (passes some attempts, no worse than chance; doesn't block), unsure (plausibly
    worse, too few attempts to tell: inconclusive, not a regression), or still failing (failed in the
    baseline too; not this change's doing)."""
    cur, base = result["attempts"], result["base_attempts"]
    st = result["stability"]
    flaky_keys = {(i["case_id"], i["field"] or "result") for i in st["flaky"]}
    unsure_keys = {(i["case_id"], i["field"] or "result") for i in st["reruns"]}
    out = {"problems": [], "flaky": [], "unsure": [], "still": [], "acked": []}
    decided = result.get("acks") or {}
    quiet, woke = decided.get("quiet") or {}, decided.get("woke") or {}
    changed = result.get("judge_changed") or {}
    out["judge_changed"] = []
    for key, a in sorted(cur.items()):
        if all(a):
            continue
        item = {"case_id": key[0], "field": key[1], "rate": sum(a) / len(a), "base_rate": None, "kind": "failing"}
        b = base.get(key)
        if key in changed and b is not None:  # a new judge: not the AI's doing, and not the same measure
            out["judge_changed"].append({**item, "base_rate": sum(b) / len(b), **changed[key]})
            continue
        if key in woke:  # acknowledged, but worse than it was: it says so, and blocks
            out["problems"].append({**item, "kind": "worse than acknowledged", "base_rate": sum(b) / len(b) if b else None,
                                    "ack": woke[key][0], "woke": woke[key][1]})
            continue
        if has_baseline and b is None:
            item["kind"] = "new"
        elif has_baseline and not any(b):
            out["acked" if key in quiet else "still"].append({**item, "ack": quiet[key]} if key in quiet else item)
            continue
        elif has_baseline:
            # With one attempt there's nothing to tell chance by: a pass that became a failure is a regression.
            item.update(kind="regression", base_rate=sum(b) / len(b), unsure=key in unsure_keys and len(a) > 1)
            # Fails on exactly the model it was routed to: a cause, not chance, however few the attempts.
            if item["rate"] < item["base_rate"] and routed((result.get("routing") or {}).get(key) or {"now": {}}):
                item["unsure"] = False
            elif key in flaky_keys:  # no worse than chance: said as such, acknowledged or not
                out["flaky"].append(item)
                continue
        if key in quiet:  # someone knows, and it's no worse than they saw: quiet, not blocking
            out["acked"].append({**item, "ack": quiet[key]})
            continue
        if item.get("unsure"):  # could be chance: more attempts settle it, a red build doesn't
            out["unsure"].append(item)
            continue
        out["problems"].append(item)
    return out


def verdict(result: dict, has_baseline: bool) -> Tuple[bool, dict]:
    """(passed, the classified failures). Flaky checks and ones that already failed don't block, but
    a pass rate that dropped beyond chance across flaky checks still does."""
    c = classify(result, has_baseline)
    result["_judge_changed"] = c["judge_changed"]
    dropped = has_baseline and result["stability"]["outcome"] == "rollback" and not result.get("judge_changed")
    worse = (result.get("behavior") or result.get("behavior_suite")) if result.get("behavior_fails", True) else []
    return not c["problems"] and not dropped and not worse and not result.get("score_regressions") \
        and not gates_failed(result), c


def gates_failed(result: dict) -> List[dict]:
    """The documents' per-field gates that failed ([documents.gates]; line items by default)."""
    from assay import documents
    if "_gates" not in result:
        result["_gates"] = documents.check_gates(result.get("documents"), result.get("documents_before"),
                                                 (result.get("documents_cfg") or {}).get("gates"))
    return [g for g in result["_gates"] if g["passed"] is False]


# ---------- the report ----------

COLORS = {"green": 32, "red": 31, "yellow": 33, "dim": 2, "bold": 1}


def _paint(text: str, color: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{COLORS[color]}m{text}\033[0m"


def _pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "—"


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'s' * (k != 1)}"


def _label(field: str) -> str:
    if field.startswith("behavior."):
        m = field[len("behavior."):]
        return f"behavior ({'retrieved context' if m == 'retrieved_context' else behavior.LABELS.get(m, m).lower()})"
    return CHECK_NAMES.get(field, field)


def _groups(problems: List[dict]) -> List[Tuple[str, List[dict]]]:
    """A field failing in 3 or more cases is one item (a pipeline field that broke); the rest are
    grouped by case (an agent run failing several checks for one reason)."""
    by_field: Dict[str, List[dict]] = defaultdict(list)
    for p in problems:
        by_field[p["field"]].append(p)
    wide = {f for f, ps in by_field.items() if len(ps) >= 3 and f not in CHECK_NAMES}
    out = []
    for f in sorted(wide):
        ps = by_field[f]
        names = ", ".join(p["case_id"] for p in ps[:3]) + (", …" if len(ps) > 3 else "")
        out.append((f"{_label(f)}  " + _paint(f"{len(ps)} cases: {names}", "dim"), ps))
    by_case: Dict[str, List[dict]] = {}
    for p in problems:
        if p["field"] not in wide:
            by_case.setdefault(p["case_id"], []).append(p)
    for case, ps in by_case.items():
        tag = "  (new case)" if any(p["kind"] == "new" for p in ps) else ""
        out.append((f"{case}{tag}  " + _paint(", ".join(_label(p["field"]) for p in ps), "dim"), ps))
    return out


def _explain(ps: List[dict], fails: dict, repeat: int, routes: Optional[dict] = None) -> List[str]:
    """Each distinct reason once (at most 3), pass rates where they say something, and per model
    when more than one served."""
    lines, seen = [], set()
    for p in ps:
        r = (routes or {}).get((p["case_id"], p["field"]))
        if r:
            lines.append(_paint(routed(r) or routing_line(r), "yellow" if routed(r) else "dim"))
        if p.get("woke"):
            lines.append(_paint(f"Acknowledged by {p['ack']['by']} ({p['ack']['reason']}), but worse: {p['woke']}",
                                "yellow"))
        f = fails.get((p["case_id"], p["field"]), {})
        why = f.get("reason") or (f"{_label(p['field'])}: expected {f.get('expected')}, got {f.get('actual')}"
                                  if f.get("expected") is not None or f.get("actual") is not None else None)
        if why and why not in seen and len(seen) < 3:
            seen.add(why)
            lines.append(why)
    rates = [p for p in ps if repeat > 1 and (0 < p["rate"] < 1 or p["base_rate"] not in (None, 1.0))]
    for p in rates[:3]:
        before = f"{p['base_rate']:.0%} of attempts before, " if p["base_rate"] is not None else ""
        lines.append(_paint(f"{p['case_id']} {_label(p['field'])}: passed {before}{p['rate']:.0%} now", "dim"))
    return lines


def case_states(result: dict, c: dict) -> Dict[str, str]:
    """Per case: failed (a problem: fails the run), known (failing, but flaky or failing in the
    baseline too), or passed."""
    problems = {p["case_id"] for p in c["problems"]} | {x["case_id"] for x in result.get("score_regressions") or []}
    if result.get("behavior_fails", True):
        problems |= {b["case_id"] for b in result.get("behavior") or []}
    out = {}
    for (case, _), a in result["attempts"].items():
        state = "failed" if case in problems else "known" if not all(a) else "passed"
        prev = out.get(case, "passed")
        out[case] = state if ["passed", "known", "failed"].index(state) > ["passed", "known", "failed"].index(prev) \
            else prev
    return out


def _file(case: str) -> Optional[str]:
    return case.split("::")[0] if "::" in case else None  # a pytest test id: tests/test_x.py::test_y


def files_block(states: Dict[str, str]) -> List[str]:
    """Per test file, for pytest suites: how many of its tests passed."""
    by_file: Dict[str, List[str]] = defaultdict(list)
    for case, st in states.items():
        if _file(case):
            by_file[_file(case)].append(st)
    if not by_file:
        return []
    width = max(len(f) for f in by_file)
    out = []
    for f, sts in sorted(by_file.items()):
        mark = _paint("✗", "red") if "failed" in sts else _paint("~", "yellow") if "known" in sts else \
            _paint("✓", "green")
        out.append(f"{mark} {f:<{width}}  {sts.count('passed')}/{len(sts)}")
    return out + [""]


def _reason(f: dict) -> str:
    return f["reason"] or f"expected {f['expected']}, got {f['actual']}"


def write_junit(path: str, run_id: str, result: dict, c: dict) -> None:
    """JUnit XML, so CI shows each case: a problem is a failure, a known failure is skipped, flaky
    cases pass with a note."""
    import xml.etree.ElementTree as ET
    states, fails = case_states(result, c), result["failing"]
    for x in result["not_judged"]:  # a case with nothing judged at all still gets a line
        states.setdefault(x["case_id"], "passed")
    unjudged: Dict[str, List[str]] = defaultdict(list)
    for x in result["not_judged"]:
        unjudged[x["case_id"]].append(f"{verdicts.VERDICTS[x['verdict']]}: {_label(x['field'] or 'result')}"
                                      f"{' (' + x['evaluator'] + ')' if x['evaluator'] else ''}: {x['reason']}")
    flaky = {p["case_id"] for p in c["flaky"]}
    for p in c.get("unsure") or []:  # not settled: JUnit's "couldn't run", like a result that couldn't be judged
        unjudged[p["case_id"]].append(f"{_label(p['field'])}: passed {p['base_rate']:.0%} of attempts before, "
                                      f"{p['rate']:.0%} now: could be chance, too few attempts to tell")
    acked = {}
    for p in c.get("acked") or []:
        acked.setdefault(p["case_id"], p["ack"])
    suite = ET.Element("testsuite", name=f"assay {run_id}", tests=str(len(states)),
                       failures=str(sum(1 for v in states.values() if v == "failed")),
                       errors=str(sum(1 for k, v in states.items() if v != "failed" and k in unjudged)),
                       skipped=str(sum(1 for v in states.values() if v == "known")))
    for case, st in sorted(states.items()):
        f = _file(case)
        tc = ET.SubElement(suite, "testcase", classname=f.replace("/", ".").removesuffix(".py") if f else "assay",
                           name=case.split("::", 1)[1] if f else case)
        grouped: Dict[str, List[str]] = {}
        for k, v in fails.items():
            if k[0] == case:
                grouped.setdefault(_reason(v), []).append(_label(k[1]))
        why = [f"{', '.join(labels)}: {r}" for r, labels in grouped.items()]
        why += [f"Behavior: {ch['text']}" for b in result.get("behavior") or [] if b["case_id"] == case
                for ch in b["changes"]]
        why += [score_text(x) for x in result.get("score_regressions") or [] if x["case_id"] == case]
        if st == "failed":
            ET.SubElement(tc, "failure", message=(why or ["failed"])[0][:500]).text = "\n".join(why)
        elif case in unjudged:  # JUnit's "couldn't run", not a failure
            ET.SubElement(tc, "error", message=unjudged[case][0][:500]).text = "\n".join(unjudged[case])
        elif st == "known":
            a = acked.get(case)
            ET.SubElement(tc, "skipped", message=("flaky: passes some attempts, as before" if case in flaky else
                                                  f"acknowledged by {a['by']} until {a['until']:%Y-%m-%d} "
                                                  f"({a['reason']})" if a else "failing in the baseline too")
                          + (f": {why[0]}"[:500] if why else ""))
    totals = result.get("behavior_suite") if result.get("behavior_fails", True) else []
    if totals:  # the whole run grew: not one case's doing, so a line of its own
        tc = ET.SubElement(suite, "testcase", classname="assay", name="whole-run totals")
        ET.SubElement(tc, "failure", message=totals[0]["text"][:500]).text = "\n".join(x["text"] for x in totals)
        suite.set("tests", str(len(states) + 1))
        suite.set("failures", str(int(suite.get("failures")) + 1))
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


CATEGORIES = [  # (name, which checks): the first that matches a check's field takes it
    ("Tool selection", lambda f: f == "tool_calls" or f.startswith(("expect.must_call", "expect.must_not_call"))),
    ("Security", lambda f: f in ("safety", "pii", "injection") or f.startswith("expect.must_get_approval")),
    ("Completion", lambda f: f in ("completed", "efficiency")
     or f.startswith(("expect.must_resolve", "expect.max_steps", "expect.must_answer"))),
    ("Planning", lambda f: f in ("plan", "plan_quality")),
    ("Reasoning", lambda f: f == "consistency"),
    ("Grounding", lambda f: f in ("faithfulness", "context_relevance")),
    ("Rewordings", lambda f: f == "rewording"),
    ("Behavior", lambda f: f in behavior.LIMITS or f.startswith(("expect.max_cost", "expect.max_latency",
                                                                  "expect.max_tools", "expect.max_context"))),
    ("Output quality", lambda f: True),  # the answer, the end state, your asserts, your own fields
]
BUCKETS = [("regressed", "✗", "red"), ("new failure", "✗", "red"), ("couldn't be judged", "?", "yellow"),
           ("needs reruns", "?", "yellow"), ("judge changed", "?", "yellow"),
           ("flaky", "⚠", "yellow"), ("known failure", "·", "dim"), ("acknowledged", "·", "dim"),
           ("passed", "✓", "green")]


def _grew(metric: str, m: dict) -> str:
    d = m["now"] - m["before"]
    return f"${d:,.2f}" if metric == "cost_usd" else f"{d:,.0f}"


def summarize(result: dict, c: dict, baseline: Optional[str]) -> dict:
    """The one-glance view: each case in one bucket, cases that improved, and each category."""
    att, base = result["attempts"], result["base_attempts"]
    cases = {case for case, _ in att} | {x["case_id"] for x in result["not_judged"]}
    fails_behavior = result.get("behavior_fails", True)
    worse_behavior = {b["case_id"] for b in result.get("behavior") or []}
    regressed = {p["case_id"] for p in c["problems"] if p["kind"] in ("regression", "worse than acknowledged")} | \
        {x["case_id"] for x in result.get("score_regressions") or []} | \
        (worse_behavior if fails_behavior else set())
    new = {p["case_id"] for p in c["problems"] if p["kind"] not in ("regression", "worse than acknowledged")} - regressed
    unjudged = {x["case_id"] for x in result["not_judged"]} - regressed - new
    unsure = {p["case_id"] for p in c.get("unsure") or []} - regressed - new - unjudged
    rejudged = {p["case_id"] for p in c.get("judge_changed") or []} - regressed - new - unjudged - unsure
    flaky = {p["case_id"] for p in c["flaky"]} - regressed - new - unjudged - unsure - rejudged
    known = {p["case_id"] for p in c["still"]} - regressed - new - unjudged - unsure - rejudged - flaky
    acked = {p["case_id"] for p in c.get("acked") or []} - regressed - new - unjudged - unsure - rejudged - flaky - known
    buckets = {"regressed": regressed, "new failure": new, "couldn't be judged": unjudged, "needs reruns": unsure,
               "judge changed": rejudged,
               "flaky": flaky, "known failure": known, "acknowledged": acked}
    buckets["passed"] = cases - set().union(*buckets.values())
    by_case = defaultdict(dict)
    for (case, field), a in att.items():
        by_case[case][field] = all(a)
    improved = sorted(case for case, fs in by_case.items() if all(fs.values()) and any(
        not all(base[(case, f)]) for f in fs if (case, f) in base))
    cats = {}
    for name, match in CATEGORIES:
        mine = {case: all(ok for f, ok in fs.items() if next(n for n, m in CATEGORIES if m(f)) == name)
                for case, fs in by_case.items() if any(next(n for n, m in CATEGORIES if m(f)) == name for f in fs)}
        if name == "Behavior":  # and how each case behaved against its baseline
            for case in result.get("behavior_compared") or []:
                mine[case] = mine.get(case, True) and case not in worse_behavior
        if mine:
            cats[name] = (sum(mine.values()), len(mine))
    return {"cases": len(cases), "buckets": {k: sorted(v) for k, v in buckets.items()}, "improved": improved,
            "categories": cats}


def summary_block(s: dict) -> List[str]:
    out = []
    for name, mark, color in BUCKETS:
        n = len(s["buckets"][name])
        if n or name == "passed":
            label = name if n == 1 or name in ("passed", "flaky", "regressed", "couldn't be judged", "acknowledged",
                                               "judge changed", "needs reruns") \
                else name + "s"
            out.append(_paint(mark, color) + f" {n} {label}")
    if s["improved"]:
        out.append(_paint("↑", "green") + f" {len(s['improved'])} improved "
                   + _paint("(failing in their baseline, passing now)", "dim"))
    out.append("")
    if s["categories"]:
        width = max(len(k) for k in s["categories"])
        for name, (ok, n) in s["categories"].items():
            out.append(f"{name:<{width}}  {ok}/{n}")
        out.append("")
    return out


MARKER = "<!-- assay-regression -->"  # finds the PR comment to update (assay/github.py)
HEADLINES = {0: "No AI regression", 1: "AI regression detected",
             3: "Inconclusive: some results couldn't be judged, or need more attempts"}


def _short(case: str) -> str:
    return case.split("::", 1)[1] if "::" in case else case


# Text in a PR comment comes from the run: test names, assertion messages, field names, all
# under the PR author's control. It goes in as text, never as Markdown or HTML: no @-mentions
# (they'd notify people), no links or images, no raw HTML, nothing that ends a code span.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]~|#])")
MAX_COMMENT = 60_000  # GitHub's limit is 65,536 characters


def _md(text) -> str:
    t = re.sub(r"\s+", " ", str(text)).strip()
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MD_SPECIAL.sub(r"\\\1", t).replace("@", "@\u200b")


def _code(text, n: int = 120) -> str:
    """Text inside a code span: nothing in it can close the span."""
    t = re.sub(r"\s+", " ", str(text)).strip().replace("`", "'")
    return "`" + (t[:n] + "…" if len(t) > n else t) + "`"


# What a reviewer should read first: safety, then what the agent decided, then what it did, then cost.
RANK = ("Safety", "Prompt injection", "PII", "expect.must_get_approval", "Approval for", "Outcome", "expect.must_resolve", "Finished",
        "Tool usage", "Plan adherence", "Consistency", "Plan quality", "expect.must_call", "expect.must_not_call", "End state", "Answer", "Your asserts",
        "Retrieved context", "Fragments per query", "Retrieved tokens per query", "Prompt size", "Cost", "Context",
        "Input tokens")


def _rank(line: str) -> int:
    return next((i for i, p in enumerate(RANK) if line.startswith(p)), len(RANK))


def _tidy(line: str) -> str:
    """One change as a reviewer reads it: no boilerplate, no trailing full stop."""
    label, _, why = line.partition(": ")
    why = re.sub(r"^(Unsafe action|Wrong tool|Wrong arguments|Looped|Stopped early|Wrong answer|Wrong end state|"
                 r"Tool error, not recovered|Ignored a tool result): ", "", why)
    return f"{label}: {why.rstrip('.')}" if why else line.rstrip(".")


def _ack_parts(result: dict) -> Tuple[list, list, list, int]:
    from assay import acks
    d = result.get("acks") or {}
    quiet = sorted({(a["case"], a["check"]): a for a in (d.get("quiet") or {}).values()}.values(), key=acks.key)
    return quiet, d.get("expired") or [], d.get("spent") or [], sum(1 for a in quiet if acks.soon(a))


def score_text(x: dict) -> str:
    """Helpful: 3.0, below the 4–5 it scores when nothing is wrong (6 scores)"""
    f = x["floor"]
    return (f"{_label(x['field'])}: {x['now']:g}, below the {_floor(f)} it scores when nothing is wrong "
            f"({f['n']} scores)")


def score_lines(result: dict) -> List[str]:
    out = []
    reg, within, none_, flips = (result.get(k) or [] for k in ("score_regressions", "within_noise", "no_floor",
                                                               "coin_flips"))
    if reg:
        out.append(_paint(f"⚠ {_n(len(reg), 'check')} still passing, but scored below {'its' if len(reg) == 1 else 'their'}"
                          f" noise floor", "yellow"))
        out += [f"  {x['case_id']}  {score_text(x)}" for x in reg[:20]] + [""]
    if within:
        out.append(_paint(f"· {_n(len(within), 'score')} lower, within the noise (not failing): "
                          + ", ".join(f"{x['case_id']} {x['now']:g} in {_floor(x['floor'])}" for x in within[:5])
                          + (", …" if len(within) > 5 else ""), "dim"))
    if none_:
        out.append(_paint(f"· {_n(len(none_), 'score')} lower, with no noise floor yet (fewer than {FLOOR_MIN} "
                          f"scores): `assay test --repeat 5` measures it", "dim"))
    if flips:
        out.append(_paint(f"~ {_n(len(flips), 'coin flip')}: different answers from the same system, so a "
                          f"pass or a fail says little", "yellow"))
        for x in flips[:10]:
            sc = f", scores {x['scores'][0]:g}–{x['scores'][1]:g}" if x["scores"] and x["scores"][0] != x["scores"][1] else ""
            out.append(_paint(f"  {x['case_id']}  {_label(x['field'])}: passed {x['passed']}/{x['n']}{sc}", "dim"))
        out.append(_paint("  Tighten its rubric or its expected output, or check it deterministically if it can be.",
                          "dim"))
    return out + ([""] if (within or none_) and not flips else [""] if flips else [])


def judge_changed_lines(items: List[dict]) -> List[str]:
    """Checks judged by another model or prompt than their baseline: not compared, and why."""
    by: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for p in items:
        by[(p["before"], p["now"])].append(p)
    out = [_paint(f"? {_n(len(items), 'failing check')} judged by a different judge than their baseline: not "
                  f"compared, since a drop could be the judge, not the AI", "yellow")]
    for (was, now), ps in by.items():
        names = ", ".join(f"{p['case_id']} {_label(p['field'])}" for p in ps[:3]) + (", …" if len(ps) > 3 else "")
        out.append(_paint(f"  {was} → {now}: {names}", "dim"))
    out.append(_paint("  They're the baseline from this run on. `assay calibrate` with the new judge checks it "
                      "agrees with people first.", "dim"))
    return out + [""]


def ack_lines(result: dict) -> List[str]:
    """The terminal's lines on acknowledgements: the quiet ones in one line, the ended ones each."""
    quiet, expired, spent, soon = _ack_parts(result)
    out = []
    if quiet:
        out.append(_paint(f"· {_n(len(quiet), 'check')} acknowledged ({_n(len({a['case'] for a in quiet}), 'case')}), "
                          f"quiet until worse"
                          + (f" ({soon} expire{'s' * (soon == 1)} within 3 days)" if soon else "")
                          + ": `assay acks` lists them", "dim"))
    for a in expired:
        out.append(_paint(f"⚠ Acknowledgement ended: {a['case']} {_label(a['check'])}, {a['why']}. It's reported as "
                          f"it is again.", "yellow"))
    for a in spent:
        out.append(_paint(f"· {a['case']} {_label(a['check'])} {a['why']}: its acknowledgement no longer applies "
                          f"(`assay acks --prune`)", "dim"))
    return out + ([""] if out else [])


def ack_markdown(result: dict) -> List[str]:
    quiet, expired, spent, soon = _ack_parts(result)
    out = []
    for a in expired:
        out.append(f"- Acknowledgement ended: {_code(_short(a['case']))} {_md(_label(a['check']))}, {_md(a['why'])}")
    if out:
        out.append("")
    if quiet:
        out += [f"<details><summary>{_n(len(quiet), 'check')} acknowledged, quiet until worse"
                f"{f' ({soon} expiring within 3 days)' if soon else ''}</summary>", ""]
        out += [f"- {_code(_short(a['case']))} {_md(_label(a['check']))}: {_md(a['reason'])} (by {_md(a['by'])}, "
                f"until {a['until']:%Y-%m-%d})" for a in quiet[:30]]
        out += ["", "</details>", ""]
    if spent:
        out += [f"<sub>{_n(len(spent), 'acknowledgement')} no longer apply: the check passed since</sub>", ""]
    return out


def summary_markdown(run_id: str, result: dict, code: int, against: Optional[str]) -> str:
    """The run for a PR comment or a CI job summary: the verdict, the counts, and each change in a line."""
    s = result["summary"]
    b = s["buckets"]
    counts = [f"**{_n(s['cases'], 'case')}**", f"{len(b['passed'])} passed"]
    for name in ("regressed", "new failure", "needs reruns", "flaky", "couldn't be judged", "known failure",
                 "acknowledged"):
        if b.get(name):
            counts.append(f"{len(b[name])} {name}")
    jc = result.get("_judge_changed") or []
    if b.get("judge changed"):
        counts.append(f"{len(b['judge changed'])} judge changed")
    if s["improved"]:
        counts.append(f"{len(s['improved'])} improved")
    paths = result.get("flows") or {}  # cases whose flow differs from their baseline's (assay/diff.py)
    still = [c for c in paths if c in b["passed"] and c not in s["improved"]]
    if still:
        counts.append(f"{len(still)} changed, still passing")
    changes = []
    shown = set(b["regressed"]) | set(b["new failure"])
    reasons: Dict[str, List[str]] = defaultdict(list)
    for (case, field), f in result["failing"].items():
        if case in shown:
            line = f"{_label(field)}: {_reason(f).splitlines()[0][:160]}"
            if line not in reasons[case]:
                reasons[case].append(line)
    for x in result.get("behavior") or []:
        reasons[x["case_id"]] += [ch["text"] for ch in x["changes"]]
    for x in result.get("score_regressions") or []:
        reasons[x["case_id"]].insert(0, score_text(x))
    for key, r in (result.get("routing") or {}).items():
        if key[0] in reasons and routed(r):
            reasons[key[0]].append(f"{_label(key[1])}: {routed(r)}")
    for (case, check), (a, why) in ((result.get("acks") or {}).get("woke") or {}).items():
        if not check.startswith("behavior."):
            reasons[case].insert(0, f"Worse than acknowledged ({_label(check)}, by {a['by']}): {why}")
    for case in sorted(reasons, key=lambda c: (min(_rank(x) for x in reasons[c]), c)):
        lines = sorted(dict.fromkeys(_tidy(x) for x in reasons[case]), key=_rank)
        changes.append(f"- {_code(_short(case))} → " + "; ".join(_md(x) for x in lines[:2])
                       + (f" (+{len(lines) - 2} more)" if len(lines) > 2 else ""))
        for x in ((result.get("setup") or {}).get("cases") or {}).get(case, []):
            changes.append(f"  - Changed: {_md(change_text(x))}")
        if case in paths:
            was, now = paths[case]
            changes += [f"  - Expected: {_code(' → '.join(was) or '(no calls)', 300)}",
                        f"  - Actual: {_code(' → '.join(now) or '(no calls)', 300)}"]
    for x in result.get("behavior_suite") or []:
        changes.append(f"- {_md(x['text'])}" + (f" (most: {', '.join(_code(_short(m['case_id'])) for m in x['most'])})"
                                                if x["most"] else ""))
    rejudged = {k[1] for k in result.get("judge_changed") or {}}
    for f in result["fields"]:  # your own fields whose accuracy dropped, e.g. extraction
        if f["field"] not in CHECK_NAMES and not f["field"].startswith("expect.") and f["base_total"] and \
                f["field"] not in rejudged and \
                f["passed"] / f["total"] < f["base_passed"] / f["base_total"]:
            changes.append(f"- {_code(f['label'])} accuracy {_pct(f['base_passed'], f['base_total'])} → "
                           f"{_pct(f['passed'], f['total'])}")
    policy = result.get("policy")
    head = "Checks weakened: this PR loosens the checks that judge it" if policy and policy["weakened"] else \
        HEADLINES.get(code, "AI regression detected")
    out = [MARKER, f"## {head}", "", " · ".join(counts), ""]
    if policy and policy["changes"]:
        out += [f"**{'Policy changes (not applied: add the `assay-policy-change` label to accept them)' if policy['weakened'] else 'Policy changes (accepted)' if policy['accepted'] else 'Policy changes (applied)'}**",
                "", *(f"- {'loosens' if c['weakens'] else 'tightens'}: {_md(c['text'])}" for c in policy["changes"]), ""]
    if changes:  # a change is its "- " line and the Expected/Actual lines under it
        starts = [i for i, x in enumerate(changes) if x.startswith("- ")]
        cut = starts[30] if len(starts) > 30 else len(changes)
        out += [f"**{_n(len(starts), 'change')} in behavior**", "", *changes[:cut], ""]
        if len(starts) > 30:
            out += [f"…and {len(starts) - 30} more", ""]
    if s["categories"]:
        out += ["| Category | Passed |", "|---|---|"] + [f"| {_md(k)} | {ok}/{n} |" for k, (ok, n) in s["categories"].items()]
        out.append("")
    out += ack_markdown(result)
    for x in result.get("surface") or []:
        out += [f"> {_md(x['text'])}", ""]
    if result.get("documents"):
        from assay import documents
        out += [f"**Documents:** {_md(documents.markdown(result['documents'], result.get('documents_before'), result.get('documents_cfg')))}", ""]
    if result.get("kinds"):
        out += [f"**Failures by kind:** {_md(kinds_text(result['kinds']))}", ""]
    if result.get("fixed_context"):
        out += [f"**Fixed context per call:** {_md(fixed_context_text(result['fixed_context']))}", ""]
    setup = result.get("setup") or {}
    if setup.get("everywhere"):
        out += ["**Changed in every case:** " + "; ".join(_md(change_text(x)) for x in setup["everywhere"]), ""]
    for line in blame(setup, list(b.get("regressed") or [])):
        out += [f"**{_md(line)}**", ""]
    if result.get("unchanged"):
        u = result["unchanged"]
        out += [f"**Nothing on your side changed** since the baseline (commit `{u['commit']}`, no uncommitted "
                f"changes, the same config and prompts): the model underneath changed, or a service a tool calls "
                f"did.", ""]
    tr = result.get("trust") or {}
    if tr:
        out += ["**Judges**", ""] + [f"- {_md(_label(f))}: {_md(trust_text(f, x))}" for f, x in tr.items()] + [""]
    flips = result.get("coin_flips") or []
    if flips:
        out += [f"<details><summary>{_n(len(flips), 'coin flip')}: different answers from the same system</summary>",
                "", *(f"- {_code(_short(x['case_id']))} {_md(_label(x['field']))}: passed {x['passed']}/{x['n']}"
                      for x in flips[:20]), "", "Tighten the rubric or the expected output, or check it "
                "deterministically.", "", "</details>", ""]
    if jc:
        pairs = sorted({(p["before"], p["now"]) for p in jc})
        out += [f"**Judge changed** ({', '.join(f'{_md(a)} → {_md(b)}' for a, b in pairs)}): "
                f"{_n(len(jc), 'failing check')} not compared with their baseline, since a drop could be the judge. "
                f"Run `assay calibrate` with the new judge.", ""]
    models = result.get("models") or {}
    if models:
        out += ["| Model | Cases passing |", "|---|---|"] + [f"| {_md(m)} | {ok}/{n} |" for m, (ok, n) in models.items()]
        out.append("")
    unsure = result.get("_unsure") or []
    if unsure:
        out += [f"<details><summary>{_n(len(unsure), 'check')} could be worse, or chance: too few attempts to "
                f"tell</summary>", ""]
        out += [f"- {_code(_short(p['case_id']))} {_md(_label(p['field']))}: passed {p['base_rate']:.0%} of attempts "
                f"before, {p['rate']:.0%} now" for p in unsure[:20]]
        out += ["", "More attempts settle it: `assay test --repeat 10`.", "", "</details>", ""]
    nj = result["not_judged"]
    if nj:
        out += [f"<details><summary>{_n(len(nj), 'result')} couldn't be judged</summary>", ""]
        out += [f"- {_code(_short(x['case_id']))} {_md(_label(x['field'] or 'result'))}: "
                f"{verdicts.VERDICTS[x['verdict']]}, {_md(x['reason'])[:300]}" for x in nj[:20]]
        out += ["", "</details>", ""]
    out.append(f"<sub>{_md(against or 'no baseline yet')} · run {_code(run_id)} · "
               f"[Assay](https://github.com/tap222/assay-evals)</sub>")
    md = "\n".join(out) + "\n"
    if len(md) > MAX_COMMENT:  # cut whole lines, and say so
        md = md[:md.rfind("\n", 0, MAX_COMMENT - 200)] + "\n\n…the rest is in the job's summary.\n"
    return md


def report(run_id: str, baseline: Optional[str], result: dict, repeat: int, codes: List[int],
           against: Optional[str] = None) -> Tuple[str, bool]:
    passed, c = verdict(result, bool(baseline))
    result["_unsure"] = c["unsure"]
    st, fails, fields = result["stability"], result["failing"], result["fields"]
    cases = len({case for case, _ in result["attempts"]})
    out = [_paint("Assay test", "bold") + f"  {run_id}", "─" * 44]
    against = (against or f"compared with the baseline, {baseline}") if baseline else \
        "no baseline yet: every failing check counts"
    out += [f"{_n(cases, 'case')} · {_n(repeat, 'attempt')} each · {against}", ""]
    result["summary"] = summarize(result, c, baseline)
    out += summary_block(result["summary"])
    everywhere = (result.get("setup") or {}).get("everywhere") or []
    if everywhere:
        out += [_paint("Changed in every case", "bold")] + [f"  {change_text(x)}" for x in everywhere] + [""]
    if result.get("models"):  # several models served: how each did
        w = max(len(m) for m in result["models"])
        out += [_paint("By model", "bold")] + [f"  {m:<{w}}  {ok}/{n} cases" for m, (ok, n) in result["models"].items()] + [""]
    out += files_block(case_states(result, c))
    trust_block = trust_lines(result)
    width = max((len(f["label"]) for f in fields), default=0)
    out.append(_paint("Checks", "bold"))
    for f in fields:
        mark = _paint("✓", "green") if f["passed"] == f["total"] else _paint("✗", "red")
        line = f"{mark} {f['label']:<{width}}  {f['passed']}/{f['total']}"
        if f["base_total"] and _pct(f["base_passed"], f["base_total"]) != _pct(f["passed"], f["total"]):
            line += _paint(f"   {_pct(f['base_passed'], f['base_total'])} → {_pct(f['passed'], f['total'])}", "dim")
        out.append(line)
    out.append("")
    if result.get("documents"):
        from assay import documents
        out += documents.lines(result["documents"], result.get("documents_before"), result.get("documents_cfg")) + [""]
    out += trust_block
    if result.get("fixed_context"):
        out += [_paint("Fixed context per call", "bold"), f"  {fixed_context_text(result['fixed_context'])}", ""]
    if result.get("kinds"):
        out += [_paint("Failures by kind", "bold"), f"  {kinds_text(result['kinds'])}", ""]
    problems = c["problems"]
    if problems:
        what = "regressed" if baseline and all(p["kind"] == "regression" for p in problems) else "failing"
        n_cases = len({p["case_id"] for p in problems})
        out.append(_paint(f"⚠ {_n(n_cases, 'case')} {what} ({_n(len(problems), 'check')})", "yellow"))
        setup = result.get("setup") or {}
        for line in blame(setup, sorted({p["case_id"] for p in problems})):
            out.append(_paint(f"  {line}", "yellow"))
        for i, (title, ps) in enumerate(_groups(problems)[:20], 1):
            out.append(f"\n{i}. {title}")
            out += [f"   {line}" for line in _explain(ps, fails, repeat, result.get("routing"))]
            mine = {p["case_id"] for p in ps}
            if len(mine) == 1 and (setup.get("cases") or {}).get(next(iter(mine))):
                out.append(_paint("   Changed around it:", "dim"))
                out += [_paint(f"     {change_text(x)}", "dim") for x in setup["cases"][next(iter(mine))]]
        if len(_groups(problems)) > 20:
            out.append(f"\n… and {len(_groups(problems)) - 20} more")
        out.append("")
    worse = result.get("behavior") or []
    if worse:
        fails_ = result.get("behavior_fails", True)
        out.append(_paint(f"{'⚠' if fails_ else '~'} {_n(len(worse), 'case')} behaved worse than their baseline"
                          + ("" if fails_ else " (not failing: [behavior] fail = false)"), "yellow"))
        for b in worse[:20]:
            out.append(f"  {b['case_id']}")
            out += [_paint(f"    {ch['text']}", "dim") for ch in b["changes"]]
        out.append("")
    suite = result.get("behavior_suite") or []
    if suite:
        fails_ = result.get("behavior_fails", True)
        out.append(_paint(f"{'⚠' if fails_ else '~'} The whole run grew against its baseline"
                          + ("" if fails_ else " (not failing: [behavior] fail = false)"), "yellow"))
        for x in suite:
            out.append(f"  {x['text']}")
            if x["most"]:
                out.append(_paint("    most: " + ", ".join(f"{m['case_id']} (+{_grew(x['metric'], m)})"
                                                         for m in x["most"]), "dim"))
        out.append("")
    nj = result["not_judged"]
    if nj:
        out.append(_paint(f"? {_n(len(nj), 'result')} couldn't be judged (not counted)", "yellow"))
        for v in verdicts.NOT_JUDGED:
            items = [x for x in nj if x["verdict"] == v]
            if not items:
                continue
            out.append(f"  {verdicts.VERDICTS[v].capitalize()}: {len(items)}")
            for x in items[:5]:
                out.append(f"    {x['case_id']}  {_label(x['field'] or 'result')}" +
                           (f"  {x['evaluator']}" if x["evaluator"] else "") + _paint(f"  {x['reason']}", "dim") +
                           (_paint(f" after {x['tries']} tries", "dim") if x.get("tries", 1) > 1 else ""))
                if x.get("raw_output") and v == "INVALID":  # what it said instead of a verdict
                    out.append(_paint(f"      it returned: {x['raw_output'][:160]!r}", "dim"))
            if len(items) > 5:
                out.append(f"    … and {len(items) - 5} more")
        out.append("")
    if c["unsure"]:
        out.append(_paint(f"? {_n(len(c['unsure']), 'check')} could be worse, or chance: too few attempts to tell "
                          "(not a regression yet)", "yellow"))
        for p in c["unsure"][:10]:
            out.append(_paint(f"  {p['case_id']}  {_label(p['field'])}  {p['base_rate']:.0%} → {p['rate']:.0%}", "dim"))
        out.append(_paint("  More attempts settle it: `assay test --repeat 10`.", "dim"))
        out.append("")
    if c["flaky"]:
        out.append(_paint(f"~ {_n(len(c['flaky']), 'flaky check')}: passing some attempts, no worse than chance; "
                          "not blocking", "yellow"))
        for p in c["flaky"][:10]:
            out.append(_paint(f"  {p['case_id']}  {_label(p['field'])}  "
                              f"{p['base_rate']:.0%} → {p['rate']:.0%}", "dim"))
            why = routed((result.get("routing") or {}).get((p["case_id"], p["field"])) or {"now": {}})
            if why:
                out.append(_paint(f"    {why}", "yellow"))
        out.append("")
    if c.get("judge_changed"):
        out += judge_changed_lines(c["judge_changed"])
    out += score_lines(result)
    for x in result.get("surface") or []:
        out += [_paint(f"~ {x['text']}", "yellow"), ""]
    out += ack_lines(result)
    if c["still"]:
        out.append(_paint(f"{_n(len(c['still']), 'check')} also failed in the baseline, so they don't count "
                          "against this change.", "dim"))
    if baseline and not problems and not passed:
        out.append(st["reasons"][0])
    if problems and baseline and repeat == 1:
        out.append(_paint("One attempt per case. If a case can vary between runs, `assay test --repeat 3` "
                          "tells flaky from broken.", "dim"))
    gf = gates_failed(result)
    if gf:
        out.append(_paint(f"{_n(len(gf), 'document gate')} failed: {gf[0]['why']}"
                          + (f", and {len(gf) - 1} more above" if len(gf) > 1 else "") + ".", "red"))
    if not baseline and not passed:
        out.append(_paint("If these failures are known, make this run the baseline with `assay accept`: "
                          "later runs then fail only on what gets worse.", "dim"))
    if TIMED_OUT in codes and not result.get("left_open"):
        out.append(_paint(f"Your command timed out ({codes.count(TIMED_OUT)} of {len(codes)} attempts) after every "
                          "case had finished, and was stopped: it hung on the way out (a thread or event loop "
                          "that never stopped?). Nothing was lost.", "yellow"))
    elif TIMED_OUT in codes:
        out.append(_paint(f"Your command timed out ({codes.count(TIMED_OUT)} of {len(codes)} attempts) and was "
                          "stopped; what it recorded is above.", "yellow"))
    if any(x and x != TIMED_OUT for x in codes):
        out.append(_paint(f"Your command exited with {', '.join(str(x) for x in codes if x and x != TIMED_OUT)}.",
                          "yellow"))
    if passed and nj:
        out.append(_paint(f"Inconclusive: nothing got worse, but {_n(len(nj), 'result')} couldn't be judged. "
                          "Fix or rerun the evaluation; the baseline stays as it was.", "yellow"))
    elif passed and c["unsure"]:
        out.append(_paint(f"Inconclusive: nothing is proven worse, but {_n(len(c['unsure']), 'check')} could be. "
                          "Rerun with more attempts; the baseline stays as it was.", "yellow"))
    else:
        kept = len(dropped(result)) if passed and baseline else 0
        out.append(_paint("Passed." if passed else "Failed.", "green" if passed else "red") +
                   (" Its cases' results are now their baseline" + (
                       f", except {_n(kept, 'case')} whose pass rate dropped within chance: "
                       f"{'it keeps its' if kept == 1 else 'they keep their'} old one." if kept else ".")
                    if passed else ""))
    return "\n".join(out), passed


SDK_MIN = (0, 2, 0)


def sdk_problem() -> Optional[str]:
    """Why the SDK here can't record for `assay test`, or None."""
    try:
        import assay_sdk
    except ImportError:
        return "The Assay SDK isn't installed here: pip install assay-evals"
    version = tuple(int(x) for x in re.findall(r"\d+", assay_sdk.__version__)[:3])
    if version < SDK_MIN:
        return (f"assay test needs assay-evals {'.'.join(map(str, SDK_MIN))} or newer (this is "
                f"{assay_sdk.__version__}): pip install -U assay-evals")
    return None


def new_run_id() -> str:
    return datetime.now().strftime("t-%Y%m%d-%H%M%S-%f")[:-3]


def test(root: Path, command: Optional[str], repeat: Optional[int], baseline: Optional[str],
         send: Optional[dict] = None, junit: Optional[str] = None, timeout: Optional[float] = None,
         failed: bool = False, judge: bool = False) -> int:
    """`assay test`. Prints the report; returns the exit code."""
    try:
        cfg = load_config(root)
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 2
    if judge:
        cfg["judge"] = {**cfg["judge"], "enabled": True}
    problem = sdk_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    command = command or cfg["command"]
    if not command:
        print(f"Give a command to run: set command under [test] in {CONFIG}, or `assay test -- <command>`.",
              file=sys.stderr)
        return 2
    repeat = repeat or cfg["repeat"]
    home = ensure_home(root)
    (home / "runs").mkdir(exist_ok=True)
    run_id = new_run_id()
    events = home / "runs" / f"{run_id}.jsonl"
    timeout = timeout or (float(os.environ["ASSAY_TIMEOUT"]) if os.environ.get("ASSAY_TIMEOUT") else None) \
        or cfg["timeout"]
    if failed:
        rerun = _state(home).get("rerun")
        if rerun is None:
            print("Nothing to rerun yet: run `assay test` first.", file=sys.stderr)
            return 2
        if not rerun:
            print("Nothing to rerun: every case passed last time.")
            return 0
        if "pytest" not in command:
            print("--failed reruns through the pytest plugin; this command isn't pytest, so it runs whole.",
                  file=sys.stderr)
    codes = run_command(command, events, run_id, repeat, timeout, failed, cfg.get("prices"))
    if not events.exists():
        print(f"\n`{command}` recorded nothing. Does it call assay.init() and record runs with "
              "assay.run(..., test=\"<case>\")?", file=sys.stderr)
        return 2
    why = f"the command timed out after {timeout:g}s" if TIMED_OUT in codes else None
    code, text = finish(root, cfg, run_id, repeat, codes, baseline, junit, why, subset=failed)
    print("\n" + text, file=sys.stderr if code == 2 else sys.stdout)
    if send is not None and code != 2:
        print()
        sent = upload(root, run_id, **send)
        code = code or sent  # a failed upload fails a run that passed; a failing run stays 1
    return code


def finish(root: Path, cfg: dict, run_id: str, repeat: int, codes: List[int], baseline: Optional[str],
           junit: Optional[str] = None, abandoned_why: Optional[str] = None, subset: bool = False) -> Tuple[int, str]:
    """Load a recorded test run, check it, compare it with the baseline, and move the baseline on
    if it passed. (exit code, report): 0 passed, 1 failed, 2 nothing to check, 3 inconclusive.
    Shared by `assay test` and `pytest --assay`. `subset`: a rerun of what failed, so the cases it
    leaves out weren't dropped."""
    subset = subset or os.environ.get("ASSAY_RERUN") == "failed"
    home = ensure_home(root)
    events = home / "runs" / f"{run_id}.jsonl"
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = _migrate(engine, home, _state(home))
    explicit = baseline not in (None, "none")
    if baseline == "none":
        baseline = None
    elif baseline is None:
        baseline = BASELINE if state.get("baseline_cases") else None
    sync_contracts(engine, cfg["contracts"])
    _, bad = load_file(engine, str(events), TENANT)
    if bad:
        return 2, "\n  ".join([f"{len(bad)} bad line(s) in {events}:", *bad[:20]])
    judged = None
    if (cfg.get("judge") or {}).get("enabled"):  # plan quality and consistency, by an LLM (assay/judge.py)
        from assay import judge
        try:
            rt = judge.runtime(cfg["judge"])
        except ValueError as exc:  # prices that don't read
            return 2, f"{CONFIG}, [judge]: {exc}"
        judged = judge.judge_run(engine, TENANT, run_id, cfg["judge"]["model"], redact=cfg["judge"]["redact"],
                                 provider=cfg["judge"].get("provider", "anthropic"), rt=rt)
    from assay import rewordings  # each rewording against its original, before anything is compared
    rewordings.check(engine, TENANT, run_id)
    result = evaluate(engine, run_id, baseline, cfg["tolerance"], cfg["pii"], cfg["behavior"], abandoned_why, cfg.get("acks"))
    if result is None:
        return 2, ("Nothing to check: record runs with assay.run(..., test=\"<case>\"), and say what each case "
                   "should do with assay.expect(), or send results with assay.check().")
    ran = {case for case, _ in result["attempts"]}
    known = {c: r for c, r in (state.get("baseline_cases") or {}).items() if c in ran}
    if baseline == BASELINE and not known:
        baseline = None  # none of these cases has a baseline yet
        result = evaluate(engine, run_id, None, cfg["tolerance"], cfg["pii"], cfg["behavior"], abandoned_why, cfg.get("acks"))
    against = None if baseline is None else f"compared with the baseline, {baseline}" if explicit else \
        (f"compared with each case's last passing run ({len(known)} of {len(ran)} cases have one, from "
         f"{_n(len(set(known.values())), 'run')})")
    result["behavior_fails"] = cfg["behavior"]["fail"]
    result["documents_cfg"] = cfg.get("documents")
    text, passed = report(run_id, baseline, result, repeat, codes, against)
    if judged is not None:
        text += _paint(f"\nJudged {_n(judged['judged'], 'run')} with {cfg['judge']['model']} (plan quality, "
                       f"consistency)" + (f"; {judged['errors']} result(s) couldn't be judged" if judged["errors"]
                                          else "") + ".", "dim")
        text += _paint("\n" + judge_cost(judged), "dim")
    if junit:
        write_junit(junit, run_id, result, verdict(result, bool(baseline))[1])
    state["last"] = run_id
    revs = state.setdefault("revisions", {})
    revs[run_id] = revision(root, engine, run_id)
    for old in list(revs)[:-200]:
        revs.pop(old)
    same = None if passed else unchanged(state, run_id, result["summary"]["buckets"]["regressed"], known)
    if same:  # zero changes on your side: the model underneath (or the service behind a tool) changed
        result["unchanged"] = same
        whose = "this case's" if same["cases"] == 1 else "these cases'"
        text += "\n" + _paint(f"Nothing on your side changed since {whose} "
                              f"baseline: the same commit ({same['commit']}), no uncommitted changes, the same "
                              f"{CONFIG} and the same prompt versions. The model underneath changed, or a service a "
                              f"tool calls did.", "yellow")
    inconclusive = passed and bool(result["not_judged"] or result["summary"]["buckets"]["needs reruns"])
    baseline_before = dict(state.get("baseline_cases") or {})
    if passed and not inconclusive:
        # A pass rate that dropped within chance passes, but isn't the new bar: otherwise a few such runs
        # in a row would walk a case from 8/8 down to 4/8 without ever failing.
        state["baseline_cases"] = {**(state.get("baseline_cases") or {}),
                                   **{c: run_id for c in promote(engine, run_id, keep=dropped(result))}}
    code = 1 if not passed else 3 if inconclusive else 0
    policy = cfg.get("policy")
    # The suite as it last ran whole outside a pull request: on the default branch, that's what the cache
    # hands a PR. A case in it that didn't run now was deleted, skipped or filtered out of the command, and
    # when nobody reads the code, nobody else notices: the checks passed because the check that fails is gone.
    ran_all = ran | {x["case_id"] for x in result["not_judged"]}
    missing = sorted(set(state["suite"]) - ran_all) if state.get("suite") is not None and not subset else []
    result["missing_cases"] = missing
    if missing:
        names = ", ".join(_short(c) for c in missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        if policy and policy.get("trusted"):  # a PR: dropping a test loosens the checks like removing a contract
            policy = {**policy, "changes": [*policy["changes"], {
                "text": f"stops running {_n(len(missing), 'test case')} the base branch runs: {names}", "weakens": True}]}
            policy["weakened"] = policy["weakened"] or not policy["accepted"]
        else:
            text += "\n" + _paint(f"{_n(len(missing), 'case')} that ran last time didn't run now: {names}.", "dim")
    if not policy and not subset and abandoned_why is None:
        state["suite"] = sorted(ran_all)
    if policy_lines(policy):
        text += "\n\n" + _paint("\n".join(policy_lines(policy)), "yellow" if policy["weakened"] else "dim")
    if policy and policy["weakened"]:  # loosening the gate needs a person to say so
        code = 1
        if passed:
            state["baseline_cases"] = baseline_before
    result["policy"] = policy
    if baseline:  # what each case did, next to what its baseline did
        from assay import diff
        before, now = diff.flows(engine, TENANT, baseline), diff.flows(engine, TENANT, run_id)
        result["flows"] = {c: (before[c]["flow"], now[c]["flow"]) for c in before.keys() & now.keys()
                           if before[c]["flow"] != now[c]["flow"]}
        moved = len(result["flows"])
        if moved:
            text += "\n" + _paint(f"{_n(moved, 'case')} took a different path than {'its' if moved == 1 else 'their'} "
                                   f"baseline: `assay diff` shows what changed.", "dim")
    # What's left to rerun (`--failed`): everything that didn't simply pass.
    state["rerun"] = sorted(set().union(*(v for k, v in result["summary"]["buckets"].items() if k != "passed")))
    _save_state(home, state)
    md = summary_markdown(run_id, result, code, against)
    (home / "summary.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):  # GitHub Actions: the job's summary page
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    return code, text


# ---------- what changed on your side: the code, the config, the prompts ----------

def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def revision(root: Path, engine, run_id: str) -> dict:
    """What this run ran: the commit, a digest of uncommitted changes, of assay.toml, and of the prompt
    and model versions its results record. Two runs with the same revision ran the same code."""
    import hashlib
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()[:12]
    commit = os.environ.get("GITHUB_SHA") or (_git(root, "rev-parse", "HEAD") or "").strip() or None
    keep = ["--", ".", ":(exclude).assay", f":(exclude){CONFIG[:-5]}.acks.toml"]  # acknowledgements change no code
    changes = (_git(root, "diff", "HEAD", *keep) or "") + (_git(root, "status", "--porcelain", *keep) or "") \
        if commit else ""
    cfg = (root / CONFIG).read_text() if (root / CONFIG).exists() else ""
    rows = _rows(engine, run_id)
    lineage = {json.dumps(r.lineage, sort_keys=True) for r in rows if r.lineage}
    for s in _setups(engine, TENANT, [r.document_id for r in rows]).values():  # prompt@version and model per call
        lineage |= {f"prompt:{p}" for p in s["prompts"]} | {f"model:{m}" for m in s["models"]}
    lineage = sorted(lineage)
    return {"commit": commit, "changes": h(changes) if changes.strip() else None, "config": h(cfg),
            "prompts": h("|".join(lineage)) if lineage else None}


def unchanged(state: dict, run_id: str, cases: List[str], bases: Dict[str, str]) -> Optional[dict]:
    """The regressed cases' baselines ran exactly this run's revision: nothing on your side changed."""
    revs = state.get("revisions") or {}
    now = revs.get(run_id) or {}
    runs = {bases.get(c) for c in cases}
    if not now.get("commit") or not cases or None in runs or any(revs.get(r) != now for r in runs):
        return None
    return {"commit": now["commit"][:7], "cases": len(cases)}


def _migrate(engine, home: Path, state: dict) -> dict:
    """A whole-run baseline (before per-case baselines) becomes each of its cases' baseline."""
    if state.get("baseline") and "baseline_cases" not in state:
        run = state.pop("baseline")
        state["baseline_cases"] = {c: run for c in promote(engine, run)}
        _save_state(home, state)
    return state


def _open(root: Path):
    home = root / HOME
    if not (home / "assay.db").exists():
        return None, None
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    return engine, _migrate(engine, home, _state(home))


def _failing(engine, run_id: str, case: Optional[str] = None) -> Dict[str, List[str]]:
    """{case: the checks it fails in the run}, behavior changes as behavior.<metric>."""
    out: Dict[str, List[str]] = defaultdict(list)
    for r in _rows(engine, run_id):
        if r.status == "fail" and (case is None or r.case_id == case) and (r.field or "result") not in out[r.case_id]:
            out[r.case_id].append(r.field or "result")
    return {c: sorted(fs) for c, fs in out.items() if fs}


def _behavior_failing(engine, run_id: str, state: dict) -> Dict[str, List[str]]:
    base = BASELINE if state.get("baseline_cases") else None
    if not base:
        return {}
    ran = {r.case_id for r in _rows(engine, run_id)}
    out = _behavior_changes(engine, run_id, base, ran, None)
    return {b["case_id"]: [f"behavior.{ch['metric']}" for ch in b["changes"] if ch["metric"] in behavior.NUMBERS
                           or ch["metric"] == "retrieved_context"] for b in out["behavior"]}


def _make_acks(engine, run_id: str, wanted: Dict[str, List[str]], reason: str, for_: str, by: Optional[str]):
    from assay import acks
    span = acks.duration(for_)
    at = acks._now()
    made = []
    for case, checks in wanted.items():
        for check in checks:
            snap = acks.snapshot(engine, TENANT, run_id, case, check)
            made.append({"case": case, "check": check, "reason": reason, "by": by or acks.who(), "at": at,
                         "until": at + span, **snap})
    return made


def _merge_acks(root: Path, made: List[dict]) -> Path:
    from assay import acks
    config = root / CONFIG
    keep = [a for a in acks.load(config) if acks.key(a) not in {acks.key(m) for m in made}]
    return acks.save(config, keep + made)


def _describe_ack(a: dict) -> str:
    parts = []
    band = a.get("band")
    if band:
        parts.append(f"its score stays within {band['min']:g}–{band['max']:g}"
                     + (f" (from {band['n']} scores)" if band["n"] > 1 else " (one score so far)"))
    parts += [f"{behavior.LABELS[k].lower()} stays at or under {behavior.NUMBERS[k][2](v)}"
              for k, v in (a.get("values") or {}).items()]
    if a.get("classes") and not a.get("values"):
        parts.append(f"it fails the way it does now ({', '.join(a['classes'])})")
    return f"{a['case']} {_label(a['check'])}, until {a['until']:%Y-%m-%d}: quiet while {' and '.join(parts)}"


def ack(root: Path, case: str, checks: List[str], reason: str, for_: str, by: Optional[str],
        run_id: Optional[str]) -> int:
    """`assay ack CASE [CHECK...]`: acknowledge failing checks, with the band they show now."""
    from assay import acks
    engine, state = _open(root)
    if engine is None or not (run_id or state.get("last")):
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    run_id = run_id or state["last"]
    fails = _failing(engine, run_id)
    for c, ms in _behavior_failing(engine, run_id, state).items():
        fails.setdefault(c, []).extend(m for m in ms if m not in fails.get(c, []))
    known = sorted({r.case_id for r in _rows(engine, run_id)})
    match = [c for c in known if c == case] or [c for c in known if c.endswith(case) or f"::{case}" in c]
    if len(match) != 1:
        print(f"No case {case!r} in {run_id}." if not match else
              f"{case!r} matches {len(match)} cases: {', '.join(match[:5])}. Give more of its id.", file=sys.stderr)
        return 2
    case = match[0]
    checks = checks or fails.get(case) or []
    if not checks:
        print(f"{case} passes in {run_id}: there's nothing to acknowledge.", file=sys.stderr)
        return 2
    try:
        made = _make_acks(engine, run_id, {case: checks}, reason, for_, by)
        path = _merge_acks(root, made)
    except acks.AckError as exc:
        print(exc, file=sys.stderr)
        return 2
    for a in made:
        print(f"Acknowledged {_describe_ack(a)}.")
    print(f"Written to {path.name}: commit it, so CI and reviewers see it.")
    return 0


def list_acks(root: Path, prune: bool = False) -> int:
    """`assay acks`: each acknowledgement and where it stands in the latest run."""
    from assay import acks
    try:
        items = acks.load(root / CONFIG)
    except acks.AckError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not items:
        print("Nothing is acknowledged. `assay ack CASE --reason ...` acknowledges a failing check.")
        return 0
    engine, state = _open(root)
    run_id = (state or {}).get("last")
    metrics = case_behavior(engine, run_id) if engine is not None and run_id else {}
    rows, ended = [], []
    now = acks._now()
    for a in items:
        if engine is None or not run_id:
            st, why = ("expired", None) if now >= a["until"] else ("unknown", "no test run here to judge it against")
        else:
            st, why = acks.decide(engine, TENANT, run_id, a, now, metrics.get(a["case"]))
        if st in ("expired", "spent", "passing"):
            ended.append(a)
        label = {"quiet": "quiet", "woke": "WORSE", "expired": "ended", "spent": "passing since", "passing": "passing",
                 "unknown": "?"}[st] + (" (expires soon)" if st == "quiet" and acks.soon(a, now) else "")
        rows.append((a, label, why))
    for a, label, why in rows:
        print(f"{label:<22} {a['case']} {_label(a['check'])}  by {a['by']} until {a['until']:%Y-%m-%d}: {a['reason']}")
        if why and label not in ("quiet",):
            print(_paint(f"{'':<22} {why}", "dim"))
    if prune and ended:
        acks.save(root / CONFIG, [a for a in items if a not in ended])
        print(f"\nRemoved {_n(len(ended), 'acknowledgement')} that ended.")
    elif ended:
        print(f"\n{_n(len(ended), 'acknowledgement')} ended: `assay acks --prune` removes them.")
    return 0


def accept(root: Path, run_id: Optional[str], reason: str = "accepted with `assay accept`",
           for_: str = "14d") -> int:
    """`assay accept`: make a run (the latest, by default) the baseline of each of its cases,
    failures and all. Its failures are acknowledged for `for_`, not for ever."""
    home = root / HOME
    if not (home / "assay.db").exists():
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = _migrate(engine, home, _state(home))
    run_id = run_id or state.get("last")
    if not run_id:
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    if not (home / "runs" / f"{run_id}.jsonl").exists():
        print(f"No run {run_id} in {home / 'runs'}.", file=sys.stderr)
        return 2
    from assay import acks
    try:
        acks.duration(for_)
    except acks.AckError as exc:
        print(exc, file=sys.stderr)
        return 2
    cases = promote(engine, run_id)
    state["baseline_cases"] = {**(state.get("baseline_cases") or {}), **{c: run_id for c in cases}}
    _save_state(home, state)
    print(f"{run_id} is now the baseline of its {_n(len(cases), 'case')}. `assay test` fails only on what "
          "gets worse than it.")
    failing = _failing(engine, run_id)
    if failing:
        made = _make_acks(engine, run_id, failing, reason, for_, None)
        path = _merge_acks(root, made)
        print(f"Its {_n(len(made), 'failing check')} {'is' if len(made) == 1 else 'are'} acknowledged until "
              f"{made[0]['until']:%Y-%m-%d} in {path.name}: quiet while no worse, then reported again. "
              f"`assay acks` lists them.")
    return 0


# ---------- judge calibration (assay/calibrate.py) ----------

def _calib_cfg(root: Path) -> dict:
    from assay.calibrate import DEFAULTS
    cfg = find_config(root)
    return cfg.get("calibrate") or dict(DEFAULTS)


def golden_add(root: Path, case: str, score: float, by: Optional[str], tags: List[str], run_id: Optional[str],
               input_: Optional[str], output: Optional[str], note: Optional[str], critique: Optional[str] = None) -> int:
    """`assay golden add CASE --score N`: a recorded output, labeled; a second labeler adds a label."""
    from assay import acks, calibrate
    ccfg = _calib_cfg(root)
    path = root / ccfg["golden"]
    items = calibrate.load_golden(path) if path.exists() else []
    lo, hi = ccfg["label_range"] or ccfg["score_range"]
    if not lo <= score <= hi:
        print(f"--score {score:g}: the labels go from {lo:g} to {hi:g} ([calibrate] label_range).", file=sys.stderr)
        return 2
    by = by or acks.who()
    mine = next((x for x in items if x["id"] == case), None)
    if mine is not None:  # another label for an item that's there: how much people agree
        crit = {"critique": critique} if critique else {}
        if any(lab.get("by") == by for lab in mine["labels"]):
            mine["labels"] = [{**lab, "score": score, **crit} if lab.get("by") == by else lab for lab in mine["labels"]]
            print(f"{case}: {by}'s label is now {score:g}.")
        else:
            mine["labels"].append({"by": by, "score": score, **crit})
            print(f"{case}: labeled {score:g} by {by} too ({len(mine['labels'])} labels).")
        mine["tags"] = sorted(set(mine["tags"]) | set(tags))
        calibrate.save_golden(path, items)
        return 0
    output_from_run = output is None
    if output is None:
        engine, state = _open(root)
        run_id = run_id or (state or {}).get("last")
        if engine is None or not run_id:
            print("No recorded run to take the output from: run `assay test` first, or give --output.", file=sys.stderr)
            return 2
        heads = [h for h in agents.run_trajectories(engine, TENANT, run_id) if (h["case_id"] or "") == case
                 or (h["case_id"] or "").endswith(case)]
        if not heads:
            print(f"No recorded run of {case!r} in {run_id} with an answer: give --output (and --input).",
                  file=sys.stderr)
            return 2
        tid = heads[0]["trajectory_id"]
        traj = EventsSource(engine, TENANT).trajectories([tid]).get(tid) or {}
        output = traj.get("answer")
        if input_ is None:
            input_ = (learn._inputs(engine, TENANT, [tid]).get(tid) or {}).get("input")
        case = heads[0]["case_id"] or case
    item = {"id": case, "input": input_, "output": output,
            "labels": [{"by": by, "score": score, **({"critique": critique} if critique else {})}],
            "tags": sorted(set(tags))}
    if output_from_run:  # which run's output was labeled: what each evaluator said of it can be matched
        item["run"] = run_id
    if note:
        item["note"] = note
    calibrate.save_golden(path, items + [item])
    print(f"Added {case} to {path.name}, labeled {score:g} by {by}. It's {len(items) + 1} items now.")
    return 0


def golden_split(root: Path, train: float = 0.2, dev: float = 0.4, seed: int = 0, by: Optional[str] = None) -> int:
    from assay import calibrate
    ccfg = _calib_cfg(root)
    path = root / ccfg["golden"]
    items = calibrate.load_golden(path)
    if not 0 < train < 1 or not 0 < dev < 1 or train + dev >= 1:
        print("--train and --dev are shares that leave some for test, e.g. 0.2 and 0.4.", file=sys.stderr)
        return 2
    sizes = calibrate.assign_splits(items, train, dev, seed, by_group=by)
    calibrate.save_golden(path, items)
    whole = f" Whole {'tags' if by == 'tags' else 'inputs'} go to one split: dev and test are ones train never saw." \
        if by else ""
    print(f"{path.name}: " + ", ".join(f"{k} {sizes.get(k, 0)}" for k in calibrate.SPLITS) + ". A judge takes its "
          f"examples from train (assay_sdk.golden_examples); `assay calibrate` reports on dev, `--final` on test."
          + whole)
    return 0


def golden_stats(root: Path) -> int:
    from assay import calibrate
    ccfg = _calib_cfg(root)
    try:
        items = calibrate.load_golden(root / ccfg["golden"])
    except calibrate.CalibrationError as exc:
        print(exc, file=sys.stderr)
        return 2
    cov = calibrate.coverage(items, tuple(ccfg["label_range"] or ccfg["score_range"]))
    print(f"{ccfg['golden']}: {_n(cov['items'], 'item')}, labeled by "
          + ", ".join(f"{k} ({v})" for k, v in cov["labelers"].items()))
    width = max(cov["levels"].values(), default=0) or 1
    for lv, n in cov["levels"].items():
        print(f"  {lv:>3}  {'█' * max(1 if n else 0, round(20 * n / width)):<20} {n}")
    if cov["missing"]:
        print(_paint(f"Nothing labeled {', '.join(map(str, cov['missing']))}: calibration can't tell how the judge "
                     f"does there. `assay golden suggest` picks outputs to label.", "yellow"))
    ag = cov["labeler_agreement"]
    if ag:
        print(f"People agree: exact {ag['exact']:.0%}, within one {ag['within_one']:.0%}, on {_n(ag['items'], 'item')} "
              f"labeled twice. No judge will do much better than that.")
    else:
        print(_paint("No item has two labels: how much people agree, the ceiling for the judge, is unknown. A second "
                     "person labeling 20 of them (`assay golden add ID --score N --by NAME`) says.", "dim"))
    if cov["tags"]:
        print("Tags: " + ", ".join(f"{t} ({n})" for t, n in cov["tags"].items()))
    if cov["splits"]:
        print("Splits: " + ", ".join(f"{k} {cov['splits'].get(k, 0)}" for k in calibrate.SPLITS))
    else:
        print(_paint("Not split yet: a judge built from these items would be measured on what it learned. "
                     "`assay golden split` assigns train, dev and test.", "dim"))
    print(f"{_n(cov['critiques'], 'item')} with a critique (why it was scored so).")
    return 0


def _disagreements(engine, field: str, vs: Optional[str], have: set) -> List[Tuple[float, str, str]]:
    """(how far apart, case, why) where two judges scored the same run far apart, or the judge
    passed a run a deterministic check failed: where the judge is most likely wrong."""
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.case_id, t.c.document_id, t.c.field, t.c.status, t.c.score).where(
            (t.c.tenant == TENANT) & (t.c.run_id != BASELINE) & t.c.document_id.isnot(None))).all()
    by: Dict[str, List] = defaultdict(list)
    for r in rows:
        if r.case_id not in have:
            by[r.document_id].append(r)
    out = {}
    for doc, rs in by.items():
        mine = [r for r in rs if r.field == field and r.score is not None]
        if not mine:
            continue
        case, s = mine[0].case_id, mine[0].score
        other = [r for r in rs if vs and r.field == vs and r.score is not None]
        if other and abs(s - other[0].score) > 0:
            gap = abs(s - other[0].score)
            out[case] = max(out.get(case, (0, "", "")), (gap, case, f"{field} {s:g} vs {vs} {other[0].score:g}"))
        broke = [r.field for r in rs if r.score is None and r.status == "fail"]
        if mine[0].status == "pass" and broke:
            # A deterministic check is the stronger witness: it outranks two judges' scores apart.
            out[case] = max(out.get(case, (0, "", "")), (10.0, case, f"{field} passed it, but {_label(broke[0])} "
                                                                      f"(deterministic) failed"))
    return sorted(out.values(), reverse=True)


def golden_suggest(root: Path, n: int, field: Optional[str], vs: Optional[str] = None,
                   disagree: bool = False) -> int:
    """Recorded outputs to label next, spread over the judge's scores so the set spans poor to great;
    or, with vs / disagree, where judges disagree with each other or with a deterministic check."""
    from assay import calibrate
    ccfg = _calib_cfg(root)
    field = field or ccfg.get("field")
    if not field:
        print("Which check is the judge? `assay golden suggest --field helpful`, or [calibrate] field.", file=sys.stderr)
        return 2
    have = set()
    if (root / ccfg["golden"]).exists():
        have = {x["id"] for x in calibrate.load_golden(root / ccfg["golden"])}
    engine, _ = _open(root)
    if engine is None:
        print("No recorded runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    if vs or disagree:
        found = _disagreements(engine, field, vs, have)[:n]
        if not found:
            print("No disagreements recorded: every run the judges both scored, they scored alike.", file=sys.stderr)
            return 2
        print("Label these next: where the judges disagree, with each other or with a deterministic check. "
              "Disagreement says more about a judge's bias than agreement does:")
        for _, case, why in found:
            print(f"  {case}  {why}")
        print("\n`assay golden add CASE --score N` labels one (your score, not the judge's).")
        return 0
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.case_id, t.c.score).where((t.c.tenant == TENANT) & (t.c.field == field)
                                                                  & t.c.score.isnot(None)
                                                                  & (t.c.run_id != BASELINE))).all()
    scores: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        if r.case_id not in have:
            scores[r.case_id].append(r.score)
    if not scores:
        print(f"No recorded scores for {field} outside the golden set.", file=sys.stderr)
        return 2
    ranked = sorted(((c, median(v)) for c, v in scores.items()), key=lambda cv: cv[1])
    k = min(n, len(ranked))
    picks = [ranked[round(i * (len(ranked) - 1) / max(1, k - 1))] for i in range(k)] if k > 1 else ranked[:1]
    picks = list(dict.fromkeys(picks))
    print(f"Label these next: spread over what the judge scored them ({field}), so the set spans poor to great, "
          f"not only the typical:")
    for case, s in picks:
        print(f"  judged {s:>5.2f}  {case}")
    print("\n`assay golden add CASE --score N` labels one (your score, not the judge's).")
    return 0


def calibrate_cmd(root: Path, baseline: Optional[str] = None, fmt: str = "text", judge_spec: Optional[str] = None,
                  repeat: Optional[int] = None, rt=None, second_judge: Optional[str] = None, final: bool = False) -> int:
    """`assay calibrate`: the judge over the golden set, compared with the last calibration that passed.
    0 as good as before, 1 worse, 2 nothing to run."""
    from assay import calibrate
    try:
        ccfg = {**_calib_cfg(root)}
        if repeat:
            ccfg["repeat"] = repeat
        spec = judge_spec or ccfg["judge"]
        every = calibrate.load_golden(root / ccfg["golden"])
        split = "test" if final else ccfg.get("split") or ("dev" if any(x.get("split") for x in every) else None)
        items = calibrate.select_split(every, split)
        if not items:
            raise calibrate.CalibrationError(f"No items in the {split} split: `assay golden split` assigns them.")
        from assay_sdk import golden as _golden
        _golden.reset()  # the leak check sees only what this judge asked for
        judge = calibrate.load_judge(spec, root)
        second_spec = second_judge or ccfg.get("second_judge")
        second_fn = calibrate.load_judge(second_spec, root) if second_spec else None
    except (calibrate.CalibrationError, SetupError) as exc:
        print(exc, file=sys.stderr)
        return 2
    results, report = calibrate.run_judge(judge, items, ccfg, rt)
    a = calibrate.analyze(items, results, ccfg)
    a["split"] = split if split and any(x.get("split") for x in every) else None
    a["split_sizes"] = calibrate.coverage(every, (0, 1)).get("splits")
    a["leaks"] = calibrate.leaks(judge, items, a["split"])
    a["judge"] = {"spec": spec, "source": calibrate.fingerprint(judge), "models": a["models"]}
    a["field"] = ccfg.get("field")
    second = None
    if second_fn is not None:
        r2, _ = calibrate.run_judge(second_fn, items, ccfg, rt)
        second = {**calibrate.between_judges(a, calibrate.analyze(items, r2, ccfg)), "spec": second_spec}
    home = ensure_home(root)
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = _state(home)
    base_id = None if baseline == "none" else baseline or state.get("calibration_baseline")
    before = None
    if base_id:
        c = store.calibrations
        with engine.connect() as conn:
            row = conn.execute(select(c.c.result).where((c.c.tenant == TENANT) & (c.c.run_id == base_id))).first()
        if row is None and baseline:
            print(f"No calibration {baseline}.", file=sys.stderr)
            return 2
        before = row and row.result["calibration"]
    if before and before.get("split") != a.get("split"):
        before = None  # another split is other items: nothing to compare
    cmp = calibrate.compare(a, before, ccfg) if before else None
    run_id = calibrate.new_id()
    stored = calibrate.as_json(a, cmp)
    passed = not (cmp and cmp["regressed"]) and not a["leaks"]
    with engine.begin() as conn:
        conn.execute(store.calibrations.insert().values(tenant=TENANT, run_id=run_id, created_at=datetime.utcnow(),
                                                        judge=spec, golden=calibrate.digest(items), passed=passed,
                                                        result=json.loads(json.dumps(stored, default=str))))
    if passed:
        state["calibration_baseline"] = run_id
        _save_state(home, state)
    s = report.to_dict()
    cost = f"estimated ${s['cost_usd']:,.2f}" if s["cost_usd"] is not None else "cost unknown (set [prices])"
    line = f"{_n(s['llm_calls'], 'LLM call')}, {s['retries']} retries, {cost}, {report.seconds:.1f}s"
    if fmt == "json":
        print(json.dumps({"run_id": run_id, "passed": passed, **stored, "second_judge": second}, indent=1, default=str))
    else:
        print(calibrate.text(run_id, spec, a, ccfg, cmp, base_id, line, _paint, second))
    return 0 if passed else 1


# ---------- the report (assay/report.py) ----------

def report_cmd(root: Path, days: float = 7, fmt: str = "markdown", out: Optional[str] = None) -> int:
    """`assay report`: what the tests caught this week, and the running log. The server's report
    (GET /v1/report) adds production: failure modes, fixes that held, surprising usage."""
    from assay import report
    engine, state = _open(root)
    if engine is None:
        print("No test runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    r = report.build(engine, TENANT, days, revisions=(state or {}).get("revisions"))
    text = json.dumps(report.as_json(r), indent=1, default=str) if fmt == "json" else report.markdown(r)
    print(text)
    if out:
        Path(out).write_text(text)
    return 0


def log_add(root: Path, text: str, by: Optional[str] = None) -> int:
    from assay import acks, report
    home = ensure_home(root)
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    report.note(engine, TENANT, text, by=by or acks.who())
    print("Added to the log. `assay report` shows it with this week's findings.")
    return 0


# ---------- sending a run to a server ----------

def _http(method: str, url: str, body: Optional[dict], headers: Dict[str, str]) -> Tuple[int, object]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def upload(root: Path, run_id: Optional[str], url: Optional[str], key: Optional[str], tenant: Optional[str],
           http=_http) -> int:
    """Send a test run's recording to an Assay server, then have it check the agent runs there.
    Sending again is safe: every event has an id."""
    home = root / HOME
    run_id = run_id or _state(home).get("last")
    url = (url or os.environ.get("ASSAY_URL") or "").rstrip("/")
    key = key or os.environ.get("ASSAY_KEY")
    if not url:
        print("Where to? Set ASSAY_URL (and ASSAY_KEY), or pass --url.", file=sys.stderr)
        return 2
    if not run_id:
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    path = home / "runs" / f"{run_id}.jsonl"
    if not path.exists():
        print(f"No recording for run {run_id} in {home / 'runs'}.", file=sys.stderr)
        return 2
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if tenant:
        headers["X-Tenant"] = tenant
    try:
        for i in range(0, len(events), 1000):
            code, body = http("POST", f"{url}/v1/ingest", {"events": events[i:i + 1000]}, headers)
            if code != 200:
                print(f"{url} refused the upload ({code}): {body}", file=sys.stderr)
                return 2
        code, me = http("GET", f"{url}/v1/whoami", None, headers)
    except OSError as exc:
        print(f"Couldn't reach {url}: {exc}", file=sys.stderr)
        return 2
    if not tenant:
        tenant = me.get("tenant") if code == 200 and isinstance(me, dict) else None
        tenant = "default" if tenant in (None, "*") else tenant
    source = f"events:{tenant}"
    print(f"Sent run {run_id} ({len(events)} events) to {url}, tenant '{tenant}'.")
    if any(e.get("type") == "run.start" and e.get("test") for e in events):
        code, _ = http("POST", f"{url}/v1/agents/runs/{run_id}/evaluate?source={source}", None, headers)
        if code == 403:
            print("It isn't checked there yet: that needs a key with the manage scope. The dashboard can "
                  "check it too.")
        elif code != 200:
            print(f"The server couldn't check it ({code}).", file=sys.stderr)
    print(f"See it in the dashboard at {url}: source {source}, run {run_id}.")
    return 0


def split_command(argv: List[str]) -> Optional[str]:
    """`assay test -- pytest -q tests` → "pytest -q tests"."""
    if argv and argv[0] == "--":
        argv = argv[1:]
    return shlex.join(argv) if argv else None
