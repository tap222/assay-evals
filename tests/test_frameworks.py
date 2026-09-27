"""Metrics from DeepEval and RAGAS as Assay checks (assay_sdk/frameworks.py). Neither library is a
dependency: these metrics are shaped like theirs, and the last tests use the real ones when installed."""
import os
import subprocess
import sys

import pytest

from assay_sdk import frameworks
from assay_sdk.evaluation import ERROR, FAIL, PASS

from test_ci import PYTEST, project, run  # noqa: F401


class Exact:
    """Shaped like a DeepEval metric: measure(), threshold, score, success, reason."""
    threshold, evaluation_model, error, skipped = 1.0, "gpt-4.1", None, False

    def __init__(self, fail_with=None):
        self.fail_with, self.score, self.success, self.reason = fail_with, None, None, None

    @property
    def __name__(self):
        return "Exact Match"

    def measure(self, test_case, _show_indicator=True):
        if self.fail_with:
            raise self.fail_with
        if test_case.expected_output is None:
            raise ValueError("'expected_output' cannot be None for the 'Exact Match' metric")
        self.score = float(test_case.actual_output == test_case.expected_output)
        self.success = self.score >= self.threshold
        self.reason = "match" if self.success else "The actual and expected outputs are different."
        return self.score

    def is_successful(self):
        return self.success


class Case:
    def __init__(self, actual, expected="10 days", input="How long?"):
        self.input, self.actual_output, self.expected_output = input, actual, expected


class Presence:
    """Shaped like ragas.metrics.collections: a name, score(**fields) giving a MetricResult."""
    name = "string_present"

    def score(self, reference, response):
        return type("MetricResult", (), {"value": float(reference in response), "reason": None})()


class Classic:
    """Shaped like a classic RAGAS metric: single_turn_score(sample)."""
    name = "exact_match"

    def single_turn_score(self, sample):
        return float(sample["reference"] == sample["response"])


class Run:
    def __init__(self):
        self.checks = []

    def check(self, field, status, **kw):
        self.checks.append({"field": field, "status": status, **kw})


def test_a_deepeval_metric_is_a_check():
    r = Run()
    assert frameworks.kind(Exact()) == "deepeval"
    ok = frameworks.check(r, Exact(), Case("10 days"))
    bad = frameworks.check(r, Exact(), Case("5 days"))
    assert (ok.status, bad.status, bad.score) == (PASS, FAIL, 0.0)
    assert [c["field"] for c in r.checks] == ["exact_match", "exact_match"]
    assert r.checks[1]["status"] == "fail" and r.checks[1]["evaluator"] == "deepeval:Exact Match"
    assert r.checks[1]["judge_model"] == "gpt-4.1" and "different" in r.checks[1]["reason"]
    assert r.checks[1]["inputs"] == {"query": "How long?", "generation": "5 days", "reference": "10 days"}


def test_what_a_metric_couldnt_judge_isnt_a_zero():
    r = Run()
    missing = frameworks.check(r, Exact(), Case("10 days", expected=None))
    assert missing.status == ERROR and missing.score is None and "expected_output" in missing.error
    assert r.checks[0]["status"] == "error"  # recorded, and never counted against the app
    timeout = type("TimeoutError", (Exception,), {})
    t = frameworks.check(Run(), Exact(fail_with=timeout("the judge took too long")), Case("10 days"))
    assert t.status == "TIMEOUT" and t.attempts == 3  # asked again, like any evaluator


def test_a_threshold_given_here_decides_instead_of_the_metrics():
    lenient = Exact()
    lenient.threshold = 0.0  # the metric's own would pass anything
    assert frameworks.check(Run(), lenient, Case("5 days")).status == PASS
    assert frameworks.check(Run(), lenient, Case("5 days"), threshold=1.0).status == FAIL


def test_a_ragas_metric_needs_a_threshold_and_takes_its_fields():
    r = Run()
    with pytest.raises(ValueError, match="no threshold of its own"):
        frameworks.check(r, Presence(), reference="days", response="10 days")
    assert frameworks.check(r, Presence(), threshold=1.0, reference="days", response="10 days").status == PASS
    assert frameworks.check(r, Presence(), threshold=1.0, reference="weeks", response="10 days").status == FAIL
    assert r.checks[0]["evaluator"] == "ragas:string_present" and r.checks[0]["field"] == "string_present"
    sample = {"reference": "10 days", "response": "10 days"}
    assert frameworks.check(r, Classic(), sample, threshold=1.0).status == PASS


def test_anything_else_goes_through_evaluate():
    with pytest.raises(TypeError, match="pass a function to evaluate"):
        frameworks.as_judge(object())


SUITE = '''
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from metrics import Exact, Case
from assay_sdk.frameworks import assert_test

def answer(q):
    return "5 days" if os.environ.get("MODE") == "after" and "return" in q else "10 days"

def test_refund_window():  # an existing DeepEval-style test: no assay_case fixture
    assert_test(Case(answer("refund")), [Exact()])

def test_return_window():
    assert_test(Case(answer("return")), [Exact()])
'''


def suite(project):
    (project / "tests").mkdir()
    src = open(__file__).read()
    (project / "tests" / "metrics.py").write_text(src[src.index("class Exact:"):src.index("class Presence:")])
    (project / "tests" / "test_existing.py").write_text(SUITE)


def test_an_existing_suite_changes_one_import(project):
    suite(project)
    assert run(project).returncode == 0  # each test an Assay case, each metric a check
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1
    assert "tests/test_existing.py::test_return_window  exact_match" in out.stdout
    assert any(x.startswith("✗ exact_match") and "1/2   100% → 50%" in x for x in out.stdout.splitlines())
    assert "Metrics failed: Exact Match (score 0, threshold 1.0): The actual and expected outputs are different." \
        in out.stdout
    # What DeepEval alone can't do: a failure someone accepted doesn't fail every later run.
    from assay.__main__ import main
    assert main(["accept"]) == 0
    assert run(project, env={"MODE": "after"}).returncode == 0
    # And plain pytest still fails the test, as DeepEval's assert_test does.
    plain = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                           capture_output=True, text=True, cwd=project, env={**os.environ, "MODE": "after"})
    assert plain.returncode == 1 and "Metrics failed: Exact Match" in plain.stdout


def test_a_metric_one_test_doesnt_use_isnt_missing(project):
    suite(project)
    (project / "tests" / "test_existing.py").write_text(SUITE + '''
def test_greeting():
    assert_test(Case("hi", expected="hi"), [])
''')
    out = run(project)
    assert out.returncode == 0 and "Missing" not in out.stdout


def test_with_the_real_deepeval():
    deepeval = pytest.importorskip("deepeval")
    from deepeval.metrics import ExactMatchMetric
    from deepeval.test_case import LLMTestCase
    r = Run()
    assert frameworks.kind(ExactMatchMetric()) == "deepeval"
    res = frameworks.check(r, ExactMatchMetric(), LLMTestCase(input="q", actual_output="5", expected_output="10"))
    assert res.status == FAIL and r.checks[0]["evaluator"] == "deepeval:Exact Match"
    missing = frameworks.check(Run(), ExactMatchMetric(), LLMTestCase(input="q", actual_output="5"))
    assert missing.status == ERROR and "expected_output" in missing.error, deepeval.__version__


def test_with_the_real_ragas():
    pytest.importorskip("ragas")
    from ragas.metrics.collections import ExactMatch
    res = frameworks.check(Run(), ExactMatch(), threshold=1.0, reference="10 days", response="10 days")
    assert res.status == PASS
