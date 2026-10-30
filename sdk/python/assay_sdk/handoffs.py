"""Multi-agent handoffs: advice vs transfer contracts, single-ownership enforcement,
scope & approvals containment (no permission by message), fact propagation checking,
and hierarchical multi-agent tree diffing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Side-effecting / mutating tool verbs
_MUTATING_VERBS = (
    "write", "update", "delete", "create", "insert", "drop", "refund", "charge",
    "pay", "send", "post", "cancel", "modify", "patch", "remove", "destroy", "execute_sql",
    "notify", "publish", "call_customer", "call_client", "transfer_funds"
)


def is_mutating(tool_name: str) -> bool:
    """Return True if tool_name implies a mutating or side-effecting action."""
    name_low = tool_name.lower().replace("-", "_")
    for verb in _MUTATING_VERBS:
        if name_low.startswith(f"{verb}_") or name_low == verb:
            return True
        if f".{verb}_" in name_low or f".{verb}" in name_low or f"_{verb}_" in name_low:
            return True
    return False


@dataclass
class HandoffContract:
    """Explicit contract governing an agent-to-agent handoff."""
    from_agent: str
    to_agent: str
    kind: str = "transfer"  # "transfer" (take over) or "advice" (second opinion)
    required_keys: List[str] = field(default_factory=list)  # Facts that MUST be passed
    read_only: bool = False  # If True (always True for advice), receiver must not mutate
    allowed_tools: Optional[List[str]] = None  # Narrowed tool whitelist for receiver
    allowed_approvals: Optional[List[str]] = None  # Approvals allowed to carry over
    requires_acceptance: bool = True  # Receiver must explicitly accept transfer
    require_findings: bool = True  # Advice receiver must return findings
    owner: Optional[str] = None

    def __post_init__(self):
        if self.kind == "advice":
            self.read_only = True


# Alias for explicit multi-agent scenarios
AgentHandoffContract = HandoffContract


@dataclass
class AgentTreeNode:
    """A node in a multi-agent execution hierarchy."""
    agent_id: str
    run_id: Optional[str] = None
    kind: str = "root"  # "root", "advice", "transfer"
    owner: Optional[str] = None
    accepted: Optional[bool] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    actions: List[str] = field(default_factory=list)
    outcome: Optional[str] = None
    children: List["AgentTreeNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "owner": self.owner,
            "accepted": self.accepted,
            "payload": self.payload,
            "actions": self.actions,
            "outcome": self.outcome,
            "children": [c.to_dict() for c in self.children],
        }


def _extract_steps(run_or_runs: Any) -> List[Dict[str, Any]]:
    if not run_or_runs:
        return []
    if isinstance(run_or_runs, list):
        steps = []
        for r in run_or_runs:
            if hasattr(r, "steps"):
                steps.extend(r.steps)
            elif isinstance(r, dict) and "steps" in r:
                steps.extend(r["steps"])
            elif isinstance(r, dict):
                steps.append(r)
        return steps
    if hasattr(run_or_runs, "steps"):
        return list(run_or_runs.steps)
    if isinstance(run_or_runs, dict):
        return list(run_or_runs.get("steps") or [])
    return []


def validate_agent_handoffs(
    run_or_runs: Any,
    contracts: Optional[Sequence[HandoffContract]] = None,
    originating_scope: Optional[Sequence[str]] = None,
    originating_approvals: Optional[Sequence[str]] = None,
) -> List[str]:
    """Strict verification of agent-to-agent handoffs:
    1. Advice: receiving agent only reads. Any mutating action or message fails.
       Advice must also return findings to caller.
    2. Take over: receiving agent explicitly accepted; originator makes no post-handoff mutating actions;
       unaccepted or unfinished handoffs are flagged.
    3. No permission by message: receiving agent cannot exceed human-granted originating scope,
       cannot execute outside narrowed contract scope, and cannot borrow unapproved permissions.
    4. Fact propagation: required keys in payload must not be dropped.
    """
    steps = _extract_steps(run_or_runs)
    violations: List[str] = []

    contracts_list = list(contracts or [])
    contract_map = {(c.from_agent, c.to_agent): c for c in contracts_list}
    wildcard_map = {c.to_agent: c for c in contracts_list if c.from_agent == "*"}

    handoff_steps = [
        s for s in steps
        if (s.get("kind") in ("stage", "approval") and str(s.get("name", "")).startswith("handoff:"))
    ]
    tool_steps = [s for s in steps if s.get("kind") == "tool"]

    for hs in handoff_steps:
        args = hs.get("args") or {}
        from_ag = args.get("from") or hs.get("name", "").split(":", 1)[-1]
        to_ag = args.get("to") or hs.get("name", "").split(":", 1)[-1]
        kind = args.get("kind") or "transfer"
        payload = args.get("payload") or {}
        h_seq = hs.get("seq") or 0
        accepted = args.get("accepted")

        contract = contract_map.get((from_ag, to_ag)) or wildcard_map.get(to_ag)
        if contract is None:
            # Create dynamic contract based on handoff declaration
            contract = HandoffContract(
                from_agent=from_ag,
                to_agent=to_ag,
                kind=kind,
                allowed_tools=args.get("allowed_tools"),
                allowed_approvals=args.get("allowed_approvals"),
            )

        # 1. Fact propagation (What was handed over)
        req_keys = contract.required_keys if contract else []
        for k in req_keys:
            if k not in payload:
                violations.append(
                    f"Handoff from '{from_ag}' to '{to_ag}' dropped required fact '{k}' at step {h_seq}"
                )

        # Steps taken by receiver after handoff
        receiver_tools = [
            t for t in tool_steps
            if (t.get("seq") or 0) > h_seq and (t.get("agent") == to_ag or not t.get("agent"))
        ]

        # 2. Advice promise: strictly read-only and return findings
        if kind == "advice" or (contract and contract.kind == "advice"):
            for t in receiver_tools:
                t_name = t.get("name") or ""
                if is_mutating(t_name):
                    violations.append(
                        f"Advice handoff violated: '{to_ag}' performed mutating action '{t_name}' at step {t.get('seq')}"
                    )

            # Advice must return findings
            if contract and contract.require_findings:
                # Check if there is an answer, return stage, or tool result
                has_findings = any(
                    ((s.get("kind") in ("answer", "stage", "tool") and (s.get("seq") or 0) > h_seq)
                     and (s.get("agent") == to_ag or "findings" in str(s.get("name", "")).lower() or s.get("kind") == "answer"))
                    for s in steps
                )
                if not has_findings and not receiver_tools:
                    violations.append(
                        f"Advice handoff to '{to_ag}' returned no findings to caller"
                    )

        # 3. Take over (transfer) promise: acceptance, completion, and single ownership
        if kind == "transfer" or (contract and contract.kind == "transfer"):
            # Explicit acceptance check
            accept_step = next(
                (s for s in steps
                 if s.get("kind") in ("tool", "stage")
                 and (s.get("name") == f"handoff_accept:{from_ag}" or "accept" in str(s.get("name", "")).lower())
                 and (s.get("seq") or 0) > h_seq),
                None
            )
            has_accepted = (accepted is True) or (
                accept_step is not None and (accept_step.get("args") or {}).get("accepted", True) is True
            )
            was_explicitly_rejected = (accepted is False) or (
                accept_step is not None and (accept_step.get("args") or {}).get("accepted") is False
            )

            if was_explicitly_rejected or (contract and contract.requires_acceptance and not has_accepted):
                violations.append(
                    f"Takeover handoff to '{to_ag}' was never accepted (task left unaccepted)"
                )
            else:
                # Check if accepted handoff was left unfinished
                has_actions = len(receiver_tools) > 0 or any(
                    (s.get("kind") in ("answer", "state") and (s.get("seq") or 0) > h_seq)
                    for s in steps
                )
                if not has_actions:
                    violations.append(
                        f"Takeover handoff to '{to_ag}' was accepted but left unfinished (no actions or outcome produced)"
                    )

            # Single ownership: originating agent makes zero subsequent mutating actions
            post_handing_mutating = [
                t for t in tool_steps
                if (t.get("seq") or 0) > h_seq and t.get("agent") == from_ag and is_mutating(t.get("name") or "")
            ]
            for p in post_handing_mutating:
                violations.append(
                    f"Post-handoff action: '{from_ag}' executed '{p.get('name')}' at step {p.get('seq')} "
                    f"after handing off to '{to_ag}'"
                )

        # 4. No permission by message: containment to human-granted scope
        # Check against human-granted originating scope
        if originating_scope is not None:
            scope_set = set(originating_scope)
            for t in receiver_tools:
                t_name = t.get("name") or ""
                if t_name not in scope_set:
                    violations.append(
                        f"Permission escalation by message: '{to_ag}' called '{t_name}' at step {t.get('seq')} "
                        f"which exceeds originating scope granted by human"
                    )

        # Check against narrowed contract scope
        if contract and contract.allowed_tools is not None:
            allowed_set = set(contract.allowed_tools)
            for t in receiver_tools:
                t_name = t.get("name") or ""
                if t_name not in allowed_set:
                    violations.append(
                        f"Scope containment violation: '{to_ag}' called '{t_name}' outside narrowed handoff contract"
                    )

        # Check approval containment
        if originating_approvals is not None or (contract and contract.allowed_approvals is not None):
            allowed_app_set = set(contract.allowed_approvals if (contract and contract.allowed_approvals is not None) else originating_approvals or [])
            # Find any approval step claimed under this handoff
            sub_approvals = [
                s for s in steps
                if s.get("kind") == "approval" and (s.get("seq") or 0) > h_seq
            ]
            for sa in sub_approvals:
                act = sa.get("name") or ""
                if act and act not in allowed_app_set:
                    violations.append(
                        f"Approval leakage: approval for '{act}' was used in handoff to '{to_ag}' "
                        f"without contract authorization"
                    )

    return violations


def build_agent_tree(run_or_runs: Any) -> AgentTreeNode:
    """Builds a hierarchical execution tree of the run and its sub-agents
    linked by handoffs and parent_run_id.
    """
    if isinstance(run_or_runs, list) and run_or_runs:
        runs = list(run_or_runs)
        # Find root: either first run, or the one with parent_run_id is None
        main_run = next((r for r in runs if not getattr(r, "parent_run_id", None) and not (isinstance(r, dict) and r.get("parent_run_id"))), runs[0])
        sub_runs = [r for r in runs if r is not main_run]
    else:
        main_run = run_or_runs
        sub_runs = []

    root_name = getattr(main_run, "agent_id", None) or getattr(main_run, "task", None) or (main_run.get("agent_id") if isinstance(main_run, dict) else None) or "root_agent"
    root_owner = getattr(main_run, "owner", None) or (main_run.get("owner") if isinstance(main_run, dict) else None)
    root_outcome = getattr(main_run, "outcome_value", None) or (main_run.get("outcome_value") if isinstance(main_run, dict) else None)
    root_id = getattr(main_run, "id", None) or (main_run.get("id") if isinstance(main_run, dict) else None)

    steps = getattr(main_run, "steps", None) or (main_run.get("steps") if isinstance(main_run, dict) else []) or []
    root_actions: List[str] = []
    handoff_nodes: List[AgentTreeNode] = []

    # Map sub runs by parent_run_id
    sub_runs_by_parent: Dict[str, List[Any]] = {}
    for sr in sub_runs:
        pid = getattr(sr, "parent_run_id", None) or (sr.get("parent_run_id") if isinstance(sr, dict) else None)
        if pid:
            sub_runs_by_parent.setdefault(pid, []).append(sr)

    for s in steps:
        if s.get("kind") == "tool" and s.get("name"):
            root_actions.append(s["name"])
        elif s.get("kind") == "stage" and str(s.get("name", "")).startswith("handoff:"):
            args = s.get("args") or {}
            to_ag = args.get("to") or s.get("name", "").split(":", 1)[-1]
            kind = args.get("kind") or "transfer"
            payload = args.get("payload") or {}
            accepted = args.get("accepted")

            # Check if there is an explicit sub-run matching this agent
            matching_sub = next(
                (sr for sr in sub_runs
                 if (getattr(sr, "agent_id", None) or (sr.get("agent_id") if isinstance(sr, dict) else None)) == to_ag),
                None
            )

            if matching_sub:
                sub_tree = build_agent_tree([matching_sub] + [r for r in sub_runs if r is not matching_sub])
                sub_tree.kind = kind
                sub_tree.accepted = accepted
                sub_tree.payload = payload
                handoff_nodes.append(sub_tree)
            else:
                child = AgentTreeNode(
                    agent_id=to_ag,
                    kind=kind,
                    owner=args.get("owner"),
                    accepted=accepted,
                    payload=payload,
                    actions=[],
                    outcome=None,
                )
                handoff_nodes.append(child)

    # Also attach any sub-runs by parent_run_id not yet linked
    if root_id and root_id in sub_runs_by_parent:
        linked_agents = {c.agent_id for c in handoff_nodes}
        for sr in sub_runs_by_parent[root_id]:
            sr_agent = getattr(sr, "agent_id", None) or (sr.get("agent_id") if isinstance(sr, dict) else None) or "sub_agent"
            if sr_agent not in linked_agents:
                sub_tree = build_agent_tree([sr] + [r for r in sub_runs if r is not sr])
                handoff_nodes.append(sub_tree)

    root = AgentTreeNode(
        agent_id=root_name,
        run_id=root_id,
        kind="root",
        owner=root_owner,
        actions=root_actions,
        outcome=root_outcome,
        children=handoff_nodes,
    )
    return root


def render_agent_tree(node: AgentTreeNode, prefix: str = "", is_last: bool = True) -> str:
    """Renders an ASCII tree visualization of the multi-agent hierarchy."""
    lines = []
    marker = "└── " if is_last else "├── "
    kind_label = f"[{node.kind}]"
    accepted_label = " (accepted)" if node.accepted is True else (" (unaccepted)" if node.accepted is False else "")
    lines.append(f"{prefix}{marker}{node.agent_id} {kind_label}{accepted_label}")

    child_prefix = prefix + ("    " if is_last else "│   ")
    for act in node.actions:
        lines.append(f"{child_prefix}• {act}")

    for i, child in enumerate(node.children):
        last_child = (i == len(node.children) - 1)
        lines.append(render_agent_tree(child, child_prefix, last_child))

    return "\n".join(lines)


def diff_agent_trees(base_tree: AgentTreeNode, cand_tree: AgentTreeNode) -> List[Dict[str, Any]]:
    """Compares baseline vs candidate multi-agent trees to pinpoint architectural changes:
    - Added or removed sub-agents
    - Handoff kind changes (transfer vs advice)
    - Actions performed by sub-agents (especially mutating actions)
    - Fact differences in payloads
    - Acceptance state changes
    """
    diffs: List[Dict[str, Any]] = []

    base_children = {c.agent_id: c for c in base_tree.children}
    cand_children = {c.agent_id: c for c in cand_tree.children}

    # Added sub-agents
    for ag_id, cand_node in cand_children.items():
        if ag_id not in base_children:
            diffs.append({
                "text": f"Multi-agent tree: added sub-agent '{ag_id}' ({cand_node.kind})",
                "detail": f"actions: {', '.join(cand_node.actions) if cand_node.actions else 'none'}",
                "key": f"handoff_added:{ag_id}",
            })
        else:
            base_node = base_children[ag_id]
            # Kind changed
            if base_node.kind != cand_node.kind:
                diffs.append({
                    "text": f"Multi-agent tree: handoff to '{ag_id}' changed from {base_node.kind} to {cand_node.kind}",
                    "detail": "handoff promise changed",
                    "key": f"handoff_kind:{ag_id}",
                })

            # Acceptance changed
            if base_node.accepted != cand_node.accepted:
                diffs.append({
                    "text": f"Multi-agent tree: handoff to '{ag_id}' acceptance changed from {base_node.accepted} to {cand_node.accepted}",
                    "detail": "acceptance state changed",
                    "key": f"handoff_accepted:{ag_id}",
                })

            # Actions changed
            base_actions = set(base_node.actions)
            cand_actions = set(cand_node.actions)
            new_actions = [a for a in cand_node.actions if a not in base_actions]
            removed_actions = [a for a in base_node.actions if a not in cand_actions]
            if new_actions:
                diffs.append({
                    "text": f"Multi-agent tree: sub-agent '{ag_id}' added actions: {', '.join(new_actions)}",
                    "detail": "new sub-agent actions",
                    "key": f"handoff_new_actions:{ag_id}",
                })
            if removed_actions:
                diffs.append({
                    "text": f"Multi-agent tree: sub-agent '{ag_id}' removed actions: {', '.join(removed_actions)}",
                    "detail": "removed sub-agent actions",
                    "key": f"handoff_removed_actions:{ag_id}",
                })

            # Fact payload differences
            missing_keys = [k for k in base_node.payload if k not in cand_node.payload]
            if missing_keys:
                diffs.append({
                    "text": f"Multi-agent tree: handoff to '{ag_id}' dropped facts: {', '.join(missing_keys)}",
                    "detail": "fact propagation dropped",
                    "key": f"handoff_dropped_facts:{ag_id}",
                })

    # Removed sub-agents
    for ag_id in base_children:
        if ag_id not in cand_children:
            diffs.append({
                "text": f"Multi-agent tree: removed sub-agent '{ag_id}'",
                "detail": "sub-agent no longer called",
                "key": f"handoff_removed:{ag_id}",
            })

    return diffs
