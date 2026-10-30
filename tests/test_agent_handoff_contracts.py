import pytest
import assay_sdk as assay
from assay_sdk.handoffs import (
    AgentHandoffContract,
    HandoffContract,
    AgentTreeNode,
    validate_agent_handoffs,
    build_agent_tree,
    render_agent_tree,
    diff_agent_trees,
)
from assay_sdk.testing import (
    expect,
    assert_valid_agent_handoffs,
    assert_advice_was_read_only,
    assert_transfer_was_accepted,
    assert_no_permission_escalation,
)
from assay.changes import items as change_items


def test_advice_read_only_passes():
    """Advice agent only reads and returns findings: must pass."""
    with assay.run("triage_case") as r:
        r.tool("classify_issue", {"text": "login issue"}, "auth")
        r.handoff("security_advisor", kind="advice", payload={"issue": "login"})
        r.tool("read_security_policy", {"topic": "auth"}, "policy_ok", agent="security_advisor")
        r.answer("Advice: require 2FA reset", agent="security_advisor")

    violations = validate_agent_handoffs(r)
    assert not violations
    expect(r).advice_was_read_only().verify()
    assert_advice_was_read_only(r)


def test_advice_mutating_fails():
    """Advice agent attempting a mutating action (e.g. write, refund, send) must fail."""
    with assay.run("triage_case") as r:
        r.tool("classify_issue", {"text": "billing issue"}, "billing")
        r.handoff("billing_advisor", kind="advice", payload={"order": "123"})
        # Advisor attempts to send a message / mutate
        r.tool("send_customer_email", {"to": "user@example.com"}, "sent", agent="billing_advisor")
        r.answer("Emailed customer", agent="billing_advisor")

    violations = validate_agent_handoffs(r)
    assert any("Advice handoff violated" in v and "send_customer_email" in v for v in violations)

    with pytest.raises(AssertionError, match="Advice handoff violated"):
        expect(r).advice_was_read_only().verify()


def test_transfer_acceptance_and_completion():
    """Take over handoff explicitly accepted with actions completed passes."""
    with assay.run("support_escalation") as r:
        r.tool("check_status", {"id": "1"}, "open")
        r.handoff("billing_agent", kind="transfer", payload={"customer_id": "c1", "amount": 50}, accepted=True)
        r.tool("refund_charge", {"customer_id": "c1", "amount": 50}, "done", agent="billing_agent")
        r.answer("Refund issued", agent="billing_agent")

    assert_transfer_was_accepted(r)
    assert not validate_agent_handoffs(r)


def test_transfer_unaccepted_flagged():
    """Take over handoff that was never accepted is flagged as unaccepted."""
    with assay.run("support_escalation") as r:
        r.tool("check_status", {"id": "1"}, "open")
        # Not accepted
        r.handoff("billing_agent", kind="transfer", payload={"customer_id": "c1"}, accepted=False)

    violations = validate_agent_handoffs(r)
    assert any("never accepted" in v for v in violations)

    with pytest.raises(AssertionError, match="never accepted"):
        expect(r).transfer_was_accepted().verify()


def test_transfer_accepted_via_accept_step():
    """Take over handoff accepted via explicit Run.accept_handoff step."""
    with assay.run("support_escalation") as r:
        r.handoff("tier2_agent", kind="transfer", payload={"case_id": "case_9"})
        r.accept_handoff("agent", accepted=True, reason="Available to assist")
        r.tool("resolve_ticket", {"case_id": "case_9"}, "ok", agent="tier2_agent")
        r.answer("Resolved", agent="tier2_agent")

    assert not validate_agent_handoffs(r)
    expect(r).transfer_was_accepted().verify()


def test_transfer_single_ownership_violation():
    """Originating agent making post-handoff mutating actions violates single ownership."""
    with assay.run("escalation") as r:
        r.tool("lookup_user", {"uid": "u1"}, "found", agent="frontline")
        r.handoff("specialist", kind="transfer", payload={"uid": "u1"}, from_agent="frontline", accepted=True)
        r.tool("update_profile", {"uid": "u1"}, "ok", agent="specialist")
        # Frontline mutates after handing off!
        r.tool("cancel_subscription", {"uid": "u1"}, "cancelled", agent="frontline")

    violations = validate_agent_handoffs(r)
    assert any("Post-handoff action: 'frontline' executed 'cancel_subscription'" in v for v in violations)


def test_no_permission_escalation_by_message():
    """Receiving agent cannot exceed originating human scope or contract scope."""
    human_scope = ["lookup_user", "read_balance", "read_history"]

    with assay.run("session") as r:
        r.tool("lookup_user", {"id": "u1"}, "ok")
        r.handoff("sub_agent", kind="transfer", payload={"id": "u1"}, accepted=True)
        # sub_agent executes tool outside human scope
        r.tool("delete_user_account", {"id": "u1"}, "deleted", agent="sub_agent")

    violations = validate_agent_handoffs(r, originating_scope=human_scope)
    assert any("Permission escalation by message" in v and "delete_user_account" in v for v in violations)

    with pytest.raises(AssertionError, match="Permission escalation by message"):
        expect(r).no_permission_escalation(originating_scope=human_scope).verify()


def test_fact_propagation_dropped_keys():
    """Handoff dropping required facts is flagged."""
    contract = HandoffContract(
        from_agent="support",
        to_agent="billing",
        required_keys=["customer_id", "constraint", "deadline"]
    )

    with assay.run("order_task") as r:
        # customer_id is provided, but constraint and deadline are dropped
        r.handoff("billing", from_agent="support", payload={"customer_id": "cust_42"}, accepted=True)
        r.tool("process_order", {"id": "cust_42"}, "ok", agent="billing")

    violations = validate_agent_handoffs(r, contracts=[contract])
    assert any("dropped required fact 'constraint'" in v for v in violations)
    assert any("dropped required fact 'deadline'" in v for v in violations)


def test_multi_agent_tree_build_and_render():
    """Build and render multi-agent hierarchical tree."""
    with assay.run("main_flow") as r:
        r.tool("parse_request", {}, "ok")
        r.handoff("advisor", kind="advice", payload={"data": 1})
        r.handoff("executor", kind="transfer", payload={"data": 2}, accepted=True)

    tree = build_agent_tree(r)
    assert tree.kind == "root"
    assert len(tree.children) == 2
    assert tree.children[0].agent_id == "advisor"
    assert tree.children[0].kind == "advice"
    assert tree.children[1].agent_id == "executor"
    assert tree.children[1].kind == "transfer"
    assert tree.children[1].accepted is True

    rendered = render_agent_tree(tree)
    assert "[root]" in rendered
    assert "advisor [advice]" in rendered
    assert "executor [transfer] (accepted)" in rendered


def test_diff_agent_trees():
    """Diffing multi-agent trees identifies architectural handoff changes."""
    # Baseline
    with assay.run("run_v1") as base:
        base.tool("parse", {}, "ok")
        base.handoff("advisor", kind="advice", payload={"case": "c1"})

    # Candidate: changed advisor to transfer, and added worker agent
    with assay.run("run_v2") as cand:
        cand.tool("parse", {}, "ok")
        cand.handoff("advisor", kind="transfer", payload={"case": "c1"}, accepted=True)
        cand.handoff("worker", kind="transfer", payload={"case": "c1"}, accepted=True)

    base_tree = build_agent_tree(base)
    cand_tree = build_agent_tree(cand)

    diffs = diff_agent_trees(base_tree, cand_tree)
    diff_texts = [d["text"] for d in diffs]

    assert any("changed from advice to transfer" in t for t in diff_texts)
    assert any("added sub-agent 'worker'" in t for t in diff_texts)


def test_changes_report_handoff_violation_and_diff():
    """Assay changes report treats handoff_violation as a stop and formats handoff_diff."""
    run_report = {
        "handoff_violations": [
            {"text": "Advice handoff violated: 'advisor' performed mutating action 'drop_table'"}
        ],
        "handoff_diffs": [
            {"text": "Multi-agent tree: added sub-agent 'worker' (transfer)", "detail": "actions: do_work"}
        ]
    }

    report = change_items({}, run_report, [])
    kinds = [item["kind"] for item in report]
    assert "handoff_violation" in kinds
    assert "handoff_diff" in kinds
    # handoff_violation is a blocker stopping merge
    assert any(item["kind"] == "handoff_violation" for item in report)
