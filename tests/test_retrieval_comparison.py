"""Tests for query-by-query retrieval comparison, label health checks, multi-stage dropout attribution,
latency percentiles, conjunctive multi-passage retrieval, and handoff page generation.
"""
import pytest
from pathlib import Path
from types import SimpleNamespace

import assay_sdk as assay
from assay_sdk import (
    DropoutAttribution,
    LabelHealthReport,
    Qrels,
    QueryRegression,
    QueryRetrievalResult,
    RetrievalComparisonReport,
    attribute_dropout,
    check_labels,
    compare_retrieval,
    compute_percentiles,
    conjunctive_recall,
    extract_retrieval_results,
    mrr,
    ndcg_at_k,
    paired_sign_test,
    paired_t_test,
    recall_at_k,
)
from assay_sdk.testing import (
    assert_labels_clean,
    assert_no_retrieval_regressions,
    assert_retrieval_latency,
    assert_retrieval_parity,
    expect,
)


def test_qrels_loading_and_conjunctive():
    # 1. From dict
    qrels = Qrels.from_dict({
        "q1": {"doc-1": 1.0, "doc-2": 2.0},
        "q2": ["doc-3", "doc-4"],
    })
    assert qrels.get_relevant("q1") == {"doc-1", "doc-2"}
    assert qrels.get_scores("q1") == {"doc-1": 1.0, "doc-2": 2.0}
    assert qrels.get_relevant("q2") == {"doc-3", "doc-4"}

    # 2. TREC qrels format
    trec_text = """
    # TREC format: query_id 0 doc_id relevance
    q10 0 doc-A 1
    q10 0 doc-B 2
    q20 0 doc-C 1
    """
    qrels_trec = Qrels.from_qrels(trec_text)
    assert qrels_trec.get_relevant("q10") == {"doc-A", "doc-B"}
    assert qrels_trec.get_scores("q10") == {"doc-A": 1.0, "doc-B": 2.0}

    # 3. CSV format with slice and conjunctive requirements
    csv_text = """query_id,doc_id,relevance,slice,required_together
q100,doc-X,1,legal,"doc-X,doc-Y"
q100,doc-Y,1,legal,"doc-X,doc-Y"
q200,doc-Z,1,faq,
"""
    qrels_csv = Qrels.from_csv(csv_text, conjunctive_col="required_together")
    assert qrels_csv.get_slice("q100") == "legal"
    assert qrels_csv.get_conjunctive("q100") == {"doc-X", "doc-Y"}
    assert qrels_csv.get_slice("q200") == "faq"


def test_label_health_checks():
    # Setup Qrels with a conflict
    qrels = Qrels()
    qrels.add("q1", "doc-1", 1.0)
    qrels.add("q1", "doc-1", 3.0)  # conflict!
    qrels.add("q2", "doc-2", 1.0)

    # Candidate retrieval retrieved doc-99 which has no label in qrels for q1
    res = [
        QueryRetrievalResult(query_id="q1", query="test q1", retrieved_docs=["doc-1", "doc-99"]),
        QueryRetrievalResult(query_id="q2", query="test q2", retrieved_docs=["doc-2"]),
    ]

    report = check_labels(res, qrels)
    assert not report.is_clean
    assert report.total_unlabeled_count == 1
    assert "doc-99" in report.unlabeled_retrieved["q1"]
    assert len(report.conflicting_labels) == 1
    assert report.conflicting_labels[0]["query_id"] == "q1"

    # Clean case
    clean_qrels = Qrels.from_dict({"q1": {"doc-1": 1.0, "doc-99": 0.0}})
    clean_report = check_labels([res[0]], clean_qrels)
    assert clean_report.is_clean


def test_retrieval_metrics():
    retrieved = ["doc-1", "doc-2", "doc-3", "doc-4", "doc-5"]
    relevant = {"doc-2", "doc-4", "doc-6"}
    scores = {"doc-2": 2.0, "doc-4": 1.0, "doc-6": 3.0}

    # Recall@3: in top 3, only doc-2 is present -> 1/3
    assert abs(recall_at_k(retrieved, relevant, k=3) - (1.0 / 3.0)) < 1e-6
    # Recall@5: in top 5, doc-2 and doc-4 are present -> 2/3
    assert abs(recall_at_k(retrieved, relevant, k=5) - (2.0 / 3.0)) < 1e-6

    # MRR: first relevant is doc-2 at position 2 -> 1/2 = 0.5
    assert abs(mrr(retrieved, relevant) - 0.5) < 1e-6
    # MRR when not found
    assert mrr(["x", "y"], {"z"}) == 0.0

    # nDCG@5:
    # retrieved top-5 has doc-2 (rel=2) at rank 2, doc-4 (rel=1) at rank 4
    # DCG = (2^2 - 1)/log2(3) + (2^1 - 1)/log2(5) = 3/1.58496 + 1/2.32193
    # IDCG has doc-6 (3) at rank 1, doc-2 (2) at rank 2, doc-4 (1) at rank 3
    ndcg = ndcg_at_k(retrieved, scores, k=5)
    assert 0.0 < ndcg < 1.0

    # Conjunctive recall: "All the evidence, not some"
    required = {"doc-2", "doc-4"}
    assert conjunctive_recall(retrieved, required, k=5) == 1.0
    # If k=3, doc-4 is not yet in top 3 -> 0.0
    assert conjunctive_recall(retrieved, required, k=3) == 0.0


def test_dropout_attribution():
    stages = [
        {
            "stage": "cluster_router",
            "selected_cluster": "kb_finance",
            "cluster": "kb_finance",
            "candidates": ["doc-1", "doc-2"],
        },
        {
            "stage": "dense_search",
            "candidates": ["doc-1", "doc-2", "doc-3"],
            "duration_ms": 25.0,
        },
        {
            "stage": "permission_filter",
            "candidates": ["doc-1", "doc-2"],
            "filtered_out": ["doc-3"],
            "filter_criteria": "tenant permission check",
            "duration_ms": 2.0,
        },
        {
            "stage": "cross_encoder_reranker",
            "candidates": ["doc-2", "doc-1"],
            "duration_ms": 40.0,
        },
    ]

    # 1. Router drops out
    attr_router = attribute_dropout(stages, "doc-legal-9", final_top_k=5, doc_metadata={"cluster": "kb_legal"})
    assert attr_router.stage == "cluster_router"
    assert attr_router.reason == "routed_away"

    # 2. Dense search never retrieved
    attr_search = attribute_dropout(stages, "doc-never-found", final_top_k=5)
    assert attr_search.stage == "dense_search"
    assert attr_search.reason == "never_in_candidates"

    # 3. Filtered out
    attr_filter = attribute_dropout(stages, "doc-3", final_top_k=5)
    assert attr_filter.stage == "permission_filter"
    assert attr_filter.reason == "filtered_out"

    # 4. Ranked too low by reranker
    rerank_stages = [
        {"stage": "dense_search", "candidates": [f"doc-{i}" for i in range(10)]},
        {"stage": "reranker", "candidates": [f"doc-{i}" for i in reversed(range(10))]},
    ]
    # doc-0 is at rank 10 in reranked candidates, which is below top 5
    attr_rank = attribute_dropout(rerank_stages, "doc-0", final_top_k=5)
    assert attr_rank.stage == "reranker"
    assert attr_rank.reason == "ranked_too_low"


def test_timings_and_percentiles():
    durations = [10.0, 20.0, 30.0, 40.0, 50.0, 100.0]
    p = compute_percentiles(durations)
    assert p.p50 > 0
    assert p.p95 >= p.p50
    assert p.p99 >= p.p95
    assert p.mean == round(250.0 / 6.0, 2)


def test_paired_statistical_significance():
    # Identical: no difference
    assert paired_sign_test([0.5, 0.8, 1.0], [0.5, 0.8, 1.0]) == 1.0
    assert paired_t_test([0.5, 0.8, 1.0], [0.5, 0.8, 1.0]) == 1.0

    # Significant improvement across many pairs
    b = [0.2] * 20
    c = [0.9] * 20
    assert paired_sign_test(b, c) < 0.01
    assert paired_t_test(b, c) < 0.01


def test_compare_retrieval_engine_and_regressions():
    qrels = Qrels.from_dict({
        "q1": {"doc-1": 1.0, "doc-2": 1.0},
        "q2": {"doc-3": 1.0},
        "q3": {"doc-5": 1.0, "doc-6": 1.0},
    }, slices={"q1": "support", "q2": "billing", "q3": "support"})

    # q3 needs both doc-5 and doc-6 ("all the evidence, not some")
    qrels.set_conjunctive("q3", {"doc-5", "doc-6"})

    # Baseline:
    # q1: retrieves doc-1 and doc-2 (Recall=1.0)
    # q2: retrieves doc-9 (Recall=0.0)
    # q3: retrieves doc-5 and doc-6 (Conjunctive pass=True)
    baseline_results = [
        QueryRetrievalResult(query_id="q1", query="q1 text", retrieved_docs=["doc-1", "doc-2"], duration_ms=45.0),
        QueryRetrievalResult(query_id="q2", query="q2 text", retrieved_docs=["doc-9"], duration_ms=50.0),
        QueryRetrievalResult(query_id="q3", query="q3 text", retrieved_docs=["doc-5", "doc-6"], duration_ms=60.0),
    ]

    # Candidate:
    # q1: drops doc-2 (Recall=0.5 -> REGRESSION!)
    # q2: finds doc-3 (Recall=1.0 -> RECOVERY!)
    # q3: drops doc-6 (Conjunctive fail!)
    candidate_results = [
        QueryRetrievalResult(
            query_id="q1",
            query="q1 text",
            retrieved_docs=["doc-1", "doc-other"],
            duration_ms=25.0,
            stages=[
                {"stage": "dense_search", "candidates": ["doc-1", "doc-2", "doc-other"]},
                {"stage": "filter", "candidates": ["doc-1", "doc-other"], "filtered_out": ["doc-2"]},
            ],
        ),
        QueryRetrievalResult(query_id="q2", query="q2 text", retrieved_docs=["doc-3"], duration_ms=30.0),
        QueryRetrievalResult(query_id="q3", query="q3 text", retrieved_docs=["doc-5", "doc-99"], duration_ms=35.0),
    ]

    report = compare_retrieval(
        baseline_results,
        candidate_results,
        qrels=qrels,
        k=5,
        baseline_name="BM25",
        candidate_name="HybridDense",
    )

    # 1. Regressions and recoveries
    assert len(report.regressions) >= 1
    reg_q1 = next(r for r in report.regressions if r.query_id == "q1")
    assert "doc-2" in reg_q1.dropped_docs
    assert len(reg_q1.dropout_attributions) == 1
    assert reg_q1.dropout_attributions[0].reason == "filtered_out"
    assert reg_q1.dropout_attributions[0].stage == "filter"

    # 2. Conjunctive failures
    assert len(report.conjunctive_failures) == 1
    assert report.conjunctive_failures[0]["query_id"] == "q3"
    assert "doc-6" in report.conjunctive_failures[0]["missing"]

    # 3. Recoveries
    assert len(report.recoveries) == 1
    assert report.recoveries[0].query_id == "q2"

    # 4. Latency
    assert report.candidate_timings["total"].p50 < report.baseline_timings["total"].p50

    # 5. Make regression tests
    guard_tests = report.make_regression_tests()
    assert len(guard_tests) >= 1
    assert any("doc-2" in t["target_doc"] for t in guard_tests)

    # 6. Handoff page generation
    handoff_md = report.generate_handoff_page()
    assert "# Retrieval Comparison Handoff" in handoff_md
    assert "Executive Summary" in handoff_md
    assert "Conjunctive Evidence Failures" in handoff_md
    assert "doc-2" in handoff_md


def test_expectations_and_assertions():
    # Setup clean report with 0 regressions
    qrels = Qrels.from_dict({"q1": {"d1": 1.0}})
    base = [QueryRetrievalResult(query_id="q1", query="q1", retrieved_docs=["d1"], duration_ms=50.0)]
    cand = [QueryRetrievalResult(query_id="q1", query="q1", retrieved_docs=["d1"], duration_ms=30.0)]

    clean_report = compare_retrieval(base, cand, qrels)

    # Passes
    expect(clean_report).no_retrieval_regressions().verify()
    expect(clean_report).recall_improved_or_equal().verify()
    expect(clean_report).latency_p95_below(100.0).verify()
    expect(clean_report).labels_clean().verify()
    expect(clean_report).all_conjunctive_evidence_retrieved().verify()

    assert_no_retrieval_regressions(clean_report)
    assert_retrieval_parity(clean_report)
    assert_labels_clean(clean_report)
    assert_retrieval_latency(clean_report, max_p95_ms=100.0)

    # Regression failure
    bad_cand = [QueryRetrievalResult(query_id="q1", query="q1", retrieved_docs=["wrong_doc"])]
    bad_report = compare_retrieval(base, bad_cand, qrels)

    with pytest.raises(AssertionError, match="no_retrieval_regressions"):
        expect(bad_report).no_retrieval_regressions(max_regressions=0).verify()


def test_run_retrieve_with_stages_and_criterion():
    # Test individual run recording with stages and retrieval criterion expectation
    assay.init()
    with assay.run("retrieval_case", test="test_q1") as r:
        r.retrieve(
            "where are the return policies?",
            [
                {"id": "kb-policy-return", "text": "Returns accepted within 30 days.", "score": 0.95},
                {"id": "kb-shipping", "text": "Free shipping over $50.", "score": 0.70},
            ],
            stages=[
                {"stage": "search", "candidates": ["kb-policy-return", "kb-shipping"]},
                {"stage": "rerank", "candidates": ["kb-policy-return", "kb-shipping"]},
            ],
            timings={"search_ms": 15.0, "rerank_ms": 22.0},
            slice="returns",
            query_id="q_return_policy",
        )
        r.answer("Returns are accepted within 30 days.")

    # Expect target document in top 1
    expect(r).retrieval_meets_criterion("kb-policy-return", max_rank=1).verify()

    # Fail if target document was not in top 1
    with pytest.raises(AssertionError, match="retrieval_meets_criterion"):
        expect(r).retrieval_meets_criterion("kb-shipping", max_rank=1).verify()


def test_assay_changes_retrieval_regression():
    from assay.changes import items

    run_with_retrieval_loss = {
        "retrieval_regressions": [
            {
                "text": "dropped kb-policy-return (filtered_out at permission_filter)",
                "detail": "recall fell from 1.0 to 0.5",
                "cases": ["test_support_retrieval"],
            }
        ]
    }

    found = items({}, run_with_retrieval_loss, [])
    assert any(x["kind"] == "retrieval_regression" for x in found)
    item = next(x for x in found if x["kind"] == "retrieval_regression")
    assert "dropped kb-policy-return" in item["text"]
