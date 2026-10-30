"""Model serving setups, serving diffs, and step-level divergence attribution.

Tracks the execution infrastructure for self-hosted and open-weight models:
- Quantization (fp16, int8, 4-bit, awq, gptq, fp8, etc.)
- Inference engine and version (vLLM, llama.cpp, Ollama, TGI, SGLang, TensorRT-LLM)
- Hardware and accelerator (A100, H100, L40S, Apple Silicon, CPU)
- Batch size, KV-cache precision, and speculative decoding

Compares test cases run across two serving setups:
- Computes latency and cost savings (e.g. "4-bit saves 60% and changes these 3 cases")
- Lists cases that gave the same answer every time on the old setup (stable) and now differ first
- Points to the exact first step that differs in multi-step agent trajectories
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union


@dataclass
class ServingSetup:
    """The infrastructure and serving configuration for an LLM call or run."""
    quantization: Optional[str] = None  # e.g. "fp16", "int8", "4-bit", "awq", "gptq", "fp8"
    engine: Optional[str] = None  # e.g. "vllm", "llama.cpp", "ollama", "tgi", "sglang"
    engine_version: Optional[str] = None  # e.g. "0.6.2", "b3920"
    hardware: Optional[str] = None  # e.g. "A100", "H100", "L40S", "Apple M3", "CPU"
    batch_size: Optional[int] = None
    kv_cache_dtype: Optional[str] = None  # e.g. "fp8", "fp16"
    tensor_parallel_size: Optional[int] = None
    speculative_model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def summary(self) -> str:
        parts = []
        if self.engine:
            eng = f"{self.engine} {self.engine_version}".strip() if self.engine_version else self.engine
            parts.append(eng)
        if self.quantization:
            parts.append(self.quantization)
        if self.hardware:
            parts.append(f"({self.hardware})")
        return " ".join(parts) if parts else "default"

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ServingSetup":
        if not d:
            return cls()
        known = {
            "quantization", "engine", "engine_version", "hardware",
            "batch_size", "kv_cache_dtype", "tensor_parallel_size", "speculative_model"
        }
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


def normalize_serving(s: Any) -> Optional[ServingSetup]:
    if s is None:
        return None
    if isinstance(s, ServingSetup):
        return s
    if isinstance(s, dict):
        return ServingSetup.from_dict(s)
    return None


@dataclass
class StepDiff:
    """The first step where an agent run diverged between two serving setups."""
    step_index: int
    step_seq: int
    kind: str  # "llm" or "tool"
    baseline_summary: str
    candidate_summary: str
    baseline_step: Dict[str, Any]
    candidate_step: Dict[str, Any]
    explanation: str


def find_first_differing_step(base_run: Any, cand_run: Any) -> Optional[StepDiff]:
    """Finds the earliest step where base_run and cand_run produced different decisions or outputs.

    In multi-step agent trajectories, this identifies the root diverging step rather than
    reporting all downstream cascade steps as independent differences.
    """
    base_steps = getattr(base_run, "steps", None) or (base_run.get("steps") if isinstance(base_run, dict) else []) or []
    cand_steps = getattr(cand_run, "steps", None) or (cand_run.get("steps") if isinstance(cand_run, dict) else []) or []

    # Filter to observable agent decisions: llm, tool, answer, state
    observable_kinds = {"llm", "tool", "answer", "state"}
    b_obs = [s for s in base_steps if s.get("kind") in observable_kinds]
    c_obs = [s for s in cand_steps if s.get("kind") in observable_kinds]

    max_len = max(len(b_obs), len(c_obs))
    for idx in range(max_len):
        if idx >= len(b_obs):
            # Candidate continued further
            c_s = c_obs[idx]
            return StepDiff(
                step_index=idx,
                step_seq=c_s.get("seq", idx),
                kind=c_s.get("kind", "step"),
                baseline_summary="(stopped/none)",
                candidate_summary=_step_summary(c_s),
                baseline_step={},
                candidate_step=c_s,
                explanation=f"Step {idx}: candidate continued with {c_s.get('kind')} where baseline stopped",
            )
        if idx >= len(c_obs):
            # Candidate stopped earlier
            b_s = b_obs[idx]
            return StepDiff(
                step_index=idx,
                step_seq=b_s.get("seq", idx),
                kind=b_s.get("kind", "step"),
                baseline_summary=_step_summary(b_s),
                candidate_summary="(stopped/none)",
                baseline_step=b_s,
                candidate_step={},
                explanation=f"Step {idx}: candidate stopped early where baseline executed {b_s.get('kind')}",
            )

        b_s = b_obs[idx]
        c_s = c_obs[idx]

        # Compare kinds
        if b_s.get("kind") != c_s.get("kind"):
            if b_s.get("kind") in ("tool", "llm") and c_s.get("kind") == "answer":
                expl = f"Step {idx}: candidate stopped early with answer where baseline called {b_s.get('kind')}"
            elif b_s.get("kind") == "answer" and c_s.get("kind") in ("tool", "llm"):
                expl = f"Step {idx}: candidate continued further with {c_s.get('kind')} where baseline answered"
            else:
                expl = f"Step {idx}: kind mismatch ({b_s.get('kind')} vs {c_s.get('kind')})"

            return StepDiff(
                step_index=idx,
                step_seq=c_s.get("seq", idx),
                kind=c_s.get("kind", "step"),
                baseline_summary=_step_summary(b_s),
                candidate_summary=_step_summary(c_s),
                baseline_step=b_s,
                candidate_step=c_s,
                explanation=expl,
            )

        kind = b_s.get("kind")
        if kind == "tool":
            if b_s.get("name") != c_s.get("name") or b_s.get("args") != c_s.get("args"):
                return StepDiff(
                    step_index=idx,
                    step_seq=c_s.get("seq", idx),
                    kind="tool",
                    baseline_summary=_step_summary(b_s),
                    candidate_summary=_step_summary(c_s),
                    baseline_step=b_s,
                    candidate_step=c_s,
                    explanation=f"Step {idx}: tool call differed (baseline: {b_s.get('name')}, candidate: {c_s.get('name')})",
                )
        elif kind == "llm":
            # Compare output text or tool_calls
            b_text = (b_s.get("text") or "").strip()
            c_text = (c_s.get("text") or "").strip()
            b_calls = b_s.get("tool_calls") or []
            c_calls = c_s.get("tool_calls") or []
            if b_text != c_text or b_calls != c_calls:
                return StepDiff(
                    step_index=idx,
                    step_seq=c_s.get("seq", idx),
                    kind="llm",
                    baseline_summary=_step_summary(b_s),
                    candidate_summary=_step_summary(c_s),
                    baseline_step=b_s,
                    candidate_step=c_s,
                    explanation=f"Step {idx}: model output changed on new serving setup",
                )
        elif kind == "answer":
            if (b_s.get("text") or "").strip() != (c_s.get("text") or "").strip():
                return StepDiff(
                    step_index=idx,
                    step_seq=c_s.get("seq", idx),
                    kind="answer",
                    baseline_summary=_step_summary(b_s),
                    candidate_summary=_step_summary(c_s),
                    baseline_step=b_s,
                    candidate_step=c_s,
                    explanation=f"Step {idx}: answer differed between setups",
                )
        elif kind == "state":
            if b_s.get("name") != c_s.get("name") or b_s.get("value") != c_s.get("value"):
                return StepDiff(
                    step_index=idx,
                    step_seq=c_s.get("seq", idx),
                    kind="state",
                    baseline_summary=_step_summary(b_s),
                    candidate_summary=_step_summary(c_s),
                    baseline_step=b_s,
                    candidate_step=c_s,
                    explanation=f"Step {idx}: state mutation differed",
                )

    return None


def _step_summary(s: Dict[str, Any]) -> str:
    kind = s.get("kind", "")
    if kind == "tool":
        name = s.get("name", "tool")
        args = s.get("args") or {}
        return f"{name}({json.dumps(args, default=str)})"
    if kind == "llm":
        t = (s.get("text") or "").strip()
        calls = s.get("tool_calls") or []
        if calls:
            call_names = [c.get("name") or c.get("tool") for c in calls]
            return f"llm -> calls:[{', '.join(str(c) for c in call_names)}]"
        return f"llm -> {t[:60]}..." if len(t) > 60 else f"llm -> {t}"
    if kind == "answer":
        t = (s.get("text") or "").strip()
        return f"answer: {t[:60]}..." if len(t) > 60 else f"answer: {t}"
    if kind == "state":
        return f"state {s.get('op', '')} {s.get('name', '')}={s.get('value')}"
    return str(s)


@dataclass
class CaseServingDiff:
    """Comparison of one test case run under baseline vs candidate serving setups."""
    case_name: str
    identical_output: bool
    was_previously_stable: bool  # True if case always produced the same answer in baseline
    first_differing_step: Optional[StepDiff]
    baseline_latency_ms: float
    candidate_latency_ms: float
    baseline_cost_usd: float
    candidate_cost_usd: float
    baseline_answer: Optional[str]
    candidate_answer: Optional[str]

    @property
    def latency_diff_ms(self) -> float:
        return self.candidate_latency_ms - self.baseline_latency_ms

    @property
    def cost_diff_usd(self) -> float:
        return self.candidate_cost_usd - self.baseline_cost_usd


@dataclass
class ServingDiffReport:
    """The aggregate comparison report across two model serving setups."""
    baseline_setup: ServingSetup
    candidate_setup: ServingSetup
    setup_changes: Dict[str, Tuple[Any, Any]]
    total_cases: int
    identical_cases_count: int
    changed_cases_count: int
    stable_regressions_count: int
    case_diffs: List[CaseServingDiff]
    baseline_total_cost_usd: float
    candidate_total_cost_usd: float
    cost_savings_pct: float
    baseline_avg_latency_ms: float
    candidate_avg_latency_ms: float
    latency_change_pct: float

    def summary(self) -> str:
        setup_desc = f"{self.candidate_setup.summary()} vs {self.baseline_setup.summary()}"
        savings_text = f"saves {self.cost_savings_pct:.1f}%" if self.cost_savings_pct >= 0 else f"+{abs(self.cost_savings_pct):.1f}% cost"
        lat_text = f"{-self.latency_change_pct:.1f}% faster" if self.latency_change_pct <= 0 else f"+{self.latency_change_pct:.1f}% slower"

        lines = [
            f"Model serving diff ({setup_desc}):",
            f"  Cost & Latency: {savings_text}, {lat_text}",
            f"  Cases: {self.changed_cases_count} changed out of {self.total_cases} total "
            f"({self.stable_regressions_count} previously stable)",
        ]

        if self.setup_changes:
            ch_str = ", ".join(f"{k}: {b} → {c}" for k, (b, c) in self.setup_changes.items())
            lines.append(f"  Infrastructure changes: {ch_str}")

        # List changed cases, with previously stable cases first
        if self.case_diffs:
            changed_cases = [c for c in self.case_diffs if not c.identical_output]
            # Stable first
            changed_cases.sort(key=lambda x: (not x.was_previously_stable, x.case_name))
            if changed_cases:
                lines.append("  Changed cases (stable cases first):")
                for cd in changed_cases[:10]:
                    tag = " [PREVIOUSLY STABLE]" if cd.was_previously_stable else ""
                    step_info = f" (first differed at step {cd.first_differing_step.step_index})" if cd.first_differing_step else ""
                    lines.append(f"    • {cd.case_name}{tag}{step_info}")
                if len(changed_cases) > 10:
                    lines.append(f"    ... and {len(changed_cases) - 10} more")

        return "\n".join(lines)


def _extract_run_cost(run: Any) -> float:
    steps = getattr(run, "steps", None) or (run.get("steps") if isinstance(run, dict) else []) or []
    cost = 0.0
    for s in steps:
        if s.get("kind") == "llm" and s.get("cost_usd") is not None:
            cost += float(s["cost_usd"])
    return cost


def _extract_run_latency(run: Any) -> float:
    steps = getattr(run, "steps", None) or (run.get("steps") if isinstance(run, dict) else []) or []
    # If ended and started on run
    started = getattr(run, "started", None)
    ended = getattr(run, "ended", None)
    if started and ended and ended > started:
        return (ended - started) * 1000.0
    # Otherwise sum step durations or latency
    total_ms = 0.0
    for s in steps:
        if s.get("duration_ms"):
            total_ms += float(s["duration_ms"])
    return total_ms or 100.0


def _extract_run_answer(run: Any) -> Optional[str]:
    steps = getattr(run, "steps", None) or (run.get("steps") if isinstance(run, dict) else []) or []
    for s in reversed(steps):
        if s.get("kind") == "answer" and s.get("text"):
            return str(s["text"])
        if s.get("kind") == "llm" and s.get("text"):
            return str(s["text"])
    return getattr(run, "answer_text", None) or (run.get("answer") if isinstance(run, dict) else None)


def _extract_run_serving(run: Any) -> ServingSetup:
    # Look at run level
    s_run = getattr(run, "serving", None) or (run.get("serving") if isinstance(run, dict) else None)
    if s_run:
        return normalize_serving(s_run) or ServingSetup()

    # Look at first llm step
    steps = getattr(run, "steps", None) or (run.get("steps") if isinstance(run, dict) else []) or []
    for s in steps:
        if s.get("kind") == "llm" and s.get("serving"):
            return normalize_serving(s["serving"]) or ServingSetup()
    return ServingSetup()


def compare_serving_setups(
    baseline_runs: Union[Sequence[Any], Dict[str, Any]],
    candidate_runs: Union[Sequence[Any], Dict[str, Any]],
    baseline_setup: Optional[Union[Dict[str, Any], ServingSetup]] = None,
    candidate_setup: Optional[Union[Dict[str, Any], ServingSetup]] = None,
    historically_stable_cases: Optional[Set[str]] = None,
) -> ServingDiffReport:
    """Compares baseline vs candidate runs under two serving setups.

    - Computes cost and latency savings ("4-bit saves 60% and changes these 3 cases").
    - Lists cases that gave the same answer every time on the old setup (stable) and now differ first.
    - Points to the first step that differs in multi-step agent trajectories.
    """
    stable_set = set(historically_stable_cases or [])

    # Map by case/task name
    def map_runs(runs: Union[Sequence[Any], Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(runs, dict):
            return runs
        out: Dict[str, Any] = {}
        for idx, r in enumerate(runs):
            name = getattr(r, "task", None) or (r.get("task") or r.get("case") if isinstance(r, dict) else None) or f"case_{idx}"
            out[name] = r
        return out

    base_map = map_runs(baseline_runs)
    cand_map = map_runs(candidate_runs)

    # Determine baseline & candidate setups
    first_b = next(iter(base_map.values()), None)
    first_c = next(iter(cand_map.values()), None)
    b_setup = normalize_serving(baseline_setup) or _extract_run_serving(first_b)
    c_setup = normalize_serving(candidate_setup) or _extract_run_serving(first_c)

    # Find setup differences
    b_dict = b_setup.to_dict()
    c_dict = c_setup.to_dict()
    all_keys = set(b_dict.keys()) | set(c_dict.keys())
    setup_changes: Dict[str, Tuple[Any, Any]] = {}
    for k in all_keys:
        val_b = b_dict.get(k)
        val_c = c_dict.get(k)
        if val_b != val_c:
            setup_changes[k] = (val_b, val_c)

    case_diffs: List[CaseServingDiff] = []
    base_total_cost = 0.0
    cand_total_cost = 0.0
    base_latencies: List[float] = []
    cand_latencies: List[float] = []
    identical_count = 0
    changed_count = 0
    stable_regressions = 0

    shared_cases = sorted(set(base_map.keys()) & set(cand_map.keys()))
    for case_name in shared_cases:
        b_run = base_map[case_name]
        c_run = cand_map[case_name]

        b_cost = _extract_run_cost(b_run)
        c_cost = _extract_run_cost(c_run)
        base_total_cost += b_cost
        cand_total_cost += c_cost

        b_lat = _extract_run_latency(b_run)
        c_lat = _extract_run_latency(c_run)
        base_latencies.append(b_lat)
        cand_latencies.append(c_lat)

        b_ans = _extract_run_answer(b_run)
        c_ans = _extract_run_answer(c_run)

        # Check for first differing step
        first_diff = find_first_differing_step(b_run, c_run)
        is_identical = (first_diff is None)

        is_stable = case_name in stable_set

        if is_identical:
            identical_count += 1
        else:
            changed_count += 1
            if is_stable:
                stable_regressions += 1

        case_diff = CaseServingDiff(
            case_name=case_name,
            identical_output=is_identical,
            was_previously_stable=is_stable,
            first_differing_step=first_diff,
            baseline_latency_ms=b_lat,
            candidate_latency_ms=c_lat,
            baseline_cost_usd=b_cost,
            candidate_cost_usd=c_cost,
            baseline_answer=b_ans,
            candidate_answer=c_ans,
        )
        case_diffs.append(case_diff)

    # Sort diffs: changed & stable cases first, then changed, then unchanged
    case_diffs.sort(key=lambda x: (
        x.identical_output,
        not x.was_previously_stable,
        x.case_name
    ))

    # Compute savings
    cost_savings = 0.0
    if base_total_cost > 0:
        cost_savings = ((base_total_cost - cand_total_cost) / base_total_cost) * 100.0

    avg_b_lat = (sum(base_latencies) / len(base_latencies)) if base_latencies else 0.0
    avg_c_lat = (sum(cand_latencies) / len(cand_latencies)) if cand_latencies else 0.0
    lat_change = 0.0
    if avg_b_lat > 0:
        lat_change = ((avg_c_lat - avg_b_lat) / avg_b_lat) * 100.0

    return ServingDiffReport(
        baseline_setup=b_setup,
        candidate_setup=c_setup,
        setup_changes=setup_changes,
        total_cases=len(shared_cases),
        identical_cases_count=identical_count,
        changed_cases_count=changed_count,
        stable_regressions_count=stable_regressions,
        case_diffs=case_diffs,
        baseline_total_cost_usd=base_total_cost,
        candidate_total_cost_usd=cand_total_cost,
        cost_savings_pct=cost_savings,
        baseline_avg_latency_ms=avg_b_lat,
        candidate_avg_latency_ms=avg_c_lat,
        latency_change_pct=lat_change,
    )
