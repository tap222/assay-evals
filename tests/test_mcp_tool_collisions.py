"""The same tool name on two MCP servers:
Contracts that name a tool by server (e.g. db.search vs web.search),
and detecting tool name collisions when multiple servers offer the same tool name in one model call."""
from __future__ import annotations

import pytest

import assay_sdk as assay
from assay.contracts import Step, _hits
from assay_sdk.checks import tool_collisions
from assay_sdk.testing import (
    assert_called,
    assert_called_before,
    assert_not_called,
    expect,
)


def test_tool_collisions_detection():
    # Detect colliding tool names offered by different MCP servers
    tools = [
        {"name": "search", "server": "db", "description": "Search local database"},
        {"name": "search", "server": "web", "description": "Search internet"},
        {"name": "get_user", "server": "auth"},
    ]
    cols = tool_collisions(tools)
    assert cols == {"search": ["db", "web"]}

    # Functions / OpenAI tool format
    openai_tools = [
        {"type": "function", "function": {"name": "query", "server": "sql"}},
        {"type": "function", "function": {"name": "query", "server": "graphql"}},
    ]
    assert tool_collisions(openai_tools) == {"query": ["graphql", "sql"]}

    # String dot format
    str_tools = ["db.search", "web.search", "auth.login"]
    assert tool_collisions(str_tools) == {"search": ["db", "web"]}

    # No collisions
    safe_tools = [
        {"name": "search", "server": "db"},
        {"name": "scrape", "server": "web"},
    ]
    assert tool_collisions(safe_tools) == {}


def test_contract_names_tool_by_server_passes():
    with assay.run("agent_run") as r:
        r.tool("db.search", {"query": "customers"})
        r.tool("web.fetch", {"url": "https://example.com"})

    # server.tool syntax
    assert_called(r, "db.search", query="customers")
    assert_called(r, "search", server="db", query="customers")
    assert_not_called(r, "web.search")
    assert_not_called(r, "search", server="web")

    # expect(run) expectations
    expect(r).must_call("db.search", query="customers").verify()
    expect(r).must_call("search", server="db").verify()
    expect(r).must_not_call("web.search").verify()


def test_contract_names_tool_by_server_fails_when_wrong_server():
    with assay.run("agent_run") as r:
        # Agent calls web.search instead of db.search
        r.tool("web.search", {"query": "customers"})

    # Expecting db.search should fail
    with pytest.raises(AssertionError, match="db.search was never called"):
        assert_called(r, "db.search")

    with pytest.raises(AssertionError, match=r"web\.search.*shouldn't have been called"):
        assert_not_called(r, "web.search")

    failures = expect(r).must_call("db.search").failures()
    assert len(failures) == 1
    assert "db.search was never called" in failures[0]


def test_at_most_once_with_server():
    with assay.run("payment_run") as r:
        r.tool("payments.charge", {"order_id": "O-1"})
        r.tool("billing.charge", {"order_id": "O-1"})

    # payments.charge ran once, billing.charge ran once
    expect(r).at_most_once("payments.charge", key="order_id").verify()
    expect(r).at_most_once("charge", server="payments", key="order_id").verify()

    # Now make payments.charge run twice
    with assay.run("payment_run_twice") as r2:
        r2.tool("payments.charge", {"order_id": "O-1"})
        r2.tool("payments.charge", {"order_id": "O-1"})

    failures = expect(r2).at_most_once("payments.charge", key="order_id").failures()
    assert len(failures) == 1
    assert "payments.charge went through 2 times" in failures[0]


def test_must_call_before_with_server():
    with assay.run("workflow") as r:
        r.tool("db.get_order", {"order_id": "O-9"})
        r.tool("payments.refund", {"order_id": "O-9"})

    assert_called_before(r, "db.get_order", "payments.refund")
    expect(r).must_call_before("db.get_order", "payments.refund").verify()

    with assay.run("bad_workflow") as r2:
        r2.tool("payments.refund", {"order_id": "O-9"})

    with pytest.raises(AssertionError, match="payments.refund.*before any db.get_order"):
        assert_called_before(r2, "db.get_order", "payments.refund")


def test_must_get_approval_before_with_server():
    with assay.run("admin_run") as r:
        r.approval("db.drop_table", "approved")
        r.tool("db.drop_table", {"table": "logs"})

    expect(r).must_get_approval_before("db.drop_table").verify()

    with assay.run("unapproved_run") as r2:
        r2.tool("db.drop_table", {"table": "logs"})

    failures = expect(r2).must_get_approval_before("db.drop_table").failures()
    assert len(failures) == 1
    assert "db.drop_table ran at step 0 without an approval" in failures[0]


def test_no_tool_collisions_expectation_passes():
    with assay.run("clean_agent") as r:
        r.llm(model="claude-3-5-sonnet", tools=[
            {"name": "search", "server": "db"},
            {"name": "fetch", "server": "web"},
        ])

    expect(r).no_tool_collisions().verify()


def test_no_tool_collisions_expectation_fails_on_collision():
    with assay.run("colliding_agent") as r:
        r.llm(model="claude-3-5-sonnet", tools=[
            {"name": "search", "server": "db"},
            {"name": "search", "server": "web"},
            {"name": "lookup", "server": "cache"},
        ])

    failures = expect(r).no_tool_collisions().failures()
    assert len(failures) == 1
    assert "Tool collision across MCP servers" in failures[0]
    assert "'search' was offered by both 'db', 'web'" in failures[0]

    with pytest.raises(AssertionError, match="Tool collision across MCP servers"):
        expect(r).no_tool_collisions().verify()


def test_contracts_step_server_matching():
    step1 = Step("search", server="db")
    step2 = Step("fetch", server="web")
    path = [step1, step2]

    # Matches unqualified name
    assert _hits({"step": "search"}, path) == [0]
    # Matches server-qualified name
    assert _hits({"step": "db.search"}, path) == [0]
    # Does not match wrong server
    assert _hits({"step": "web.search"}, path) == []
