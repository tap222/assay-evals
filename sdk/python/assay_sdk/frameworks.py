"""Metrics you already have, from DeepEval or RAGAS, as Assay checks.

A metric scores one output. Recorded as a check, it's also compared with its baseline, told
apart from noise across attempts, kept apart when it couldn't be judged (a missing parameter,
a timeout: never a score of 0), and labeled with the model that judged. Neither library is a
dependency: each is imported only when one of its metrics is passed.

An existing DeepEval suite changes one import:

    from assay_sdk.frameworks import assert_test          # was: from deepeval import assert_test

    def test_answer():
        test_case = LLMTestCase(input="...", actual_output=my_app("..."), expected_output="...")
        assert_test(test_case, [AnswerRelevancyMetric(threshold=0.7), ExactMatchMetric()])

Under `pytest --assay` (or `assay test`) the test becomes an Assay case with one check per
metric, and fails, as DeepEval's does, when a metric fails. Outside, it's DeepEval's assert_test
with Assay's validity rules. A metric on its own, in any test:

    from assay_sdk.frameworks import check

    check(assay_case, AnswerRelevancyMetric(threshold=0.7), test_case)             # DeepEval
    check(assay_case, Faithfulness(llm=llm), threshold=0.8,                         # RAGAS
          user_input=q, response=answer, retrieved_contexts=docs)

Anything else that scores (another framework, your own function) goes through evaluate().
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable, List, Optional

from assay_sdk.evaluation import FAIL, PASS, Result, evaluate

__all__ = ["check", "as_judge", "assert_test", "kind"]


def kind(metric: Any) -> Optional[str]:
    """Which framework a metric is from: "deepeval", "ragas" or None. By shape, so a metric of
    your own that follows one of them works too."""
    mod = type(metric).__module__.split(".")[0]
    if mod == "deepeval" or (hasattr(metric, "measure") and hasattr(metric, "threshold") and hasattr(metric, "success")):
        return "deepeval"
    if mod == "ragas" or hasattr(metric, "single_turn_score") or (
            hasattr(metric, "name") and (hasattr(metric, "ascore") or callable(getattr(metric, "score", None)))):
        return "ragas"
    return None


def name_of(metric: Any) -> str:
    """The metric's name: "Answer Relevancy", "faithfulness"."""
    n = getattr(metric, "__name__", None) or getattr(metric, "name", None) or type(metric).__name__
    return str(n)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "metric"


def _model(metric: Any) -> Optional[str]:
    """The model that judges, where the metric says: DeepEval's evaluation_model, RAGAS's llm."""
    m = getattr(metric, "evaluation_model", None)
    if isinstance(m, str) and m:
        return m
    llm = getattr(metric, "llm", None)
    for attr in ("model", "model_name"):
        v = getattr(llm, attr, None)
        if isinstance(v, str) and v:
            return v
    return None


def _deepeval(metric: Any, own: bool = True) -> Callable:
    """own: the metric decides pass or fail (its threshold, strict mode, metrics where lower is
    better); otherwise only its score is returned, for a threshold given here."""
    def judge(test_case):
        m = copy.copy(metric)  # a metric keeps its last score on itself: one copy per judgement
        try:
            m.measure(test_case, _show_indicator=False)
        except TypeError:  # a metric of your own without DeepEval's private keyword
            m.measure(test_case)
        if getattr(m, "error", None):
            raise RuntimeError(f"{name_of(m)}: {m.error}")
        if getattr(m, "skipped", False):
            raise RuntimeError(f"{name_of(m)} skipped this test case")
        if not own:
            return {"score": m.score, "reason": m.reason}
        passed = m.is_successful() if hasattr(m, "is_successful") else m.success
        return {"score": m.score, "passed": bool(passed), "reason": m.reason}
    return judge


def _ragas(metric: Any) -> Callable:
    def judge(sample=None, **fields):
        if hasattr(metric, "single_turn_score"):  # the classic API: a SingleTurnSample
            if sample is None:
                from ragas.dataset_schema import SingleTurnSample
                sample = SingleTurnSample(**fields)
            out = metric.single_turn_score(sample)
        else:  # ragas.metrics.collections: named fields, a MetricResult back
            out = metric.score(**({**(sample if isinstance(sample, dict) else {}), **fields}))
        value = getattr(out, "value", out)
        return {"score": value, "reason": getattr(out, "reason", None)}
    return judge


def as_judge(metric: Any, threshold: Optional[float] = None) -> Callable:
    """The metric as a judge for evaluate(): called with a DeepEval test case, or RAGAS's fields.
    A DeepEval metric decides pass or fail itself, unless `threshold` is given."""
    k = kind(metric)
    if k == "deepeval":
        return _deepeval(metric, own=threshold is None)
    if k == "ragas":
        return _ragas(metric)
    raise TypeError(f"{type(metric).__name__} isn't a DeepEval or RAGAS metric: pass a function to evaluate() instead")


def check(run, metric: Any, test_case: Any = None, *, field: Optional[str] = None, threshold: Optional[float] = None,
          **fields) -> Result:
    """Score with the metric and record it on `run` (a test case's run) as a check.

    DeepEval: pass its test case; the metric's own threshold decides, unless `threshold` is
    given. RAGAS: pass the sample, or its fields as keywords, and `threshold` (RAGAS metrics have
    none of their own). The field is the metric's name ("answer_relevancy") unless `field` says."""
    k = kind(metric)
    name = name_of(metric)
    if k == "ragas" and threshold is None:
        raise ValueError(f"{name}: a RAGAS metric has no threshold of its own; pass threshold=, e.g. 0.8")
    if k == "deepeval" and test_case is None:
        raise ValueError(f"{name}: pass the DeepEval test case it scores")
    args = (test_case,) if test_case is not None else ()
    inputs = _inputs(test_case, fields)
    # DeepEval decides pass/fail itself (strict mode, metrics where lower is better), unless a
    # threshold is given here; its score is checked for range either way.
    return evaluate(as_judge(metric, threshold), *args, threshold=threshold if threshold is not None else
                    (None if k == "deepeval" else 0.5), run=run, field=field or _slug(name),
                    evaluator=f"{k}:{name}", judge_model=_model(metric), inputs=inputs or None, **fields)


def _inputs(test_case: Any, fields: dict) -> dict:
    """What the metric saw, by the roles Assay audits against the trace (see docs/verdicts.md)."""
    src = {**{k: getattr(test_case, k, None) for k in ("input", "actual_output", "expected_output",
                                                         "retrieval_context", "context")}, **fields}
    roles = {"input": "query", "user_input": "query", "actual_output": "generation", "response": "generation",
             "expected_output": "reference", "reference": "reference", "retrieval_context": "context",
             "retrieved_contexts": "context"}
    return {roles[k]: v for k, v in src.items() if k in roles and v not in (None, "", [])}


def assert_test(test_case: Any, metrics: List[Any], run=None, **kwargs) -> List[Result]:
    """DeepEval's assert_test, with each metric recorded as an Assay check: fails the test when a
    metric fails, and says which, with its score, threshold and reason. A metric that couldn't be
    judged (a missing parameter, a timeout) fails it too, as DeepEval's does, but is recorded as
    not judged, never as a failure of your app. `run` is the test's Assay run; under pytest it's
    found (or opened) for the running test."""
    if run is None:
        from assay_sdk import pytest_plugin
        run = pytest_plugin.current_case()
    results = [check(run, m, test_case) for m in metrics] if run is not None else \
        [evaluate(as_judge(m), test_case, threshold=None, field=_slug(name_of(m))) for m in metrics]
    bad = [(m, r) for m, r in zip(metrics, results) if r.status != PASS]
    if bad:
        lines = []
        for m, r in bad:
            what = f"score {r.score:g}, threshold {getattr(m, 'threshold', None)}" if r.status == FAIL and \
                r.score is not None else f"not judged: {r.error}"
            lines.append(f"{name_of(m)} ({what})" + (f": {r.reason}" if r.reason else ""))
        raise AssertionError("Metrics failed: " + "; ".join(lines))
    return results
