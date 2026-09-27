# assay-evals

Record what your AI system does, and how it went, in
[Assay](https://github.com/tap222/docai-eval): runs, agent steps, user feedback and test
results. Standard library only, Python 3.9+.

```bash
pip install assay-evals
```

With an Assay server, events go there (see the
[setup guide](https://github.com/tap222/docai-eval/blob/main/docs/setup.md)). Without one, they're
recorded to a local file, so you can start with no account and no server (see
"No server" below).

```python
import assay_sdk as assay

assay.init("https://assay.example.com", key="ak_...")   # or set ASSAY_URL / ASSAY_KEY

# An agent
with assay.run("refund_request", input=message, version={"prompt": "support@v5", "model": "claude-sonnet-5"}) as run:
    run.llm(model="claude-sonnet-5", tokens_in=620, tokens_out=180, cost_usd=0.0024)
    order = run.call("get_order", get_order, order_id="O-17")    # runs it; records the result or the error
    run.state("refund:O-17", "create", {"amount": order["price"]})
    run.answer(f"Refunded ${order['price']}.")

# A pipeline
with assay.run("invoice", kind="pipeline", input_ref="s3://inbox/inv-9.pdf") as run:
    with run.stage("extract", prompt="extract_fields@v13") as s:
        run.llm(model="claude-sonnet-5", cost_usd=0.01)          # nested under the stage
        s.outputs.update(fields)

# Outcomes, whenever they're known
assay.feedback(run.id, "thumbs_down")
p = assay.prompt("support", "13", template=text, note="faster refunds")   # "support@13", for run.llm(prompt=p)
assay.feedback(run.id, "edited")      # the quiet ones: they fixed the answer, or "redone": did it themselves
assay.correction(run.id, "total", expected="1240.00", observed="1204.00")
assay.check("nightly-0924", "case-17", "fail", run_id=run.id, field="total", expected="1240.00", actual="1204.00")
assay.check("nightly-0924", "case-17", "pass", run_id=run.id, field="helpful", evaluator="helpful@1",
            inputs={"query": question, "generation": graded})   # what the judge saw, checked against the trace
assay.expect("case-17", calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
```

| Call | Records |
|---|---|
| `assay.run(task, kind="agent"\|"pipeline", input=, version=, test="case-17")` | one run; an exception ends it as failed. `test` can also be `{"run", "case", "attempt"}`: under `assay test` the run and attempt are filled in |
| `run.llm(...)`, `run.tool(name, args, result)`, `run.call(name, fn, **args)`, `run.state(obj, op, value)`, `run.answer(text)`, `with run.stage(name) as s` | its steps, in order |
| `run.llm(..., tools=[...])`, `run.approval(action, decision, by=)`, `run.outcome("resolved")` | what the model was offered, decisions to allow an action, and whether the request was resolved |
| `assay.feedback`, `assay.check`, `assay.correction`, `assay.expect` | outcomes, sent whenever they're known |
| `run.expect(...)`, `run.check(field, status, expected=, actual=)` | the same, for a test-case run's own case |
| `assay.flush()` | send now (short-lived scripts); also happens every second and at exit |

These options go to `init()`:
- `redact`: a function applied to inputs, arguments, results, text and outputs before they
  leave the process.
- `sample=0.1`: record one run in ten. A run is recorded whole or not at all, and outcomes
  are always sent.
- `strict=True`: raise send errors while developing. Otherwise the SDK never raises into
  your code.
- `enabled=False`: the SDK does nothing, e.g. in unit tests.
- `path`: where to record when there's no server (see below).

Events follow the
[Assay event schema v1](https://github.com/tap222/docai-eval/blob/main/docs/event-schema.md). They stream to
`POST /v1/ingest` in the background, so a run that crashes still shows every step up to
the crash.

## No server: record locally

Call `assay.init()` with no URL, and with `ASSAY_URL` unset. Events are then appended to
`.assay/events.jsonl`, one per line, in the same form the server takes. Set `path=` or
`ASSAY_PATH` to use another file. Several processes can record to the same file, e.g.
`pytest -n 4`.

When the SDK creates the `.assay` folder, it adds a `.gitignore` there, so recorded inputs
don't end up in git.

To look at a recording, load it into a local Assay server
([`assay-server`](https://pypi.org/project/assay-server/)) and open the dashboard:

```bash
pip install assay-server
assay load            # reads .assay/events.jsonl into the tenant "local"
assay serve           # http://127.0.0.1:8400, source events:local
```

Loading the same file twice changes nothing, because every event has an id.

## Attach with a few lines: `@assay.step`, `@assay.tool`, `assay.instrument()`

```python
import assay_sdk as assay

assay.init()
assay.instrument()                     # Anthropic and OpenAI calls are recorded, in the step they're in

@assay.step("classification")          # a step of the pipeline; a dict it returns is its outputs
def classify(doc): ...

@assay.tool                            # a tool the agent calls: arguments, result or error, timing
def get_order(order_id): ...

@assay.pipeline("invoice", id_from="document_id")   # one run per call; @assay.agent for an agent
def handle(document_id, pdf):
    return graph.invoke({"pdf": pdf})
```

The steps, tools and model calls made inside `@assay.pipeline`, `@assay.agent` or `with
assay.run(...)` are recorded into that run. A step called outside any run starts one of its
own. With no run at all, a tool just runs. `assay.instrument()` never changes what a call
returns, and recording never breaks your code. Everything works on async functions too.
`assay connect code` proposes these lines for your code, as a diff.

## Any model provider, one shape: `Judge` and `normalize()`

```python
from assay_sdk import Judge, normalize

judge = Judge(provider="openai", model="gpt-5")        # anthropic, gemini, ollama, openai-compatible
r = judge.ask("Rate this answer…", system=RUBRIC, schema=VERDICT, temperature=0)
r.text, r.structured, r.tool_calls, r.usage, r.reasoning, r.finish_reason, r.error

r = normalize(response)   # a response you already have, from any of them (or LiteLLM)
```

Whatever answered, the fields are the same:
- `tool_calls`: `[{"name", "arguments", "id"}]`, with `arguments` always a dict. A JSON string
  (OpenAI's) is parsed; anything else is kept as `{"_raw": value}`. Nothing is dropped.
- `usage`: tokens `input`, `output`, `cached`, `reasoning`.
- `finish_reason`: `stop`, `length`, `tool_call`, `refusal`, `content_filter` or `error`.
- `structured`: the answer parsed as JSON, and checked against `schema`. When it doesn't fit,
  it's `None` with `error_kind="invalid"`, never an empty stand-in.
- `error` and `error_kind` (`timeout`, `rate_limited`, `unavailable`, `invalid`, `error`), when
  there's no answer. `ask()` never raises for a provider's failure.

Every other parameter goes to the provider unchanged: nothing is filtered or renamed.
Credentials are the provider SDK's own (its key variables, or a CLI login it supports).
Ollama and OpenAI-compatible servers (vLLM, LM Studio, a LiteLLM proxy) need no SDK:
`base_url`, and `api_key` if the server wants one. `evaluate()` takes a Judge's answer as it
is: `evaluate(judge, prompt, schema=VERDICT, judge_kwargs={"schema": VERDICT})`.
`judge_kwargs` is how arguments reach your judge when their names are also `evaluate()`'s own
(`schema`, `field`, `run`, ...); `evaluate()` warns when one looks misrouted.

A judge's few-shot examples come from the golden set's train split, never the items it's
measured on: `golden_examples(split="train", k=8)` returns them with their labels and critiques,
and `assay calibrate` fails a judge that read dev or test (see
[calibration](../../docs/calibration.md)).

`with assay.faults(get_order="error"):` breaks a tool on purpose, and
`expect(run).handles_failure()`, `.no_false_success()`, `.well_formed_arguments()` and
`.checkpoint(name, ...)` check what the agent did about it (see [agents](../../docs/agents.md)).

`assay.claim_review(run_id, claim, verdict, evidence=..., correction=..., by=...)` records an
expert's decision on one claim (supported, wrong, a conflict between sources resolved), made while
they use the product. A run tagged `consent="shared"` is one its user agreed to share. See
[sensitive data](../../docs/sensitive.md).

`with assay.tagged(origin="synthetic"):` gives every run started inside its tags, including runs
the code opens itself; `assay synth run` uses it so generated traffic is never counted as
production.

`assay.instrument()` records Anthropic, OpenAI, Gemini, Ollama and LiteLLM calls through the
same reading, with the tool calls asked for and why each call stopped.

## Your own evaluators: results whose validity is explicit

An LLM judge that answers with something that isn't a verdict, or a metric that divides by
zero, shouldn't become a score of 0: that reads as a real failure of your AI. `evaluate()`
calls your evaluator and says whether what came back is a verdict at all:

```python
from assay_sdk import evaluate

VERDICT = {"type": "object", "required": ["score", "reason"],
           "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}}

result = evaluate(my_judge, question, answer, schema=VERDICT, threshold=0.7, retries=2,
                  run=assay_case, field="helpful", evaluator="helpful@2")
result.status             # PASS, FAIL, INVALID, ERROR, TIMEOUT or RATE_LIMITED
result.score              # only for PASS and FAIL: an invalid result has none
result.reason, result.error, result.attempts, result.raw_judge_output
```

- **INVALID:** it answered, but not with a verdict: not JSON (a fenced JSON block is fine),
  not the schema, a score that's `None`, `NaN`, infinite or outside `score_range` (0 to 1 by
  default), or a score with no `threshold` to decide by.
- **TIMEOUT, RATE_LIMITED, ERROR:** it raised. The kind comes from the exception's type, its
  `status_code` (429, 5xx) or its message.
- **Retries:** INVALID, timeouts, rate limits and an unavailable service (connection error,
  5xx) are tried again, `retries` times, with a pause that doubles (`backoff` seconds first).
  Any other exception is an ERROR at once: asking a bug again doesn't fix it.
- **Recorded:** with `run=`, the result becomes a check. PASS and FAIL are passes and fails;
  the rest are errors with their kind, what the judge said, and how many tries it took. So
  they're `INVALID`, `TIMEOUT`, `RATE_LIMITED`, `INFRA_ERROR` or `EVALUATOR_ERROR` in Assay,
  and never count against the AI.

The judge may return a bool, a number, a dict (`score`, `passed` or `pass`, `reason`), JSON
text, or an object with those attributes. `aevaluate()` is the same for an async judge. A
`category` in the verdict (`{"score": 0.2, "category": "grounding"}`) names the kind of failure:
it's `result.category`, it's recorded with the check, and an acknowledged failure wakes when it
changes. `judge_model=` and `judge_prompt="helpful@3"` say which judge it was (a `Judge`'s
answer gives its model on its own): a check judged by another model or prompt than its
baseline's isn't compared with it as a regression.

## Many samples at once: `EvalRuntime`

Evaluating a dataset is where evaluation tools break down. A 429 cancels the whole run, a
retry loop never ends, a library retries out of sight and doubles the bill, one bad sample
crashes the job. `EvalRuntime` runs `evaluate()` over every sample within limits, and says
what the run cost:

```python
import assay_sdk as assay

rt = assay.EvalRuntime(concurrency=10, retries=3, timeout=30, rate_limit=50,
                       retry_on=[429, 500, 502, 503], max_time=600, budget_usd=5,
                       prices={"claude-opus-5": (5, 25)})      # dollars per million tokens
judge = assay.Judge("anthropic", "claude-opus-5")
report = rt.run(judge, prompts, schema=VERDICT, threshold=0.7)   # or: await rt.arun(...)
print(report)
```

```
50 samples in 1m12s

46 judged (41 passed, 5 failed)
 2 invalid judge outputs
 1 rate limited
 1 timed out

LLM calls:      57
Retries:        7
Tokens:         68,400 in, 4,560 out
Estimated cost: $0.46
```

| | |
|---|---|
| `concurrency` | samples judged at once |
| `rate_limit` | model calls a minute, evenly spaced and shared by every sample. A 429 pauses them all for as long as the provider asked (`Retry-After`) |
| `timeout` | seconds for one model call (for a judge that isn't a `Judge`, for one attempt) |
| `retries` | asks again per sample, in all: after a timeout, a dropped connection, an HTTP error whose status is in `retry_on`, or an answer that isn't a verdict (`retry_invalid=True`). A bug in the judge is final at once |
| `max_time`, `budget_usd` | for the whole run. When one is reached, no more samples start. Those left are reported as not run, never as failures |
| `prices` | `{model or prefix: (input, output[, cached])}`, dollars per million tokens, or `ASSAY_PRICES` as JSON. The provider's own figure is used when it gives one (LiteLLM, OpenRouter). A model with no price is reported as unpriced, never guessed |
| `cache=True` | identical samples are judged once |

- **One retry layer, counted.** Through a `Judge`, the provider SDK's own retries are off
  inside the runtime, so every request is one LLM call in the report, with its tokens. For a
  judge of your own, an attempt counts as one call unless its calls are seen (through a
  `Judge`, or with `assay.instrument()` on). An attempt that called the model more than once
  is reported, because that's where doubled costs hide.
- **One sample's failure is its own.** An exception, a timeout, even a `CancelledError` that
  a client raises on a 429, is that sample's result, and the others go on. Cancelling the run
  itself does stop it: `rt.report` has what was judged so far, and `run()` returns it after
  Ctrl-C.
- **Samples:** each is the judge's one argument, and a tuple is spread over its arguments.
  `assay.Sample(question, answer, id="q7", run=case)` says exactly what goes where, and records
  the result as a check on that case's run.
- **Results:** `report.results[i].result` is the `evaluate()` Result, with `calls`,
  `retries`, `seconds` and `cost_usd` for that sample. `report.to_dict()` has the totals.
  `rt.map(fn, items)` runs a function of your own per item under the same limits and
  accounting.

`assay test --judge` judges with this runtime too ([Agents](../../docs/agents.md#an-llm-judge-was-the-plan-a-good-one-and-does-the-run-hang-together)).

## With pytest

Take the `assay_case` fixture. It's a pytest plugin that comes with this package, so there's
nothing to configure:

```python
def test_refund(assay_case):
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
    reply = my_agent("Refund O-17", run=assay_case)   # record steps on it
    assert "27.61" in reply
```

The fixture wraps the test in `assay.run(<test name>, test=<test id>)`. It also records the
test's own outcome as a check on the field `pytest`, so failing asserts count too. Tests
that don't take the fixture are left alone.

With [`assay-server`](https://pypi.org/project/assay-server/) installed, the test also fails
when its run fails Assay's checks: its `expect(...)`, and the contracts and PII rules in
`assay.toml`. So plain `pytest` goes red on an unsafe tool call:

```
The run failed Assay's checks:
  Safety: Unsafe action: Broke “delete_order never runs”: ran delete_order (step 2, order_id='O-2').
```

`expect(run)` declares everything a run should do, checked together when the test ends:
`expect(run).must_call("get_order").must_not_call("delete_order").max_steps(8)
.must_get_approval_before("refund").max_cost(0.05).max_latency(8).max_tools_exposed(10)
.max_context_tokens(8000).must_resolve()`.

Each model call also records what its input was made of: the system prompt, tool definitions,
history and user message in tokens, images and video (count, bytes, size), and settings
(temperature, max tokens, reasoning effort). `assay.instrument()` reads them from the request;
`run.llm(context=, media=, settings=)` takes them otherwise.

What retrieval put into the prompt is recorded with `run.retrieve(query, docs, used=4)`: the
fragments (text, dicts, LangChain Documents, LlamaIndex nodes), their tokens, and which went into
the prompt. `assay.instrument()` records LangChain and LlamaIndex retrievers on its own.
`simulate(agent, Persona(goal=..., traits=..., facts=...), user=Judge(...), run=assay_case)` tests a
whole conversation against a simulated user
([Simulated users](../../docs/agents.md#simulated-users-the-whole-conversation-not-one-message)).

`faithfulness(judge, question, answer, fragments)` checks the answer claim by claim against
them, with the judge's quotes checked
([Faithfulness](../../docs/agents.md#faithfulness-is-the-answer-backed-by-what-was-retrieved)).
Fragments and retrieved tokens per query are compared with the baseline, the whole run's
totals are too, and `[behavior] max_fragments` and `max_retrieved_tokens` are limits
([Retrieved context](../../docs/agents.md#retrieved-context-what-rag-puts-in-the-prompt-and-what-it-costs)).

For the test body, `assay_sdk.testing` has `assert_called(run, tool, **args)`,
`assert_not_called`, `assert_called_before(run, first, then)`, `assert_max_steps(run, n)`,
`assert_answer_contains` and `assert_no_pii`. Each fails with what the run actually did.
`assay_sdk.frameworks` runs DeepEval and RAGAS metrics as checks: `check(run, metric, test_case)`, and
`assert_test`, a drop-in for DeepEval's ([DeepEval and RAGAS](../../docs/frameworks.md)).
`@rewordings("Can I get a refund for O-18?", "refund O-18 pls")` runs a test once per wording,
and `pytest --assay` checks each rewording does what the original does
([Rewordings](../../docs/testing.md#rewordings-the-same-request-in-other-words)).

`pytest --assay` (with `assay-server` installed) also compares each test with its last passing
run, prints Assay's report in pytest's summary, and exits 1 only when something got worse.

To test with it, `assay test` (in `assay-server`) runs your code with the SDK recording,
checks each run against its `assay.expect(...)`, and compares with the last run that passed.
See [Test your AI app locally](https://github.com/tap222/docai-eval/blob/main/docs/testing.md).
