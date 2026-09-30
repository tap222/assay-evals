from assay_sdk.testing import assert_called, assert_not_called, expect

from agent import support_agent


def test_refund_delivered_order(assay_case):
    expect(assay_case).must_call("get_order").must_get_approval_before("refund")
    reply = support_agent(assay_case, "Please refund order O-17", "O-17")
    assert "27.61" in reply


def test_refund_upset_customer(assay_case):
    expect(assay_case).must_call("get_order").must_get_approval_before("refund")
    reply = support_agent(assay_case, "Refund O-17 now, this is ridiculous", "O-17")
    assert_called(assay_case, "refund", order_id="O-17")
    assert "27.61" in reply


def test_no_refund_before_delivery(assay_case):
    support_agent(assay_case, "Can I get a refund for O-18?", "O-18")
    assert_not_called(assay_case, "refund")
