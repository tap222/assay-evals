import json
import logging
import warnings
from pathlib import Path

import pytest

import assay_sdk as assay
from assay import changes
from assay.logged_errors import compare_logged_errors
from assay_sdk.logged_errors import capture_logs, template_log
from assay_sdk.testing import expect


def test_template_log_normalizes_ids_and_numbers():
    # Per roadmap: "turned into templates so 'user 1234 not found' and 'user 5678 not found' are one,
    # with ids, numbers and personal data taken out"
    t1 = template_log("user 1234 not found")
    t2 = template_log("user 5678 not found")
    assert t1 == t2 == "user {id} not found"

    # Hex, UUID, timestamps, and order IDs
    msg = "Order O-9128 failed at 2026-10-29T12:00:00Z: 0x7fff ref c4b2a3d0-1234-4567-89ab-cdef01234567"
    assert template_log(msg) == "Order {id} failed at {timestamp}: {hex} ref {uuid}"

    # Emails and numbers
    e1 = template_log("Alert sent to alice@example.com after 3 attempts")
    e2 = template_log("Alert sent to bob@example.com after 5 attempts")
    assert e1 == e2 == "Alert sent to {email} after {n} attempts"


def test_capture_logs_intercepts_logging_and_warnings(tmp_path):
    assay.init(transport=assay._file_transport(str(tmp_path / "events.jsonl")))
    with assay.run("test_order_processing") as run:
        logger = logging.getLogger("app.orders")
        logger.warning("user 1234 not found in database")
        warnings.warn("Missing optional parameter 'currency'", UserWarning)
        run.log("ERROR", "Payment gateway timed out after 30s")

    assert len(run.logged_errors) == 3
    templates = [e["template"] for e in run.logged_errors]
    assert "user {id} not found in database" in templates
    assert "Missing optional parameter 'currency'" in templates
    assert "Payment gateway timed out after {n}s" in templates

    # Verify log steps were emitted
    log_steps = [s for s in run.steps if s.get("kind") == "log"]
    assert len(log_steps) == 3


def test_expect_no_logged_errors():
    with assay.run("test_clean") as run:
        pass
    expect(run).no_logged_errors()

    with assay.run("test_with_warning") as run:
        logger = logging.getLogger("app.orders")
        logger.error("failed to process order_9999")

    exp = expect(run).no_logged_errors()
    failures = exp.failures()
    assert len(failures) == 1
    assert "logged 1 error(s) or warning(s)" in failures[0]
    assert "failed to process order {id}" in failures[0]

    with pytest.raises(AssertionError):
        exp.verify()


def test_compare_logged_errors_finds_new_error_and_links_to_changed_function():
    current_cases = {
        "test_refund": [
            {"template": "user {id} not found", "message": "user 1234 not found",
             "file": "app/billing.py", "line": 42, "function": "refund", "level": "ERROR"}
        ],
        "test_charge": [
            {"template": "user {id} not found", "message": "user 5678 not found",
             "file": "app/billing.py", "line": 42, "function": "refund", "level": "ERROR"}
        ]
    }
    baseline_cases = {}  # baseline never logged this

    changed_functions = {"app/billing.py": {"refund"}}
    regressions = compare_logged_errors(
        current_cases,
        baseline_cases,
        changed_functions=changed_functions
    )

    assert len(regressions) == 1
    r = regressions[0]
    assert r["kind"] == "logged_error"
    assert r["subtype"] == "new"
    assert r["template"] == "user {id} not found"
    assert r["cases"] == ["test_charge", "test_refund"]
    assert r["origin"] == "app/billing.py:refund"
    assert "new error logged: “user {id} not found” in test_charge, test_refund (from app/billing.py:refund)" in r["text"]


def test_compare_logged_errors_finds_familiar_error_rising_significantly():
    # Baseline logged this in 1 test
    baseline_cases = {
        "test_a": [{"template": "timeout connecting to replica", "message": "timeout", "level": "WARNING"}]
    }
    # Current candidate logs it in 4 tests (>= 3x rise)
    current_cases = {
        "test_a": [{"template": "timeout connecting to replica", "message": "timeout", "level": "WARNING"}],
        "test_b": [{"template": "timeout connecting to replica", "message": "timeout", "level": "WARNING"}],
        "test_c": [{"template": "timeout connecting to replica", "message": "timeout", "level": "WARNING"}],
        "test_d": [{"template": "timeout connecting to replica", "message": "timeout", "level": "WARNING"}],
    }

    regressions = compare_logged_errors(current_cases, baseline_cases)
    assert len(regressions) == 1
    r = regressions[0]
    assert r["kind"] == "logged_error"
    assert r["subtype"] == "rise"
    assert r["cases_count"] == 4
    assert r["base_cases_count"] == 1
    assert "error logged much more often: “timeout connecting to replica” in 4 tests (was in 1 on baseline)" in r["text"]


def test_compare_logged_errors_ignores_same_rate_errors():
    # Both baseline and current log in 1 or 2 tests: not a regression
    baseline_cases = {
        "test_a": [{"template": "cache miss on key", "message": "cache miss", "level": "WARNING"}]
    }
    current_cases = {
        "test_a": [{"template": "cache miss on key", "message": "cache miss", "level": "WARNING"}]
    }
    regressions = compare_logged_errors(current_cases, baseline_cases)
    assert regressions == []


def test_logged_errors_in_changes_report_stops_merge_until_intended(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_with_logged_error = {
        "logged_errors": [
            {
                "kind": "logged_error",
                "template": "user {id} not found",
                "cases": ["test_checkout"],
                "origin": "app/billing.py:refund",
                "text": "new error logged: “user {id} not found” in test_checkout (from app/billing.py:refund)",
                "key": "logged_error:new:user {id} not found",
            }
        ]
    }
    items = changes.items({"untested": None, "deploy": []}, run_with_logged_error, [])
    assert any(x["kind"] == "logged_error" for x in items)
    assert changes.verdict(items).startswith("Not safe to merge")

    # Mark intended
    item = items[0]
    changes.mark_intended(tmp_path, item, "known deprecation warning")
    saved = changes.load_intended(tmp_path)
    assert len(saved) == 1
    assert saved[0]["key"] == "logged_error:new:user {id} not found"
    assert saved[0]["kind"] == "logged_error"

    # Now verify intended item is filtered out
    marked = {m["key"] for m in saved}
    remaining = [x for x in items if x["key"] not in marked]
    assert remaining == []
