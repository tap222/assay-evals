[Assay](../README.md) › [Documentation](README.md)

# Metrics you already have: DeepEval and RAGAS

A suite built on DeepEval or RAGAS already has metrics people trust. Assay runs them as checks,
so each one is also:

- **compared with its baseline:** a metric that failed before and fails now isn't news; one that
  passed and fails now is a regression, named with its case;
- **told apart from noise:** with repeats, a metric that wavers is flaky, not a red build
  ([Repeated attempts](repeats.md));
- **kept apart when it couldn't be judged:** a missing parameter, a timeout or a rate limit is
  "couldn't be judged", never a score of 0 ([Results you can trust](verdicts.md));
- **labeled with the model that judged,** and whether that judge was ever calibrated against
  people ([Judge calibration](calibration.md)).

Neither library is a dependency of Assay: an adapter loads only when one of its metrics is
passed.

## An existing DeepEval suite: one import

```python
from deepeval.test_case import LLMTestCase
from deepeval.metrics import AnswerRelevancyMetric, ExactMatchMetric
from assay_sdk.frameworks import assert_test          # was: from deepeval import assert_test

def test_refund_window():
    test_case = LLMTestCase(input="How long is the refund window?", actual_output=my_app("..."),
                            expected_output="10 days")
    assert_test(test_case, [AnswerRelevancyMetric(threshold=0.7), ExactMatchMetric()])
```

The tests don't need to take the `assay_case` fixture. Under `pytest --assay` (or `assay test`),
each test becomes an Assay case, each metric a check of its own (`answer_relevancy`,
`exact_match`), and the test's own pass or fail another:

```
✗ exact_match    1/2   100% → 50%

⚠ 1 case regressed (2 checks)

1. tests/test_answers.py::test_return_window  exact_match, Your asserts
   The actual and expected outputs are different.
   AssertionError: Metrics failed: Exact Match (score 0, threshold 1.0): The actual and expected outputs are different.
```

`assert_test` fails the test when a metric fails, as DeepEval's does, so plain `pytest` stays
red. With `--assay`, the exit code is Assay's: a failure that was in the baseline too, or that
someone accepted with `assay accept`, doesn't fail every later run.

## One metric, in any test

```python
from assay_sdk.frameworks import check

def test_answer(assay_case):
    reply = my_agent("How long is the refund window?", run=assay_case)
    check(assay_case, AnswerRelevancyMetric(threshold=0.7), LLMTestCase(input=q, actual_output=reply))
    check(assay_case, Faithfulness(llm=llm), threshold=0.8,
          user_input=q, response=reply, retrieved_contexts=docs)
```

- **DeepEval:** pass the metric and its test case. The metric decides pass or fail, as it does
  in DeepEval (its threshold, strict mode, metrics where a lower score is better). A
  `threshold=` given to `check` decides instead.
- **RAGAS:** pass the metric, `threshold=` (RAGAS metrics have no threshold of their own, so
  `check` asks for one), and the sample's fields as keywords. Both of RAGAS's metric APIs work:
  `ragas.metrics.collections` (fields as keywords) and the classic metrics (a `SingleTurnSample`,
  or its fields).
- **The check's name** is the metric's (`answer_relevancy`, `faithfulness`); `field=` changes it.
  Its evaluator is `deepeval:Answer Relevancy` or `ragas:faithfulness`, and its judge model is
  the one the metric says it used.
- **What the metric saw** is recorded by role (the question, the answer, the reference, the
  retrieved context). When the run records what the app did (its answer, its tool results), Assay
  checks the two against each other: a metric handed the wrong answer, or another run's
  documents, is caught ([Was the judge given the right data?](verdicts.md#was-the-judge-given-the-right-data)).
  A DeepEval test that only builds an `LLMTestCase` records no trace, so there's nothing to check
  it against.

A test that doesn't use a metric isn't "missing" it: a suite whose tests use different metrics is
normal. A test that used one in its baseline and doesn't now is still reported.

## Anything else

Any other framework's metric, or a function of your own, goes through `evaluate()`: it takes a
function that returns a bool, a score or a verdict ([SDK](../sdk/python/README.md#your-own-evaluators-results-whose-validity-is-explicit)).

Tested with DeepEval 4.2.6 and RAGAS 0.4.3. (RAGAS 0.4.3 imports only with
`langchain-community` below 0.4.)
