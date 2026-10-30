"""Agent security in production: identity tracking, runtime drift against history,
access held but unused auditing/trimming, and action audits with rubber-stamp detection.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

# Common regex patterns for extracting tables and mutating queries
_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+[`\"'\[]?([a-zA-Z0-9_]+)[`\"'\]]?",
    re.I,
)
_MUTATING_SQL_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE\s+|DELETE\s+FROM|DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE\s+TABLE|ALTER\s+TABLE)\b",
    re.I,
)
_MUTATING_VERBS = (
    "create", "update", "delete", "insert", "drop", "refund", "charge", "pay",
    "write", "modify", "patch", "remove", "destroy", "cancel", "post", "send_money",
)
_READ_VERBS = (
    "get", "read", "fetch", "search", "list", "describe", "find", "select", "scan", "lookup", "query", "check",
)


def _ts(dt: Optional[Union[datetime, str]]) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        clean = dt.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def hash_arguments(args: Any) -> str:
    """Computes a canonical SHA-256 hash of arguments for binding to approvals."""
    if args is None:
        args = {}
    normalized = json.dumps(args, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ============================================================================
# 1. Identity Tracking
# ============================================================================

@dataclass
class AgentIdentity:
    agent_id: str
    owner: Optional[str] = None
    requested_by: Optional[str] = None
    credential_id: Optional[str] = None
    credential_type: Optional[str] = None  # e.g. "user_oauth", "service_account", "api_key", "agent_dedicated"
    credential_owner: Optional[str] = None  # e.g. "alice"


@dataclass
class IdentityViolation:
    kind: str  # "cross_user_credential", "missing_owner", "unidentified_agent"
    reason: str
    agent_id: Optional[str] = None
    tool: Optional[str] = None
    seq: Optional[int] = None
    run_id: Optional[str] = None
    severity: str = "high"


def _normalize_runs(run_or_runs: Any) -> List[Dict[str, Any]]:
    if not run_or_runs:
        return []
    if isinstance(run_or_runs, (list, tuple)):
        items = list(run_or_runs)
    else:
        items = [run_or_runs]

    out = []
    for item in items:
        if hasattr(item, "steps"):  # Run instance
            out.append({
                "id": getattr(item, "id", None),
                "agent_id": getattr(item, "agent_id", None),
                "owner": getattr(item, "owner", None),
                "requested_by": getattr(item, "requested_by", None),
                "credential": getattr(item, "credential", None),
                "credential_type": getattr(item, "credential_type", None),
                "credential_owner": getattr(item, "credential_owner", None),
                "steps": getattr(item, "steps", []),
                "outcome": getattr(item, "outcome_value", None),
                "answer_text": getattr(item, "answer_text", None),
            })
        elif isinstance(item, dict):
            out.append(item)
    return out


def check_identity_violations(run_or_runs: Any) -> List[IdentityViolation]:
    """Inspects run(s) for identity and credential violations:
    1. Cross-user credential usage (acting with a person's credential on another user's request).
    2. Missing owner on record.
    3. Unidentified agent.
    """
    runs = _normalize_runs(run_or_runs)
    violations: List[IdentityViolation] = []

    for r in runs:
        run_id = r.get("id") or r.get("run_id")
        agent_id = r.get("agent_id")
        owner = r.get("owner")
        run_req = r.get("requested_by") or r.get("user")
        run_cred = r.get("credential")
        run_cred_type = r.get("credential_type")
        run_cred_owner = r.get("credential_owner")

        # Flag missing agent identity
        if not agent_id:
            violations.append(IdentityViolation(
                kind="unidentified_agent",
                reason=f"Agent action in run '{run_id or 'unknown'}' has no agent identity on record",
                run_id=run_id,
                severity="high",
            ))

        # Flag missing owner
        if not owner:
            violations.append(IdentityViolation(
                kind="missing_owner",
                reason=f"Agent '{agent_id or 'unknown'}' has no owner on record",
                agent_id=agent_id,
                run_id=run_id,
                severity="high",
            ))

        # Check tool steps
        for step in r.get("steps") or []:
            if step.get("kind") != "tool":
                continue
            tool_name = step.get("name") or "unknown"
            seq = step.get("seq")
            cred_owner = step.get("credential_owner") or run_cred_owner
            step_req = step.get("requested_by") or run_req
            cred_type = step.get("credential_type") or run_cred_type

            # Check cross-user delegation
            if cred_owner and step_req and cred_owner != step_req:
                violations.append(IdentityViolation(
                    kind="cross_user_credential",
                    reason=(
                        f"Agent used person's credential ({cred_owner}) on someone else's request "
                        f"({step_req}) for tool '{tool_name}' at step {seq}"
                    ),
                    agent_id=agent_id,
                    tool=tool_name,
                    seq=seq,
                    run_id=run_id,
                    severity="critical",
                ))
            elif cred_type in ("user_oauth", "personal_access_token") and cred_owner is None and step_req:
                # If credential type indicates a user credential without an explicit credential_owner
                pass

    return violations


def compute_identity_metrics(run_or_runs: Any) -> Dict[str, Any]:
    """Computes fleet identity metrics:
    - share of agent actions with a known owner
    - share of agent actions with an agent identity
    - cross-user credential actions count
    - unowned actions count
    """
    runs = _normalize_runs(run_or_runs)
    total_actions = 0
    actions_with_owner = 0
    actions_with_identity = 0
    cross_user_actions = 0
    unowned_actions = 0

    violations = check_identity_violations(runs)

    for r in runs:
        has_owner = bool(r.get("owner"))
        has_identity = bool(r.get("agent_id"))
        run_cred_owner = r.get("credential_owner")
        run_req = r.get("requested_by") or r.get("user")

        for s in r.get("steps") or []:
            if s.get("kind") == "tool":
                total_actions += 1
                if has_owner:
                    actions_with_owner += 1
                else:
                    unowned_actions += 1

                if has_identity:
                    actions_with_identity += 1

                step_cred_owner = s.get("credential_owner") or run_cred_owner
                step_req = s.get("requested_by") or run_req
                if step_cred_owner and step_req and step_cred_owner != step_req:
                    cross_user_actions += 1

    return {
        "total_actions": total_actions,
        "actions_with_known_owner": actions_with_owner,
        "actions_with_agent_identity": actions_with_identity,
        "known_owner_share": (actions_with_owner / total_actions) if total_actions > 0 else 1.0,
        "agent_identity_share": (actions_with_identity / total_actions) if total_actions > 0 else 1.0,
        "cross_user_credential_actions": cross_user_actions,
        "unowned_actions": unowned_actions,
        "violations": violations,
    }


# ============================================================================
# 2. Runtime Drift Against History
# ============================================================================

@dataclass
class DriftAlert:
    agent_id: str
    kind: str  # "new_host", "new_table", "read_to_write", "new_tenant", "volume_spike"
    target: str
    reason: str
    seq: Optional[int] = None
    run_id: Optional[str] = None
    severity: str = "high"


@dataclass
class AgentBaseline:
    agent_id: str
    tools: Set[str] = field(default_factory=set)
    hosts: Set[str] = field(default_factory=set)
    tables: Set[str] = field(default_factory=set)
    read_only_tools: Set[str] = field(default_factory=set)
    write_tools: Set[str] = field(default_factory=set)
    tenants: Set[str] = field(default_factory=set)
    max_records_per_run: int = 50
    volume_multiplier: float = 3.0

    @classmethod
    def from_runs(cls, run_or_runs: Any, agent_id: Optional[str] = None) -> "AgentBaseline":
        return learn_agent_baseline(run_or_runs, agent_id=agent_id)


def _extract_host_from_tool_name(tool_name: str) -> Optional[str]:
    if not tool_name:
        return None
    if tool_name.startswith("HTTP "):
        # Format: "HTTP GET api.stripe.com/v1/..."
        parts = tool_name.split()
        if len(parts) >= 3:
            raw = parts[2]
            return raw.split("/")[0].split("?")[0].lower()
    elif tool_name.startswith("SOCKET "):
        # Format: "SOCKET db.internal:5432"
        parts = tool_name.split()
        if len(parts) >= 2:
            raw = parts[1]
            return raw.split(":")[0].lower()
    return None


def _extract_tables_from_step(step: Dict[str, Any]) -> Set[str]:
    tables: Set[str] = set()
    tool_name = (step.get("name") or "").lower()
    args = step.get("args") or {}

    # 1. State step
    if step.get("kind") == "state":
        st_name = step.get("name") or ""
        if ":" in st_name:
            tables.add(st_name.split(":")[0].lower())
        elif st_name:
            tables.add(st_name.lower())

    # 2. Tool args
    for key in ("table", "entity", "collection", "model"):
        val = args.get(key)
        if val and isinstance(val, str):
            tables.add(val.lower())

    # 3. SQL query inside args
    for key in ("sql", "query", "statement"):
        val = args.get(key)
        if val and isinstance(val, str):
            for m in _SQL_TABLE_RE.findall(val):
                tables.add(m.lower())

    # 4. Tool name heuristics (e.g. read_customers, update_orders)
    for prefix in ("read_", "get_", "update_", "delete_", "list_", "insert_", "search_"):
        if tool_name.startswith(prefix):
            remainder = tool_name[len(prefix):]
            if remainder and not remainder.startswith("by_"):
                tables.add(remainder.lower())

    return tables


def _is_write_operation(step: Dict[str, Any]) -> bool:
    if step.get("kind") == "state":
        return step.get("op") in ("create", "update", "delete")

    if step.get("kind") == "tool":
        tool_name = (step.get("name") or "").lower()
        args = step.get("args") or {}

        # Verb match
        for v in _MUTATING_VERBS:
            if tool_name.startswith(f"{v}_") or tool_name == v:
                return True

        # SQL match
        for key in ("sql", "query", "statement"):
            val = args.get(key)
            if val and isinstance(val, str) and _MUTATING_SQL_RE.search(val):
                return True

    return False


def _is_read_operation(step: Dict[str, Any]) -> bool:
    if step.get("kind") == "tool":
        tool_name = (step.get("name") or "").lower()
        if _is_write_operation(step):
            return False
        for v in _READ_VERBS:
            if tool_name.startswith(f"{v}_") or tool_name == v:
                return True
        return True
    return False


def _extract_tenants_from_step(step: Dict[str, Any]) -> Set[str]:
    tenants: Set[str] = set()
    args = step.get("args") or {}
    if isinstance(args, dict):
        for k in ("customer_id", "tenant_id", "org_id", "account_id"):
            v = args.get(k)
            if v is not None:
                tenants.add(str(v))
    return tenants


def _extract_record_count_from_step(step: Dict[str, Any]) -> int:
    result = step.get("result")
    args = step.get("args") or {}

    count = 0
    if isinstance(result, list):
        count = max(count, len(result))
    elif isinstance(result, dict):
        for k in ("count", "total", "records_count", "rows"):
            if isinstance(result.get(k), int):
                count = max(count, result[k])
        for k in ("records", "items", "results", "rows", "customers", "orders"):
            if isinstance(result.get(k), list):
                count = max(count, len(result[k]))

    # Also check query limit in args
    if isinstance(args, dict):
        for k in ("limit", "count", "batch_size"):
            if isinstance(args.get(k), int) and args[k] > count:
                count = args[k]

    return count


def learn_agent_baseline(run_or_runs: Any, agent_id: Optional[str] = None) -> AgentBaseline:
    """Learns normal operational boundaries from historical runs:
    - Allowed tools
    - Known outside hosts
    - Accessed tables
    - Tools/entities that were only ever read
    - Known tenants
    - Normal query volume
    """
    runs = _normalize_runs(run_or_runs)
    tools: Set[str] = set()
    hosts: Set[str] = set()
    tables: Set[str] = set()
    read_tools: Set[str] = set()
    write_tools: Set[str] = set()
    tenants: Set[str] = set()
    max_records = 0

    determined_agent_id = agent_id

    for r in runs:
        if not determined_agent_id and r.get("agent_id"):
            determined_agent_id = r.get("agent_id")

        run_records = 0
        for s in r.get("steps") or []:
            if s.get("kind") != "tool" and s.get("kind") != "state":
                continue

            tool_name = s.get("name") or ""
            if tool_name:
                tools.add(tool_name)

            h = _extract_host_from_tool_name(tool_name)
            if h:
                hosts.add(h)

            tbs = _extract_tables_from_step(s)
            tables.update(tbs)

            if _is_write_operation(s):
                write_tools.add(tool_name)
                # Mark tables written
                for tb in tbs:
                    write_tools.add(f"table_write:{tb}")
            elif _is_read_operation(s):
                read_tools.add(tool_name)
                for tb in tbs:
                    read_tools.add(f"table_read:{tb}")

            tenants.update(_extract_tenants_from_step(s))
            run_records += _extract_record_count_from_step(s)

        if run_records > max_records:
            max_records = run_records

    # Read-only tools are those used for reads that were never in write_tools
    read_only = {t for t in read_tools if t not in write_tools}

    return AgentBaseline(
        agent_id=determined_agent_id or "default",
        tools=tools,
        hosts=hosts,
        tables=tables,
        read_only_tools=read_only,
        write_tools=write_tools,
        tenants=tenants,
        max_records_per_run=max(max_records, 10),
    )


def detect_runtime_drift(
    baseline: AgentBaseline,
    run_or_runs: Any,
) -> List[DriftAlert]:
    """Compares candidate run(s) against baseline history and alerts immediately on:
    - Contacting a new host not seen in history
    - Accessing a new database table not seen in history
    - Read-to-write drift (attempting writes where history only read)
    - Accessing customer/tenant records outside baseline
    - Volume drainage anomaly (draining massive data in a single run)
    """
    runs = _normalize_runs(run_or_runs)
    alerts: List[DriftAlert] = []

    for r in runs:
        run_id = r.get("id") or r.get("run_id")
        agent_id = r.get("agent_id") or baseline.agent_id
        run_records = 0

        for s in r.get("steps") or []:
            if s.get("kind") != "tool" and s.get("kind") != "state":
                continue

            tool_name = s.get("name") or ""
            seq = s.get("seq")

            # 1. New host drift
            host = _extract_host_from_tool_name(tool_name)
            if host and baseline.hosts and host not in baseline.hosts:
                alerts.append(DriftAlert(
                    agent_id=agent_id,
                    kind="new_host",
                    target=host,
                    reason=f"Agent contacted new host '{host}' not seen in history",
                    seq=seq,
                    run_id=run_id,
                    severity="critical",
                ))

            # 2. New table drift
            tables = _extract_tables_from_step(s)
            if baseline.tables:
                for tb in tables:
                    if tb not in baseline.tables:
                        alerts.append(DriftAlert(
                            agent_id=agent_id,
                            kind="new_table",
                            target=tb,
                            reason=f"Agent accessed new table '{tb}' not seen in history",
                            seq=seq,
                            run_id=run_id,
                            severity="high",
                        ))

            # 3. Read-to-write drift
            if _is_write_operation(s):
                # Check if tool itself was previously read-only
                is_drift = False
                reason = ""
                if tool_name in baseline.read_only_tools:
                    is_drift = True
                    reason = f"Read-to-write drift: tool '{tool_name}' previously only performed reads"
                else:
                    # Check if the tables touched were only ever read
                    for tb in tables:
                        if f"table_read:{tb}" in baseline.read_only_tools and f"table_write:{tb}" not in baseline.write_tools:
                            is_drift = True
                            reason = f"Read-to-write drift: attempted write on table '{tb}' where history only read"
                            break

                # Or if baseline has no write tools at all (a pure read-only agent attempting a write)
                if not baseline.write_tools and not is_drift and tool_name not in baseline.write_tools:
                    is_drift = True
                    reason = f"Read-to-write drift: read-only agent attempted mutating action '{tool_name}'"

                if is_drift:
                    alerts.append(DriftAlert(
                        agent_id=agent_id,
                        kind="read_to_write",
                        target=tool_name,
                        reason=reason,
                        seq=seq,
                        run_id=run_id,
                        severity="critical",
                    ))

            # 4. New tenant / customer drift
            step_tenants = _extract_tenants_from_step(s)
            if baseline.tenants:
                for tenant in step_tenants:
                    if tenant not in baseline.tenants:
                        alerts.append(DriftAlert(
                            agent_id=agent_id,
                            kind="new_tenant",
                            target=tenant,
                            reason=f"Agent reached customer/tenant '{tenant}' outside historical baseline",
                            seq=seq,
                            run_id=run_id,
                            severity="high",
                        ))

            # 5. Volume drainage tracking
            run_records += _extract_record_count_from_step(s)

        volume_threshold = max(
            int(baseline.max_records_per_run * baseline.volume_multiplier),
            baseline.max_records_per_run + 50,
        )
        if run_records >= volume_threshold and run_records >= 100:
            alerts.append(DriftAlert(
                agent_id=agent_id,
                kind="volume_spike",
                target=str(run_records),
                reason=(
                    f"Volume drainage anomaly: agent queried {run_records} records in a single run "
                    f"(baseline max: {baseline.max_records_per_run}, threshold: {volume_threshold})"
                ),
                run_id=run_id,
                severity="high",
            ))

    return alerts


# ============================================================================
# 3. Access Held but Unused (Audit & Trimming)
# ============================================================================

@dataclass
class AccessAuditReport:
    agent_id: str
    owner: Optional[str]
    granted_tools: List[str]
    used_tools: List[str]
    unused_tools: List[str]
    unused_access_share: float
    is_inactive_agent: bool
    is_owner_inactive: bool
    recommended_permissions: List[str]
    summary: str


def audit_access_held(
    agent_permissions: Dict[str, Any],
    run_or_runs: Any,
    inactive_days: int = 30,
    reference_time: Optional[datetime] = None,
    active_owners: Optional[Set[str]] = None,
) -> AccessAuditReport:
    """Audits granted tools and scopes against actual usage in the last 30 days:
    - Lists tools granted but never invoked (over-permissioned)
    - Flags agents with no runs in 30+ days that still hold access
    - Flags agents whose owner is inactive or no longer signs in
    - Generates recommended trimmed policy (least privilege)
    """
    runs = _normalize_runs(run_or_runs)
    agent_id = agent_permissions.get("agent_id") or "unknown"
    owner = agent_permissions.get("owner")
    granted = list(agent_permissions.get("granted_tools") or agent_permissions.get("tools") or [])

    ref_time = reference_time or datetime.now(timezone.utc)
    cutoff = ref_time - timedelta(days=inactive_days)

    active_runs = 0
    invoked_tools: Set[str] = set()

    for r in runs:
        # Check agent match if specified
        r_agent = r.get("agent_id")
        if r_agent and r_agent != agent_id:
            continue

        # Check timestamp
        ts = _ts(r.get("ts") or r.get("created_at") or (r.get("steps", [{}])[0].get("ts") if r.get("steps") else None))
        if ts and ts < cutoff:
            continue

        active_runs += 1
        for s in r.get("steps") or []:
            if s.get("kind") == "tool" and s.get("name"):
                invoked_tools.add(s["name"])

    used_tools = [t for t in granted if t in invoked_tools]
    unused_tools = [t for t in granted if t not in invoked_tools]
    unused_share = (len(unused_tools) / len(granted)) if granted else 0.0

    is_inactive_agent = (active_runs == 0 and len(granted) > 0)
    is_owner_inactive = bool(active_owners is not None and owner and owner not in active_owners)

    recommended = [t for t in granted if t in invoked_tools]

    parts = []
    if unused_tools:
        parts.append(f"{len(unused_tools)} unused tools ({', '.join(unused_tools[:3])}{'...' if len(unused_tools)>3 else ''})")
    if is_inactive_agent:
        parts.append(f"no runs in {inactive_days} days")
    if is_owner_inactive:
        parts.append(f"owner '{owner}' is inactive")

    summary = f"Agent '{agent_id}': " + ("; ".join(parts) if parts else "access healthy and aligned with usage")

    return AccessAuditReport(
        agent_id=agent_id,
        owner=owner,
        granted_tools=granted,
        used_tools=used_tools,
        unused_tools=unused_tools,
        unused_access_share=round(unused_share, 4),
        is_inactive_agent=is_inactive_agent,
        is_owner_inactive=is_owner_inactive,
        recommended_permissions=recommended,
        summary=summary,
    )


def audit_all_agents_access(
    agent_registry: List[Dict[str, Any]],
    run_or_runs: Any,
    inactive_days: int = 30,
    active_owners: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Audits access fleet-wide across all registered agents and computes team metric."""
    reports = [
        audit_access_held(agent_info, run_or_runs, inactive_days=inactive_days, active_owners=active_owners)
        for agent_info in agent_registry
    ]
    agents_with_unused = [r for r in reports if r.unused_tools or r.is_inactive_agent]
    total = len(reports)
    share_with_unused = (len(agents_with_unused) / total) if total > 0 else 0.0
    total_unused_count = sum(len(r.unused_tools) for r in reports)

    return {
        "total_agents": total,
        "agents_with_unused_access": len(agents_with_unused),
        "share_agents_with_unused_access": round(share_with_unused, 4),
        "total_unused_permissions_count": total_unused_count,
        "reports": reports,
    }


# ============================================================================
# 4. Action Audits & Rubber-Stamp Approval Detection
# ============================================================================

@dataclass
class ActionAuditRecord:
    run_id: str
    action: str
    arguments: Dict[str, Any]
    amount: Optional[float] = None
    agent_id: Optional[str] = None
    owner: Optional[str] = None
    requested_by: Optional[str] = None
    approved_by: Optional[str] = None
    time_to_approve_ms: Optional[float] = None
    rule: Optional[str] = None
    evidence: Optional[str] = None
    outcome: Optional[str] = None
    timestamp: Optional[str] = None


def _extract_amount(args: Dict[str, Any]) -> Optional[float]:
    if not isinstance(args, dict):
        return None
    for k in ("amount", "total", "refund_amount", "price", "cost", "usd", "value"):
        v = args.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                cleaned = re.sub(r"[^\d.-]", "", v)
                if cleaned:
                    return float(cleaned)
            except Exception:
                pass
    return None


def query_action_audit(
    run_or_runs: Any,
    action: Optional[str] = None,
    min_amount: Optional[float] = None,
    since: Optional[Union[datetime, str]] = None,
    until: Optional[Union[datetime, str]] = None,
    agent_id: Optional[str] = None,
    requested_by: Optional[str] = None,
) -> List[ActionAuditRecord]:
    """Queries actions across runs answering:
    'What did this agent do, and who approved it?' in a minute.
    Extracts every action matching criteria, along with approver, time-to-approve,
    evidence, and outcome.
    """
    runs = _normalize_runs(run_or_runs)
    records: List[ActionAuditRecord] = []

    dt_since = _ts(since)
    dt_until = _ts(until)

    for r in runs:
        r_agent = r.get("agent_id")
        if agent_id and r_agent != agent_id:
            continue

        r_req = r.get("requested_by") or r.get("user")
        if requested_by and r_req != requested_by:
            continue

        run_id = r.get("id") or r.get("run_id") or "unknown"
        owner = r.get("owner")
        outcome = r.get("outcome")

        steps = r.get("steps") or []

        # Find approval steps in this run indexed by action
        approvals_by_action: Dict[str, Dict[str, Any]] = {}
        for s in steps:
            if s.get("kind") == "approval" and s.get("decision") == "approved":
                act_name = s.get("name")
                if act_name:
                    approvals_by_action[act_name] = s

        for s in steps:
            if s.get("kind") != "tool":
                continue

            act = s.get("name") or "unknown"
            if action and act != action and not act.endswith(f".{action}"):
                continue

            step_ts_raw = s.get("ts")
            step_ts = _ts(step_ts_raw)
            if dt_since and step_ts and step_ts < dt_since:
                continue
            if dt_until and step_ts and step_ts > dt_until:
                continue

            args = s.get("args") or {}
            amt = _extract_amount(args)
            if min_amount is not None:
                if amt is None or amt < min_amount:
                    continue

            # Correlate approval
            app_step = approvals_by_action.get(act)
            approved_by = app_step.get("by") if app_step else s.get("approved_by")
            time_to_approve = app_step.get("time_to_approve_ms") if app_step else s.get("time_to_approve_ms")
            rule = app_step.get("rule") if app_step else s.get("rule")
            evidence = app_step.get("evidence") if app_step else s.get("evidence")

            records.append(ActionAuditRecord(
                run_id=run_id,
                action=act,
                arguments=args,
                amount=amt,
                agent_id=r_agent,
                owner=owner,
                requested_by=r_req,
                approved_by=approved_by,
                time_to_approve_ms=time_to_approve,
                rule=rule,
                evidence=evidence,
                outcome=outcome or ("ok" if s.get("status") == "ok" else "error"),
                timestamp=step_ts_raw,
            ))

    return records


def export_audit_csv(records: Sequence[ActionAuditRecord]) -> str:
    """Exports action audit records to RFC 4180 CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "run_id", "timestamp", "agent_id", "owner", "requested_by",
        "action", "amount", "arguments", "approved_by",
        "time_to_approve_ms", "rule", "evidence", "outcome",
    ])
    for r in records:
        writer.writerow([
            r.run_id,
            r.timestamp or "",
            r.agent_id or "",
            r.owner or "",
            r.requested_by or "",
            r.action,
            "" if r.amount is None else f"{r.amount:.2f}",
            json.dumps(r.arguments, sort_keys=True),
            r.approved_by or "",
            "" if r.time_to_approve_ms is None else f"{r.time_to_approve_ms:.1f}",
            r.rule or "",
            r.evidence or "",
            r.outcome or "",
        ])
    return output.getvalue()


def export_signed_audit(records: Sequence[ActionAuditRecord], secret_key: str) -> Dict[str, Any]:
    """Exports action audit records with an HMAC-SHA256 signature sealing the payload."""
    rec_dicts = [asdict(r) for r in records]
    payload = {
        "algorithm": "HMAC-SHA256",
        "count": len(records),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": rec_dicts,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        **payload,
        "signature": sig,
    }


def verify_signed_audit(audit_package: Dict[str, Any], secret_key: str) -> bool:
    """Verifies HMAC signature of an exported audit package, ensuring no edits/tampering."""
    pkg = dict(audit_package)
    sig = pkg.pop("signature", None)
    if not sig:
        return False
    canonical = json.dumps(pkg, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(secret_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def detect_rubber_stamp_approvals(
    run_or_runs: Any,
    min_review_seconds: float = 1.0,
) -> List[Dict[str, Any]]:
    """Detects rubber-stamp approvals:
    - Approvals made faster than human comprehension threshold (e.g. < 1.0s)
    - Approvals missing approver identity
    """
    runs = _normalize_runs(run_or_runs)
    rubber_stamps: List[Dict[str, Any]] = []

    min_ms = min_review_seconds * 1000

    for r in runs:
        run_id = r.get("id") or r.get("run_id")
        for s in r.get("steps") or []:
            if s.get("kind") != "approval":
                continue

            action = s.get("name") or "unknown"
            decision = s.get("decision")
            approver = s.get("by")
            duration_ms = s.get("time_to_approve_ms")

            if decision == "approved":
                if duration_ms is not None and duration_ms < min_ms:
                    rubber_stamps.append({
                        "run_id": run_id,
                        "action": action,
                        "approver": approver,
                        "time_to_approve_ms": duration_ms,
                        "kind": "fast_approval",
                        "reason": (
                            f"Suspicious rapid approval for '{action}' approved in {duration_ms:.0f}ms "
                            f"(< {min_ms:.0f}ms threshold); likely rubber-stamped without review"
                        ),
                    })
                elif not approver or not str(approver).strip():
                    rubber_stamps.append({
                        "run_id": run_id,
                        "action": action,
                        "approver": None,
                        "kind": "missing_approver",
                        "reason": f"Approval for '{action}' has no approver on record",
                    })

    return rubber_stamps


def verify_approval_for_action(
    approval_step: Dict[str, Any],
    tool_step: Dict[str, Any],
    max_age_seconds: Optional[float] = None,
    bind_args: bool = True,
    allowed_sources: Optional[Sequence[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Strict verification of approval against tool call:
    - Verifies approval decision is "approved"
    - Verifies argument-hash binding (preventing approving modified requests)
    - Verifies approval freshness (max_age_seconds)
    - Verifies approved source is trusted
    """
    if approval_step.get("decision") != "approved":
        return False, f"Action not approved (decision: {approval_step.get('decision')})"

    action = tool_step.get("name") or "unknown"

    # 1. Source verification
    if allowed_sources:
        src = approval_step.get("source") or approval_step.get("by")
        if not src or src not in allowed_sources:
            return False, f"Approval for '{action}' came from untrusted source '{src}' (allowed: {list(allowed_sources)})"

    # 2. Argument-hash binding
    if bind_args:
        bound_hash = approval_step.get("arg_hash")
        if bound_hash:
            actual_hash = hash_arguments(tool_step.get("args"))
            if actual_hash != bound_hash:
                return False, (
                    f"Argument-hash binding mismatch for '{action}': "
                    f"arguments were modified after approval was granted"
                )

    # 3. Freshness / expiration window
    if max_age_seconds is not None:
        app_ts = _ts(approval_step.get("ts"))
        tool_ts = _ts(tool_step.get("ts"))
        if app_ts and tool_ts:
            age = (tool_ts - app_ts).total_seconds()
            if age > max_age_seconds:
                return False, f"Approval for '{action}' expired (age {age:.1f}s > {max_age_seconds}s)"

    return True, None


def check_self_claimed_approvals(run_or_runs: Any) -> List[str]:
    """Detects cases where an agent falsely claimed an approval itself (in its output
    or tool arguments) rather than having been granted one by an authentic approval step.
    """
    runs = _normalize_runs(run_or_runs)
    spoofed = []

    fake_claims_re = re.compile(
        r"\b(?:pre-approved|approved by|approval granted|preapproved|trusted: true)\b",
        re.I,
    )

    for r in runs:
        has_approval_step = any(s.get("kind") == "approval" and s.get("decision") == "approved" for s in r.get("steps") or [])

        # Check answer
        ans = r.get("answer_text") or ""
        for s in r.get("steps") or []:
            if s.get("kind") == "answer" and s.get("text"):
                ans = s.get("text")
            elif s.get("kind") == "llm" and s.get("text") and fake_claims_re.search(s.get("text")):
                if not has_approval_step:
                    spoofed.append(f"Agent model claimed approval without an approval step: '{s.get('text')[:100]}...'")

            # Check tool arguments passing fake approval
            if s.get("kind") == "tool":
                args = s.get("args") or {}
                if isinstance(args, dict):
                    if any(k in args for k in ("approved_by", "pre_approved", "trusted", "is_approved")):
                        if not has_approval_step:
                            spoofed.append(f"Tool '{s.get('name')}' invoked with self-claimed approval in arguments")

        if ans and fake_claims_re.search(ans) and not has_approval_step:
            spoofed.append("Agent claimed approval in final answer without an approval step")

    return spoofed
