import pytest
import assay_sdk as assay
from assay_sdk.replay import (
    Decision,
    DecisionDivergence,
    ReplayReport,
    RunReplayResult,
    classify_tool_risk,
    check_tool_definitions_drift,
    replay_decision_by_decision,
)
from assay_sdk.testing import (
    expect,
    assert_no_risky_divergences,
    assert_replay_agreement,
    assert_max_divergence_rate,
)
from assay.changes import items as change_items


def test_classify_tool_risk():
    """Verify write/spend actions are weighted high risk, read actions are weighted low."""
    # Write & spend verbs
    assert classify_tool_risk("refund_order") == ("high", True)
    assert classify_tool_risk("charge_customer") == ("high", True)
    assert classify_tool_risk("delete_account") == ("high", True)
    assert classify_tool_risk("update_user_profile") == ("high", True)
    assert classify_tool_risk("send_slack_message") == ("high", True)
    assert classify_tool_risk("execute_sql") == ("high", True)

    # Read verbs
    assert classify_tool_risk("get_order") == ("low", False)
    assert classify_tool_risk("search_documents") == ("low", False)
    assert classify_tool_risk("fetch_weather") == ("low", False)
    assert classify_tool_risk(None) == ("low", False)


def test_identical_replay_agreement():
    """Candidate that makes the exact same decisions as recorded achieves 100% agreement."""
    # 1. Create a recorded run
    with assay.run("check_order_status") as r:
        r.tool("get_order", {"order_id": "O-100"}, {"status": "shipped"})
        r.answer("Your order has shipped.")
        r.outcome("resolved")

    # 2. Candidate that reproduces the recorded decisions
    def candidate_agent(history, input, step_index, tool_definitions=None):
        if step_index == 0:
            return Decision.call("get_order", {"order_id": "O-100"})
        else:
            return Decision.stop("Your order has shipped.")

    report = replay_decision_by_decision(r, candidate_agent)

    assert report.total_runs == 1
    assert report.agreement_rate == 1.0
    assert len(report.divergences) == 0
    assert report.scores["identical"] == 1
    assert not report.has_risky_divergences()

    # Test assertion helpers
    assert_no_risky_divergences(report)
    assert_replay_agreement(report, min_agreement=0.99)
    assert_max_divergence_rate(report, max_rate=0.01)


def test_divergence_another_tool_and_different_args():
    """Detect 'another_tool' and 'different_args' divergences."""
    with assay.run("lookup_flight") as r:
        r.tool("search_flights", {"from": "SFO", "to": "JFK", "date": "2026-11-01"}, [{"flight": "UA100"}])
        r.tool("get_seat_map", {"flight": "UA100"}, {"available": ["12A"]})
        r.answer("Seat 12A is available.")
        r.outcome("resolved")

    # Candidate picks different search args at step 0, and a different tool at step 1
    def candidate_agent(history, input, step_index, tool_definitions=None):
        if step_index == 0:
            # Same tool, different args
            return Decision.call("search_flights", {"from": "SFO", "to": "LGA", "date": "2026-11-01"})
        elif step_index == 1:
            # Different tool
            return Decision.call("fetch_airline_policies", {"airline": "UA"})
        return Decision.stop("Found flight.")

    report = replay_decision_by_decision(r, candidate_agent)

    assert report.total_runs == 1
    assert len(report.divergences) == 2
    kinds = [d.kind for d in report.divergences]
    assert "different_args" in kinds
    assert "another_tool" in kinds
    # Both tools are read-only
    assert len(report.write_spend_divergences) == 0
    assert len(report.read_divergences) == 2


def test_divergence_stopped_early_and_went_further():
    """Detect candidate stopping early vs going further."""
    with assay.run("multi_step_support") as r:
        r.tool("check_account", {"user_id": "u1"}, {"verified": True})
        r.tool("check_eligibility", {"user_id": "u1"}, {"eligible": True})
        r.answer("Account is verified and eligible.")
        r.outcome("resolved")

    # Candidate 1: stops early at step 0
    def candidate_stops_early(history, input, step_index, tool_definitions=None):
        return Decision.stop("Account is fine.")

    report_early = replay_decision_by_decision(r, candidate_stops_early)
    assert report_early.divergences_by_kind["stopped_early"] >= 1
    # Since original succeeded and candidate stopped without checking, scored as worse
    assert report_early.scores["worse"] >= 1

    # Candidate 2: goes further at step 2 (where original stopped/answered)
    def candidate_goes_further(history, input, step_index, tool_definitions=None):
        if step_index == 0:
            return Decision.call("check_account", {"user_id": "u1"})
        elif step_index == 1:
            return Decision.call("check_eligibility", {"user_id": "u1"})
        else:
            return Decision.call("extra_audit_log", {"action": "checked"})

    report_further = replay_decision_by_decision(r, candidate_goes_further)
    assert report_further.divergences_by_kind["went_further"] == 1


def test_risky_write_spend_divergence_flagged():
    """A divergence on a tool that writes or spends is heavily weighted and fails safety checks."""
    with assay.run("refund_claim") as r:
        r.tool("read_invoice", {"inv_id": "I-9"}, {"amount": 25})
        r.tool("refund_charge", {"amount": 25}, {"success": True})
        r.answer("Refunded $25.")
        r.outcome("resolved")

    # Candidate diverges on the refund tool by altering amount or calling unauthorized charge
    def candidate_rogue(history, input, step_index, tool_definitions=None):
        if step_index == 0:
            return Decision.call("read_invoice", {"inv_id": "I-9"})
        elif step_index == 1:
            # Altered amount on financial spend tool
            return Decision.call("refund_charge", {"amount": 250})
        return Decision.stop("Done")

    report = replay_decision_by_decision(r, candidate_rogue)

    assert len(report.write_spend_divergences) == 1
    assert report.write_spend_divergences[0].weight == "high"
    assert report.write_spend_divergences[0].is_write_or_spend is True
    assert report.scores["worse"] >= 1
    assert report.has_risky_divergences()

    with pytest.raises(AssertionError, match="Risky write/spend divergence"):
        assert_no_risky_divergences(report)


def test_scoring_better_when_candidate_avoids_known_failure():
    """When original run was unresolved/failed, avoiding bad steps scores 'better'."""
    with assay.run("failing_query") as r:
        r.tool("failing_db_query", {"bad_sql": True}, error="Syntax error near 'WHERE'")
        r.answer("Failed to find data.")
        r.outcome("unresolved")

    # Candidate fixes the query or stops cleanly
    def candidate_fixed(history, input, step_index, tool_definitions=None):
        return Decision.call("read_cache", {"key": "data"})

    report = replay_decision_by_decision(r, candidate_fixed)
    assert report.scores["better"] >= 1


def test_tool_definitions_drift_detection():
    """Runs with changed tool definitions are flagged and set apart."""
    recorded_tools = [
        {"name": "get_user", "parameters": {"user_id": "string"}},
        {"name": "charge", "parameters": {"amount": "number"}},
    ]

    candidate_tools = [
        {"name": "get_user", "parameters": {"user_id": "string"}},
        # charge was removed, new_tool was added
        {"name": "new_tool", "parameters": {}},
    ]

    has_drift, diffs = check_tool_definitions_drift(recorded_tools, candidate_tools)
    assert has_drift is True
    assert any("charge' removed" in d for d in diffs)
    assert any("new_tool' added" in d for d in diffs)

    with assay.run("run_with_tools", tool_definitions=recorded_tools) as r:
        r.tool("get_user", {"user_id": "u1"}, {"name": "Alice"})
        r.answer("Alice")

    report = replay_decision_by_decision(
        r,
        lambda h, i, s, td: Decision.call("get_user", {"user_id": "u1"}),
        candidate_tool_definitions=candidate_tools
    )

    assert len(report.runs_with_changed_tool_definitions) == 1
    assert report.results[0].tool_definitions_changed is True
    assert report.results[0].verdict == "definitions_drift"


def test_tools_never_re_executed():
    """Verify recorded tool outputs are fed back without executing external code."""
    real_tool_called = False

    def dangerous_real_tool():
        nonlocal real_tool_called
        real_tool_called = True
        return "charged $10,000"

    with assay.run("safe_record") as r:
        r.tool("dangerous_tool", {"action": "charge"}, "recorded_charge_result")
        r.answer("Charged.")
        r.outcome("resolved")

    # Candidate makes the call
    def candidate(history, input, step_index, tool_definitions=None):
        if step_index == 0:
            return Decision.call("dangerous_tool", {"action": "charge"})
        # Check that history received the RECORDED output, not a re-executed one!
        assert len(history) == 2
        assert history[1]["result"] == "recorded_charge_result"
        return Decision.stop("Done.")

    report = replay_decision_by_decision(r, candidate)
    assert real_tool_called is False
    assert report.agreement_rate == 1.0


def test_report_summary_formatting():
    """ReplayReport summary produces informative metrics."""
    with assay.run("test_run") as r:
        r.tool("read_data", {}, "data")
        r.answer("ok")

    report = replay_decision_by_decision(
        r,
        lambda h, i, s, td: Decision.call("read_data", {}) if s == 0 else Decision.stop("ok")
    )
    summary = report.summary()
    assert "Replay decision-by-decision: 1 run(s) replayed" in summary
    assert "Overall agreement rate: 100.0%" in summary
    assert "Risk weighting:" in summary


def test_changes_report_decision_divergence():
    """assay.changes detects decision_divergence as a stop signal."""
    run_report = {
        "decision_divergences": [
            {"text": "Decision divergence: candidate called 'refund_charge' with amount=250 instead of 25"}
        ]
    }
    items = change_items({}, run_report, [])
    kinds = [item["kind"] for item in items]
    assert "decision_divergence" in kinds
    # Must be in STOPS (blocking merge)
    from assay.changes import STOPS
    assert "decision_divergence" in STOPS


def test_replay_from_raw_dict_runs():
    """Verify replay works directly on serialized/loaded JSON dictionaries."""
    raw_run = {
        "run_id": "r-jsonl-1",
        "task": "dict_task",
        "input": "Get order info",
        "outcome_value": "resolved",
        "steps": [
            {"seq": 0, "kind": "tool", "name": "get_order", "args": {"id": 1}, "result": {"item": "book"}},
            {"seq": 1, "kind": "answer", "text": "Book ordered"},
        ]
    }

    report = replay_decision_by_decision(
        raw_run,
        lambda h, i, s, td: Decision.call("get_order", {"id": 1}) if s == 0 else Decision.stop("Book ordered")
    )
    assert report.total_runs == 1
    assert report.agreement_rate == 1.0
    assert len(report.divergences) == 0
