"""Core security evaluations: autonomous security flags, gate analysis, reach expansion,
identity violations, runtime drift, and access auditing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from assay_sdk.scopes import (
    compare_agent_reach,
    profile_agent_reach,
    scan_security_flags,
)
from assay_sdk.production_security import (
    AgentBaseline,
    audit_access_held,
    check_identity_violations,
    detect_runtime_drift,
    learn_agent_baseline,
)


def security_violations_from_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scans candidate runs for security violations (critical/high security flags,
    markdown exfiltration, secret leakage, destructive commands, privilege escalation).
    """
    items: List[Dict[str, Any]] = []
    seen: set = set()

    for r in runs:
        case_name = r.get("case") or r.get("name") or "unknown"
        steps = r.get("steps") or []
        flags = scan_security_flags(steps)

        for f in flags:
            if f.severity in ("critical", "high"):
                key = f"security:{f.category}:{f.tool}:{f.target}"
                if key in seen:
                    continue
                seen.add(key)
                text = f"Security violation: {f.reason} ({f.tool})"
                detail = f"severity: {f.severity} · category: {f.category} · target: {f.target}"
                items.append({
                    "text": text,
                    "detail": detail,
                    "key": key,
                    "severity": f.severity,
                    "category": f.category,
                    "cases": [case_name],
                })

    return items


def detect_widened_reach(baseline_runs: List[Dict[str, Any]],
                         candidate_runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detects whether candidate runs widened the agent's tool reach compared to baseline
    (e.g. gained delete, spending money, messaging, or outside network reach).
    """
    def _extract_tools(runs: List[Dict[str, Any]]) -> List[str]:
        tools = set()
        for r in runs:
            # from offered tools in llm steps
            for s in r.get("steps") or []:
                for t in s.get("tools") or []:
                    tools.add(t)
                if s.get("kind") == "tool" and s.get("name"):
                    tools.add(s["name"])
        return sorted(tools)

    base_tools = _extract_tools(baseline_runs)
    cand_tools = _extract_tools(candidate_runs)

    widened = compare_agent_reach(base_tools, cand_tools)
    items: List[Dict[str, Any]] = []
    for w in widened:
        items.append({
            "text": w,
            "detail": "widened agent tool reach",
            "key": f"reach:{w}",
            "cases": [],
        })

    return items


def identity_violations_from_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scans candidate runs for identity and credential violations:
    cross-user credentials, missing owners, unidentified agents.
    """
    violations = check_identity_violations(runs)
    items: List[Dict[str, Any]] = []
    seen: set = set()

    for v in violations:
        key = f"identity:{v.kind}:{v.tool or ''}:{v.reason}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "text": f"Identity violation: {v.reason}",
            "detail": f"kind: {v.kind} · severity: {v.severity}",
            "key": key,
            "severity": v.severity,
            "kind": v.kind,
            "cases": [v.run_id] if v.run_id else [],
        })

    return items


def drift_alerts_from_runs(
    baseline_or_runs: Any,
    candidate_runs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detects runtime drift in candidate runs against baseline history:
    new hosts, new tables, read-to-write operations, volume drainage anomalies.
    """
    if isinstance(baseline_or_runs, AgentBaseline):
        baseline = baseline_or_runs
    else:
        baseline = learn_agent_baseline(baseline_or_runs)

    alerts = detect_runtime_drift(baseline, candidate_runs)
    items: List[Dict[str, Any]] = []
    seen: set = set()

    for a in alerts:
        key = f"drift:{a.kind}:{a.target}:{a.reason}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "text": f"Runtime drift: {a.reason}",
            "detail": f"kind: {a.kind} · target: {a.target} · severity: {a.severity}",
            "key": key,
            "severity": a.severity,
            "kind": a.kind,
            "cases": [a.run_id] if a.run_id else [],
        })

    return items


def unused_access_from_runs(
    agent_permissions: Dict[str, Any],
    runs: List[Dict[str, Any]],
    inactive_days: int = 30,
    active_owners: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Audits over-permissioned access and inactive agents/owners."""
    report = audit_access_held(agent_permissions, runs, inactive_days=inactive_days, active_owners=active_owners)
    items: List[Dict[str, Any]] = []

    if report.unused_tools:
        items.append({
            "text": f"Unused permissions: {len(report.unused_tools)} tools inactive for {inactive_days}+ days",
            "detail": f"agent: {report.agent_id} · tools: {', '.join(report.unused_tools[:4])}",
            "key": f"unused_tools:{report.agent_id}",
            "cases": [],
        })

    if report.is_inactive_agent:
        items.append({
            "text": f"Inactive agent holding access: '{report.agent_id}' has no runs in {inactive_days}+ days",
            "detail": f"granted {len(report.granted_tools)} tools",
            "key": f"inactive_agent:{report.agent_id}",
            "cases": [],
        })

    if report.is_owner_inactive:
        items.append({
            "text": f"Orphaned agent: owner '{report.owner}' is inactive or no longer signs in",
            "detail": f"agent: {report.agent_id}",
            "key": f"inactive_owner:{report.agent_id}",
            "cases": [],
        })

    return items
