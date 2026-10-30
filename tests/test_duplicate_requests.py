"""The same request sent twice: simulate duplicate client webhooks or double clicks,
expect the second delivery to change nothing, still answer success to the sender (not 409 Conflict),
and be recorded as a duplicate."""
from __future__ import annotations

import pytest

import assay_sdk as assay
from assay_sdk.duplicate import DuplicateResult, aduplicate, duplicate
from assay_sdk.testing import expect


@pytest.fixture
def sent_events():
    out = []
    assay.init(transport=lambda batch: out.extend(batch), flush_interval=60)
    yield out
    assay.init(enabled=False)


def test_duplicate_request_idempotent_success(sent_events):
    seen = {}

    def handle_payment_webhook(req):
        order_id = req["order_id"]
        if order_id in seen:
            return {"status": 200, "message": "already processed", "order_id": order_id}
        # First delivery: call side-effecting tool
        cur = assay.current()
        if cur:
            cur.tool("charge_card", {"order_id": order_id, "amount": 100})
        seen[order_id] = True
        return {"status": 200, "message": "charged", "order_id": order_id}

    with assay.run("payment_hook", test={"run": "r-1", "case": "webhook_double_delivery"}) as r:
        res = duplicate(handle_payment_webhook, {"order_id": "O-123"}, run=r, key="order_id")

    assert res.ok
    assert res.first == {"status": 200, "message": "charged", "order_id": "O-123"}
    assert res.second == {"status": 200, "message": "already processed", "order_id": "O-123"}
    assert res.duplicate_run.duplicate is True
    assert res.duplicate_run.original_run_id == r.id
    res.verify()

    # Also verify through expect(run).duplicate_safe(duplicate_run)
    expect(r).duplicate_safe(res.duplicate_run, key="order_id").verify()

    # Verify event emission: run.start had duplicate=True and original_run_id
    assay.flush()
    starts = [e for e in sent_events if e.get("type") == "run.start"]
    assert len(starts) == 2
    assert starts[0].get("duplicate") is None or starts[0].get("duplicate") is False
    assert starts[1].get("duplicate") is True
    assert starts[1].get("original_run_id") == starts[0]["run_id"]


def test_duplicate_request_conflict_409_fails():
    seen = set()

    def handle_webhook_returns_409(req):
        if req["id"] in seen:
            return {"status_code": 409, "error": "Conflict: duplicate request"}
        seen.add(req["id"])
        return {"status_code": 200, "ok": True}

    res = duplicate(handle_webhook_returns_409, {"id": "req-1"})
    assert not res.ok
    assert any("status 409 (Conflict)" in f and "retries pile up" in f for f in res.failures)

    with pytest.raises(AssertionError, match="status 409"):
        res.verify()


def test_duplicate_request_raises_conflict_fails():
    seen = set()

    class ConflictError(Exception):
        status_code = 409

    def handle_webhook_raises(req):
        if req["id"] in seen:
            raise ConflictError("409 Conflict: already exists")
        seen.add(req["id"])
        return {"ok": True}

    res = duplicate(handle_webhook_raises, {"id": "req-2"})
    assert not res.ok
    assert any("status 409 (Conflict)" in f for f in res.failures)


def test_duplicate_request_side_effects_repeated_fails():
    # If the second delivery repeats side effects (e.g. charging or paying again)
    def broken_handler(req):
        cur = assay.current()
        if cur:
            cur.tool("pay_supplier", {"supplier_id": "S-9", "amount": 500})
        return {"status": 200}

    res = duplicate(broken_handler, {"supplier_id": "S-9"})
    assert not res.ok
    assert any("pay_supplier" in f and "duplicate delivery should change nothing" in f for f in res.failures)


def test_duplicate_request_state_mutation_fails():
    seen = {}

    def state_mutating_handler(req):
        cur = assay.current()
        if cur:
            cur.state(f"order:{req['id']}", {"processed": True, "count": seen.get(req["id"], 0) + 1})
        seen[req["id"]] = seen.get(req["id"], 0) + 1
        return {"status": 200}

    res = duplicate(state_mutating_handler, {"id": "O-99"})
    assert not res.ok
    assert any("recorded state change" in f for f in res.failures)


def test_duplicate_request_key_idempotency_fails():
    def key_handler(req):
        cur = assay.current()
        if cur:
            cur.tool("custom_action", {"order_id": req["order_id"], "attempt": 1})
        return {"status": 200}

    res = duplicate(key_handler, {"order_id": "O-555"}, key="order_id")
    assert not res.ok
    assert any("called again with order_id='O-555'" in f for f in res.failures)


@pytest.mark.asyncio
async def test_async_aduplicate():
    processed = set()

    async def async_handler(req):
        key = req["id"]
        if key in processed:
            return {"status": 200, "deduped": True}
        cur = assay.current()
        if cur:
            cur.tool("execute_task", {"id": key})
        processed.add(key)
        return {"status": 200, "deduped": False}

    res = await aduplicate(async_handler, {"id": "task-42"})
    assert res.ok
    assert res.first == {"status": 200, "deduped": False}
    assert res.second == {"status": 200, "deduped": True}
    assert res.duplicate_run.duplicate is True
    res.verify()


def test_sync_duplicate_rejects_coroutine_function():
    async def async_fn(req):
        return {"status": 200}

    with pytest.raises(TypeError, match="aduplicate"):
        duplicate(async_fn, {"id": 1})
