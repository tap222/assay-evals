import pytest
import assay_sdk as assay
from assay_sdk.evidence import (
    EvidenceItem,
    DecisionProvenance,
    CaseEvidenceDiff,
    EvidenceDiffReport,
    is_superseded,
    extract_evidence_items,
    extract_decision_provenance,
    compare_case_evidence,
    compare_evidence_diffs,
)
from assay_sdk.testing import (
    expect,
    assert_no_changed_basis,
    assert_no_superseded_evidence,
    assert_no_wrong_entity_records,
    assert_evidence_parity,
)
from assay.changes import items as change_items


def test_is_superseded_helper():
    """Verify version and date comparison for superseded evidence."""
    # Semantic/numerical versions
    assert is_superseded("v1.0", "v2.0") is True
    assert is_superseded("v2.0", "v1.0") is False
    assert is_superseded("v1.1", "v1.2") is True
    assert is_superseded("v2.0", "v2.0") is False

    # Dates
    assert is_superseded(None, None, cand_date="2024-01-01", base_date="2026-05-01") is True
    assert is_superseded(None, None, cand_date="2026-05-01", base_date="2024-01-01") is False


def test_decision_provenance_extraction():
    """Extract complete decision provenance: policy versions, entities, records."""
    with assay.run("loan_approval") as r:
        # 1. Retrieval of policy
        r.retrieve("loan guidelines", [
            {"id": "doc_loan_1", "source": "credit_policy.md", "version": "v3.0", "date": "2026-03-01", "text": "Minimum score: 680"}
        ], used=True)
        # 2. Tool call fetching customer record
        r.tool("get_customer_profile", {"customer_id": "cust_42"}, {"customer_id": "cust_42", "credit_score": 720})
        # 3. Answer
        r.answer("Loan approved.")

    prov = extract_decision_provenance(r)
    assert prov.case_name == "loan_approval"
    assert prov.answer == "Loan approved."
    assert "credit_policy.md" in prov.policy_versions
    assert prov.policy_versions["credit_policy.md"] == "v3.0"
    assert "cust_42" in prov.entities_consulted
    assert any("cust_42" in rec for rec in prov.records_consulted)


def test_changed_basis_under_unchanged_answer_is_a_finding():
    """Two runs both say 'approved', but candidate read an older policy: MUST be a finding!"""
    # Baseline run: approved under modern policy v2.0
    with assay.run("claim_approval") as base:
        base.retrieve("warranty policy", [
            {"id": "p_v2", "source": "warranty.md", "version": "v2.0", "date": "2026-04-01", "text": "2 year warranty"}
        ], used=True)
        base.answer("Claim approved.")

    # Candidate run: also says 'Claim approved.', but rested on superseded v1.0
    with assay.run("claim_approval") as cand:
        cand.retrieve("warranty policy", [
            {"id": "p_v1", "source": "warranty.md", "version": "v1.0", "date": "2024-01-01", "text": "1 year warranty"}
        ], used=True)
        cand.answer("Claim approved.")

    diff = compare_case_evidence(base, cand)

    assert diff.answers_match is True  # Both say 'Claim approved.'
    assert diff.basis_matches is False  # Evidence differed
    assert diff.is_changed_basis_finding is True  # Changed basis under unchanged answer is a finding!
    assert len(diff.superseded_evidence) == 1
    assert any("Superseded evidence" in f and "v1.0 vs baseline v2.0" in f for f in diff.findings)


def test_cross_entity_wrong_record_consulted():
    """Candidate consulted records of another entity (wrong customer/account)."""
    # Baseline for customer cust_101
    with assay.run("check_balance", input={"customer_id": "cust_101"}) as base:
        base.tool("get_account", {"customer_id": "cust_101"}, {"customer_id": "cust_101", "balance": 500})
        base.answer("Balance is $500.")

    # Candidate inadvertently fetched data for cust_999
    with assay.run("check_balance", input={"customer_id": "cust_101"}) as cand:
        cand.tool("get_account", {"customer_id": "cust_999"}, {"customer_id": "cust_999", "balance": 500})
        cand.answer("Balance is $500.")

    diff = compare_case_evidence(base, cand, target_entity="cust_101")

    assert diff.answers_match is True
    assert diff.is_changed_basis_finding is True
    assert len(diff.wrong_entity_records) == 1
    assert diff.wrong_entity_records[0].entity == "cust_999"
    assert any("Wrong entity record" in f and "cust_999" in f for f in diff.findings)


def test_evidence_dropped_and_added():
    """Detect dropped evidence sources and newly added sources."""
    with assay.run("tax_calc") as base:
        base.retrieve("tax code", [
            {"id": "t1", "source": "federal_tax.md", "text": "Federal rules"},
            {"id": "t2", "source": "state_tax.md", "text": "State rules"},
        ], used=True)
        base.answer("Taxes computed.")

    with assay.run("tax_calc") as cand:
        # State tax dropped, municipal tax added
        cand.retrieve("tax code", [
            {"id": "t1", "source": "federal_tax.md", "text": "Federal rules"},
            {"id": "t3", "source": "municipal_tax.md", "text": "City rules"},
        ], used=True)
        cand.answer("Taxes computed.")

    diff = compare_case_evidence(base, cand)
    assert len(diff.dropped_evidence) == 1
    assert diff.dropped_evidence[0].source == "state_tax.md"
    assert len(diff.added_evidence) == 1
    assert diff.added_evidence[0].source == "municipal_tax.md"


def test_compare_evidence_diffs_report_and_expectations():
    """Aggregate evidence diff report and expectation assertions."""
    # Case 1: identical basis
    with assay.run("case_clean") as b1:
        b1.retrieve("q", [{"id": "d1", "source": "s1", "version": "v1"}], used=True)
        b1.answer("OK")
    with assay.run("case_clean") as c1:
        c1.retrieve("q", [{"id": "d1", "source": "s1", "version": "v1"}], used=True)
        c1.answer("OK")

    # Case 2: changed basis (superseded policy)
    with assay.run("case_superseded") as b2:
        b2.retrieve("q", [{"id": "p2", "source": "policy", "version": "v2.0"}], used=True)
        b2.answer("Approved")
    with assay.run("case_superseded") as c2:
        c2.retrieve("q", [{"id": "p1", "source": "policy", "version": "v1.0"}], used=True)
        c2.answer("Approved")

    report = compare_evidence_diffs([b1, b2], [c1, c2])

    assert report.total_cases == 2
    assert report.matching_basis_count == 1
    assert report.changed_basis_count == 1
    assert report.changed_basis_under_same_answer_count == 1
    assert report.superseded_evidence_count == 1
    assert report.has_findings() is True

    # Expectation assertions
    with pytest.raises(AssertionError, match="unchanged answer from changed evidence basis"):
        assert_no_changed_basis(report)

    with pytest.raises(AssertionError, match="superseded evidence item"):
        assert_no_superseded_evidence(report)


def test_evidence_diff_report_clean_passes():
    """When all cases have matching evidentiary basis, assertions pass."""
    with assay.run("case_pass") as b:
        b.retrieve("q", [{"id": "d1", "source": "s1", "version": "v1"}], used=True)
        b.answer("Answer")
    with assay.run("case_pass") as c:
        c.retrieve("q", [{"id": "d1", "source": "s1", "version": "v1"}], used=True)
        c.answer("Answer")

    report = compare_evidence_diffs([b], [c])
    assert not report.has_findings()
    assert_no_changed_basis(report)
    assert_no_superseded_evidence(report)
    assert_no_wrong_entity_records(report)
    assert_evidence_parity(report)


def test_changes_report_evidence_diff():
    """assay.changes detects evidence_diff as a stopping blocker."""
    run_report = {
        "evidence_diffs": [
            {"text": "claim_approval: unchanged answer 'Approved' rested on superseded policy 'warranty.md' (v1.0 vs v2.0)"}
        ]
    }
    items = change_items({}, run_report, [])
    kinds = [item["kind"] for item in items]
    assert "evidence_diff" in kinds

    # Must be in STOPS (blocking merge)
    from assay.changes import STOPS
    assert "evidence_diff" in STOPS
