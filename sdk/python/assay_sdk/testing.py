"""Assertions on what an agent run did, for pytest (or any test runner).

    from assay_sdk.testing import assert_called, assert_not_called, assert_max_steps

    def test_refund(assay_case):
        reply = my_agent("Refund O-17", run=assay_case)
        assert_called(assay_case, "get_order", order_id="O-17")
        assert_not_called(assay_case, "delete_order")
        assert_max_steps(assay_case, 6)

Each reads the steps the run recorded, and fails with what actually happened.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["tool_calls", "assert_called", "assert_not_called", "assert_called_before", "assert_max_steps",
           "assert_answer_contains", "assert_no_pii", "expect", "Expectations", "rewordings"]


def rewordings(*wordings: str, arg: str = "wording"):
    """Run a test once per wording of the same request; the first is the original.

        @rewordings("Can I get a refund for O-18?", "I want my money back for O-18", "refund O-18 pls")
        def test_no_refund_before_delivery(assay_case, wording):
            support_agent(assay_case, wording, "O-18")

    Each wording is a case (test_no_refund_before_delivery[wording1]), and with `pytest --assay`
    each rewording is also checked against the original: the same tools in the same order, and
    passing where it passes. Add wordings at the end, so the others keep their case ids.
    """
    import pytest
    if len(wordings) < 2:
        raise ValueError("rewordings: the original and at least one other wording")

    def mark(fn):
        fn = pytest.mark.parametrize(arg, list(wordings), ids=[f"wording{i}" for i in range(len(wordings))])(fn)
        return pytest.mark.assay_rewordings(arg=arg, wordings=list(wordings))(fn)
    return mark


def tool_calls(run) -> List[dict]:
    """The run's tool calls, in order: [{"name", "args", "result", "error", "seq"}]."""
    return [s for s in run.steps if s["kind"] == "tool"]


def _tool_fullname(c: dict) -> str:
    return f"{c['server']}.{c['name']}" if c.get("server") else c["name"]


def _call(c: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (c.get("args") or {}).items())
    return f"{_tool_fullname(c)}({args}){' → error' if c.get('error') else ''}"


def _calls_text(run) -> str:
    calls = tool_calls(run)
    return "; ".join(_call(c) for c in calls) if calls else "no tool calls"


def _matches(c: dict, args: dict) -> bool:
    got = c.get("args") or {}
    return all(k in got and got[k] == v for k, v in args.items())


def _tool_matches(c: dict, tool: str, server: Optional[str] = None) -> bool:
    """Matches tool call against name and server. Supports server.tool syntax."""
    target_server = server
    if "." in tool and target_server is None:
        target_server, target_name = tool.split(".", 1)
    else:
        target_name = tool
    fullname = f"{c['server']}.{c['name']}" if c.get("server") else c.get("name")
    if fullname == tool:
        return True
    if c.get("name") != target_name:
        return False
    if target_server is not None:
        return c.get("server") == target_server
    return True


def assert_called(run, tool: str, server: Optional[str] = None, **args: Any) -> dict:
    """`tool` was called, with at least these arguments. Supports server.tool syntax or server=."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    for c in tool_calls(run):
        if _tool_matches(c, tool, server) and _matches(c, args):
            return c
    target_name = f"{server}.{tool}" if server and "." not in tool else tool
    want = f"{target_name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
    same = [c for c in tool_calls(run) if _tool_matches(c, tool, server)]
    raise AssertionError(f"Expected a call to {want}; "
                         + (f"{target_name} was called with {'; '.join(_call(c) for c in same)}" if same else
                            f"{target_name} was never called") + f". The run made: {_calls_text(run)}.")


def assert_not_called(run, tool: str, server: Optional[str] = None, **args: Any) -> None:
    """`tool` wasn't called (with these arguments, if any are given). Supports server.tool syntax."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    hits = [c for c in tool_calls(run) if _tool_matches(c, tool, server) and _matches(c, args)]
    if hits:
        target_name = f"{server}.{tool}" if server and "." not in tool else tool
        raise AssertionError(f"{target_name} shouldn't have been called, but was: "
                             f"{'; '.join(_call(c) for c in hits)} (step {hits[0]['seq']}).")


def assert_called_before(run, first: str, then: str) -> None:
    """Every call to `then` comes after a call to `first`, e.g. get_order before refund. Supports server.tool syntax."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    seen = False
    for c in tool_calls(run):
        seen = seen or _tool_matches(c, first)
        if _tool_matches(c, then) and not seen:
            raise AssertionError(f"{then} was called at step {c['seq']} before any {first}. "
                                 f"The run made: {_calls_text(run)}.")


def assert_max_steps(run, n: int) -> None:
    """The run took at most `n` steps (model calls, tool calls, state changes and the answer)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    if len(run.steps) > n:
        kinds = ", ".join(f"{s['kind']}{' ' + s['name'] if s.get('name') else ''}" for s in run.steps)
        raise AssertionError(f"Took {len(run.steps)} steps; at most {n} expected: {kinds}.")


def assert_answer_contains(run, text: str) -> None:
    """The run's answer contains `text` (ignoring case)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    answer = run.answer_text
    if answer is None:
        raise AssertionError(f"The run gave no answer (expected one containing {text!r}).")
    if text.lower() not in answer.lower():
        raise AssertionError(f"The answer doesn't contain {text!r}: {answer!r}.")


def assert_no_pii(run, allow: Any = ()) -> None:
    """No personal data (email, card, IBAN, SSN, phone) in any tool's arguments, except kinds in
    `allow`. Uses the same scanner as `assay test`, so assay-server must be installed."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    try:
        from assay.learn import pii_scan
    except ImportError:
        raise ImportError("assert_no_pii uses Assay's PII scanner: pip install assay-server") from None
    found = []
    for c in tool_calls(run):
        for hit in pii_scan(json.dumps(c.get("args") or {}, default=str)):
            if hit["kind"] not in allow:
                found.append(f"{hit['kind']} ({hit['sample']}) sent to {c['name']} (step {c['seq']})")
    if found:
        raise AssertionError("Personal data in tool arguments: " + "; ".join(found) + ".")


# ---------- expect(run): everything a run should do, checked together when the test ends ----------

def _cost(run) -> float:
    return sum(s.get("cost_usd") or 0 for s in run.steps)


def _seconds(run) -> float:
    return (run.ended or time.monotonic()) - run.started


def _max(run, key: str) -> int:
    return max((len(s.get(key) or []) if key == "tools" else s.get(key) or 0
                for s in run.steps if s["kind"] == "llm"), default=0)


def _retrievals(run) -> List[dict]:
    return [s for s in run.steps if s["kind"] == "retrieval"]


def _found_nothing(s: dict) -> bool:
    """A retrieval that failed, was broken by assay.faults(), or came back empty."""
    return bool(s.get("error") or s.get("fault") or not s.get("fragments"))


def _false_success(run) -> Optional[str]:
    from assay_sdk.checks import claims_success
    claim = claims_success(run.answer_text or "")
    if claim is None:
        return None
    calls = tool_calls(run)
    for i, c in enumerate(calls):
        if c.get("error") and not any(d["name"] == c["name"] and not d.get("error") for d in calls[i + 1:]):
            return f"The answer says “{claim}”, but {_call(c)} failed and was never done."
    offered = any(s.get("tools") for s in run.steps if s["kind"] == "llm")
    if offered and not any(s["kind"] in ("tool", "state") for s in run.steps):
        return f"The answer says “{claim}”, but no tool was called and nothing changed."
    return None


class Expectations:
    """What a run should do, beyond its answer. Declare them anywhere in the test, even before the
    agent runs; they're checked when the test ends, and every one that fails is reported, not just
    the first:

        expect(run).must_call("get_order").must_not_call("delete_order").max_steps(8) \\
                   .must_get_approval_before("refund").max_cost(0.05).max_latency(8).must_resolve()

    Under pytest (the assay_case fixture) that happens by itself. Elsewhere, call .verify(), or
    use `with expect(run) as e:`. Each expectation is also recorded as a check on the run, so it's
    compared with its baseline like any other."""

    def __init__(self, run):
        self.run = run
        self.rules: List[Tuple[str, Callable[[], Optional[str]]]] = []
        self.verified = False
        run.expectations.append(self)

    def _add(self, name: str, rule: Callable[[], Optional[str]]) -> "Expectations":
        self.rules.append((name, rule))
        return self

    def _via(self, name: str, fn, *args, **kwargs) -> "Expectations":
        def rule():
            try:
                fn(self.run, *args, **kwargs)
            except AssertionError as e:
                return str(e)
            return None
        return self._add(name, rule)

    def must_call(self, tool: str, server: Optional[str] = None, **args: Any) -> "Expectations":
        label = f"{server}.{tool}" if server and "." not in tool else tool
        return self._via(f"must_call({label})", assert_called, tool, server=server, **args)

    def must_not_call(self, tool: str, server: Optional[str] = None, **args: Any) -> "Expectations":
        label = f"{server}.{tool}" if server and "." not in tool else tool
        return self._via(f"must_not_call({label})", assert_not_called, tool, server=server, **args)

    def must_call_before(self, first: str, then: str) -> "Expectations":
        return self._via(f"must_call_before({first}, {then})", assert_called_before, first, then)

    def max_steps(self, n: int) -> "Expectations":
        return self._via(f"max_steps({n})", assert_max_steps, n)

    def must_answer(self, containing: Optional[str] = None) -> "Expectations":
        if containing is not None:
            return self._via(f"must_answer({containing!r})", assert_answer_contains, containing)
        return self._add("must_answer()", lambda: None if self.run.answer_text is not None else "The run gave no answer.")

    def must_get_approval_before(self, action: str, server: Optional[str] = None) -> "Expectations":
        """Every call to `action` comes after an approval for it whose decision is "approved"
        (run.approval(action, "approved")); a later rejection takes it back. Supports server.action syntax."""
        def rule():
            decision = None
            for s in self.run.steps:
                if s["kind"] == "approval" and _tool_matches(s, action, server):
                    decision = s.get("decision")
                elif s["kind"] == "tool" and _tool_matches(s, action, server) and decision != "approved":
                    why = "without an approval" if decision is None else f"after it was {decision}"
                    target = f"{server}.{action}" if server and "." not in action else action
                    return f"{target} ran at step {s['seq']} {why}."
            return None
        label = f"{server}.{action}" if server and "." not in action else action
        return self._add(f"must_get_approval_before({label})", rule)

    def max_cost(self, usd: float) -> "Expectations":
        return self._add(f"max_cost({usd:g})", lambda: None if _cost(self.run) <= usd else
                         f"Cost ${_cost(self.run):.4f}; at most ${usd:g} expected.")

    def max_latency(self, seconds: float) -> "Expectations":
        return self._add(f"max_latency({seconds:g})", lambda: None if _seconds(self.run) <= seconds else
                         f"Took {_seconds(self.run):.1f}s; at most {seconds:g}s expected.")

    def max_tools_exposed(self, n: int) -> "Expectations":
        return self._add(f"max_tools_exposed({n})", lambda: None if _max(self.run, "tools") <= n else
                         f"A model call was offered {_max(self.run, 'tools')} tools; at most {n} expected.")

    def max_context_tokens(self, n: int) -> "Expectations":
        return self._add(f"max_context_tokens({n})", lambda: None if _max(self.run, "tokens_in") <= n else
                         f"A model call's input reached {_max(self.run, 'tokens_in'):,} tokens; at most {n:,} expected.")

    def must_resolve(self) -> "Expectations":
        def rule():
            o = self.run.outcome_value
            if o == "resolved":
                return None
            return (f"The run's outcome is {o}, not resolved." if o else
                    "The run has no outcome: call run.outcome(\"resolved\") when it resolves the request.")
        return self._add("must_resolve()", rule)

    def well_formed_arguments(self) -> "Expectations":
        """Every tool call's arguments fit the tool's input schema (the definition the model was given):
        nothing required missing, the right types, values in the enum, no fields the tool doesn't take."""
        def rule():
            from assay_sdk.checks import arg_problems
            bad = []
            for c in tool_calls(self.run):
                schema = self.run.tool_schemas.get(c["name"])
                p = arg_problems(schema, c.get("args") or {}) if schema else []
                if p:
                    bad.append(f"{c['name']} at step {c['seq']}: {', '.join(p)}")
            return ("Malformed arguments: " + "; ".join(bad) + ".") if bad else None
        return self._add("well_formed_arguments()", rule)

    def no_false_success(self) -> "Expectations":
        """The answer doesn't say an action was done when the tool for it failed (and wasn't retried
        successfully), or when no tool ran at all."""
        return self._add("no_false_success()", lambda: _false_success(self.run))

    def must_abstain(self) -> "Expectations":
        """The case's sources don't answer its question, so the answer should say so ("the documents
        don't say") instead of filling the gap with something plausible. An answer that says it and
        then answers anyway still passes; faithfulness checks what it then claims."""
        def rule():
            from assay_sdk.checks import abstains
            answer = self.run.answer_text
            if answer is None:
                return "The run gave no answer; it should say the sources don't answer the question."
            if abstains(answer):
                return None
            return f"The sources don't answer this, but the answer doesn't say so: {answer[:160]!r}."
        return self._add("must_abstain()", rule)

    def must_not_abstain(self) -> "Expectations":
        """The case's sources do answer its question: an answer that says they don't fails. The other
        half of must_abstain(), so a system that refuses everything doesn't pass both."""
        def rule():
            from assay_sdk.checks import abstains
            if self.run.answer_text is None:
                return "The run gave no answer."
            said = abstains(self.run.answer_text)
            return f"The answer says the sources don't answer this (“{said}”), but they do." if said else None
        return self._add("must_not_abstain()", rule)

    def handles_failure(self, max_retries: int = 2) -> "Expectations":
        """After a tool or a retrieval failed or came back empty (assay.faults() makes it happen): the
        answer doesn't claim success, nothing is retried more than max_retries times, and the user is
        told, or told the sources don't say (or the run is escalated, or a retry worked)."""
        def rule():
            calls, rets = tool_calls(self.run), _retrievals(self.run)
            hit = [c for c in calls if c.get("error") or c.get("fault")]
            missed = [s for s in rets if _found_nothing(s)]
            if not hit and not missed:
                return ("No tool or retrieval failed or came back empty: inject one with "
                        "assay.faults(tool=\"error\").")
            why = _false_success(self.run) if hit else None
            if why:
                return why
            for name in {c["name"] for c in hit}:
                n = sum(1 for c in calls if c["name"] == name)
                if n > max_retries + 1:
                    return f"{name} was called {n} times after it failed; at most {max_retries} retries expected."
            for name in {s.get("name") for s in missed}:
                n = sum(1 for s in rets if s.get("name") == name)
                if n > max_retries + 1:
                    return f"The {name} retrieval ran {n} times after it failed; at most {max_retries} retries expected."
            from assay_sdk.checks import abstains, says_it_failed
            recovered = all(any(d["name"] == c["name"] and not d.get("error") and not d.get("fault")
                                for d in calls[i + 1:]) for i, c in enumerate(calls) if c in hit) and \
                all(any(d.get("name") == s.get("name") and not _found_nothing(d) for d in rets[i + 1:])
                    for i, s in enumerate(rets) if _found_nothing(s))
            answer = self.run.answer_text
            told = says_it_failed(answer) or abstains(answer) or self.run.outcome_value in ("unresolved", "escalated")
            if not (recovered or told):
                if missed and not hit:
                    s = missed[0]
                    what = "failed" if s.get("error") else "found nothing"
                    return (f"The {s.get('name')} retrieval {what} (step {s['seq']}), but the answer goes on as if "
                            f"it had the evidence: {(answer or '(no answer)')[:160]!r}.")
                return ("The answer doesn't tell the user anything went wrong: "
                        f"{(answer or '(no answer)')[:160]!r}.")
            return None
        return self._add(f"handles_failure({max_retries})", rule)

    def must_retrieve(self, name: Optional[str] = None, source: Optional[str] = None) -> "Expectations":
        """A retrieval ran and found something: from the retriever `name` (run.search's name, or a
        LangChain or LlamaIndex retriever's), with a fragment from `source` (part of its source, e.g.
        "refund-policy"), or both. An answer that never searched where its evidence is fails."""
        def rule():
            rets = _retrievals(self.run)
            mine = [s for s in rets if name is None or s.get("name") == name]
            if not mine:
                ran = ", ".join(dict.fromkeys(str(s.get("name")) for s in rets)) or "none"
                return f"No retrieval from {name or 'any retriever'} ran (retrievals: {ran})."
            found = [s for s in mine if not _found_nothing(s)]
            if not found:
                return f"{name or 'Retrieval'} ran, but failed or found nothing every time."
            if source is not None and not any(source in str(f.get("source") or f.get("id") or "")
                                              for s in found for f in s.get("fragments") or []):
                seen = sorted({str(f.get("source") or f.get("id")) for s in found for f in s.get("fragments") or []})
                return (f"Nothing from {source!r} was retrieved; it found: {', '.join(seen[:8])}"
                        + (", …" if len(seen) > 8 else "") + ".")
            return None
        label = ", ".join(x for x in (name, f"source={source!r}" if source else None) if x)
        return self._add(f"must_retrieve({label})", rule)

    def at_most_once(self, tool: str, key: Optional[str] = None, server: Optional[str] = None) -> "Expectations":
        """`tool` went through at most once per value of its `key` argument (once in all without one):
        a payment, booking or email isn't repeated when a retry follows a lost reply. Supports server.tool syntax."""
        def rule():
            seen: Dict[str, List[int]] = {}
            for c in tool_calls(self.run):
                if _tool_matches(c, tool, server) and (not c.get("error") or c.get("fault") == "lost"):
                    k = "" if key is None else repr((c.get("args") or {}).get(key))
                    seen.setdefault(k, []).append(c["seq"])
            twice = {k: s for k, s in seen.items() if len(s) > 1}
            if not twice:
                return None
            k, steps = next(iter(twice.items()))
            target = f"{server}.{tool}" if server and "." not in tool else tool
            what = f" for {key}={k}" if key is not None else ""
            return f"{target} went through {len(steps)} times{what} (steps {', '.join(map(str, steps))}); once expected."
        label = f"{server}.{tool}" if server and "." not in tool else tool
        return self._add(f"at_most_once({label}" + (f", key={key}" if key else "") + ")", rule)

    def no_tool_collisions(self) -> "Expectations":
        """No model call was offered a tool name by more than one MCP server in the same call:
        two servers offering 'search' leaves the client or model to pick one arbitrarily without saying so."""
        def rule():
            from assay_sdk.checks import tool_collisions
            bad = []
            for s in self.run.steps:
                if s.get("kind") == "llm":
                    cols = s.get("tool_collisions")
                    if cols is None:
                        raw = s.get("raw_tools") or s.get("tools")
                        found = tool_collisions(raw) if raw else {}
                    else:
                        found = {c["name"]: c["servers"] for c in cols}
                    for name, servers in found.items():
                        bad.append(f"'{name}' was offered by both {', '.join(repr(x) for x in servers)} (step {s.get('seq')})")
            return (f"Tool collision across MCP servers: {'; '.join(bad)}. The model or client picks one arbitrarily without saying so.") if bad else None
        return self._add("no_tool_collisions()", rule)

    def duplicate_safe(self, duplicate_run: Optional[Any] = None, *, key: Optional[str] = None) -> "Expectations":
        """The run handles duplicate requests safely: the second delivery changes nothing (no repeated
        charges, payments or writes), answers success to the sender (not HTTP 409 Conflict, which causes
        retries to pile up), and is recorded as a duplicate."""
        def rule():
            from assay_sdk.duplicate import check_duplicate_safety, DuplicateResult
            target = duplicate_run
            orig = self.run
            if isinstance(target, DuplicateResult):
                return "; ".join(target.failures) if target.failures else None
            if target is None:
                if getattr(orig, "duplicate", False):
                    target = orig
                    orig = None
                else:
                    return "duplicate_safe needs the duplicate run: expect(first_run).duplicate_safe(duplicate_run)"
            failures = check_duplicate_safety(orig or target, target, key=key)
            return "; ".join(failures) if failures else None
        return self._add("duplicate_safe()" if not key else f"duplicate_safe(key={key!r})", rule)

    def citations_valid(self, require_citations: bool = False) -> "Expectations":
        """Every citation in the answer (a document, a page, a section) names a source that exists,
        was retrieved, and says what the sentence citing it says. A right claim with the wrong page
        fails as a wrong citation, not as a wrong answer."""
        def rule():
            from assay_sdk.citations import check_citations, extract_citations
            answer = self.run.answer_text or ""
            cits = extract_citations(answer)
            if require_citations and not cits:
                return "The answer gave no citations."
            if not cits:
                return None
            frags = []
            for s in self.run.steps:
                if s.get("kind") == "retrieval":
                    frags.extend(s.get("fragments") or [])
            results = check_citations(answer, frags)
            bad = [r.reason for r in results if not r.valid and r.reason]
            return "; ".join(bad) if bad else None
        return self._add("citations_valid()", rule)

    def checkpoint(self, name: str, rule: Optional[Callable[[Any], bool]] = None, *, tool: Optional[str] = None,
                   args: Optional[dict] = None, result: str = "ok", answer: Optional[str] = None) -> "Expectations":
        """A goal checkpoint of a long workflow, passing or failing on its own: rule(run) -> bool, or a
        tool call (tool=, args= partial, result= "ok" | "nonempty"), or text the answer has."""
        def check():
            if rule is not None:
                return None if rule(self.run) else "not met"
            if tool is not None:
                same = [c for c in tool_calls(self.run) if c["name"] == tool and _matches(c, args or {})]
                ok = [c for c in same if not c.get("error") and (result != "nonempty" or c.get("result") not in
                                                                (None, [], {}, ""))]
                return None if ok else (f"{tool} was never called" + (f" with {args}" if args else "") if not same else
                                        f"{tool} failed: {same[-1]['error']}" if same[-1].get("error") else
                                        f"{tool} returned nothing")
            if answer is not None:
                return None if answer.lower() in (self.run.answer_text or "").lower() else f"the answer doesn't say {answer!r}"
            return "give rule=, tool= or answer="
        return self._add(f"checkpoint({name})", check)

    def failures(self) -> List[str]:
        """Every expectation that doesn't hold, as "name: why"."""
        out = []
        self.took: dict = {}
        for name, rule in self.rules:
            t0 = time.perf_counter()
            why = rule()
            self.took[name] = (time.perf_counter() - t0) * 1000  # ms: could it run in the request path?
            if why:
                out.append(f"{name}: {why}")
        return out

    def verify(self) -> None:
        """Check them all, record each as a check on the run, and fail with every one that doesn't hold."""
        __tracebackhide__ = True
        failed = self.failures() if not self.verified else []
        if not self.verified:
            self.verified = True
            if self.run.test:  # a test case: each expectation is a check, compared with its baseline
                bad = {f.split(": ", 1)[0]: f.split(": ", 1)[1] for f in failed}
                for name, _ in self.rules:
                    self.run.check(f"expect.{name}", "fail" if name in bad else "pass", evaluator="assay.expect@1",
                                   reason=bad.get(name), duration_ms=getattr(self, "took", {}).get(name))
        if failed:
            raise AssertionError("Expected of the run:\n  " + "\n  ".join(failed))

    def __enter__(self) -> "Expectations":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.verify()


def expect(run) -> Expectations:
    """Expectations for this run: expect(run).must_call("get_order").max_steps(8)... See Expectations."""
    return Expectations(run)
