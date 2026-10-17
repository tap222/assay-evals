"""Agentic workflows, step by step: tool calls checked in parts, arguments against the tool's schema, a claimed
success that didn't happen, goal checkpoints, injected faults and how the agent handles them, transition failure
matrices, and context retention across turns."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

import assay_sdk as assay
from assay import agents, judge, transitions
from assay.api import create_app
from assay.config import Settings
from assay_sdk.checks import arg_problems, claims_success, says_it_failed, tool_schemas
from assay_sdk.testing import expect

H, SRC = {"X-Tenant": "w"}, {"source": "events:w"}
CANCEL = {"name": "cancel_order", "description": "Cancel an order",
          "input_schema": {"type": "object", "additionalProperties": False, "required": ["order_id", "reason"],
                           "properties": {"order_id": {"type": "string"},
                                          "reason": {"type": "string", "enum": ["changed_mind", "damaged", "late"]},
                                          "notify": {"type": "boolean"}}}}


def test_arguments_against_the_tools_own_schema():
    s = tool_schemas([CANCEL, {"type": "function", "function": {"name": "get_order", "parameters": {
        "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}}])
    assert set(s) == {"cancel_order", "get_order"}
    assert arg_problems(s["cancel_order"], {"order_id": "O-1", "reason": "damaged"}) == []
    assert arg_problems(s["cancel_order"], {"order_id": 17, "reason": "bored", "urgent": True}) == [
        "urgent isn't a field the tool takes", "order_id should be string, got int",
        "reason is 'bored', not one of 'changed_mind', 'damaged', 'late'"]
    assert arg_problems(s["get_order"], {}) == ["id is missing"]
    assert arg_problems(s["get_order"], {"id": True}) == ["id should be integer, got bool"]


def test_what_counts_as_claiming_success():
    assert claims_success("Done! Your order O-17 has been cancelled.") == "Done!"
    assert claims_success("I've cancelled your order.") == "I've cancelled your order."
    assert claims_success("Sorry, the order couldn't be cancelled.") is None
    assert claims_success("Your order ships on Tuesday.") is None
    assert says_it_failed("I wasn't able to reach the order system; please try again.")
    assert not says_it_failed("Your order has been cancelled.")


@pytest.fixture
def sent():
    out = []
    assay.init(transport=lambda b: out.extend(b), flush_interval=60)
    yield out
    assay.init(enabled=False)


@assay.tool
def get_order(order_id):
    return {"id": order_id, "status": "delivered"}


@assay.tool
def cancel_order(order_id, reason):
    return {"ok": True}


def agent(q, claim=True, tries=1):
    """Looks the order up, cancels it; says it did, or says what went wrong."""
    run = assay.current()
    run.llm(model="m", tools=[CANCEL])
    try:
        get_order("O-17")
        for i in range(tries):
            try:
                cancel_order("O-17", "damaged")
                break
            except Exception:
                if i == tries - 1:
                    raise
        return "Your order has been cancelled."
    except Exception:
        return "Your order has been cancelled." if claim else "Sorry, I couldn't cancel it: the order system failed."


def test_faults_and_handling_them(sent):
    with assay.run("support", input="cancel O-17") as r:
        with assay.faults(cancel_order="error"):
            r.answer(agent("cancel O-17", claim=True))
    e = expect(r).no_false_success().handles_failure()
    assert "no_false_success(): The answer says “Your order has been cancelled.”" in e.failures()[0]
    assay.flush()
    step = next(x for x in sent if x["type"] == "step" and x.get("name") == "cancel_order")
    assert step["fault"] == "error" and "injected by assay.faults" in step["error"]
    with assay.run("support", input="cancel O-17") as r2:
        with assay.faults(cancel_order="error"):
            r2.answer(agent("cancel O-17", claim=False))
    assert expect(r2).no_false_success().handles_failure().failures() == []  # it told the user
    with assay.run("support", input="cancel O-17") as r3:
        with assay.faults(cancel_order="error:1"):  # fails once, then works: does it retry?
            r3.answer(agent("cancel O-17", tries=2))
    assert expect(r3).handles_failure().failures() == []
    with assay.run("support", input="cancel O-17") as r4:
        with assay.faults(get_order={"return": {"status": "unknown"}}, cancel_order="empty"):
            agent("cancel O-17")
    assert [s.get("fault") for s in r4.steps if s["kind"] == "tool"] == ["return", "empty"]
    with pytest.raises(ValueError):
        with assay.faults(get_order="explode"):
            pass




# ---------- a payment that went through, with its reply lost ----------

charges = []


@assay.tool
def charge(order_id, amount):
    charges.append(order_id)
    return {"charged": amount}


def checkout(retry: bool, check_first: bool = False):
    """Charges O-17; when that fails, a careless one charges again, a careful one asks first."""
    for _ in range(2 if retry else 1):
        try:
            if check_first and charges:
                return "Paid already."
            return f"Paid {charge('O-17', 40)['charged']}."
        except Exception:
            continue
    return "Payment failed."


def test_a_retry_after_a_lost_reply_charges_twice(sent):
    charges.clear()
    with assay.run("checkout") as r:
        with assay.faults(charge="lost"):
            checkout(retry=True)
    assert charges == ["O-17", "O-17"]  # "lost" calls the tool for real, then times out
    assert [s.get("fault") for s in r.steps if s["kind"] == "tool"] == ["lost", "lost"]
    assert expect(r).at_most_once("charge", key="order_id").failures() == [
        "at_most_once(charge, key=order_id): charge went through 2 times for order_id='O-17' (steps 0, 1); once expected."]
    charges.clear()
    with assay.run("checkout") as r2:
        with assay.faults(charge="lost"):
            checkout(retry=True, check_first=True)
    assert charges == ["O-17"] and expect(r2).at_most_once("charge").failures() == []
    with assay.run("checkout") as r3:  # a call that failed before it ran doesn't count
        with assay.faults(charge="error:1"):
            checkout(retry=True)
    assert expect(r3).at_most_once("charge", key="order_id").failures() == []

# ---------- the world changing while the agent works ----------

@assay.tool
def get_invoice(invoice_id):
    return {"id": invoice_id, "status": "approved", "amount": 1240.0}


@assay.tool
def get_supplier(supplier_id):
    return {"id": supplier_id, "status": "active"}


@assay.tool
def pay(invoice_id, amount):
    return {"paid": True}


def payer(recheck: bool):
    """Plans the payment on the first reads; a careful one reads the supplier again just before paying."""
    run = assay.current()
    invoice, supplier = get_invoice("INV-17"), get_supplier("S-3")
    run.approval("pay", "approved", by="ap-clerk")
    if supplier["status"] != "active" or (recheck and get_supplier("S-3")["status"] != "active"):
        return "I couldn't pay INV-17: the supplier is on hold now. Sent back for review."
    pay("INV-17", invoice["amount"])
    return "Paid INV-17."


def test_a_state_that_changes_between_reading_and_acting():
    from assay_sdk.testing import assert_not_called
    # 9:00 the supplier is active; 9:07 it goes on hold; 9:10 the agent is about to pay.
    held = {"get_supplier": {"returns": [assay.REAL, {"id": "S-3", "status": "on_hold"}]}}
    with assay.run("ap", input="pay INV-17") as careful:
        with assay.faults(**held):
            careful.answer(payer(recheck=True))
    assert_not_called(careful, "pay")
    assert [s.get("fault") for s in careful.steps if s["kind"] == "tool"] == [None, None, "return"]
    assert expect(careful).handles_failure().failures() == []  # it said it didn't pay, and why
    with assay.run("ap", input="pay INV-17") as stale:
        with assay.faults(**held):
            stale.answer(payer(recheck=False))
    with pytest.raises(AssertionError, match="pay shouldn't have been called"):  # paid on a 9:00 read
        assert_not_called(stale, "pay")
    # The last value holds from then on; and an empty list is a mistake, not "no fault".
    with assay.run("ap") as r:
        with assay.faults(get_supplier={"returns": [{"status": "a"}, {"status": "b"}]}):
            got = [get_supplier("S-3")["status"] for _ in range(4)]
    assert got == ["a", "b", "b", "b"]
    with pytest.raises(ValueError, match="returns"):
        with assay.faults(get_supplier={"returns": []}):
            pass


def test_schema_and_checkpoint_expectations(sent):
    with assay.run("support", input="cancel O-17") as r:
        r.llm(model="m", tools=[CANCEL])
        r.tool("cancel_order", {"order_id": 17, "reason": "bored"}, {"ok": True})
        r.answer("Cancelled.")
    e = expect(r).well_formed_arguments().checkpoint("order cancelled", tool="cancel_order") \
        .checkpoint("said so", answer="cancelled").checkpoint("refund issued", tool="refund")
    got = e.failures()
    assert got[0].startswith("well_formed_arguments(): Malformed arguments: cancel_order at step 1:")
    assert got[1] == "checkpoint(refund issued): refund was never called" and len(got) == 2
    assay.flush()
    schema_steps = [x for x in sent if x["type"] == "step" and x.get("tool_schemas")]
    assert len(schema_steps) == 1 and "cancel_order" in schema_steps[0]["tool_schemas"]


def step(seq, kind, name=None, **kw):
    return {"seq": seq, "kind": kind, "name": name, "args": kw.get("args"), "result": kw.get("result"),
            "error": kw.get("error"), "text": kw.get("text"), "tokens": None, "cost_usd": None,
            "tools": kw.get("tools"), "tool_schemas": kw.get("tool_schemas"), "tool_calls": kw.get("tool_calls")}


def traj(steps, answer):
    return {"steps": steps, "answer": answer, "task": "support", "status": "completed", "started_at": None,
            "finished_at": None}


def test_server_checks_in_parts_arguments_claims_and_checkpoints():
    t = traj([step(0, "reason", tools=["get_order", "cancel_order"], tool_schemas={"cancel_order": CANCEL["input_schema"]}),
              step(1, "tool", "get_order", args={"order_id": "O-17"}, result={"status": "delivered"}),
              step(2, "tool", "cancel_order", args={"order_id": "O-18", "reason": "damaged"}, error="503 unavailable"),
              step(3, "answer", text="Your order O-17 has been cancelled.")], "Your order O-17 has been cancelled.")
    ref = {"calls": [{"tool": "get_order", "args": {"order_id": "O-17"}}, {"tool": "cancel_order", "args": {"order_id": "O-17"}}],
           "split": True, "checkpoints": [{"name": "order found", "tool": "get_order", "result": "nonempty"},
                                          {"name": "order cancelled", "tool": "cancel_order"},
                                          {"name": "told the user", "answer": "cancelled"}]}
    got = {c["field"]: c for c in agents.checks_for(t, ref, [])}
    assert got["tool_choice"]["status"] == "pass"
    assert got["tool_args"]["status"] == "fail" and "order_id: expected 'O-17', got 'O-18'" in got["tool_args"]["reason"]
    assert got["tool_results"]["status"] == "fail" and "cancel_order failed: 503" in got["tool_results"]["reason"]
    assert got["arguments"]["status"] == "pass"
    assert got["claimed_success"]["status"] == "fail" and "failed (503 unavailable) and was never done" in got["claimed_success"]["reason"]
    assert [got[f"checkpoint.{n}"]["status"] for n in ("order found", "order cancelled", "told the user")] == ["pass", "fail", "pass"]
    no_split = {c["field"] for c in agents.checks_for(t, {**ref, "split": False}, [])}
    assert "tool_choice" not in no_split and "tool_calls" in no_split  # one check unless a case asks for parts
    assert transitions.failure_of(t, ref, []) == ("order found", "order cancelled", "missed checkpoint")
    bad = traj([step(0, "reason", tool_calls=[{"name": "cancel_order", "arguments": {"order_id": "O-1"}}],
                     tool_schemas={"cancel_order": CANCEL["input_schema"]}), step(1, "answer", text="ok")], "ok")
    assert agents.argument_problems(bad) == [{"seq": 0, "tool": "cancel_order", "problems": ["reason is missing"]}]


def ingest_run(c, run_id, cases):
    ts = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    evs = []
    for case, fail_exec, bad_answer in cases:
        rid = f"{run_id}-{case}"
        ev = lambda i, **k: {"v": 1, "id": f"{rid}-{i}", "ts": ts, "run_id": rid, **k}
        evs += [ev(0, type="run.start", task="sql", input="top customers", test={"run": run_id, "case": case, "attempt": 0}),
                ev(1, type="step", seq=0, kind="tool", name="gen_sql", args={"q": "top"}, result={"sql": "SELECT"}),
                ev(2, type="step", seq=1, kind="tool", name="exec_sql", args={"sql": "SELECT"},
                   **({"status": "error", "error": "syntax error"} if fail_exec else {"result": [{"n": 3}]})),
                ev(3, type="step", seq=2, kind="answer", text="3 customers" if not bad_answer else "none"),
                ev(4, type="run.end", status="completed")]
    assert c.post("/v1/ingest", headers=H, json=evs).status_code == 200


def test_transition_matrix_where_failures_cluster_and_what_got_worse(tmp_path):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    refs = [{"v": 1, "id": f"ref-{k}", "ts": "2026-09-01T00:00:00Z", "type": "expect", "case": k, "answer": "3",
             "calls": [{"tool": "gen_sql"}, {"tool": "exec_sql"}]} for k in ("a", "b", "c", "d", "e")]
    assert c.post("/v1/ingest", headers=H, json=refs).status_code == 200
    ingest_run(c, "r0", [("a", True, False), ("b", False, False), ("c", False, False), ("d", False, True), ("e", False, False)])
    ingest_run(c, "r1", [("a", True, False), ("b", True, False), ("c", True, False), ("d", False, True), ("e", False, False)])
    m = c.get("/v1/agents/matrix", params={**SRC, "run": "r1"}).json()
    assert (m["runs"], m["failed"]) == (5, 4)
    top = m["hotspots"][0]
    assert (top["from"], top["to"], top["count"]) == ("gen_sql", "exec_sql", 3)
    assert top["how"] == {"Tool error, not recovered": 3}
    assert any(x["from"] == "exec_sql" and x["to"] == "answer" and x["count"] == 1 for x in m["cells"])
    assert m["states"][0] == "gen_sql" and m["states"][-1] == "answer"
    # A plain test (pytest --assay-all) that calls one endpoint three times isn't an agent: not in the matrix.
    ev = lambda i, **k: {"v": 1, "id": f"plain-{i}", "ts": "2026-09-01T00:00:00Z", "run_id": "r1-plain", **k}
    c.post("/v1/ingest", headers=H, json=[
        ev(0, type="run.start", task="tests/test_api.py", tags={"assay_record": "plain"},
           test={"run": "r1", "case": "test_polls", "attempt": 0}),
        *[ev(1 + i, type="step", seq=i, kind="tool", name="HTTP GET testserver/v1/jobs", args={}, result={"status": 200})
          for i in range(3)], ev(9, type="run.end", status="completed")])
    m = c.get("/v1/agents/matrix", params={**SRC, "run": "r1"}).json()
    assert (m["runs"], m["failed"], m["plain_left_out"]) == (5, 4, 1) and not any("HTTP" in s for s in m["states"])
    # In plain words, with every run to open, and the same failing runs the run summary counts.
    top = next(x for x in m["cells"] if x["from"] == "gen_sql")
    assert top["sentence"] == "gen_sql worked, then exec_sql went wrong"
    assert sorted(r["case"] for r in top["runs"]) == ["a", "b", "c"] and "syntax error" in top["runs"][0]["detail"]
    s = c.get("/v1/agents/runs/r1", params=SRC).json()
    failing = {t["trajectory_id"] for t in s["current"]["trajectories_list"]
               if any(k != "recorded" and v == "fail" for k, v in t["checks"].items())}
    assert failing == {r["trajectory_id"] for x in m["cells"] for r in x["runs"]}
    d = c.get("/v1/agents/matrix", params={**SRC, "run": "r1", "baseline": "r0"}).json()
    assert d["worse"][0]["from"] == "gen_sql" and d["worse"][0]["change"] == 2
    txt = transitions.text(d)
    assert "3 (+2)  gen_sql worked, then exec_sql went wrong: Tool error, not recovered 3" in txt
    assert "plain tests left out" in txt and "last ok" not in txt
    # From the first failures people marked, in production.
    ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"p-{i}", "ts": ts, "run_id": "prod-1", **k}
    c.post("/v1/ingest", headers=H, json=[ev(0, type="run.start", task="sql", input="q"),
                                          ev(1, type="step", seq=0, kind="tool", name="gen_sql", result={"sql": "x"}),
                                          ev(2, type="step", seq=1, kind="tool", name="exec_sql", result=[]),
                                          ev(3, type="step", seq=2, kind="answer", text="none"), ev(4, type="run.end")])
    c.post("/v1/review/notes", params=SRC, json={"conversation": "prod-1", "went_wrong": True, "note": "wrong join",
                                                 "trace_ids": ["prod-1"], "first_step": {"trace_id": "prod-1", "seq": 1}})
    rv = c.get("/v1/agents/matrix", params=SRC).json()
    assert rv["from_review"] and rv["cells"][0]["from"] == "gen_sql" and rv["cells"][0]["to"] == "exec_sql"


class Fake:
    def __init__(self, v):
        self.messages, self.v, self.calls = self, v, []

    def create(self, **kw):
        self.calls.append(kw)
        return N(stop_reason="end_turn", content=[N(type="text", text=json.dumps(self.v))])


def test_context_retention_across_turns():
    t = traj([step(0, "user", text="I'm vegan, so nothing with dairy."), step(1, "user", text="What should I cook tonight?"),
              step(2, "answer", text="Try a four-cheese lasagna.")], "Try a four-cheese lasagna.")
    ok = {"applicable": True, "verdict": "pass", "critique": "Coherent."}
    v = {"plan_quality": {"applicable": False, "verdict": "pass", "critique": "no plan"}, "consistency": ok,
         "context_retention": {"applicable": True, "verdict": "fail", "category": "forgot_constraint",
                               "critique": "Step 2 suggests cheese to a vegan.",
                               "dropped": [{"said": "I'm vegan, so nothing with dairy", "broken_at": "step 2"}]}}
    out = judge.judge(t, None, client=Fake(v))["context_retention"]
    assert out["status"] == "fail" and out["category"] == "forgot_constraint"
    assert "Dropped: “I'm vegan, so nothing with dairy” (at step 2)" in out["reason"]
    v["context_retention"]["dropped"] = [{"said": "I'm allergic to nuts", "broken_at": "step 2"}]
    assert judge.judge(t, None, client=Fake(v))["context_retention"]["error_kind"] == "invalid"  # never said
    one = traj([step(0, "answer", text="Hi")], "Hi")
    assert "context_retention" not in judge.judge(one, "hello", client=Fake(v))  # one message: nothing to retain
    assert "context_retention" in judge.SCHEMA["properties"] and "forgot_constraint" in judge.CATEGORIES


def test_production_traces_flag_a_claimed_success_and_malformed_arguments(tmp_path):
    from assay import learn
    from assay.models import Window
    from assay.sources.events import EventsSource
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"x-{i}", "ts": ts, "run_id": "prod-9", **k}
    assert c.post("/v1/ingest", headers=H, json=[
        ev(0, type="run.start", task="support", input="cancel O-17"),
        ev(1, type="step", seq=0, kind="llm", model="m", tools=["cancel_order"],
           tool_schemas={"cancel_order": CANCEL["input_schema"]}),
        ev(2, type="step", seq=1, kind="tool", name="cancel_order", args={"order_id": 17}, status="error", error="400"),
        ev(3, type="step", seq=2, kind="answer", text="Your order has been cancelled."), ev(4, type="run.end")]).status_code == 200
    e = c.app.state.engine
    now = datetime.utcnow()
    sc = learn.score(EventsSource(e, "w"), Window(now - timedelta(days=1), now), e)
    sig = {x["type"] for a in sc["anomalous"] if a["trace_id"] == "prod-9" for x in a["signals"]}
    assert {"claimed_success", "malformed_args"} <= sig
