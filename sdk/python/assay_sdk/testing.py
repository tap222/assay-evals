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
from typing import Any, Callable, List, Optional, Tuple

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


def _call(c: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (c.get("args") or {}).items())
    return f"{c['name']}({args}){' → error' if c.get('error') else ''}"


def _calls_text(run) -> str:
    calls = tool_calls(run)
    return "; ".join(_call(c) for c in calls) if calls else "no tool calls"


def _matches(c: dict, args: dict) -> bool:
    got = c.get("args") or {}
    return all(k in got and got[k] == v for k, v in args.items())


def assert_called(run, tool: str, **args: Any) -> dict:
    """`tool` was called, with at least these arguments. Returns the first matching call."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    for c in tool_calls(run):
        if c["name"] == tool and _matches(c, args):
            return c
    want = f"{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
    same = [c for c in tool_calls(run) if c["name"] == tool]
    raise AssertionError(f"Expected a call to {want}; "
                         + (f"{tool} was called with {'; '.join(_call(c) for c in same)}" if same else
                            f"{tool} was never called") + f". The run made: {_calls_text(run)}.")


def assert_not_called(run, tool: str, **args: Any) -> None:
    """`tool` wasn't called (with these arguments, if any are given)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    hits = [c for c in tool_calls(run) if c["name"] == tool and _matches(c, args)]
    if hits:
        raise AssertionError(f"{tool} shouldn't have been called, but was: {'; '.join(_call(c) for c in hits)} "
                             f"(step {hits[0]['seq']}).")


def assert_called_before(run, first: str, then: str) -> None:
    """Every call to `then` comes after a call to `first`, e.g. get_order before refund."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    seen = False
    for c in tool_calls(run):
        seen = seen or c["name"] == first
        if c["name"] == then and not seen:
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

    def must_call(self, tool: str, **args: Any) -> "Expectations":
        return self._via(f"must_call({tool})", assert_called, tool, **args)

    def must_not_call(self, tool: str, **args: Any) -> "Expectations":
        return self._via(f"must_not_call({tool})", assert_not_called, tool, **args)

    def must_call_before(self, first: str, then: str) -> "Expectations":
        return self._via(f"must_call_before({first}, {then})", assert_called_before, first, then)

    def max_steps(self, n: int) -> "Expectations":
        return self._via(f"max_steps({n})", assert_max_steps, n)

    def must_answer(self, containing: Optional[str] = None) -> "Expectations":
        if containing is not None:
            return self._via(f"must_answer({containing!r})", assert_answer_contains, containing)
        return self._add("must_answer()", lambda: None if self.run.answer_text is not None else "The run gave no answer.")

    def must_get_approval_before(self, action: str) -> "Expectations":
        """Every call to `action` comes after an approval for it whose decision is "approved"
        (run.approval(action, "approved")); a later rejection takes it back."""
        def rule():
            decision = None
            for s in self.run.steps:
                if s["kind"] == "approval" and s.get("name") == action:
                    decision = s.get("decision")
                elif s["kind"] == "tool" and s.get("name") == action and decision != "approved":
                    why = "without an approval" if decision is None else f"after it was {decision}"
                    return f"{action} ran at step {s['seq']} {why}."
            return None
        return self._add(f"must_get_approval_before({action})", rule)

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

    def handles_failure(self, max_retries: int = 2) -> "Expectations":
        """After a tool failed or came back empty (assay.faults() makes it happen): the answer doesn't
        claim success, no tool is retried more than max_retries times, and the user is told (or the run
        is escalated, or a retry worked)."""
        def rule():
            calls = tool_calls(self.run)
            hit = [c for c in calls if c.get("error") or c.get("fault")]
            if not hit:
                return "No tool failed or came back empty: inject one with assay.faults(tool=\"error\")."
            why = _false_success(self.run)
            if why:
                return why
            for name in {c["name"] for c in hit}:
                n = sum(1 for c in calls if c["name"] == name)
                if n > max_retries + 1:
                    return f"{name} was called {n} times after it failed; at most {max_retries} retries expected."
            from assay_sdk.checks import says_it_failed
            recovered = all(any(d["name"] == c["name"] and not d.get("error") and not d.get("fault")
                                for d in calls[calls.index(c) + 1:]) for c in hit)
            told = says_it_failed(self.run.answer_text) or self.run.outcome_value in ("unresolved", "escalated")
            if not (recovered or told):
                return ("The answer doesn't tell the user anything went wrong: "
                        f"{(self.run.answer_text or '(no answer)')[:160]!r}.")
            return None
        return self._add(f"handles_failure({max_retries})", rule)

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
