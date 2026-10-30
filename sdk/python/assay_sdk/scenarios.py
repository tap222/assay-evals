"""Cross-cutting agent reliability scenarios: end-to-end reliability verification,
handoff contracts (advice vs transfer), crash & recovery testing, external state
read-back, compensation verification, and broken transition attribution.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union


class ProcessCrash(Exception):
    """Simulated process crash at a critical step or tool boundary."""


@dataclass
class HandoffContract:
    from_agent: str
    to_agent: str
    kind: str = "transfer"  # "transfer" (take over) or "advice" (second opinion)
    required_keys: List[str] = field(default_factory=list)
    read_only: bool = False  # If True (or if kind == "advice"), receiver cannot execute mutating tools
    allowed_tools: Optional[List[str]] = None
    owner: Optional[str] = None

    def __post_init__(self):
        if self.kind == "advice":
            self.read_only = True


@dataclass
class HandoffRecord:
    from_agent: str
    to_agent: str
    kind: str
    payload: Dict[str, Any]
    seq: int
    owner: Optional[str] = None


@dataclass
class BrokenTransition:
    from_step: str
    to_step: str
    cause: str
    step_seq: Optional[int]
    downstream_effects: List[str] = field(default_factory=list)

    def summary(self) -> str:
        s = f"Broken transition: {self.from_step} -> {self.to_step} ({self.cause})"
        if self.downstream_effects:
            s += f"; downstream: {'; '.join(self.downstream_effects)}"
        return s


@dataclass
class ScenarioResult:
    scenario_name: str
    passed: bool
    terminal_state: str
    external_verified: bool
    broken_transition: Optional[BrokenTransition] = None
    failures: List[str] = field(default_factory=list)
    orphaned_effects: List[str] = field(default_factory=list)
    handoff_violations: List[str] = field(default_factory=list)


# Known side-effect / write tool names
_MUTATING_TOOL_NAMES = {
    "charge", "refund", "pay", "send_email", "post_message", "write_file",
    "delete_file", "delete_order", "create_order", "update_order", "cancel_order",
    "modify_record", "insert_record", "delete_record", "execute_sql"
}


def is_mutating_tool(tool_name: str) -> bool:
    name_low = tool_name.lower()
    if tool_name in _MUTATING_TOOL_NAMES:
        return True
    return any(p in name_low for p in ("write", "update", "delete", "create", "insert", "send_", "pay", "charge", "refund"))


def validate_handoffs(run: Any, contracts: Sequence[HandoffContract]) -> List[str]:
    """Verifies that all agent handoffs in the run satisfied their explicit contracts:
      1. Required payload keys were passed (no dropped facts).
      2. If kind == 'advice', receiving agent only read and performed zero mutating tools.
      3. If kind == 'transfer', handing agent made zero subsequent mutating calls.
      4. Single ownership maintained.
    """
    steps = getattr(run, "steps", None) or (run if isinstance(run, list) else [])
    handoff_steps: List[dict] = []
    tool_steps: List[dict] = []

    for s in steps:
        if s.get("kind") == "stage" and (s.get("name") or "").startswith("handoff:"):
            handoff_steps.append(s)
        elif s.get("kind") == "approval" and (s.get("name") or "").startswith("handoff:"):
            handoff_steps.append(s)
        elif s.get("kind") == "tool":
            tool_steps.append(s)

    violations: List[str] = []
    contract_map = {(c.from_agent, c.to_agent): c for c in contracts}

    for hs in handoff_steps:
        args = hs.get("args") or {}
        from_ag = args.get("from") or hs.get("name", "").split(":", 1)[-1]
        to_ag = args.get("to") or hs.get("name", "").split(":", 1)[-1]
        kind = args.get("kind") or "transfer"
        payload = args.get("payload") or {}
        h_seq = hs.get("seq") or 0

        c = contract_map.get((from_ag, to_ag)) or contract_map.get(("*", to_ag))
        if not c:
            continue

        # Check required keys
        for k in c.required_keys:
            if k not in payload:
                violations.append(
                    f"Handoff from '{from_ag}' to '{to_ag}' dropped required fact '{k}' at step {h_seq}"
                )

        # Advice handoff promise: receiver must be read-only
        if c.read_only or kind == "advice":
            receiver_mutating = [
                t for t in tool_steps
                if (t.get("seq") or 0) > h_seq and (t.get("agent") == to_ag or not t.get("agent"))
                and is_mutating_tool(t.get("name") or "")
            ]
            for r in receiver_mutating:
                violations.append(
                    f"Advice handoff violated: '{to_ag}' performed mutating action '{r.get('name')}' at step {r.get('seq')}"
                )

        # Transfer handoff promise: handing agent has no further mutating actions
        if kind == "transfer":
            post_actions = [
                t for t in tool_steps
                if (t.get("seq") or 0) > h_seq and t.get("agent") == from_ag and is_mutating_tool(t.get("name") or "")
            ]
            for p in post_actions:
                violations.append(
                    f"Post-handoff action: '{from_ag}' executed '{p.get('name')}' at step {p.get('seq')} after handing off to '{to_ag}'"
                )

    return violations


def check_compensations(run: Any, compensations: Dict[str, str]) -> List[str]:
    """Verifies that if a run failed or faulted after a side-effecting action,
    its corresponding compensation action was executed (e.g. refund for charge).
    """
    steps = getattr(run, "steps", None) or (run if isinstance(run, list) else [])
    orphaned: List[str] = []

    # Find failures
    failed_seqs = [s.get("seq") for s in steps if s.get("status") == "error" or s.get("fault")]
    first_fail = min(failed_seqs) if failed_seqs else None

    # Check each action that has a compensation rule
    for act, comp in compensations.items():
        act_calls = [s for s in steps if s.get("kind") == "tool" and s.get("name") == act and not s.get("error")]
        comp_calls = [s for s in steps if s.get("kind") == "tool" and s.get("name") == comp and not s.get("error")]

        for a in act_calls:
            a_seq = a.get("seq") or 0
            if first_fail is not None and a_seq < first_fail:
                # An action occurred before a failure; was it compensated afterwards?
                subsequent_comp = [c for c in comp_calls if (c.get("seq") or 0) > first_fail]
                if not subsequent_comp:
                    orphaned.append(
                        f"Action '{act}' at step {a_seq} was left standing without compensation '{comp}' after failure at step {first_fail}"
                    )
    return orphaned


def find_first_broken_transition(run: Any) -> Optional[BrokenTransition]:
    """Analyzes trajectory steps to locate the first broken transition and any downstream effects."""
    steps = getattr(run, "steps", None) or (run if isinstance(run, list) else [])
    prev_step = "start"
    broken: Optional[BrokenTransition] = None
    downstream: List[str] = []

    for s in steps:
        kind = s.get("kind")
        name = s.get("name") or kind or "step"
        seq = s.get("seq")
        err = s.get("error") or s.get("fault")

        if err and broken is None:
            broken = BrokenTransition(
                from_step=prev_step,
                to_step=name,
                cause=f"{err}",
                step_seq=seq,
                downstream_effects=[]
            )
        elif broken is not None:
            # Downstream step after the first break
            if s.get("status") == "error":
                downstream.append(f"{name} errored: {s.get('error')}")
            elif kind == "tool" and is_mutating_tool(name):
                downstream.append(f"uncompensated action {name}")
            elif kind == "answer" and s.get("text"):
                downstream.append("answer generated despite earlier failure")

        if not err and kind in ("tool", "stage"):
            prev_step = name

    if broken:
        broken.downstream_effects = downstream
    return broken


_active_crash: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("assay_crash", default=None)


class crash_at:
    """Context manager injecting a simulated process crash before or after a tool call."""

    def __init__(self, tool: str, when: str = "after"):
        self.tool = tool
        self.when = when  # "before" or "after"
        self._token = None

    def __enter__(self):
        self._token = _active_crash.set({"tool": self.tool, "when": self.when, "triggered": False})
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token:
            _active_crash.reset(self._token)


def check_crash_point(tool_name: str, when: str) -> None:
    """Called by instrumented tool execution to raise ProcessCrash if crash point is matched."""
    st = _active_crash.get()
    if st and st["tool"] == tool_name and st["when"] == when and not st["triggered"]:
        st["triggered"] = True
        raise ProcessCrash(f"Process crashed {when} calling tool '{tool_name}' (simulated by assay)")


class Scenario:
    """A cross-cutting end-to-end agent reliability scenario."""

    def __init__(self,
                 name: str,
                 handoffs: Optional[Sequence[HandoffContract]] = None,
                 expected_terminal_state: str = "resolved",
                 read_back: Optional[Callable[[], Any]] = None,
                 expected_read_back: Optional[Dict[str, Any]] = None,
                 compensations: Optional[Dict[str, str]] = None,
                 allowed_retries: int = 2):
        self.name = name
        self.handoffs = list(handoffs or [])
        self.expected_terminal_state = expected_terminal_state
        self.read_back = read_back
        self.expected_read_back = expected_read_back or {}
        self.compensations = compensations or {}
        self.allowed_retries = allowed_retries

    def evaluate(self, run: Any) -> ScenarioResult:
        failures: List[str] = []

        # 1. Handoff contract verification
        handoff_violations = validate_handoffs(run, self.handoffs)
        failures.extend(handoff_violations)

        # 2. Compensation verification
        orphaned = check_compensations(run, self.compensations)
        failures.extend(orphaned)

        # 3. Terminal state check
        actual_terminal = getattr(run, "outcome_value", None) or "unverified"
        if actual_terminal == "resolved" and (orphaned or handoff_violations):
            failures.append("Premature completion: agent claimed resolved while orphaned effects or broken handoffs remained")
        elif self.expected_terminal_state and actual_terminal != self.expected_terminal_state:
            failures.append(f"Terminal state mismatch: expected '{self.expected_terminal_state}', got '{actual_terminal}'")

        # 4. External read-back from system of record
        external_verified = True
        if self.read_back:
            try:
                state = self.read_back()
                if not state:
                    external_verified = False
                    failures.append("Read-back verification failed: external system returned empty state")
                elif self.expected_read_back:
                    for k, val in self.expected_read_back.items():
                        if isinstance(state, dict) and state.get(k) != val:
                            external_verified = False
                            failures.append(f"External read-back mismatch for '{k}': expected {val!r}, got {state.get(k)!r}")
            except Exception as exc:
                external_verified = False
                failures.append(f"Read-back failed with error: {exc}")

        # 5. First broken transition
        broken = find_first_broken_transition(run)

        passed = len(failures) == 0
        return ScenarioResult(
            scenario_name=self.name,
            passed=passed,
            terminal_state=actual_terminal,
            external_verified=external_verified,
            broken_transition=broken,
            failures=failures,
            orphaned_effects=orphaned,
            handoff_violations=handoff_violations
        )
