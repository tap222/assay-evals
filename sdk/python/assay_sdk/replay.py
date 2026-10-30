"""Replay recorded runs decision by decision against a candidate model, prompt or tool router.

Gives the new setup the recorded history up to each decision point and compares its
next decision (the tool, its arguments, or stopping) with the recorded one.
Tools are never re-executed: recorded results are fed back, so nothing is charged,
sent or deleted.

Divergences are classified by kind (another tool, different arguments, stopped early,
went further), weighted by risk (write/spend vs read), and scored against run outcome
and case checks (better, worse, or unclear).
Runs whose tool definitions changed since they were recorded are set apart.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Verbs associated with financial charges, refunds, or spend
_SPEND_VERBS = (
    "charge", "refund", "pay", "transfer", "purchase", "bill", "invoice", "cost", "payout"
)

# Verbs associated with database or state mutations
_WRITE_VERBS = (
    "write", "update", "delete", "drop", "insert", "modify", "cancel", "destroy",
    "patch", "execute_sql", "alter", "create", "remove", "save", "flush"
)

# Verbs associated with external messaging / side-effects
_MESSAGE_VERBS = (
    "send", "post", "email", "sms", "notify", "publish", "call", "webhook"
)


def classify_tool_risk(tool_name: Optional[str]) -> Tuple[str, bool]:
    """Classify risk level of a tool call.
    Returns (weight, is_write_or_spend).
    write/spend actions have weight "high"; read-only actions have weight "low".
    """
    if not tool_name:
        return ("low", False)
    name_low = tool_name.lower().replace("-", "_")
    for verb in _SPEND_VERBS + _WRITE_VERBS + _MESSAGE_VERBS:
        if name_low.startswith(f"{verb}_") or name_low == verb or f"_{verb}_" in name_low or f".{verb}" in name_low:
            return ("high", True)
    return ("low", False)


@dataclass
class Decision:
    """An agent's decision at a particular step."""
    action: str  # "tool" or "stop"
    tool: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    answer: Optional[str] = None
    confidence: Optional[float] = None
    raw: Any = None

    @classmethod
    def call(cls, tool: str, args: Optional[Dict[str, Any]] = None, **kwargs) -> "Decision":
        return cls(action="tool", tool=tool, args=args or {}, **kwargs)

    @classmethod
    def stop(cls, answer: Optional[str] = None, **kwargs) -> "Decision":
        return cls(action="stop", answer=answer, **kwargs)

    def is_tool(self) -> bool:
        return self.action == "tool"

    def is_stop(self) -> bool:
        return self.action == "stop"

    def to_dict(self) -> Dict[str, Any]:
        d = {"action": self.action}
        if self.tool is not None:
            d["tool"] = self.tool
        if self.args is not None:
            d["args"] = self.args
        if self.answer is not None:
            d["answer"] = self.answer
        return d


@dataclass
class DecisionDivergence:
    """A divergence point between recorded decision and candidate decision."""
    step_seq: int
    step_index: int
    recorded_decision: Decision
    candidate_decision: Decision
    kind: str  # "another_tool", "different_args", "stopped_early", "went_further"
    weight: str  # "high" (write/spend), "low" (read)
    is_write_or_spend: bool
    score: str  # "better", "worse", "unclear"
    detail: str
    run_id: Optional[str] = None
    case_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_seq": self.step_seq,
            "step_index": self.step_index,
            "recorded_decision": self.recorded_decision.to_dict(),
            "candidate_decision": self.candidate_decision.to_dict(),
            "kind": self.kind,
            "weight": self.weight,
            "is_write_or_spend": self.is_write_or_spend,
            "score": self.score,
            "detail": self.detail,
            "run_id": self.run_id,
            "case_name": self.case_name,
        }


@dataclass
class RunReplayResult:
    """Result of replaying a single run decision by decision."""
    run_id: str
    case_name: Optional[str]
    total_decision_points: int
    matching_decisions: int
    divergences: List[DecisionDivergence]
    tool_definitions_changed: bool
    tool_diffs: List[str]
    verdict: str  # "identical", "better", "worse", "unclear", "definitions_drift"

    @property
    def agreement_rate(self) -> float:
        if self.total_decision_points == 0:
            return 1.0
        return self.matching_decisions / self.total_decision_points


@dataclass
class ReplayReport:
    """Aggregated report across all replayed runs."""
    total_runs: int
    results: List[RunReplayResult]
    divergences: List[DecisionDivergence]
    divergences_by_kind: Dict[str, int]
    write_spend_divergences: List[DecisionDivergence]
    read_divergences: List[DecisionDivergence]
    runs_with_changed_tool_definitions: List[RunReplayResult]
    scores: Dict[str, int]
    agreement_rate: float

    def has_risky_divergences(self) -> bool:
        return len(self.write_spend_divergences) > 0 or self.scores.get("worse", 0) > 0

    def summary(self) -> str:
        lines = [
            f"Replay decision-by-decision: {self.total_runs} run(s) replayed",
            f"  Overall agreement rate: {self.agreement_rate * 100:.1f}%",
            f"  Divergences: {len(self.divergences)} total "
            f"({self.divergences_by_kind.get('another_tool', 0)} another tool, "
            f"{self.divergences_by_kind.get('different_args', 0)} different args, "
            f"{self.divergences_by_kind.get('stopped_early', 0)} stopped early, "
            f"{self.divergences_by_kind.get('went_further', 0)} went further)",
            f"  Risk weighting: {len(self.write_spend_divergences)} write/spend (HIGH), "
            f"{len(self.read_divergences)} read (LOW)",
            f"  Scoring: {self.scores.get('better', 0)} better, "
            f"{self.scores.get('worse', 0)} worse, {self.scores.get('unclear', 0)} unclear, "
            f"{self.scores.get('identical', 0)} identical runs",
        ]
        if self.runs_with_changed_tool_definitions:
            lines.append(
                f"  Tool definitions changed: {len(self.runs_with_changed_tool_definitions)} run(s) set apart"
            )
        return "\n".join(lines)


def check_tool_definitions_drift(
    recorded_tools: Optional[Any],
    candidate_tools: Optional[Any]
) -> Tuple[bool, List[str]]:
    """Compares candidate's tool definitions against what was recorded.
    If tools were added, removed, or their schemas altered, returns (True, diffs).
    """
    if recorded_tools is None and candidate_tools is None:
        return False, []
    if recorded_tools is None or candidate_tools is None:
        return True, ["Tool definitions set changed between None and declared"]

    # Normalize to dict of name -> schema/definition
    def normalize_defs(defs: Any) -> Dict[str, Any]:
        if isinstance(defs, dict):
            return defs
        if isinstance(defs, (list, tuple)):
            out = {}
            for item in defs:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool")
                    if name:
                        out[name] = item
                elif isinstance(item, str):
                    out[item] = {"name": item}
            return out
        return {}

    rec_map = normalize_defs(recorded_tools)
    cand_map = normalize_defs(candidate_tools)

    diffs: List[str] = []
    for name in cand_map:
        if name not in rec_map:
            diffs.append(f"Tool '{name}' added in candidate")
        elif cand_map[name] != rec_map[name]:
            diffs.append(f"Tool '{name}' definition altered")

    for name in rec_map:
        if name not in cand_map:
            diffs.append(f"Tool '{name}' removed in candidate")

    return (len(diffs) > 0), diffs


def _normalize_candidate_output(out: Any) -> Decision:
    """Converts various candidate return types into a standard Decision."""
    if isinstance(out, Decision):
        return out
    if isinstance(out, dict):
        if "tool" in out:
            return Decision.call(tool=out["tool"], args=out.get("args") or {})
        if "action" in out and out["action"] == "tool":
            return Decision.call(tool=out.get("name") or out.get("tool", ""), args=out.get("args") or {})
        if "answer" in out or out.get("action") == "stop":
            return Decision.stop(answer=out.get("answer") or out.get("text"))
    if isinstance(out, (list, tuple)) and len(out) >= 2:
        return Decision.call(tool=str(out[0]), args=out[1] if isinstance(out[1], dict) else {})
    if isinstance(out, str):
        return Decision.stop(answer=out)
    return Decision.stop(answer=str(out) if out is not None else None)


def _compare_decisions(
    rec: Decision,
    cand: Decision,
    step_seq: int,
    step_index: int,
    run_outcome: Optional[str] = None,
    checks_passed: bool = True,
    run_id: Optional[str] = None,
    case_name: Optional[str] = None,
    evaluator: Optional[Callable[[DecisionDivergence], str]] = None,
) -> Optional[DecisionDivergence]:
    """Compares recorded decision with candidate decision."""
    if rec.action == cand.action:
        if rec.is_tool():
            if rec.tool == cand.tool:
                # Compare args
                rec_args = rec.args or {}
                cand_args = cand.args or {}
                if rec_args == cand_args:
                    return None  # Exact match!
                kind = "different_args"
                detail = f"Same tool '{rec.tool}', different args: recorded={rec_args} vs candidate={cand_args}"
            else:
                kind = "another_tool"
                detail = f"Different tool chosen: candidate picked '{cand.tool}' instead of recorded '{rec.tool}'"
        else:
            # Both stopped
            return None
    elif rec.is_tool() and cand.is_stop():
        kind = "stopped_early"
        detail = f"Candidate stopped early with answer '{cand.answer}' where recorded called '{rec.tool}'"
    elif rec.is_stop() and cand.is_tool():
        kind = "went_further"
        detail = f"Candidate called tool '{cand.tool}' where recorded run stopped/answered"
    else:
        kind = "another_tool"
        detail = f"Decision action mismatch: {cand.action} vs recorded {rec.action}"

    # Determine risk weighting
    target_tool = cand.tool or rec.tool
    weight, is_write_spend = classify_tool_risk(target_tool)

    # Score against outcome and checks
    score = "unclear"
    if evaluator:
        div_candidate = DecisionDivergence(
            step_seq=step_seq,
            step_index=step_index,
            recorded_decision=rec,
            candidate_decision=cand,
            kind=kind,
            weight=weight,
            is_write_or_spend=is_write_spend,
            score="unclear",
            detail=detail,
            run_id=run_id,
            case_name=case_name,
        )
        try:
            score = evaluator(div_candidate)
        except Exception:
            score = "unclear"
    else:
        # Default scoring heuristic
        if is_write_spend:
            if kind in ("another_tool", "different_args") and (run_outcome == "resolved" or checks_passed):
                # Diverging on a write/spend tool when original succeeded is risky
                score = "worse"
            elif kind == "stopped_early" and (run_outcome == "resolved" or checks_passed):
                score = "worse"
            elif run_outcome in ("unresolved", "error", "failed") or not checks_passed:
                score = "better"
            else:
                score = "unclear"
        else:
            if run_outcome in ("unresolved", "error", "failed") or not checks_passed:
                score = "better"
            elif kind == "stopped_early":
                score = "worse" if (run_outcome == "resolved" or checks_passed) else "better"
            elif kind == "went_further":
                score = "worse" if (run_outcome == "resolved") else "unclear"
            else:
                score = "unclear"

    return DecisionDivergence(
        step_seq=step_seq,
        step_index=step_index,
        recorded_decision=rec,
        candidate_decision=cand,
        kind=kind,
        weight=weight,
        is_write_or_spend=is_write_spend,
        score=score,
        detail=detail,
        run_id=run_id,
        case_name=case_name,
    )


def extract_decision_points(run_or_dict: Any) -> Tuple[Any, List[Dict[str, Any]], Optional[str], bool, Optional[Any]]:
    """Extracts (initial_input, decision_steps, outcome, checks_passed, tool_definitions) from run."""
    steps = getattr(run_or_dict, "steps", None)
    if steps is None and isinstance(run_or_dict, dict):
        steps = run_or_dict.get("steps") or []
    steps = list(steps or [])

    inp = getattr(run_or_dict, "request", None) or (run_or_dict.get("input") if isinstance(run_or_dict, dict) else None)
    outcome = getattr(run_or_dict, "outcome_value", None) or (run_or_dict.get("outcome_value") if isinstance(run_or_dict, dict) else None)
    tool_defs = getattr(run_or_dict, "tool_definitions", None) or (run_or_dict.get("tool_definitions") if isinstance(run_or_dict, dict) else None)

    # Filter steps that represent agent decisions: tools and answers
    decision_steps = [s for s in steps if s.get("kind") in ("tool", "answer")]

    checks_passed = True
    if hasattr(run_or_dict, "checks"):
        checks = run_or_dict.checks
        checks_passed = not any(c.get("status") in ("fail", "error") for c in checks)
    elif isinstance(run_or_dict, dict) and "checks" in run_or_dict:
        checks_passed = not any(c.get("status") in ("fail", "error") for c in run_or_dict["checks"])

    return inp, decision_steps, outcome, checks_passed, tool_defs


def replay_decision_by_decision(
    runs: Union[Any, Sequence[Any]],
    candidate_agent: Callable[..., Any],
    candidate_tool_definitions: Optional[Any] = None,
    evaluator: Optional[Callable[[DecisionDivergence], str]] = None,
) -> ReplayReport:
    """Replays recorded runs step by step against a candidate agent setup.

    For each step:
      1. History up to this step (input and prior recorded tool results) is provided.
      2. The candidate predicts its next decision.
      3. Tools are NOT executed again; the decision is compared against the recorded one.
      4. Divergences are classified and scored.
    """
    runs_list = list(runs) if isinstance(runs, (list, tuple, set)) else [runs]
    all_results: List[RunReplayResult] = []
    all_divergences: List[DecisionDivergence] = []
    divergences_by_kind: Dict[str, int] = {
        "another_tool": 0,
        "different_args": 0,
        "stopped_early": 0,
        "went_further": 0,
    }
    write_spend_divergences: List[DecisionDivergence] = []
    read_divergences: List[DecisionDivergence] = []
    runs_with_changed_defs: List[RunReplayResult] = []
    score_counts: Dict[str, int] = {"better": 0, "worse": 0, "unclear": 0, "identical": 0}

    total_decisions_fleet = 0
    total_matches_fleet = 0

    for r in runs_list:
        run_id = getattr(r, "id", None) or (r.get("run_id") or r.get("id") if isinstance(r, dict) else "unknown_run")
        case_name = getattr(r, "task", None) or (r.get("task") or r.get("case") if isinstance(r, dict) else None)

        inp, decision_steps, outcome, checks_passed, rec_tool_defs = extract_decision_points(r)

        # Check tool definitions drift
        has_defs_drift, tool_diffs = check_tool_definitions_drift(rec_tool_defs, candidate_tool_definitions)

        history: List[Dict[str, Any]] = []
        run_divergences: List[DecisionDivergence] = []
        matching_count = 0

        for idx, step in enumerate(decision_steps):
            step_seq = step.get("seq", idx)
            step_kind = step.get("kind")

            if step_kind == "tool":
                recorded_decision = Decision.call(tool=step.get("name", ""), args=step.get("args") or {})
            else:
                recorded_decision = Decision.stop(answer=step.get("text"))

            # Query candidate for its decision given history up to this step
            candidate_raw = None
            try:
                candidate_raw = candidate_agent(
                    history=list(history),
                    input=inp,
                    step_index=idx,
                    tool_definitions=candidate_tool_definitions,
                )
            except TypeError:
                try:
                    candidate_raw = candidate_agent(list(history), inp, idx, candidate_tool_definitions)
                except TypeError:
                    try:
                        candidate_raw = candidate_agent(list(history), inp, idx)
                    except TypeError:
                        try:
                            candidate_raw = candidate_agent(list(history))
                        except TypeError:
                            candidate_raw = candidate_agent(inp)

            candidate_decision = _normalize_candidate_output(candidate_raw)

            # Compare decisions
            div = _compare_decisions(
                rec=recorded_decision,
                cand=candidate_decision,
                step_seq=step_seq,
                step_index=idx,
                run_outcome=outcome,
                checks_passed=checks_passed,
                run_id=run_id,
                case_name=case_name,
                evaluator=evaluator,
            )

            if div is None:
                matching_count += 1
            else:
                run_divergences.append(div)
                all_divergences.append(div)
                divergences_by_kind[div.kind] = divergences_by_kind.get(div.kind, 0) + 1
                if div.is_write_or_spend:
                    write_spend_divergences.append(div)
                else:
                    read_divergences.append(div)
                score_counts[div.score] = score_counts.get(div.score, 0) + 1

            # Advance history with RECORDED result so tools are never re-executed!
            if step_kind == "tool":
                history.append({
                    "role": "assistant",
                    "tool": step.get("name"),
                    "args": step.get("args"),
                })
                history.append({
                    "role": "tool",
                    "name": step.get("name"),
                    "result": step.get("result"),
                    "error": step.get("error"),
                })
            else:
                history.append({
                    "role": "assistant",
                    "answer": step.get("text"),
                })

        total_points = len(decision_steps)
        total_decisions_fleet += total_points
        total_matches_fleet += matching_count

        # Compute per-run verdict
        if has_defs_drift:
            verdict = "definitions_drift"
        elif not run_divergences:
            verdict = "identical"
            score_counts["identical"] = score_counts.get("identical", 0) + 1
        elif any(d.score == "worse" for d in run_divergences):
            verdict = "worse"
        elif any(d.score == "better" for d in run_divergences):
            verdict = "better"
        else:
            verdict = "unclear"

        run_result = RunReplayResult(
            run_id=run_id,
            case_name=case_name,
            total_decision_points=total_points,
            matching_decisions=matching_count,
            divergences=run_divergences,
            tool_definitions_changed=has_defs_drift,
            tool_diffs=tool_diffs,
            verdict=verdict,
        )
        all_results.append(run_result)

        if has_defs_drift:
            runs_with_changed_defs.append(run_result)

    fleet_agreement = (total_matches_fleet / total_decisions_fleet) if total_decisions_fleet > 0 else 1.0

    return ReplayReport(
        total_runs=len(runs_list),
        results=all_results,
        divergences=all_divergences,
        divergences_by_kind=divergences_by_kind,
        write_spend_divergences=write_spend_divergences,
        read_divergences=read_divergences,
        runs_with_changed_tool_definitions=runs_with_changed_defs,
        scores=score_counts,
        agreement_rate=fleet_agreement,
    )
