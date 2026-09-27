from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.config import Settings
from assay.failures import classify, diff_shape, reason_pattern


def test_diff_shapes():
    assert diff_shape("1240.00", "$1,240.00") == "format_only"
    assert diff_shape("Acme Supply Co", "ACME SUPPLY CO") == "format_only"
    assert diff_shape("2026-03-07", "2026-07-03") == "date_swap"
    assert diff_shape("2026-03-07", "2026-03-09") == "date_other"
    assert diff_shape("1240", "12.40") == "scale"
    assert diff_shape("1240", "1100.36") == "number_off"
    assert diff_shape("INV-12345", "INV-123") == "truncated"
    assert diff_shape("invoice", "contract") == "label_mismatch"
    assert diff_shape("x", None) == "missing" and diff_shape(None, "x") == "unexpected"
    assert diff_shape("same", "same") == "identical"


def test_reason_pattern_masks_specifics():
    a = reason_pattern("schema violation: 'total' is a string ('16871.67'), expected number")
    b = reason_pattern("schema violation: 'total' is a string ('40.10'), expected number")
    assert a == b


def base(**kw):
    return {"status": "fail", "shape": "different_text", "origin": "eval", "expected": "a b", "actual": "c d",
            "field": "f", "signals": [], "doc_failed": []} | kw


def test_classify_each_kind():
    assert classify(base(status="error", reason="judge endpoint returned 503"))[0] == "infrastructure"
    assert classify(base(status="error", reason="judge returned invalid JSON"))[:2] == ("evaluator", "evaluator_error")
    assert classify(base(shape="format_only"))[:2] == ("evaluator", "format_only")
    assert classify(base(disagreement=True))[:2] == ("evaluator", "disagreement")
    assert classify(base(shape="missing", doc_failed=["field_extraction"]))[:2] == ("infrastructure", "step_failed")
    assert classify(base(verdict="upstream", text_actual=True, text_expected=False))[:2] == \
        ("evaluator", "expected_not_in_source")
    assert classify(base(verdict="upstream", origin_ai=True))[:2] == ("ai", "lost_in_text")
    assert classify(base(verdict="after"))[:2] == ("evaluator", "reads_other_value")
    assert classify(base(verdict="after", origin="production"))[:2] == ("infrastructure", "delivery")
    assert classify(base(verdict="introduced", origin_stage="x", origin_ai=True))[:2] == ("ai", "introduced")
    # A fallback because the primary wasn't confident is still the AI's answer; one because it was down isn't.
    low = ["a fallback tier answered (fallback_1: low_confidence)"]
    assert classify(base(verdict="introduced", signals=low))[0] == "ai"
    assert classify(base(verdict="introduced", signals=["a fallback tier answered (fallback_1: timeout)"]))[0] == \
        "infrastructure"


# ---------- a two-run evaluation, end to end ----------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


VENDORS = ["Acme Supply Co", "Blue Harbor Freight", "Crestline Medical"]


def send_run(client, run_id, start, after, cases=60):
    """Each case: a date, a vendor and a total. `after` is the release: dates read day-first on
    US documents (a regression), vendors upper-cased (intended), and a stretch of timeouts."""
    lineage = {"prompt": "extract@v2" if after else "extract@v1", "build": "b2" if after else "b1"}
    docs, runs, results = [], [], []
    for i in range(cases):
        did, ts = f"{run_id}/c{i}", start + timedelta(seconds=30 * i)
        month, day = 3, 1 + i % 12
        date = f"2026-{month:02d}-{day:02d}"
        us = i % 2 == 0
        vendor, total = VENDORS[i % 3], f"{1000 + i}.00"
        text = f"INVOICE {vendor} Date: {month:02d}/{day:02d}/2026 Total due ${int(total[:-3]):,}.00 " + "x" * 80
        got_date = f"2026-{day:02d}-{month:02d}" if after and us and day != month else date
        timeout = after and 40 <= i < 46
        fields = None if timeout else {"date": got_date, "vendor": vendor, "total": total}
        final = None if timeout else dict(fields, vendor=vendor.upper() if after else vendor)
        docs.append({"document_id": did, "received_at": ts.isoformat(), "completed_at": ts.isoformat()})
        for k, (stage, out, prompt, status) in enumerate([
                ("text_extraction", {"_text": text}, "ocr@v1", "success"),
                ("field_extraction", fields, lineage["prompt"], "timeout" if timeout else "success"),
                ("validation", final, None, "success")]):
            runs.append({"document_id": did, "stage": stage, "status": status, "sequence": k, "outputs": out,
                         "started_at": (ts + timedelta(seconds=k)).isoformat(),
                         **({"prompt_id": prompt.split("@")[0], "prompt_version": prompt.split("@")[1]} if prompt else {})})
        expected = {"date": date, "vendor": vendor, "total": f"${int(total[:-3]):,}.00" if i % 5 == 0 else total}
        for f, exp in expected.items():
            act = (final or {}).get(f)
            results.append({"run_id": run_id, "case_id": f"c{i}", "document_id": did, "field": f, "expected": exp,
                            "actual": act, "status": "pass" if act == exp else "fail", "evaluator": "exact@1",
                            "ts": ts.isoformat(), "lineage": lineage})
        if after and i in (3, 13, 23, 33):
            results.append({"run_id": run_id, "case_id": f"c{i}", "document_id": did, "field": "vendor",
                            "status": "error", "evaluator": "judge@1", "reason": "judge request timed out",
                            "ts": ts.isoformat(), "lineage": lineage})
    r = client.post("/v1/events", json={"documents": docs, "stage_runs": runs, "eval_results": results},
                    headers={"X-Tenant": "t"})
    assert r.status_code == 200, r.text


def test_evaluation_run_is_grouped_into_causes_of_each_kind(client):
    src = "events:t"
    now = datetime.utcnow()
    send_run(client, "r1", now - timedelta(days=3), after=False)
    send_run(client, "r2", now - timedelta(hours=3), after=True)
    runs = client.get("/v1/evals/runs", params={"source": src}).json()
    assert [r["run_id"] for r in runs] == ["r2", "r1"]

    first = client.get("/v1/evals/runs/r1/failures", params={"source": src}).json()
    assert [(g["kind"], g["mechanism"]) for g in first["groups"]] == [("evaluator", "format_only")]

    out = client.get("/v1/evals/runs/r2/failures", params={"source": src}).json()
    assert out["scope"]["baseline"] == "r1" and set(out["scope"]["changes"]) == {"prompt", "build"}
    kinds = {(g["kind"], g["mechanism"]): g for g in out["groups"]}

    dates = kinds[("ai", "introduced")]
    assert dates["regression"] is True and dates["fields"] == {"date": dates["failures"]}
    assert "extract@v1 → extract@v2" in " ".join(dates["evidence"])  # the prompt, not the build

    vendors = kinds[("intended_change", "format_only")]
    assert vendors["fields"] == {"vendor": 54} and "build b1 → b2" in " ".join(vendors["evidence"])

    strict = next(g for g in out["groups"] if g["kind"] == "evaluator" and g["mechanism"] == "format_only")
    assert strict["persisting"] == strict["failures"]  # already failing before the release

    timeouts = kinds[("infrastructure", "step_failed")]
    assert timeouts["stage"] == "field_extraction" and timeouts["burst"] is not None
    assert kinds[("infrastructure", "harness")]["failures"] == 4

    # Accept the vendor change; its new expected values can be exported.
    client.put("/v1/failures/decisions", json={"source": src, "key": vendors["key"], "decision": "accepted_change"})
    again = client.get("/v1/evals/runs/r2/failures", params={"source": src}).json()
    g = next(x for x in again["groups"] if x["key"] == vendors["key"])
    assert g["decision"]["decision"] == "accepted_change"
    assert again["groups"][-1]["key"] == vendors["key"] or again["groups"][-1]["decision"]  # decided ones sink
    exp = client.get("/v1/evals/runs/r2/expectations", params={"source": src, "key": vendors["key"]}).json()
    assert len(exp) == 54 and exp[0]["new_expected"] == exp[0]["old_expected"].upper()


def test_bad_decision_is_rejected(client):
    r = client.put("/v1/failures/decisions", json={"source": "events:t", "key": "k", "decision": "ignore"})
    assert r.status_code == 422


def test_reported_errors_are_grouped_too(client):
    now = datetime.utcnow() - timedelta(hours=5)
    docs, runs, errors = [], [], []
    for i in range(40):
        did, ts = f"d{i}", now + timedelta(minutes=i)
        docs.append({"document_id": did, "received_at": ts.isoformat(), "completed_at": ts.isoformat()})
        runs += [{"document_id": did, "stage": "extract", "status": "success", "sequence": 0,
                  "started_at": ts.isoformat(), "outputs": {"total": "12.40" if i < 6 else "1240.00"}},
                 {"document_id": did, "stage": "deliver", "status": "success", "sequence": 1,
                  "started_at": ts.isoformat(), "outputs": {"total": "12.40" if i < 6 else "1240.00"}}]
        if i < 6:
            errors.append({"document_id": did, "field": "total", "expected": "1240.00", "observed": "12.40"})
    client.post("/v1/events", json={"documents": docs, "stage_runs": runs, "errors": errors}, headers={"X-Tenant": "p"})
    out = client.get("/v1/failures", params={"source": "events:p"}).json()
    g = out["groups"][0]
    assert out["failures"] == 6 and g["kind"] == "ai" and g["stage"] == "extract" and g["shapes"] == {
        "off by a power of ten": 6}
    assert client.get("/v1/failures", params={"source": "events:nobody"}).status_code == 404


def test_eval_gate_reruns_a_flaky_drop_and_records_it(client):
    now = datetime.utcnow()
    for run, start, flaky_now in (("g1", now - timedelta(days=2), False), ("g2", now - timedelta(hours=1), True)):
        results = []
        for i in range(5):
            for k in range(6):
                bad = flaky_now and i < 2 and k % 2 == 1  # 6/6 → 3/6 on two of five checks: plausible, unproven
                results.append({"run_id": run, "case_id": f"c{i}", "field": "total", "expected": "10",
                                "actual": "11" if bad else "10", "status": "fail" if bad else "pass",
                                "evaluator": "exact@1", "attempt": k,
                                "ts": (start + timedelta(seconds=i * 6 + k)).isoformat()})
        client.post("/v1/events/eval-results", json=results, headers={"X-Tenant": "g"})
    st = client.get("/v1/evals/runs/g2/stability", params={"source": "events:g"}).json()
    assert st["outcome"] == "rerun" and st["states"]["needs_reruns"] == 2
    assert st["reruns"][0]["outcomes"] == "●○●○●○" and st["reruns"][0]["reruns"] > 0
    g = client.post("/v1/evals/runs/g2/gate", json={"source": "events:g"}).json()
    assert g["outcome"] == "rerun" and g["lineage"]["eval_run"] == "g2" and g["lineage"]["baseline_run"] == "g1"
    assert client.get("/v1/gates").json()[0]["outcome"] == "rerun"
