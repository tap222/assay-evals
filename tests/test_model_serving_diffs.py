import pytest
import assay_sdk as assay
from assay_sdk.serving import (
    ServingSetup,
    StepDiff,
    CaseServingDiff,
    ServingDiffReport,
    find_first_differing_step,
    compare_serving_setups,
    normalize_serving,
)
from assay_sdk.testing import (
    expect,
    assert_serving_matches,
    assert_no_stable_regressions,
    assert_serving_parity,
    assert_cost_savings,
)
from assay.changes import items as change_items


def test_serving_setup_normalization_and_summary():
    """ServingSetup stores and summarizes engine, quantization, and hardware."""
    setup = ServingSetup(
        quantization="4-bit",
        engine="vllm",
        engine_version="0.6.2",
        hardware="A100",
        batch_size=8,
    )
    assert setup.quantization == "4-bit"
    assert setup.engine == "vllm"
    assert setup.summary() == "vllm 0.6.2 4-bit (A100)"

    norm = normalize_serving({"quantization": "int8", "engine": "llama.cpp", "hardware": "Metal"})
    assert norm.quantization == "int8"
    assert norm.engine == "llama.cpp"
    assert norm.hardware == "Metal"


def test_record_serving_with_call_and_run():
    """Run and LLM step record the serving setup."""
    setup = ServingSetup(quantization="fp16", engine="vllm", engine_version="0.5.4", hardware="A100")

    with assay.run("summarize_email", serving=setup) as r:
        r.llm(model="llama-3.3-70b", tokens_in=500, tokens_out=100, cost_usd=0.01, text="Summary")
        r.answer("Summary")

    assert r.serving == setup
    llm_step = [s for s in r.steps if s["kind"] == "llm"][0]
    assert llm_step["serving"]["quantization"] == "fp16"
    assert llm_step["serving"]["engine"] == "vllm"

    expect(r).serving_matches(setup)
    expect(r).serving_matches({"quantization": "fp16", "engine": "vllm"})
    assert_serving_matches(r, setup)


def test_find_first_differing_step_in_agent_run():
    """Identify the exact first step that diverges in an agent trajectory."""
    # Baseline run: step 0 search, step 1 fetch_order, step 2 answer
    with assay.run("order_flow") as base:
        base.tool("search_user", {"email": "user@example.com"}, {"user_id": "u1"})
        base.tool("fetch_order", {"user_id": "u1"}, {"order_id": "O-9"})
        base.answer("Order O-9 found.")

    # Candidate run: step 0 identical, step 1 divergent tool, step 2 downstream divergence
    with assay.run("order_flow") as cand:
        cand.tool("search_user", {"email": "user@example.com"}, {"user_id": "u1"})
        # Divergence at step 1!
        cand.tool("lookup_all_orders", {"user_id": "u1"}, [{"order_id": "O-9"}])
        cand.answer("Orders: O-9.")

    diff = find_first_differing_step(base, cand)
    assert diff is not None
    assert diff.step_index == 1
    assert diff.kind == "tool"
    assert "lookup_all_orders" in diff.candidate_summary
    assert "fetch_order" in diff.baseline_summary
    assert "tool call differed" in diff.explanation


def test_find_first_differing_step_llm_output_changed():
    """Detect when model text output changes between serving setups."""
    with assay.run("sentiment") as base:
        base.llm(model="mistral-7b", text="positive", serving={"quantization": "fp16"})

    with assay.run("sentiment") as cand:
        cand.llm(model="mistral-7b", text="neutral", serving={"quantization": "4-bit"})

    diff = find_first_differing_step(base, cand)
    assert diff is not None
    assert diff.step_index == 0
    assert diff.kind == "llm"
    assert "model output changed" in diff.explanation


def test_find_first_differing_step_early_stop():
    """Detect when candidate stops earlier or later than baseline."""
    with assay.run("triage") as base:
        base.tool("check_cache", {}, "miss")
        base.tool("query_db", {}, "found")
        base.answer("Done")

    with assay.run("triage") as cand:
        cand.tool("check_cache", {}, "miss")
        # Candidate stopped early
        cand.answer("Done")

    diff = find_first_differing_step(base, cand)
    assert diff is not None
    assert diff.step_index == 1
    assert "stopped early" in diff.explanation


def test_compare_serving_setups_cost_and_latency_savings():
    """Compare baseline vs candidate runs under two serving setups: 4-bit saves 60%."""
    base_runs = []
    cand_runs = []

    b_setup = ServingSetup(quantization="fp16", engine="vllm", engine_version="0.5.4", hardware="A100")
    c_setup = ServingSetup(quantization="4-bit", engine="vllm", engine_version="0.6.2", hardware="L40S")

    # Case 1: identical output, candidate costs 60% less and is faster
    with assay.run("case_1", serving=b_setup) as r1_b:
        r1_b.llm(model="llama-3-8b", cost_usd=0.010, text="Answer 1")
        r1_b.answer("Answer 1")
    with assay.run("case_1", serving=c_setup) as r1_c:
        r1_c.llm(model="llama-3-8b", cost_usd=0.004, text="Answer 1")  # 60% savings
        r1_c.answer("Answer 1")

    # Case 2: identical output
    with assay.run("case_2", serving=b_setup) as r2_b:
        r2_b.llm(model="llama-3-8b", cost_usd=0.010, text="Answer 2")
        r2_b.answer("Answer 2")
    with assay.run("case_2", serving=c_setup) as r2_c:
        r2_c.llm(model="llama-3-8b", cost_usd=0.004, text="Answer 2")
        r2_c.answer("Answer 2")

    base_runs.extend([r1_b, r2_b])
    cand_runs.extend([r1_c, r2_c])

    report = compare_serving_setups(base_runs, cand_runs, baseline_setup=b_setup, candidate_setup=c_setup)

    assert report.total_cases == 2
    assert report.identical_cases_count == 2
    assert report.changed_cases_count == 0
    # Cost savings: ($0.020 - $0.008) / $0.020 = 60.0%
    assert abs(report.cost_savings_pct - 60.0) < 0.1
    assert report.setup_changes["quantization"] == ("fp16", "4-bit")
    assert report.setup_changes["hardware"] == ("A100", "L40S")

    # Test expectation helpers
    expect(report).cost_savings_at_least(50.0)
    expect(report).max_serving_divergence(0)
    expect(report).no_stable_regressions()
    assert_cost_savings(report, min_savings_pct=50.0)
    assert_serving_parity(report, max_divergent_cases=0)


def test_historically_stable_cases_listed_first():
    """Cases that gave the same answer every time on the old setup and now differ are ranked first."""
    b_setup = ServingSetup(quantization="16-bit", engine="vllm")
    c_setup = ServingSetup(quantization="4-bit", engine="vllm")

    # Case A: Flaky/unstable case that changed
    with assay.run("case_unstable", serving=b_setup) as r_a_b:
        r_a_b.answer("Old unstable answer")
    with assay.run("case_unstable", serving=c_setup) as r_a_c:
        r_a_c.answer("New unstable answer")

    # Case B: Historically stable case that changed!
    with assay.run("case_critical_stable", serving=b_setup) as r_b_b:
        r_b_b.answer("Gold answer")
    with assay.run("case_critical_stable", serving=c_setup) as r_b_c:
        r_b_c.answer("Different answer under 4-bit")

    # Case C: Unchanged case
    with assay.run("case_unchanged", serving=b_setup) as r_c_b:
        r_c_b.answer("Same answer")
    with assay.run("case_unchanged", serving=c_setup) as r_c_c:
        r_c_c.answer("Same answer")

    report = compare_serving_setups(
        [r_a_b, r_b_b, r_c_b],
        [r_a_c, r_b_c, r_c_c],
        baseline_setup=b_setup,
        candidate_setup=c_setup,
        historically_stable_cases={"case_critical_stable"}
    )

    assert report.total_cases == 3
    assert report.changed_cases_count == 2
    assert report.stable_regressions_count == 1

    # In sorted diffs: previously stable case must be first among changed cases!
    assert report.case_diffs[0].case_name == "case_critical_stable"
    assert report.case_diffs[0].was_previously_stable is True

    # Expectation should fail because a previously stable case changed
    with pytest.raises(AssertionError, match="previously stable case.*case_critical_stable"):
        assert_no_stable_regressions(report)


def test_serving_diff_report_summary():
    """Report summary produces clear, concise metrics with savings and stable tags."""
    b_setup = ServingSetup(quantization="fp16", engine="vllm", engine_version="0.5.4", hardware="A100")
    c_setup = ServingSetup(quantization="4-bit", engine="vllm", engine_version="0.6.2", hardware="L40S")

    with assay.run("stable_order", serving=b_setup) as rb:
        rb.llm(cost_usd=0.010, text="Answer A")
        rb.answer("Answer A")
    with assay.run("stable_order", serving=c_setup) as rc:
        rc.llm(cost_usd=0.004, text="Answer B")
        rc.answer("Answer B")

    report = compare_serving_setups(
        [rb], [rc],
        baseline_setup=b_setup, candidate_setup=c_setup,
        historically_stable_cases={"stable_order"}
    )
    summary = report.summary()
    assert "Model serving diff (vllm 0.6.2 4-bit (L40S) vs vllm 0.5.4 fp16 (A100))" in summary
    assert "saves 60.0%" in summary
    assert "1 changed out of 1 total (1 previously stable)" in summary
    assert "PREVIOUSLY STABLE" in summary


def test_changes_report_serving_change():
    """assay.changes surfaces model serving infrastructure changes."""
    run_report = {
        "serving_changes": [
            {"text": "model serving changed: vllm fp16 (A100) → vllm 4-bit (L40S) · 3 cases changed (1 previously stable)"}
        ]
    }
    items = change_items({}, run_report, [])
    kinds = [item["kind"] for item in items]
    assert "serving_change" in kinds
