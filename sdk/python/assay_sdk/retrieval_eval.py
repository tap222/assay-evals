"""Query-by-query retrieval and reranker evaluation, comparison, and regression tracking.

Compares two retrievers or rerankers query by query against relevance labels (qrels),
computes recall@k, MRR, nDCG@k, conjunctive all-or-nothing recall, audits label health,
attributes intermediate stage dropouts, measures latency percentiles (p50, p95, p99),
performs paired significance testing, generates client/reviewer handoff pages, and
turns regressions into executable tests.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union


# ---------------------------------------------------------------------------
# Qrels and Label Health
# ---------------------------------------------------------------------------

class Qrels:
    """Relevance labels mapping query_id -> {doc_id: relevance_score}."""

    def __init__(
        self,
        data: Optional[Dict[str, Dict[str, float]]] = None,
        slices: Optional[Dict[str, str]] = None,
        conjunctive: Optional[Dict[str, Set[str]]] = None,
    ):
        self.data: Dict[str, Dict[str, float]] = {}
        self.slices: Dict[str, str] = dict(slices or {})
        self.conjunctive: Dict[str, Set[str]] = {
            qid: set(docs) for qid, docs in (conjunctive or {}).items()
        }
        self.conflicts: List[Dict[str, Any]] = []
        self.raw_entries_count: int = 0

        if data:
            for qid, docs in data.items():
                if isinstance(docs, dict):
                    for doc_id, score in docs.items():
                        self.add(qid, doc_id, float(score))
                elif isinstance(docs, (list, set, tuple)):
                    for doc_id in docs:
                        self.add(qid, doc_id, 1.0)

    def add(
        self,
        query_id: str,
        doc_id: str,
        relevance: float = 1.0,
        slice: Optional[str] = None,
    ) -> None:
        qid, did = str(query_id), str(doc_id)
        self.raw_entries_count += 1
        if qid not in self.data:
            self.data[qid] = {}
        if did in self.data[qid] and abs(self.data[qid][did] - float(relevance)) > 1e-6:
            self.conflicts.append({
                "query_id": qid,
                "doc_id": did,
                "first_relevance": self.data[qid][did],
                "conflicting_relevance": float(relevance),
            })
        self.data[qid][did] = float(relevance)
        if slice:
            self.slices[qid] = str(slice)

    def set_conjunctive(self, query_id: str, required_docs: Iterable[str]) -> None:
        """Query needs all these documents retrieved to pass ('all the evidence, not some')."""
        self.conjunctive[str(query_id)] = {str(d) for d in required_docs}

    def set_slice(self, query_id: str, slice_name: str) -> None:
        self.slices[str(query_id)] = str(slice_name)

    def get_relevant(self, query_id: str, threshold: float = 1.0) -> Set[str]:
        scores = self.data.get(str(query_id), {})
        return {doc_id for doc_id, score in scores.items() if score >= threshold}

    def get_scores(self, query_id: str) -> Dict[str, float]:
        return dict(self.data.get(str(query_id), {}))

    def get_slice(self, query_id: str) -> Optional[str]:
        return self.slices.get(str(query_id))

    def get_conjunctive(self, query_id: str) -> Optional[Set[str]]:
        return self.conjunctive.get(str(query_id))

    @classmethod
    def from_dict(
        cls,
        d: Dict[str, Any],
        slices: Optional[Dict[str, str]] = None,
        conjunctive: Optional[Dict[str, Iterable[str]]] = None,
    ) -> Qrels:
        return cls(data=d, slices=slices, conjunctive=conjunctive)

    @classmethod
    def from_qrels(cls, path_or_str: Union[str, Path]) -> Qrels:
        """Parse standard TREC qrels format: query_id 0 doc_id relevance."""
        text = path_or_str
        if isinstance(path_or_str, Path) or (isinstance(path_or_str, str) and "\n" not in path_or_str and Path(path_or_str).is_file()):
            text = Path(path_or_str).read_text(encoding="utf-8")

        qrels = cls()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 4:
                qid, _, did, rel = parts[0], parts[1], parts[2], parts[3]
            elif len(parts) == 3:
                qid, did, rel = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                qid, did, rel = parts[0], parts[1], 1.0
            else:
                continue
            try:
                score = float(rel)
            except ValueError:
                score = 1.0
            qrels.add(qid, did, score)
        return qrels

    @classmethod
    def from_csv(
        cls,
        path_or_str: Union[str, Path],
        query_col: str = "query_id",
        doc_col: str = "doc_id",
        score_col: str = "relevance",
        slice_col: Optional[str] = None,
        conjunctive_col: Optional[str] = None,
    ) -> Qrels:
        """Parse relevance labels from CSV file or string."""
        text = path_or_str
        if isinstance(path_or_str, Path) or (isinstance(path_or_str, str) and "\n" not in path_or_str and Path(path_or_str).is_file()):
            text = Path(path_or_str).read_text(encoding="utf-8")

        reader = csv.DictReader(io.StringIO(text))
        qrels = cls()
        for row in reader:
            qid = row.get(query_col) or row.get("query") or row.get("qid")
            did = row.get(doc_col) or row.get("doc") or row.get("document_id")
            if not qid or not did:
                continue
            rel_str = row.get(score_col) or row.get("score") or "1"
            try:
                rel = float(rel_str)
            except ValueError:
                rel = 1.0
            slice_val = row.get(slice_col) if slice_col else row.get("slice")
            qrels.add(qid, did, rel, slice=slice_val)
            if conjunctive_col and row.get(conjunctive_col):
                # comma-separated required doc IDs
                reqs = [d.strip() for d in str(row[conjunctive_col]).split(",") if d.strip()]
                qrels.set_conjunctive(qid, reqs)
        return qrels


@dataclass
class LabelHealthReport:
    """Audit results of relevance labels before scoring."""
    unlabeled_retrieved: Dict[str, List[str]] = field(default_factory=dict)
    conflicting_labels: List[Dict[str, Any]] = field(default_factory=list)
    different_scales: Dict[str, Any] = field(default_factory=dict)
    total_unlabeled_count: int = 0
    total_queries_affected: int = 0

    @property
    def is_clean(self) -> bool:
        return (
            self.total_unlabeled_count == 0
            and len(self.conflicting_labels) == 0
            and not self.different_scales.get("has_scale_mismatch", False)
        )

    def summary(self) -> str:
        lines = []
        if self.is_clean:
            return "Label health check: All labels clean (no missing judgments or conflicts)."
        lines.append("Label Health Check Findings:")
        if self.total_unlabeled_count > 0:
            lines.append(
                f"  • {self.total_unlabeled_count} retrieved documents have no label across "
                f"{self.total_queries_affected} queries (missing judgments drag retrievers down)"
            )
        if self.conflicting_labels:
            lines.append(
                f"  • {len(self.conflicting_labels)} conflicting label assignments (same doc labeled multiple ways)"
            )
        if self.different_scales.get("has_scale_mismatch"):
            lines.append(
                f"  • Inconsistent label scales detected across queries: {self.different_scales.get('scales_found')}"
            )
        return "\n".join(lines)


def check_labels(
    retrieval_results: Iterable[Any],
    qrels: Qrels,
) -> LabelHealthReport:
    """Check labels before scoring: unlabeled retrieved docs, conflicting labels, scale mismatches."""
    unlabeled_by_query: Dict[str, List[str]] = {}
    total_unlabeled = 0

    for item in retrieval_results:
        qid = getattr(item, "query_id", None) or (item.get("query_id") if isinstance(item, dict) else None)
        if not qid:
            qid = getattr(item, "query", None) or (item.get("query") if isinstance(item, dict) else None)
        if not qid:
            continue
        qid = str(qid)
        retrieved = getattr(item, "retrieved_docs", None) or getattr(item, "retrieved", None)
        if retrieved is None and isinstance(item, dict):
            retrieved = item.get("retrieved_docs") or item.get("retrieved") or item.get("fragments")
        doc_ids = []
        if isinstance(retrieved, list):
            for r in retrieved:
                did = r.get("id") if isinstance(r, dict) else str(r)
                if did:
                    doc_ids.append(str(did))

        known_labels = qrels.data.get(qid, {})
        unlabeled = [did for did in doc_ids if did not in known_labels]
        if unlabeled:
            unlabeled_by_query[qid] = unlabeled
            total_unlabeled += len(unlabeled)

    # Check scale consistency
    max_scores = [max(scores.values()) for scores in qrels.data.values() if scores]
    scales_found = set()
    for s in max_scores:
        if s <= 1.0:
            scales_found.add("binary_or_normalized (<= 1.0)")
        elif s <= 5.0:
            scales_found.add("graded_5 (<= 5.0)")
        elif s <= 100.0:
            scales_found.add("scale_100 (<= 100.0)")
        else:
            scales_found.add(f"scale_{int(s)}")

    has_scale_mismatch = len(scales_found) > 1

    return LabelHealthReport(
        unlabeled_retrieved=unlabeled_by_query,
        conflicting_labels=list(qrels.conflicts),
        different_scales={"has_scale_mismatch": has_scale_mismatch, "scales_found": sorted(scales_found)},
        total_unlabeled_count=total_unlabeled,
        total_queries_affected=len(unlabeled_by_query),
    )


# ---------------------------------------------------------------------------
# Metrics: Recall@k, MRR, nDCG@k, Conjunctive Recall
# ---------------------------------------------------------------------------

def recall_at_k(retrieved: List[str], relevant: Set[str], k: int = 5) -> float:
    """Share of relevant documents found in top-k."""
    if not relevant:
        return 0.0
    top_k = retrieved[:k]
    hits = sum(1 for d in top_k if d in relevant)
    return hits / float(len(relevant))


def mrr(retrieved: List[str], relevant: Set[str], k: Optional[int] = None) -> float:
    """Mean Reciprocal Rank of the first relevant document."""
    if not relevant:
        return 0.0
    items = retrieved[:k] if k is not None else retrieved
    for rank, doc_id in enumerate(items, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: List[str], relevance_scores: Dict[str, float], k: int = 10) -> float:
    """Normalized Discounted Cumulative Gain at rank k."""
    if not relevance_scores:
        return 0.0
    items = retrieved[:k]
    dcg = 0.0
    for rank, doc_id in enumerate(items, start=1):
        rel = relevance_scores.get(doc_id, 0.0)
        if rel > 0:
            dcg += (2.0 ** rel - 1.0) / math.log2(rank + 1.0)

    ideal_rels = sorted(relevance_scores.values(), reverse=True)[:k]
    idcg = 0.0
    for rank, rel in enumerate(ideal_rels, start=1):
        if rel > 0:
            idcg += (2.0 ** rel - 1.0) / math.log2(rank + 1.0)

    if idcg <= 0.0:
        return 0.0
    return min(1.0, dcg / idcg)


def conjunctive_recall(retrieved: List[str], required: Set[str], k: Optional[int] = None) -> float:
    """All the evidence, not some: 1.0 if ALL required documents are in top-k, else 0.0."""
    if not required:
        return 1.0
    top_k = set(retrieved[:k] if k is not None else retrieved)
    return 1.0 if required.issubset(top_k) else 0.0


# ---------------------------------------------------------------------------
# Multi-Stage Candidate Tracking and Dropout Attribution
# ---------------------------------------------------------------------------

@dataclass
class DropoutAttribution:
    """Pinpoints intermediate stage where a relevant document dropped out."""
    doc_id: str
    stage: str
    reason: str  # "routed_away", "never_in_candidates", "filtered_out", "ranked_too_low", "unknown"
    explanation: str


def attribute_dropout(
    stages: Optional[List[Dict[str, Any]]],
    target_doc_id: str,
    final_top_k: int = 5,
    doc_metadata: Optional[Dict[str, Any]] = None,
) -> DropoutAttribution:
    """Report missed document at the exact stage that dropped it."""
    doc_id = str(target_doc_id)
    if not stages:
        return DropoutAttribution(
            doc_id=doc_id,
            stage="retrieval",
            reason="never_in_candidates",
            explanation=f"Document {doc_id} was never retrieved in top {final_top_k} results (no pipeline stages recorded).",
        )

    prev_candidates: Optional[Set[str]] = None
    for stage_info in stages:
        s_name = str(stage_info.get("stage") or stage_info.get("name") or "stage")
        cands = [str(c.get("id") if isinstance(c, dict) else c) for c in (stage_info.get("candidates") or [])]
        cands_set = set(cands)

        # Check router / cluster choice
        if "router" in s_name.lower() or "cluster" in s_name.lower():
            selected_cluster = stage_info.get("selected_cluster") or stage_info.get("cluster")
            expected_cluster = (doc_metadata or {}).get("cluster") or stage_info.get("target_cluster")
            if selected_cluster and expected_cluster and selected_cluster != expected_cluster:
                return DropoutAttribution(
                    doc_id=doc_id,
                    stage=s_name,
                    reason="routed_away",
                    explanation=f"Routed to wrong cluster '{selected_cluster}' instead of '{expected_cluster}'.",
                )

        # Check filtering stage
        filtered_out = {str(f.get("id") if isinstance(f, dict) else f) for f in (stage_info.get("filtered_out") or [])}
        if doc_id in filtered_out:
            filter_reason = stage_info.get("filter_criteria") or "metadata/permission filters"
            return DropoutAttribution(
                doc_id=doc_id,
                stage=s_name,
                reason="filtered_out",
                explanation=f"Filtered out by {filter_reason} at stage '{s_name}'.",
            )

        # Check candidate presence
        if prev_candidates is not None and doc_id in prev_candidates and doc_id not in cands_set:
            return DropoutAttribution(
                doc_id=doc_id,
                stage=s_name,
                reason="filtered_out",
                explanation=f"Present in candidate pool before stage '{s_name}', but dropped during filtering.",
            )

        # If candidate generation stage (e.g. dense_search or retriever)
        if ("search" in s_name.lower() or "retriev" in s_name.lower() or "index" in s_name.lower()) and cands:
            if doc_id not in cands_set:
                return DropoutAttribution(
                    doc_id=doc_id,
                    stage=s_name,
                    reason="never_in_candidates",
                    explanation=f"Dense/sparse search at stage '{s_name}' never retrieved document in candidates.",
                )

        prev_candidates = cands_set

    # Check reranker stage
    final_stage = stages[-1]
    final_name = str(final_stage.get("stage") or final_stage.get("name") or "reranker")
    final_cands = [str(c.get("id") if isinstance(c, dict) else c) for c in (final_stage.get("candidates") or [])]
    if doc_id in final_cands:
        rank = final_cands.index(doc_id) + 1
        return DropoutAttribution(
            doc_id=doc_id,
            stage=final_name,
            reason="ranked_too_low",
            explanation=f"Document was in candidate pool but reranker ranked it #{rank} (below top {final_top_k}).",
        )

    return DropoutAttribution(
        doc_id=doc_id,
        stage=final_name,
        reason="ranked_too_low",
        explanation=f"Document dropped out before final top {final_top_k} results.",
    )


# ---------------------------------------------------------------------------
# Statistics and Timings
# ---------------------------------------------------------------------------

@dataclass
class TimingPercentiles:
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    mean: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"p50": self.p50, "p95": self.p95, "p99": self.p99, "mean": self.mean}


def compute_percentiles(values: List[float]) -> TimingPercentiles:
    if not values:
        return TimingPercentiles()
    s = sorted(values)
    n = len(s)
    mean_val = sum(s) / n

    def p(pct: float) -> float:
        idx = max(0, min(n - 1, int(round((pct / 100.0) * (n - 1)))))
        return s[idx]

    return TimingPercentiles(
        p50=round(p(50), 2),
        p95=round(p(95), 2),
        p99=round(p(99), 2),
        mean=round(mean_val, 2),
    )


def paired_sign_test(baseline: List[float], candidate: List[float]) -> float:
    """Exact two-tailed paired sign test using binomial distribution."""
    diffs = [c - b for b, c in zip(baseline, candidate) if abs(c - b) > 1e-9]
    n = len(diffs)
    if n == 0:
        return 1.0
    pos = sum(1 for d in diffs if d > 0)
    k = min(pos, n - pos)
    prob = sum(math.comb(n, i) * (0.5 ** n) for i in range(k + 1))
    return min(1.0, 2.0 * prob)


def paired_t_test(baseline: List[float], candidate: List[float]) -> float:
    """Paired t-test using normal approximation on sample differences."""
    diffs = [c - b for b, c in zip(baseline, candidate)]
    n = len(diffs)
    if n < 2:
        return 1.0
    mean_diff = sum(diffs) / n
    variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    if variance <= 1e-12:
        return 1.0 if abs(mean_diff) < 1e-9 else 0.0
    std_err = math.sqrt(variance / n)
    t_stat = mean_diff / std_err
    # Two-tailed p-value via complementary error function
    p_val = math.erfc(abs(t_stat) / math.sqrt(2))
    return min(1.0, max(0.0, p_val))


# ---------------------------------------------------------------------------
# Query Retrieval and Comparison Models
# ---------------------------------------------------------------------------

@dataclass
class QueryRetrievalResult:
    """One query's recorded retrieval run."""
    query_id: str
    query: str
    retrieved_docs: List[str]
    scores: Optional[List[float]] = None
    slice: Optional[str] = None
    stages: Optional[List[Dict[str, Any]]] = None
    timings: Optional[Dict[str, float]] = None
    filters: Optional[Dict[str, Any]] = None
    concurrency: Optional[int] = None
    duration_ms: Optional[float] = None


@dataclass
class QueryRegression:
    """A query that got worse in the candidate configuration."""
    query_id: str
    query: str
    slice: Optional[str]
    baseline_rank: Optional[int]
    candidate_rank: Optional[int]
    baseline_recall: float
    candidate_recall: float
    baseline_mrr: float
    candidate_mrr: float
    dropped_docs: List[str]
    dropout_attributions: List[DropoutAttribution]
    evidence: Dict[str, Any]


@dataclass
class RetrievalComparisonReport:
    """Full comparison of two retrieval configurations query by query."""
    baseline_name: str
    candidate_name: str
    k: int
    ndcg_k: int
    total_queries: int
    regressions: List[QueryRegression]
    recoveries: List[QueryRegression]
    shared_failures: List[Dict[str, Any]]
    conjunctive_failures: List[Dict[str, Any]]
    baseline_metrics: Dict[str, float]
    candidate_metrics: Dict[str, float]
    slice_metrics: Dict[str, Dict[str, Dict[str, float]]]
    baseline_timings: Dict[str, TimingPercentiles]
    candidate_timings: Dict[str, TimingPercentiles]
    paired_p_value: float
    is_significant: bool
    label_health: LabelHealthReport

    def summary(self) -> str:
        base_rec = self.baseline_metrics.get("recall@k", 0.0)
        cand_rec = self.candidate_metrics.get("recall@k", 0.0)
        delta_rec = cand_rec - base_rec
        sign = "+" if delta_rec >= 0 else ""
        sig_str = " (statistically significant, p < 0.05)" if self.is_significant else " (not statistically significant)"

        lines = [
            f"Retrieval Comparison: {self.candidate_name} vs {self.baseline_name} (k={self.k})",
            f"  Queries: {self.total_queries} total | {len(self.regressions)} regressions | {len(self.recoveries)} recoveries | {len(self.shared_failures)} shared failures",
            f"  Recall@{self.k}: {base_rec:.3f} -> {cand_rec:.3f} ({sign}{delta_rec * 100.0:.1f}%){sig_str}",
            f"  MRR: {self.baseline_metrics.get('mrr', 0.0):.3f} -> {self.candidate_metrics.get('mrr', 0.0):.3f}",
            f"  nDCG@{self.ndcg_k}: {self.baseline_metrics.get('ndcg@k', 0.0):.3f} -> {self.candidate_metrics.get('ndcg@k', 0.0):.3f}",
        ]
        if self.conjunctive_failures:
            lines.append(
                f"  Conjunctive ('all the evidence') failures: {len(self.conjunctive_failures)} queries missed required multi-passage evidence"
            )

        # Timings
        base_tot = self.baseline_timings.get("total", TimingPercentiles())
        cand_tot = self.candidate_timings.get("total", TimingPercentiles())
        if base_tot.p50 > 0 or cand_tot.p50 > 0:
            lines.append(
                f"  Latency p50/p95/p99: {base_tot.p50}/{base_tot.p95}/{base_tot.p99}ms -> {cand_tot.p50}/{cand_tot.p95}/{cand_tot.p99}ms"
            )

        if not self.label_health.is_clean:
            lines.append(f"  Warning: {self.label_health.summary()}")

        if self.regressions:
            lines.append("  Top Regressions:")
            for reg in self.regressions[:3]:
                drop_summary = ", ".join(f"{d.doc_id} ({d.reason} at {d.stage})" for d in reg.dropout_attributions) or str(reg.dropped_docs)
                lines.append(f"    • [{reg.query_id}] '{reg.query}': dropped {drop_summary}")

        return "\n".join(lines)

    def generate_handoff_page(self, format: str = "markdown") -> str:
        """Generate a complete Markdown handoff report for clients or approval reviewers."""
        base_rec = self.baseline_metrics.get("recall@k", 0.0)
        cand_rec = self.candidate_metrics.get("recall@k", 0.0)
        delta_rec = cand_rec - base_rec
        sign = "+" if delta_rec >= 0 else ""

        verdict = "SAFE TO DEPLOY" if len(self.regressions) == 0 and delta_rec >= 0 else "REVIEW REGRESSIONS"
        if delta_rec < -0.02:
            verdict = "BLOCK: QUALITY REGRESSION"

        md = [
            f"# Retrieval Comparison Handoff: {self.candidate_name}",
            "",
            f"**Verdict:** `{verdict}`  ",
            f"**Baseline:** `{self.baseline_name}` | **Candidate:** `{self.candidate_name}` | **Total Queries:** `{self.total_queries}`  ",
            f"**Statistical Significance:** p={self.paired_p_value:.4f} ({'Significant' if self.is_significant else 'Inconclusive'})  ",
            "",
            "## 1. Executive Summary",
            "",
            "| Metric | Baseline | Candidate | Delta |",
            "|---|---|---|---|",
            f"| **Recall@{self.k}** | {base_rec:.4f} | {cand_rec:.4f} | {sign}{delta_rec * 100.0:.2f}% |",
            f"| **MRR** | {self.baseline_metrics.get('mrr', 0.0):.4f} | {self.candidate_metrics.get('mrr', 0.0):.4f} | {self.candidate_metrics.get('mrr', 0.0) - self.baseline_metrics.get('mrr', 0.0):+.4f} |",
            f"| **nDCG@{self.ndcg_k}** | {self.baseline_metrics.get('ndcg@k', 0.0):.4f} | {self.candidate_metrics.get('ndcg@k', 0.0):.4f} | {self.candidate_metrics.get('ndcg@k', 0.0) - self.baseline_metrics.get('ndcg@k', 0.0):+.4f} |",
            f"| **Conjunctive Success** | {self.baseline_metrics.get('conjunctive_rate', 1.0)*100.0:.1f}% | {self.candidate_metrics.get('conjunctive_rate', 1.0)*100.0:.1f}% | {(self.candidate_metrics.get('conjunctive_rate', 1.0) - self.baseline_metrics.get('conjunctive_rate', 1.0))*100.0:+.1f}% |",
            "",
            "## 2. Latency & Platform Timing (ms)",
            "",
            "| Stage / Metric | Base p50 | Base p95 | Base p99 | Cand p50 | Cand p95 | Cand p99 |",
            "|---|---|---|---|---|---|---|",
        ]

        all_timing_keys = sorted(set(list(self.baseline_timings.keys()) + list(self.candidate_timings.keys())))
        for stage in all_timing_keys:
            bt = self.baseline_timings.get(stage, TimingPercentiles())
            ct = self.candidate_timings.get(stage, TimingPercentiles())
            md.append(f"| `{stage}` | {bt.p50} | {bt.p95} | {bt.p99} | {ct.p50} | {ct.p95} | {ct.p99} |")

        if self.slice_metrics:
            md.extend([
                "",
                "## 3. Slice Breakdown",
                "",
                "| Slice | Baseline Recall | Candidate Recall | Regressions | Recoveries |",
                "|---|---|---|---|---|",
            ])
            for sl, vals in sorted(self.slice_metrics.items()):
                b_s = vals.get("baseline", {}).get("recall@k", 0.0)
                c_s = vals.get("candidate", {}).get("recall@k", 0.0)
                regs = sum(1 for r in self.regressions if r.slice == sl)
                recs = sum(1 for r in self.recoveries if r.slice == sl)
                md.append(f"| `{sl}` | {b_s:.3f} | {c_s:.3f} | {regs} | {recs} |")

        if not self.label_health.is_clean:
            md.extend([
                "",
                "## 4. Label Health Checks",
                "",
                f"> [!WARNING]",
                f"> {self.label_health.summary()}",
                "",
            ])

        md.extend([
            "",
            "## 5. Regressions: Dropped Documents & Dropout Attribution",
            "",
            f"Total regressions: **{len(self.regressions)}**",
            "",
        ])
        if self.regressions:
            md.append("| Query ID | Query | Dropped Document(s) | Dropout Stage | Reason |")
            md.append("|---|---|---|---|---|")
            for reg in self.regressions:
                for attr in reg.dropout_attributions:
                    md.append(
                        f"| `{reg.query_id}` | {reg.query[:40]} | `{attr.doc_id}` | `{attr.stage}` | {attr.reason} |"
                    )
        else:
            md.append("*No regressions detected across evaluated queries.*")

        if self.conjunctive_failures:
            md.extend([
                "",
                "## 6. Conjunctive Evidence Failures ('All the evidence, not some')",
                "",
                "Questions requiring multiple mandatory passages where candidate retrieved only partial evidence:",
                "",
                "| Query ID | Required Docs | Missing Docs | Retrieved in Top-k |",
                "|---|---|---|---|",
            ])
            for cf in self.conjunctive_failures:
                md.append(
                    f"| `{cf.get('query_id')}` | `{cf.get('required')}` | `{cf.get('missing')}` | `{cf.get('retrieved')}` |"
                )

        tests = self.make_regression_tests()
        if tests:
            md.extend([
                "",
                "## 7. Generated Regression Guard Tests",
                "",
                "These cases can be added to CI to hold future changes to the baseline standard:",
                "",
                "```python",
                "# Guard cases generated from observed retrieval losses",
                "CASES = [",
            ])
            for t in tests[:5]:
                md.append(f"    {t},")
            md.extend([
                "]",
                "```",
            ])

        return "\n".join(md)

    def make_regression_tests(self) -> List[Dict[str, Any]]:
        """Turn each regression into a persistent test case with success criterion."""
        cases = []
        for reg in self.regressions:
            for doc_id in reg.dropped_docs:
                target_rank = reg.baseline_rank if reg.baseline_rank is not None else self.k
                cases.append({
                    "name": f"retrieval_loss_{reg.query_id}_{doc_id}",
                    "query_id": reg.query_id,
                    "query": reg.query,
                    "target_doc": doc_id,
                    "min_rank": target_rank,
                    "slice": reg.slice,
                    "criterion": f"{doc_id} in top {target_rank}",
                })
        return cases


# ---------------------------------------------------------------------------
# Extraction and Main Comparison Engine
# ---------------------------------------------------------------------------

def _to_query_result(item: Any) -> Optional[QueryRetrievalResult]:
    """Convert a Run, dict, or QueryRetrievalResult into QueryRetrievalResult."""
    if isinstance(item, QueryRetrievalResult):
        return item
    if hasattr(item, "steps") or (isinstance(item, dict) and "steps" in item):
        steps = item.steps if hasattr(item, "steps") else item["steps"]
        ret_steps = [s for s in steps if (isinstance(s, dict) and s.get("kind") == "retrieval") or getattr(s, "kind", None) == "retrieval"]
        if not ret_steps:
            return None
        s = ret_steps[0]
        s_dict = s if isinstance(s, dict) else s.__dict__
        q = s_dict.get("query")
        qid = s_dict.get("query_id") or (item.test if hasattr(item, "test") else item.get("test") if isinstance(item, dict) else None) or q
        frags = s_dict.get("fragments") or []
        doc_ids = [f.get("id") if isinstance(f, dict) else str(f) for f in frags]
        scores = [f.get("score") for f in frags if isinstance(f, dict) and f.get("score") is not None]
        return QueryRetrievalResult(
            query_id=str(qid or "q"),
            query=str(q or ""),
            retrieved_docs=doc_ids,
            scores=scores if scores else None,
            slice=s_dict.get("slice"),
            stages=s_dict.get("stages"),
            timings=s_dict.get("timings"),
            filters=s_dict.get("filters"),
            duration_ms=s_dict.get("duration_ms"),
        )
    if isinstance(item, dict):
        qid = item.get("query_id") or item.get("id") or item.get("query")
        q = item.get("query") or str(qid or "")
        docs = item.get("retrieved_docs") or item.get("retrieved") or item.get("docs") or []
        doc_ids = [d.get("id") if isinstance(d, dict) else str(d) for d in docs]
        return QueryRetrievalResult(
            query_id=str(qid or "q"),
            query=str(q),
            retrieved_docs=doc_ids,
            scores=item.get("scores"),
            slice=item.get("slice"),
            stages=item.get("stages"),
            timings=item.get("timings"),
            filters=item.get("filters"),
            duration_ms=item.get("duration_ms"),
        )
    return None


def extract_retrieval_results(runs_or_items: Any) -> List[QueryRetrievalResult]:
    """Extract list of QueryRetrievalResult from runs, dicts, or results."""
    items = runs_or_items if isinstance(runs_or_items, (list, tuple, set)) else [runs_or_items]
    out = []
    for it in items:
        res = _to_query_result(it)
        if res:
            out.append(res)
    return out


def compare_retrieval(
    baseline: Any,
    candidate: Any,
    qrels: Union[Qrels, Dict[str, Any], str, Path],
    k: int = 5,
    ndcg_k: int = 10,
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
    primary_metric: str = "recall",
) -> RetrievalComparisonReport:
    """Compare two retrieval setups query by query against relevance labels."""
    if not isinstance(qrels, Qrels):
        if isinstance(qrels, dict):
            qrels_obj = Qrels.from_dict(qrels)
        elif isinstance(qrels, (str, Path)):
            qrels_obj = Qrels.from_qrels(qrels)
        else:
            qrels_obj = Qrels()
    else:
        qrels_obj = qrels

    base_list = extract_retrieval_results(baseline)
    cand_list = extract_retrieval_results(candidate)

    base_by_id = {r.query_id: r for r in base_list}
    cand_by_id = {r.query_id: r for r in cand_list}

    # Match queries
    common_ids = [qid for qid in cand_by_id if qid in base_by_id]
    if not common_ids:
        # Fall back to matching by query text
        base_by_q = {r.query: r for r in base_list}
        cand_by_q = {r.query: r for r in cand_list}
        common_queries = [q for q in cand_by_q if q in base_by_q]
        common_ids = [cand_by_q[q].query_id for q in common_queries]
        base_by_id = {base_by_q[q].query_id: base_by_q[q] for q in common_queries}
        cand_by_id = {cand_by_q[q].query_id: cand_by_q[q] for q in common_queries}

    label_health = check_labels(base_list + cand_list, qrels_obj)

    regressions: List[QueryRegression] = []
    recoveries: List[QueryRegression] = []
    shared_failures: List[Dict[str, Any]] = []
    conjunctive_failures: List[Dict[str, Any]] = []

    base_recalls: List[float] = []
    cand_recalls: List[float] = []
    base_mrrs: List[float] = []
    cand_mrrs: List[float] = []
    base_ndcgs: List[float] = []
    cand_ndcgs: List[float] = []
    base_conjunctive: List[float] = []
    cand_conjunctive: List[float] = []

    slice_data: Dict[str, Dict[str, Dict[str, List[float]]]] = {}

    for qid in common_ids:
        b_res = base_by_id[qid]
        c_res = cand_by_id[qid]

        relevant_docs = qrels_obj.get_relevant(qid)
        scores_map = qrels_obj.get_scores(qid)
        conj_required = qrels_obj.get_conjunctive(qid)
        query_slice = qrels_obj.get_slice(qid) or b_res.slice or c_res.slice

        b_rec = recall_at_k(b_res.retrieved_docs, relevant_docs, k=k)
        c_rec = recall_at_k(c_res.retrieved_docs, relevant_docs, k=k)
        b_mrr = mrr(b_res.retrieved_docs, relevant_docs, k=k)
        c_mrr = mrr(c_res.retrieved_docs, relevant_docs, k=k)
        b_ndcg = ndcg_at_k(b_res.retrieved_docs, scores_map, k=ndcg_k)
        c_ndcg = ndcg_at_k(c_res.retrieved_docs, scores_map, k=ndcg_k)

        b_conj = conjunctive_recall(b_res.retrieved_docs, conj_required, k=k) if conj_required else 1.0
        c_conj = conjunctive_recall(c_res.retrieved_docs, conj_required, k=k) if conj_required else 1.0

        base_recalls.append(b_rec)
        cand_recalls.append(c_rec)
        base_mrrs.append(b_mrr)
        cand_mrrs.append(c_mrr)
        base_ndcgs.append(b_ndcg)
        cand_ndcgs.append(c_ndcg)
        base_conjunctive.append(b_conj)
        cand_conjunctive.append(c_conj)

        if query_slice:
            if query_slice not in slice_data:
                slice_data[query_slice] = {"baseline": {"recall": []}, "candidate": {"recall": []}}
            slice_data[query_slice]["baseline"]["recall"].append(b_rec)
            slice_data[query_slice]["candidate"]["recall"].append(c_rec)

        # Check conjunctive requirements ("all the evidence, not some")
        if conj_required and c_conj < 1.0:
            missing = conj_required - set(c_res.retrieved_docs[:k])
            conjunctive_failures.append({
                "query_id": qid,
                "query": c_res.query,
                "required": sorted(conj_required),
                "missing": sorted(missing),
                "retrieved": c_res.retrieved_docs[:k],
            })

        # Compare primary metric
        primary_b = b_rec if primary_metric == "recall" else b_mrr
        primary_c = c_rec if primary_metric == "recall" else c_mrr

        b_top_k = set(b_res.retrieved_docs[:k])
        c_top_k = set(c_res.retrieved_docs[:k])
        dropped = sorted((b_top_k & relevant_docs) - c_top_k)

        if primary_c < primary_b - 1e-6 or (conj_required and b_conj > c_conj):
            # Regression: found before, missed now
            attributions = [
                attribute_dropout(c_res.stages, did, final_top_k=k)
                for did in dropped
            ]
            first_b_rank = next((i + 1 for i, d in enumerate(b_res.retrieved_docs) if d in relevant_docs), None)
            first_c_rank = next((i + 1 for i, d in enumerate(c_res.retrieved_docs) if d in relevant_docs), None)

            regressions.append(QueryRegression(
                query_id=qid,
                query=c_res.query,
                slice=query_slice,
                baseline_rank=first_b_rank,
                candidate_rank=first_c_rank,
                baseline_recall=b_rec,
                candidate_recall=c_rec,
                baseline_mrr=b_mrr,
                candidate_mrr=c_mrr,
                dropped_docs=dropped,
                dropout_attributions=attributions,
                evidence={
                    "query": c_res.query,
                    "baseline_top_k": b_res.retrieved_docs[:k],
                    "candidate_top_k": c_res.retrieved_docs[:k],
                    "labels": scores_map,
                },
            ))
        elif primary_c > primary_b + 1e-6:
            # Recovery: missed before, found now
            first_b_rank = next((i + 1 for i, d in enumerate(b_res.retrieved_docs) if d in relevant_docs), None)
            first_c_rank = next((i + 1 for i, d in enumerate(c_res.retrieved_docs) if d in relevant_docs), None)
            recovered = sorted((c_top_k & relevant_docs) - b_top_k)
            recoveries.append(QueryRegression(
                query_id=qid,
                query=c_res.query,
                slice=query_slice,
                baseline_rank=first_b_rank,
                candidate_rank=first_c_rank,
                baseline_recall=b_rec,
                candidate_recall=c_rec,
                baseline_mrr=b_mrr,
                candidate_mrr=c_mrr,
                dropped_docs=[],
                dropout_attributions=[],
                evidence={
                    "query": c_res.query,
                    "recovered_docs": recovered,
                    "baseline_top_k": b_res.retrieved_docs[:k],
                    "candidate_top_k": c_res.retrieved_docs[:k],
                },
            ))
        elif primary_b <= 1e-6 and primary_c <= 1e-6:
            shared_failures.append({
                "query_id": qid,
                "query": c_res.query,
                "relevant_docs": sorted(relevant_docs),
                "candidate_top_k": c_res.retrieved_docs[:k],
            })

    # Statistical significance: paired sign test and t-test
    p_val_sign = paired_sign_test(base_recalls, cand_recalls)
    p_val_t = paired_t_test(base_recalls, cand_recalls)
    p_val = min(p_val_sign, p_val_t)
    is_significant = p_val < 0.05

    # Compute aggregate metrics
    n = max(1, len(common_ids))
    base_metrics = {
        "recall@k": sum(base_recalls) / n,
        "mrr": sum(base_mrrs) / n,
        "ndcg@k": sum(base_ndcgs) / n,
        "conjunctive_rate": sum(base_conjunctive) / n,
    }
    cand_metrics = {
        "recall@k": sum(cand_recalls) / n,
        "mrr": sum(cand_mrrs) / n,
        "ndcg@k": sum(cand_ndcgs) / n,
        "conjunctive_rate": sum(cand_conjunctive) / n,
    }

    # Slice metrics
    slice_metrics: Dict[str, Dict[str, Dict[str, float]]] = {}
    for sl, vals in slice_data.items():
        b_r = vals["baseline"]["recall"]
        c_r = vals["candidate"]["recall"]
        slice_metrics[sl] = {
            "baseline": {"recall@k": sum(b_r) / len(b_r) if b_r else 0.0},
            "candidate": {"recall@k": sum(c_r) / len(c_r) if c_r else 0.0},
        }

    # Timings
    def extract_timings(results: List[QueryRetrievalResult]) -> Dict[str, TimingPercentiles]:
        totals = [r.duration_ms for r in results if r.duration_ms is not None]
        stages_map: Dict[str, List[float]] = {}
        for r in results:
            if r.timings:
                for st_name, val in r.timings.items():
                    if isinstance(val, (int, float)):
                        stages_map.setdefault(st_name, []).append(float(val))
            if r.stages:
                for st in r.stages:
                    st_name = st.get("stage") or st.get("name")
                    dur = st.get("duration_ms")
                    if st_name and isinstance(dur, (int, float)):
                        stages_map.setdefault(str(st_name), []).append(float(dur))

        out_timings = {"total": compute_percentiles(totals)}
        for st_name, vals in stages_map.items():
            out_timings[st_name] = compute_percentiles(vals)
        return out_timings

    baseline_timings = extract_timings(base_list)
    candidate_timings = extract_timings(cand_list)

    return RetrievalComparisonReport(
        baseline_name=baseline_name,
        candidate_name=candidate_name,
        k=k,
        ndcg_k=ndcg_k,
        total_queries=len(common_ids),
        regressions=regressions,
        recoveries=recoveries,
        shared_failures=shared_failures,
        conjunctive_failures=conjunctive_failures,
        baseline_metrics=base_metrics,
        candidate_metrics=cand_metrics,
        slice_metrics=slice_metrics,
        baseline_timings=baseline_timings,
        candidate_timings=candidate_timings,
        paired_p_value=p_val,
        is_significant=is_significant,
        label_health=label_health,
    )
