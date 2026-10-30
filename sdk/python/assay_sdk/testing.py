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
           "assert_answer_contains", "assert_no_pii", "expect", "Expectations", "rewordings",
           "assert_within_scope", "assert_fails_closed", "assert_no_data_exfiltration", "assert_used_gateway",
           "assert_valid_handoffs", "assert_no_orphaned_effects", "assert_terminal_state",
           "assert_valid_identity", "assert_no_cross_user_credentials", "assert_within_baseline",
           "assert_no_rubber_stamp_approvals", "assert_strictly_approved",
           "assert_booking_holds", "assert_read_back_details", "assert_no_spoken_leaks", "assert_no_talk_overs",
           "assert_valid_agent_handoffs", "assert_advice_was_read_only", "assert_transfer_was_accepted",
           "assert_no_permission_escalation",
           "assert_no_risky_divergences", "assert_replay_agreement", "assert_max_divergence_rate",
           "assert_serving_matches", "assert_no_stable_regressions", "assert_serving_parity", "assert_cost_savings",
           "assert_no_changed_basis", "assert_no_superseded_evidence", "assert_no_wrong_entity_records", "assert_evidence_parity"]


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


def assert_within_scope(run, scope: Any) -> None:
    """All tool calls, arguments, and network endpoints must remain strictly within scope."""
    __tracebackhide__ = True
    from assay_sdk.scopes import Scope
    sc = scope if isinstance(scope, Scope) else Scope(tools=scope if isinstance(scope, (dict, list, set)) else None)
    violations = sc.check_run(run)
    if violations:
        raise AssertionError(f"Run violated scope: {violations[0]}")


def assert_fails_closed(run, permission_tool: str, protected_action: str) -> None:
    """If permission_tool failed or faulted, protected_action must not have executed."""
    __tracebackhide__ = True
    from assay_sdk.scopes import check_fails_closed
    err = check_fails_closed(run, permission_tool, protected_action)
    if err:
        raise AssertionError(err)


def assert_no_data_exfiltration(run) -> None:
    """The agent answer must not embed markdown images or links that exfiltrate data."""
    __tracebackhide__ = True
    from assay_sdk.scopes import check_markdown_exfiltration
    findings = check_markdown_exfiltration(getattr(run, "answer_text", None))
    if findings:
        raise AssertionError(f"Markdown data exfiltration: {findings[0]}")


def assert_used_gateway(run, gateway_host: str, model_hosts: Optional[Iterable[str]] = None) -> None:
    """Model calls must route through the gateway and never reach provider endpoints directly."""
    __tracebackhide__ = True
    from assay_sdk.scopes import check_gateway_bypass
    bypasses = check_gateway_bypass(run, gateway_host, model_hosts=model_hosts)
    if bypasses:
        raise AssertionError(f"Gateway bypass: {bypasses[0]}")


def assert_valid_handoffs(run, contracts: Sequence[Any]) -> None:
    """Every agent handoff in the run must satisfy its contract and permission bounds."""
    __tracebackhide__ = True
    from assay_sdk.scenarios import validate_handoffs
    v = validate_handoffs(run, contracts)
    if v:
        raise AssertionError(f"Handoff contract violation: {v[0]}")


def assert_no_orphaned_effects(run, compensations: Dict[str, str]) -> None:
    """Every side-effecting action followed by a failure must be properly compensated."""
    __tracebackhide__ = True
    from assay_sdk.scenarios import check_compensations
    orphaned = check_compensations(run, compensations)
    if orphaned:
        raise AssertionError(f"Orphaned side-effect: {orphaned[0]}")


def assert_terminal_state(run, expected: str = "resolved", read_back: Optional[Callable] = None) -> None:
    """Run outcome and external read-back must both agree on the expected terminal state."""
    __tracebackhide__ = True
    actual = getattr(run, "outcome_value", None)
    if actual != expected:
        raise AssertionError(f"Terminal state mismatch: expected {expected!r}, got {actual!r}")
    if read_back:
        state = read_back()
        if not state:
            raise AssertionError("External read-back verification failed: empty state returned")


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
        if hasattr(run, "expectations") and isinstance(run.expectations, list):
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

    def must_get_approval_before(self, action: str, server: Optional[str] = None,
                                allowed_sources: Optional[Sequence[str]] = None,
                                max_age_seconds: Optional[float] = None,
                                bind_args: Optional[bool] = None,
                                reject_self_claimed: bool = True) -> "Expectations":
        """Every call to `action` comes after an approval for it whose decision is "approved"
        (run.approval(action, "approved")); a later rejection takes it back.
        Supports server.action syntax, source verification, argument-hash binding,
        and freshness checks."""
        def rule():
            from assay_sdk.production_security import hash_arguments, _ts, check_self_claimed_approvals
            decision = None
            last_approval = None

            # First, check for self-claimed approvals if enabled
            if reject_self_claimed:
                fake_claims = check_self_claimed_approvals(self.run)
                if fake_claims:
                    has_legit = any(
                        s.get("kind") == "approval" and s.get("decision") == "approved" and _tool_matches(s, action, server)
                        for s in self.run.steps
                    )
                    if not has_legit:
                        target = f"{server}.{action}" if server and "." not in action else action
                        return f"{target}: self-claimed approval detected ({fake_claims[0]})."

            for s in self.run.steps:
                if s["kind"] == "approval" and _tool_matches(s, action, server):
                    decision = s.get("decision")
                    if decision == "approved":
                        if allowed_sources:
                            src = s.get("source") or s.get("by")
                            if not src or src not in allowed_sources:
                                decision = "untrusted_source"
                                last_approval = None
                                continue
                        last_approval = s
                    else:
                        last_approval = None
                elif s["kind"] == "tool" and _tool_matches(s, action, server):
                    target = f"{server}.{action}" if server and "." not in action else action
                    if decision != "approved":
                        why = "without an approval" if decision is None else f"after it was {decision}"
                        return f"{target} ran at step {s['seq']} {why}."

                    # Verify argument binding
                    if (bind_args is True) or (bind_args is None and last_approval and last_approval.get("arg_hash")):
                        if last_approval and last_approval.get("arg_hash"):
                            actual_hash = hash_arguments(s.get("args"))
                            if actual_hash != last_approval["arg_hash"]:
                                return (f"{target} ran at step {s['seq']} with modified arguments "
                                        f"(argument-hash binding mismatch: request was changed after approval).")

                    # Verify freshness
                    if max_age_seconds is not None and last_approval:
                        app_ts = _ts(last_approval.get("ts"))
                        tool_ts = _ts(s.get("ts"))
                        if app_ts and tool_ts:
                            age = (tool_ts - app_ts).total_seconds()
                            if age > max_age_seconds:
                                return f"{target} ran at step {s['seq']} with expired approval (age {age:.1f}s > {max_age_seconds}s)."

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

    def no_logged_errors(self, min_level: str = "WARNING") -> "Expectations":
        """Expect that no error or warning (at or above min_level) was logged during the run."""
        levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
        thresh = levels.get(min_level.upper(), 30)

        def check():
            logs = getattr(self.run, "logged_errors", [])
            step_logs = [s for s in getattr(self.run, "steps", []) if s.get("kind") == "log"]
            all_errors = []
            for l in logs:
                lvl = levels.get(str(l.get("level", "")).upper(), 30)
                if lvl >= thresh:
                    all_errors.append(l)
            for s in step_logs:
                lvl = levels.get(str(s.get("level", "")).upper(), 30)
                if lvl >= thresh:
                    tmpl = s.get("template") or s.get("error") or s.get("text")
                    if not any(e.get("template") == tmpl for e in all_errors):
                        all_errors.append({"template": tmpl, "level": s.get("level")})
            if all_errors:
                tmpls = ", ".join(f"“{e['template']}”" for e in all_errors[:3])
                extra = f" and {len(all_errors) - 3} more" if len(all_errors) > 3 else ""
                return f"logged {len(all_errors)} error(s) or warning(s) ({tmpls}{extra})"
            return None

        return self._add("no_logged_errors", check)

    def no_external_touches(self, allow: Optional[Iterable[str]] = None) -> "Expectations":
        """Expect that the test touched nothing real (no commands, non-local sockets, or outside file writes)."""
        allowed = set(allow or [])

        def check():
            real_touches = []
            for s in getattr(self.run, "steps", []):
                name = s.get("name") or ""
                if name.startswith(("CMD ", "SOCKET ", "FILE write ")):
                    target = name.split(" ", 1)[1]
                    if target not in allowed and not any(a in target for a in allowed):
                        real_touches.append(name)
            if real_touches:
                touches_str = ", ".join(real_touches[:3])
                extra = f" and {len(real_touches) - 3} more" if len(real_touches) > 3 else ""
                return f"reached something real: {touches_str}{extra}"
            return None

        return self._add("no_external_touches", check)

    def within_scope(self, scope: Any) -> "Expectations":
        """Expect that all tool calls, arguments, and hosts remained strictly within scope."""
        from assay_sdk.scopes import Scope
        sc = scope if isinstance(scope, Scope) else Scope(tools=scope if isinstance(scope, (dict, list, set)) else None)

        def check():
            violations = sc.check_run(self.run)
            if violations:
                first = violations[0]
                more = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
                return f"{first}{more}"
            return None

        return self._add("within_scope", check)

    def fails_closed(self, permission_tool: str, protected_action: str) -> "Expectations":
        """Expect that if permission_tool faulted or failed, protected_action was never called."""
        from assay_sdk.scopes import check_fails_closed

        def check():
            return check_fails_closed(self.run, permission_tool, protected_action)

        return self._add(f"fails_closed({permission_tool} -> {protected_action})", check)

    def no_data_exfiltration(self) -> "Expectations":
        """Expect that the answer embeds no markdown images or links carrying exfiltrated data."""
        from assay_sdk.scopes import check_markdown_exfiltration

        def check():
            answer = getattr(self.run, "answer_text", None)
            findings = check_markdown_exfiltration(answer)
            if findings:
                return f"Markdown data exfiltration detected: {findings[0]}"
            return None

        return self._add("no_data_exfiltration", check)

    def no_untrusted_taint(self, sensitive_tools: Optional[Iterable[str]] = None,
                           sensitive_args: Optional[Iterable[str]] = None) -> "Expectations":
        """Expect that outbound sensitive tools did not receive argument values planted in untrusted documents."""
        from assay_sdk.scopes import check_untrusted_taint

        def check():
            tainted = check_untrusted_taint(self.run, sensitive_tools=sensitive_tools, sensitive_args=sensitive_args)
            if tainted:
                return f"Untrusted taint detected: {tainted[0]}"
            return None

        return self._add("no_untrusted_taint", check)

    def no_security_flags(self, min_severity: str = "high", allow: Optional[List[str]] = None) -> "Expectations":
        """Expect that autonomous security scanner found no security flags at or above min_severity."""
        from assay_sdk.scopes import scan_security_flags
        severities = {"critical": 3, "high": 2, "warning": 1}
        min_rank = severities.get(min_severity.lower(), 2)

        def check():
            flags = scan_security_flags(self.run, allow=allow)
            relevant = [f for f in flags if severities.get(f.severity.lower(), 1) >= min_rank]
            if relevant:
                first = relevant[0]
                more = f" (+{len(relevant) - 1} more)" if len(relevant) > 1 else ""
                return f"{first}{more}"
            return None

        return self._add(f"no_security_flags(min_severity={min_severity})", check)

    def used_gateway(self, gateway_host: str, model_hosts: Optional[Iterable[str]] = None) -> "Expectations":
        """Expect that all model provider calls went through gateway_host and never directly to provider APIs."""
        from assay_sdk.scopes import check_gateway_bypass

        def check():
            bypasses = check_gateway_bypass(self.run, gateway_host, model_hosts=model_hosts)
            if bypasses:
                return f"Gateway bypass detected: {bypasses[0]}"
            return None

        return self._add(f"used_gateway({gateway_host})", check)

    def valid_handoffs(self, contracts: Sequence[Any]) -> "Expectations":
        """Expect that every agent handoff satisfied input/output contracts and read-only/transfer promises."""
        from assay_sdk.scenarios import validate_handoffs

        def check():
            violations = validate_handoffs(self.run, contracts)
            if violations:
                return violations[0]
            return None

        return self._add("valid_handoffs", check)

    def terminal_state(self, expected: str = "resolved", read_back: Optional[Callable] = None) -> "Expectations":
        """Expect that the run reached the expected terminal state and external system agrees."""
        def check():
            actual = getattr(self.run, "outcome_value", None)
            if actual != expected:
                return f"Terminal state mismatch: expected {expected!r}, got {actual!r}"
            if read_back:
                try:
                    state = read_back()
                    if not state:
                        return "External read-back verification failed: empty state returned"
                except Exception as exc:
                    return f"External read-back failed with error: {exc}"
            return None

        return self._add(f"terminal_state({expected})", check)

    def no_orphaned_effects(self, compensations: Dict[str, str]) -> "Expectations":
        """Expect that any side-effecting action followed by a failure was compensated."""
        from assay_sdk.scenarios import check_compensations

        def check():
            orphaned = check_compensations(self.run, compensations)
            if orphaned:
                return orphaned[0]
            return None

        return self._add("no_orphaned_effects", check)

    def scenario_passed(self, scenario: Any) -> "Expectations":
        """Expect that the entire cross-cutting reliability scenario passed with no broken transitions."""
        def check():
            res = scenario.evaluate(self.run)
            if not res.passed:
                first = res.failures[0] if res.failures else "scenario failed"
                if res.broken_transition:
                    return f"{first} ({res.broken_transition.summary()})"
                return first
            return None

        return self._add(f"scenario_passed({scenario.name})", check)

    def has_valid_identity(self) -> "Expectations":
        """Expect that the agent has a known owner, an agent identity, and no cross-user credential violations."""
        from assay_sdk.production_security import check_identity_violations

        def check():
            violations = check_identity_violations(self.run)
            if violations:
                return violations[0].reason
            return None

        return self._add("has_valid_identity", check)

    def no_cross_user_credentials(self) -> "Expectations":
        """Expect that no tool call uses personal credentials of a user other than who requested the action."""
        from assay_sdk.production_security import check_identity_violations

        def check():
            violations = [v for v in check_identity_violations(self.run) if v.kind == "cross_user_credential"]
            if violations:
                return violations[0].reason
            return None

        return self._add("no_cross_user_credentials", check)

    def within_baseline(self, baseline: Any) -> "Expectations":
        """Expect that the run does not exhibit runtime drift (new hosts, new tables, read-to-write, volume spikes)."""
        from assay_sdk.production_security import detect_runtime_drift

        def check():
            alerts = detect_runtime_drift(baseline, self.run)
            if alerts:
                return alerts[0].reason
            return None

        return self._add("within_baseline", check)

    def no_rubber_stamp_approvals(self, min_review_seconds: float = 1.0) -> "Expectations":
        """Expect that all approvals took sufficient review time (>= min_review_seconds) and have an approver."""
        from assay_sdk.production_security import detect_rubber_stamp_approvals

        def check():
            stamps = detect_rubber_stamp_approvals(self.run, min_review_seconds=min_review_seconds)
            if stamps:
                return stamps[0]["reason"]
            return None

        return self._add(f"no_rubber_stamp_approvals({min_review_seconds}s)", check)

    def strictly_approved(self, action: str, server: Optional[str] = None,
                          allowed_sources: Optional[Sequence[str]] = None,
                          max_age_seconds: Optional[float] = None,
                          bind_args: bool = True) -> "Expectations":
        """Expect that action is strictly approved with valid source, argument-hash binding, and freshness."""
        return self.must_get_approval_before(
            action,
            server=server,
            allowed_sources=allowed_sources,
            max_age_seconds=max_age_seconds,
            bind_args=bind_args,
            reject_self_claimed=True,
        )

    def booking_holds(self, expected: Dict[str, Any], initial_rejected: Optional[Dict[str, Any]] = None) -> "Expectations":
        """Expect that the booking holds the final, corrected details, not initial details."""
        from assay_sdk.voice import check_booking_details

        def check():
            ok, why = check_booking_details(self.run, expected, initial_rejected)
            if not ok:
                return why
            return None

        return self._add("booking_holds", check)

    def read_back_details(self, fields: Sequence[str] = ("date", "time"),
                          expected_values: Optional[Dict[str, Any]] = None) -> "Expectations":
        """Expect that the agent read back confirmed details to the caller before concluding."""
        from assay_sdk.voice import check_read_back

        def check():
            ok, why = check_read_back(self.run, required_fields=fields, expected_values=expected_values)
            if not ok:
                return why
            return None

        return self._add("read_back_details", check)

    def resolved_relative_date(self, relative_phrase: str, expected_date: str, fixed_clock: datetime) -> "Expectations":
        """Expect that relative dates ('next Tuesday', 'tomorrow') resolved correctly against fixed clock."""
        from assay_sdk.voice import resolve_relative_date

        def check():
            actual = resolve_relative_date(relative_phrase, fixed_clock)
            if actual != expected_date:
                return f"Relative date '{relative_phrase}' resolved to {actual}, expected {expected_date}"
            return None

        return self._add(f"resolved_relative_date({relative_phrase})", check)

    def no_spoken_leaks(self, forbidden_phrases: Sequence[str]) -> "Expectations":
        """Expect that no internal instructions or system prompt guidelines were spoken to the caller."""
        from assay_sdk.voice import check_spoken_leaks

        def check():
            leaks = check_spoken_leaks(self.run, forbidden_phrases)
            if leaks:
                return leaks[0]
            return None

        return self._add("no_spoken_leaks", check)

    def handoff_carried_summary(self, required_keys: Optional[Sequence[str]] = None) -> "Expectations":
        """Expect that an escalation to a human agent carried a summary in its handoff payload."""
        from assay_sdk.voice import check_handoff_summary

        def check():
            ok, why = check_handoff_summary(self.run, required_keys)
            if not ok:
                return why
            return None

        return self._add("handoff_carried_summary", check)

    def voice_latency(self, max_ttfr_ms: float = 1500.0) -> "Expectations":
        """Expect that time to first response is within max_ttfr_ms."""
        def check():
            call_record = getattr(self.run, "voice_call_record", None)
            if call_record and call_record.metrics.time_to_first_response_ms is not None:
                actual = call_record.metrics.time_to_first_response_ms
                if actual > max_ttfr_ms:
                    return f"Time to first response was {actual:.0f}ms; at most {max_ttfr_ms:.0f}ms expected"
            return None

        return self._add(f"voice_latency({max_ttfr_ms}ms)", check)

    def no_talk_overs(self, max_allowed: int = 0) -> "Expectations":
        """Expect that the agent did not talk over the caller more than max_allowed times."""
        def check():
            call_record = getattr(self.run, "voice_call_record", None)
            if call_record and call_record.metrics.talk_over_count > max_allowed:
                actual = call_record.metrics.talk_over_count
                return f"Agent talked over caller {actual} times (max allowed: {max_allowed})"
            return None

        return self._add(f"no_talk_overs({max_allowed})", check)

    def no_answers_during_hold(self) -> "Expectations":
        """Expect that the agent stayed silent and did not speak while caller placed call on hold."""
        def check():
            call_record = getattr(self.run, "voice_call_record", None)
            if call_record and call_record.metrics.spoke_during_hold:
                return "Agent spoke while caller placed call on hold"
            return None

        return self._add("no_answers_during_hold", check)

    def max_dead_air(self, max_seconds: float = 5.0) -> "Expectations":
        """Expect that cumulative dead air / silence does not exceed max_seconds."""
        def check():
            call_record = getattr(self.run, "voice_call_record", None)
            if call_record and call_record.metrics.dead_air_seconds > max_seconds:
                actual = call_record.metrics.dead_air_seconds
                return f"Total dead air was {actual:.1f}s (max allowed: {max_seconds:.1f}s)"
            return None

        return self._add(f"max_dead_air({max_seconds}s)", check)

    def valid_agent_handoffs(self, contracts: Optional[Sequence[Any]] = None,
                              originating_scope: Optional[Sequence[str]] = None,
                              originating_approvals: Optional[Sequence[str]] = None) -> "Expectations":
        """Expect that all agent handoffs adhere to advice/transfer contracts, single ownership,
        fact propagation, and permission containment."""
        from assay_sdk.handoffs import validate_agent_handoffs

        def check():
            violations = validate_agent_handoffs(
                self.run, contracts=contracts, originating_scope=originating_scope,
                originating_approvals=originating_approvals
            )
            if violations:
                return violations[0]
            return None

        return self._add("valid_agent_handoffs", check)

    def advice_was_read_only(self) -> "Expectations":
        """Expect that any agent asked for advice performed only read-only actions and returned findings."""
        from assay_sdk.handoffs import validate_agent_handoffs

        def check():
            violations = validate_agent_handoffs(self.run)
            advice_violations = [v for v in violations if "advice" in v.lower()]
            if advice_violations:
                return advice_violations[0]
            return None

        return self._add("advice_was_read_only", check)

    def transfer_was_accepted(self, to_agent: Optional[str] = None) -> "Expectations":
        """Expect that a transfer/takeover handoff was explicitly accepted and not left unfinished."""
        from assay_sdk.handoffs import validate_agent_handoffs

        def check():
            violations = validate_agent_handoffs(self.run)
            transfer_violations = [v for v in violations if "takeover" in v.lower() or "unaccepted" in v.lower() or "unfinished" in v.lower()]
            if to_agent:
                transfer_violations = [v for v in transfer_violations if f"'{to_agent}'" in v]
            if transfer_violations:
                return transfer_violations[0]
            return None

        return self._add("transfer_was_accepted", check)

    def no_permission_escalation(self, originating_scope: Optional[Sequence[str]] = None,
                                 originating_approvals: Optional[Sequence[str]] = None) -> "Expectations":
        """Expect that no sub-agent or handoff receiver escalated permissions beyond human-granted scope."""
        from assay_sdk.handoffs import validate_agent_handoffs

        def check():
            violations = validate_agent_handoffs(
                self.run, originating_scope=originating_scope, originating_approvals=originating_approvals
            )
            escalation_violations = [v for v in violations if "escalation" in v.lower() or "approval leakage" in v.lower() or "scope containment" in v.lower()]
            if escalation_violations:
                return escalation_violations[0]
            return None

        return self._add("no_permission_escalation", check)

    def no_risky_divergences(self, report: Optional[Any] = None) -> "Expectations":
        """Expect that replay had zero write/spend divergences and zero worse scores."""
        def check():
            target_report = report if report is not None else getattr(self.run, "replay_report", self.run)
            if hasattr(target_report, "write_spend_divergences"):
                if target_report.write_spend_divergences:
                    first = target_report.write_spend_divergences[0]
                    return f"Risky write/spend divergence: {first.detail} (step {first.step_seq})"
                worse_count = target_report.scores.get("worse", 0)
                if worse_count > 0:
                    return f"Replay produced {worse_count} worse decision divergence(s)"
            return None

        return self._add("no_risky_divergences", check)

    def min_replay_agreement(self, min_rate: float = 0.95, report: Optional[Any] = None) -> "Expectations":
        """Expect that decision replay agreement rate is at least min_rate."""
        def check():
            target_report = report if report is not None else getattr(self.run, "replay_report", self.run)
            if hasattr(target_report, "agreement_rate"):
                if target_report.agreement_rate < min_rate:
                    return f"Replay agreement rate {target_report.agreement_rate * 100:.1f}% is below required {min_rate * 100:.1f}%"
            return None

        return self._add(f"min_replay_agreement({min_rate * 100:.1f}%)", check)

    def max_divergence_rate(self, max_rate: float = 0.05, report: Optional[Any] = None) -> "Expectations":
        """Expect that decision divergence rate does not exceed max_rate."""
        def check():
            target_report = report if report is not None else getattr(self.run, "replay_report", self.run)
            if hasattr(target_report, "agreement_rate"):
                div_rate = 1.0 - target_report.agreement_rate
                if div_rate > max_rate:
                    return f"Replay divergence rate {div_rate * 100:.1f}% exceeds max allowed {max_rate * 100:.1f}%"
            return None

        return self._add(f"max_divergence_rate({max_rate * 100:.1f}%)", check)

    def serving_matches(self, expected: Union[Dict[str, Any], Any]) -> "Expectations":
        """Expect that the model serving setup (quantization, engine, hardware) matches expected."""
        from assay_sdk.serving import normalize_serving

        def check():
            target_setup = getattr(self.run, "serving", None)
            if not target_setup and hasattr(self.run, "steps"):
                for s in self.run.steps:
                    if s.get("kind") == "llm" and s.get("serving"):
                        target_setup = s.get("serving")
                        break
            actual = normalize_serving(target_setup)
            exp = normalize_serving(expected)
            if actual is None or exp is None:
                if actual != exp:
                    return f"Serving setup mismatch: expected {expected!r}, got {target_setup!r}"
                return None
            exp_dict = exp.to_dict()
            act_dict = actual.to_dict()
            for k, v in exp_dict.items():
                if act_dict.get(k) != v:
                    return f"Serving {k} mismatch: expected {v!r}, got {act_dict.get(k)!r}"
            return None

        return self._add("serving_matches", check)

    def no_stable_regressions(self, report: Optional[Any] = None) -> "Expectations":
        """Expect zero regressions on cases that were historically stable under baseline serving."""
        def check():
            target_report = report if report is not None else getattr(self.run, "serving_report", self.run)
            if hasattr(target_report, "stable_regressions_count"):
                if target_report.stable_regressions_count > 0:
                    stable_cases = [c.case_name for c in getattr(target_report, "case_diffs", []) if not c.identical_output and c.was_previously_stable]
                    return f"{target_report.stable_regressions_count} previously stable case(s) changed: {', '.join(stable_cases)}"
            return None

        return self._add("no_stable_regressions", check)

    def max_serving_divergence(self, max_cases: int = 0, report: Optional[Any] = None) -> "Expectations":
        """Expect at most max_cases differing between two serving setups."""
        def check():
            target_report = report if report is not None else getattr(self.run, "serving_report", self.run)
            if hasattr(target_report, "changed_cases_count"):
                if target_report.changed_cases_count > max_cases:
                    return f"{target_report.changed_cases_count} cases changed under new serving setup (max allowed: {max_cases})"
            return None

        return self._add(f"max_serving_divergence({max_cases})", check)

    def cost_savings_at_least(self, min_savings_pct: float = 0.0, report: Optional[Any] = None) -> "Expectations":
        """Expect that the candidate serving setup achieved at least min_savings_pct cost savings."""
        def check():
            target_report = report if report is not None else getattr(self.run, "serving_report", self.run)
            if hasattr(target_report, "cost_savings_pct"):
                if target_report.cost_savings_pct < min_savings_pct:
                    return f"Cost savings was {target_report.cost_savings_pct:.1f}% (required: at least {min_savings_pct:.1f}%)"
            return None

        return self._add(f"cost_savings_at_least({min_savings_pct:.1f}%)", check)

    def no_changed_basis(self, report: Optional[Any] = None) -> "Expectations":
        """Expect zero changed evidence basis findings (especially under unchanged answers)."""
        def check():
            target_report = report if report is not None else getattr(self.run, "evidence_report", self.run)
            if hasattr(target_report, "changed_basis_under_same_answer_count"):
                if target_report.changed_basis_under_same_answer_count > 0:
                    changed_cases = [c.case_name for c in getattr(target_report, "case_diffs", []) if c.is_changed_basis_finding]
                    return f"{target_report.changed_basis_under_same_answer_count} case(s) produced unchanged answer from changed evidence basis: {', '.join(changed_cases)}"
            return None

        return self._add("no_changed_basis", check)

    def no_superseded_evidence(self, report: Optional[Any] = None) -> "Expectations":
        """Expect that no case rested on superseded or older policy versions / dates."""
        def check():
            target_report = report if report is not None else getattr(self.run, "evidence_report", self.run)
            if hasattr(target_report, "superseded_evidence_count"):
                if target_report.superseded_evidence_count > 0:
                    cases = [c.case_name for c in getattr(target_report, "case_diffs", []) if c.superseded_evidence]
                    return f"{target_report.superseded_evidence_count} superseded evidence item(s) consulted in case(s): {', '.join(cases)}"
            return None

        return self._add("no_superseded_evidence", check)

    def no_wrong_entity_records(self, report: Optional[Any] = None) -> "Expectations":
        """Expect that no case consulted records of another entity (wrong customer or account)."""
        def check():
            target_report = report if report is not None else getattr(self.run, "evidence_report", self.run)
            if hasattr(target_report, "wrong_entity_count"):
                if target_report.wrong_entity_count > 0:
                    cases = [c.case_name for c in getattr(target_report, "case_diffs", []) if c.wrong_entity_records]
                    return f"{target_report.wrong_entity_count} cross-entity record(s) consulted in case(s): {', '.join(cases)}"
            return None

        return self._add("no_wrong_entity_records", check)

    def no_retrieval_regressions(self, max_regressions: int = 0, report: Optional[Any] = None) -> "Expectations":
        """Expect at most max_regressions queries performing worse than baseline."""
        def check():
            target_report = report if report is not None else getattr(self.run, "retrieval_report", self.run)
            if hasattr(target_report, "regressions"):
                reg_count = len(target_report.regressions)
                if reg_count > max_regressions:
                    qids = [r.query_id for r in target_report.regressions[:5]]
                    return f"{reg_count} query retrieval regression(s) (max allowed: {max_regressions}): {', '.join(qids)}"
            return None

        return self._add(f"no_retrieval_regressions({max_regressions})", check)

    def recall_improved_or_equal(self, min_delta: float = 0.0, report: Optional[Any] = None) -> "Expectations":
        """Expect that candidate recall@k improved or maintained baseline recall (delta >= min_delta)."""
        def check():
            target_report = report if report is not None else getattr(self.run, "retrieval_report", self.run)
            if hasattr(target_report, "baseline_metrics") and hasattr(target_report, "candidate_metrics"):
                base_r = target_report.baseline_metrics.get("recall@k", 0.0)
                cand_r = target_report.candidate_metrics.get("recall@k", 0.0)
                delta = cand_r - base_r
                if delta < min_delta - 1e-6:
                    return f"Recall delta {delta:+.4f} is below required min_delta {min_delta:+.4f} ({base_r:.4f} -> {cand_r:.4f})"
            return None

        return self._add(f"recall_improved_or_equal({min_delta})", check)

    def latency_p95_below(self, max_ms: float, stage: str = "total", report: Optional[Any] = None) -> "Expectations":
        """Expect that candidate retrieval p95 latency is at or below max_ms."""
        def check():
            target_report = report if report is not None else getattr(self.run, "retrieval_report", self.run)
            if hasattr(target_report, "candidate_timings"):
                timings = target_report.candidate_timings.get(stage)
                if timings and timings.p95 > max_ms:
                    return f"Candidate {stage} p95 latency {timings.p95:.1f}ms exceeds max allowed {max_ms:.1f}ms"
            return None

        return self._add(f"latency_p95_below({stage}={max_ms}ms)", check)

    def labels_clean(self, report: Optional[Any] = None) -> "Expectations":
        """Expect that ground-truth relevance labels are clean without unlabeled docs or conflicts."""
        def check():
            target_report = report if report is not None else getattr(self.run, "retrieval_report", self.run)
            lh = getattr(target_report, "label_health", target_report)
            if hasattr(lh, "is_clean") and not lh.is_clean:
                return lh.summary()
            return None

        return self._add("labels_clean", check)

    def all_conjunctive_evidence_retrieved(self, report: Optional[Any] = None) -> "Expectations":
        """Expect all conjunctive evidence queries retrieved all required passages ('all the evidence, not some')."""
        def check():
            target_report = report if report is not None else getattr(self.run, "retrieval_report", self.run)
            if hasattr(target_report, "conjunctive_failures"):
                if target_report.conjunctive_failures:
                    qids = [f.get("query_id") for f in target_report.conjunctive_failures[:5]]
                    return f"{len(target_report.conjunctive_failures)} conjunctive query failure(s): {', '.join(qids)}"
            return None

        return self._add("all_conjunctive_evidence_retrieved", check)

    def retrieval_meets_criterion(self, target_doc: str, max_rank: int = 5) -> "Expectations":
        """Expect that target_doc was retrieved in the run's fragments at rank <= max_rank."""
        def check():
            from assay_sdk.testing import _retrievals
            rets = _retrievals(self.run)
            found_ranks = []
            for s in rets:
                frags = s.get("fragments") or []
                for i, f in enumerate(frags, start=1):
                    did = f.get("id") if isinstance(f, dict) else str(f)
                    if did == str(target_doc):
                        found_ranks.append(i)
            if not found_ranks:
                return f"Target document {target_doc!r} was not retrieved in any retrieval step"
            best_rank = min(found_ranks)
            if best_rank > max_rank:
                return f"Target document {target_doc!r} best rank #{best_rank} exceeded max allowed #{max_rank}"
            return None

        return self._add(f"retrieval_meets_criterion({target_doc} in top {max_rank})", check)

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
            if getattr(self.run, "test", None) and hasattr(self.run, "check"):  # a test case: each expectation is a check, compared with its baseline
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


def assert_valid_identity(run) -> None:
    expect(run).has_valid_identity().verify()


def assert_no_cross_user_credentials(run) -> None:
    expect(run).no_cross_user_credentials().verify()


def assert_within_baseline(run, baseline: Any) -> None:
    expect(run).within_baseline(baseline).verify()


def assert_no_rubber_stamp_approvals(run, min_review_seconds: float = 1.0) -> None:
    expect(run).no_rubber_stamp_approvals(min_review_seconds=min_review_seconds).verify()


def assert_strictly_approved(run, action: str, server: Optional[str] = None,
                             allowed_sources: Optional[Sequence[str]] = None,
                             max_age_seconds: Optional[float] = None,
                             bind_args: bool = True) -> None:
    expect(run).strictly_approved(action, server=server, allowed_sources=allowed_sources,
                                  max_age_seconds=max_age_seconds, bind_args=bind_args).verify()


def assert_booking_holds(run, expected: Dict[str, Any], initial_rejected: Optional[Dict[str, Any]] = None) -> None:
    expect(run).booking_holds(expected, initial_rejected=initial_rejected).verify()


def assert_read_back_details(run, fields: Sequence[str] = ("date", "time"),
                             expected_values: Optional[Dict[str, Any]] = None) -> None:
    expect(run).read_back_details(fields=fields, expected_values=expected_values).verify()


def assert_no_spoken_leaks(run, forbidden_phrases: Sequence[str]) -> None:
    expect(run).no_spoken_leaks(forbidden_phrases).verify()


def assert_no_talk_overs(run, max_allowed: int = 0) -> None:
    expect(run).no_talk_overs(max_allowed=max_allowed).verify()


def assert_valid_agent_handoffs(run, contracts: Optional[Sequence[Any]] = None,
                                originating_scope: Optional[Sequence[str]] = None,
                                originating_approvals: Optional[Sequence[str]] = None) -> None:
    expect(run).valid_agent_handoffs(contracts=contracts, originating_scope=originating_scope,
                                     originating_approvals=originating_approvals).verify()


def assert_advice_was_read_only(run) -> None:
    expect(run).advice_was_read_only().verify()


def assert_transfer_was_accepted(run, to_agent: Optional[str] = None) -> None:
    expect(run).transfer_was_accepted(to_agent=to_agent).verify()


def assert_no_permission_escalation(run, originating_scope: Optional[Sequence[str]] = None,
                                    originating_approvals: Optional[Sequence[str]] = None) -> None:
    expect(run).no_permission_escalation(originating_scope=originating_scope,
                                         originating_approvals=originating_approvals).verify()


def assert_no_risky_divergences(report: Any) -> None:
    expect(report).no_risky_divergences(report).verify()


def assert_replay_agreement(report: Any, min_agreement: float = 0.95) -> None:
    expect(report).min_replay_agreement(min_rate=min_agreement, report=report).verify()


def assert_max_divergence_rate(report: Any, max_rate: float = 0.05) -> None:
    expect(report).max_divergence_rate(max_rate=max_rate, report=report).verify()


def assert_serving_matches(run: Any, expected: Union[Dict[str, Any], Any]) -> None:
    expect(run).serving_matches(expected).verify()


def assert_no_stable_regressions(report: Any) -> None:
    expect(report).no_stable_regressions(report=report).verify()


def assert_serving_parity(report: Any, max_divergent_cases: int = 0) -> None:
    expect(report).max_serving_divergence(max_cases=max_divergent_cases, report=report).verify()


def assert_cost_savings(report: Any, min_savings_pct: float = 0.0) -> None:
    expect(report).cost_savings_at_least(min_savings_pct=min_savings_pct, report=report).verify()


def assert_no_changed_basis(report: Any) -> None:
    expect(report).no_changed_basis(report=report).verify()


def assert_no_superseded_evidence(report: Any) -> None:
    expect(report).no_superseded_evidence(report=report).verify()


def assert_no_wrong_entity_records(report: Any) -> None:
    expect(report).no_wrong_entity_records(report=report).verify()


def assert_evidence_parity(report: Any) -> None:
    expect(report).no_changed_basis(report=report).no_superseded_evidence(report=report).no_wrong_entity_records(report=report).verify()


def assert_no_retrieval_regressions(report: Any, max_regressions: int = 0) -> None:
    expect(report).no_retrieval_regressions(max_regressions=max_regressions, report=report).verify()


def assert_retrieval_parity(report: Any, min_delta: float = 0.0) -> None:
    expect(report).recall_improved_or_equal(min_delta=min_delta, report=report).verify()


def assert_labels_clean(report: Any) -> None:
    expect(report).labels_clean(report=report).verify()


def assert_retrieval_latency(report: Any, max_p95_ms: float, stage: str = "total") -> None:
    expect(report).latency_p95_below(max_ms=max_p95_ms, stage=stage, report=report).verify()
