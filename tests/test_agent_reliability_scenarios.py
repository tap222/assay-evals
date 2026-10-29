import pytest
import assay_sdk as assay
from assay_sdk.scenarios import (
    Scenario,
    ScenarioResult,
    HandoffContract,
    BrokenTransition,
    ProcessCrash,
    crash_at,
    validate_handoffs,
    check_compensations,
    find_first_broken_transition,
)
from assay_sdk.testing import (
    expect,
    assert_valid_handoffs,
    assert_no_orphaned_effects,
    assert_terminal_state,
)
from assay.changes import items as change_items


def test_handoff_contract_advice_must_be_read_only():
    contract = HandoffContract(
        from_agent="support_lead",
        to_agent="billing_consultant",
        kind="advice",
        required_keys=["customer_id", "issue_summary"],
    )

    # 1. Valid advice handoff: advice agent only reads
    with assay.run("case_advice_valid") as run:
        run.handoff(
            to_agent="billing_consultant",
            kind="advice",
            payload={"customer_id": "C-1", "issue_summary": "Disputed bill"},
            from_agent="support_lead",
        )
        run.tool("read_invoice", {"invoice_id": "INV-1"})
        run.answer("Invoice looks valid.")

    violations = validate_handoffs(run, [contract])
    assert violations == []
    expect(run).valid_handoffs([contract])
    assert_valid_handoffs(run, [contract])

    # 2. Advice agent attempts a mutating tool (e.g. refund) -> VIOLATION
    with assay.run("case_advice_mutating") as run_bad:
        run_bad.handoff(
            to_agent="billing_consultant",
            kind="advice",
            payload={"customer_id": "C-1", "issue_summary": "Disputed bill"},
            from_agent="support_lead",
        )
        run_bad.tool("refund", {"amount": 50})  # Mutating!

    violations_bad = validate_handoffs(run_bad, [contract])
    assert len(violations_bad) == 1
    assert "Advice handoff violated" in violations_bad[0]
    with pytest.raises(AssertionError, match="Advice handoff violated"):
        assert_valid_handoffs(run_bad, [contract])


def test_handoff_contract_transfer_and_dropped_facts():
    contract = HandoffContract(
        from_agent="intake_agent",
        to_agent="fulfillment_agent",
        kind="transfer",
        required_keys=["order_id", "shipping_address"],
    )

    # 1. Missing required facts in handoff payload
    with assay.run("case_missing_facts") as run_missing:
        run_missing.handoff(
            to_agent="fulfillment_agent",
            kind="transfer",
            payload={"order_id": "O-100"},  # missing shipping_address
            from_agent="intake_agent",
        )
    v_missing = validate_handoffs(run_missing, [contract])
    assert len(v_missing) == 1
    assert "dropped required fact 'shipping_address'" in v_missing[0]

    # 2. Handing agent continues mutating task after transfer
    with assay.run("case_post_handoff") as run_post:
        run_post.handoff(
            to_agent="fulfillment_agent",
            kind="transfer",
            payload={"order_id": "O-100", "shipping_address": "123 Main St"},
            from_agent="intake_agent",
        )
        # Intake agent executes post-handoff mutation
        s = run_post.tool("update_order", {"status": "in_transit"})
        run_post.steps[-1]["agent"] = "intake_agent"

    v_post = validate_handoffs(run_post, [contract])
    assert len(v_post) == 1
    assert "Post-handoff action" in v_post[0]


def test_external_read_back_and_terminal_state():
    mock_db = {"orders": {"O-1": {"status": "shipped", "tracking": "TRK-99"}}}

    def read_back_db():
        return mock_db["orders"].get("O-1")

    # 1. Successful run matching external system of record
    with assay.run("case_verified") as run:
        run.tool("ship_order", {"order_id": "O-1"})
        run.outcome("resolved")

    expect(run).terminal_state("resolved", read_back=read_back_db)
    assert_terminal_state(run, "resolved", read_back=read_back_db)

    # 2. Agent claims resolved, but external read-back mismatch
    def read_back_unverified():
        return {"status": "failed_verification"}

    with assay.run("case_unverified") as run_unverified:
        run_unverified.outcome("resolved")

    scenario = Scenario(
        name="shipping_verification",
        expected_terminal_state="resolved",
        read_back=read_back_unverified,
        expected_read_back={"status": "shipped"},
    )
    res = scenario.evaluate(run_unverified)
    assert res.passed is False
    assert res.external_verified is False
    assert any("External read-back mismatch" in f for f in res.failures)


def test_compensations_for_half_finished_work():
    compensations = {"charge": "refund", "hold_inventory": "release_inventory"}

    # Scenario A: Charged card, but sending email timed out, and refund was NOT called -> ORPHANED
    with assay.run("case_orphaned") as run_orphaned:
        run_orphaned.tool("charge", {"amount": 100})
        run_orphaned.tool("send_confirmation", {"to": "user@test.com"}, fault="timeout")

    orphaned = check_compensations(run_orphaned, compensations)
    assert len(orphaned) == 1
    assert "without compensation 'refund'" in orphaned[0]

    with pytest.raises(AssertionError, match="without compensation 'refund'"):
        expect(run_orphaned).no_orphaned_effects(compensations).verify()

    with pytest.raises(AssertionError, match="Orphaned side-effect"):
        assert_no_orphaned_effects(run_orphaned, compensations)

    # Scenario B: Charged card, confirmation timed out, but refund WAS called -> COMPENSATED
    with assay.run("case_compensated") as run_compensated:
        run_compensated.tool("charge", {"amount": 100})
        run_compensated.tool("send_confirmation", {"to": "user@test.com"}, fault="timeout")
        run_compensated.tool("refund", {"amount": 100})

    assert check_compensations(run_compensated, compensations) == []
    expect(run_compensated).no_orphaned_effects(compensations)
    assert_no_orphaned_effects(run_compensated, compensations)


def test_first_broken_transition_attribution():
    with assay.run("case_cascade") as run:
        run.tool("fetch_user", {"id": "U-1"})
        run.tool("charge_card", {"amount": 25}, error="CardNetworkError: upstream declined")
        run.tool("send_receipt", {"id": "U-1"}, error="MissingChargeId")
        run.answer("Something went wrong.")

    broken = find_first_broken_transition(run)
    assert broken is not None
    assert broken.from_step == "fetch_user"
    assert broken.to_step == "charge_card"
    assert "CardNetworkError" in broken.cause
    assert len(broken.downstream_effects) >= 1
    assert "send_receipt errored" in broken.downstream_effects[0]


def test_crash_and_recovery_simulation():
    def charge_api(amount):
        return {"charged": amount, "tx": "tx_123"}

    # Simulate worker crash immediately after charge succeeds
    with pytest.raises(ProcessCrash, match="Process crashed after calling tool 'charge'"):
        with crash_at(tool="charge", when="after"):
            with assay.run("case_crash") as run_crashed:
                run_crashed.call("charge", charge_api, amount=50)

    # Verify that the charge was recorded before the crash stopped the process
    assert len(run_crashed.steps) == 1
    assert run_crashed.steps[0]["name"] == "charge"


def test_scenario_evaluation_and_changes_report():
    scenario = Scenario(
        name="checkout_reliability",
        handoffs=[
            HandoffContract("cart", "checkout", "transfer", required_keys=["cart_id", "items"]),
        ],
        expected_terminal_state="resolved",
        compensations={"charge": "refund"},
    )

    # Failing run: cart handed off without required keys, charge made then email failed without refund
    with assay.run("run_scenario_fail") as run:
        run.handoff("checkout", "transfer", {"cart_id": "c_1"}, from_agent="cart")  # missing items
        run.tool("charge", {"amount": 30})
        run.tool("email_receipt", {}, fault="timeout")
        run.outcome("resolved")  # falsely claimed resolved

    result = scenario.evaluate(run)
    assert result.passed is False
    assert result.broken_transition is not None
    assert any("dropped required fact 'items'" in f for f in result.failures)
    assert any("without compensation 'refund'" in f for f in result.failures)
    assert any("Premature completion" in f for f in result.failures)

    with pytest.raises(AssertionError, match="scenario_passed"):
        expect(run).scenario_passed(scenario).verify()

    # Integration with assay.changes
    run_report = {
        "broken_transitions": [
            {
                "text": result.broken_transition.summary(),
                "detail": "scenario checkout_reliability failed",
                "cases": ["test_checkout"],
            }
        ]
    }
    items = change_items({}, run_report, [])
    item_kinds = [i["kind"] for i in items]
    assert "broken_transition" in item_kinds
    # broken_transition stops merge
    assert items[0]["kind"] == "broken_transition"
