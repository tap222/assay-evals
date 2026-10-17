[Assay](../README.md) › [Documentation](README.md)

# Agents

## Agents: evaluating the trajectory, not just the answer

An agent reasons, calls tools, reads what they return, changes things, and answers. Judging
only the answer misses a refund issued to the wrong order, or a correct answer reached by
deleting something first. Send each run as a **trajectory**:

```json
{"trajectory_id": "agent-0923.case-017.a0", "run_id": "agent-0923", "case_id": "case-017", "attempt": 0,
 "task": "refund_request", "started_at": "2026-09-23T10:00:00Z", "answer": "Refunded $27.61.",
 "lineage": {"prompt": "support_agent@v5", "model": "claude-sonnet-5"},
 "steps": [
   {"kind": "reason", "model": "claude-sonnet-5", "tokens": 812, "cost_usd": 0.0024},
   {"kind": "tool", "name": "get_order", "args": {"order_id": "O-10017"}, "result": {"price": 27.61}},
   {"kind": "tool", "name": "issue_refund", "args": {"order_id": "O-10017", "amount": 27.61}},
   {"kind": "state", "name": "refund:O-10017", "args": {"op": "create"}, "result": {"amount": 27.61}},
   {"kind": "answer", "text": "Refunded $27.61."}]}
```

Also send what each test case expects to `POST /v1/agents/references`:
- the tool calls, in order where it matters. Arguments match partially, and a call can be
  `optional` or `any_order`;
- tools that may be called beyond those (`allow_extra`, e.g. read-only lookups);
- the answer;
- end-state assertions, such as `{"object": "refund:O-10017", "exists": true}` or
  `{"object": "order:*", "field": "qty", "equals": 3}`;
- a step budget.

OpenTelemetry works too. `execute_tool` spans become tool steps and model spans become
reasoning steps (see the mapping at `/docs`).

`POST /v1/agents/runs/{run}/evaluate` checks every trajectory five ways and stores the checks
as evaluation results. Failure causes, flakiness across attempts and the release call then
work on agents unchanged.

| Check | Passes when |
|---|---|
| answer | the final answer has the expected value (whole-word, or written another usual way) |
| end state | the world afterwards matches, folded from the state-change steps |
| tool calls | every required call was made with the right arguments, in order. Retrying a call that errored is fine; so are allowed extras |
| safety | no critical path contract broke. Contracts see tool arguments: `delete_order` never runs `where` `confirmed` isn't true; `issue_refund` only after `get_order` with the `same` `order_id`; at most 2 `identical` `lookup_customer` calls |
| efficiency | within the step budget, and no call repeated 3 times with identical arguments |

For a failing run, **credit assignment** finds the first bad step and how it went wrong:
unsafe action, a tool error it never recovered from (infrastructure when the error says the
tool was unavailable), a loop, the wrong tool, the right tool with the wrong arguments,
stopping before an expected call, ignoring a tool result that held the answer, the wrong end
state, or a wrong answer after correct calls. These are the mechanisms Failure causes groups
by, so you get lines like "Wrong tool: `search_orders` where `get_order` was expected: 21
cases, a regression since `support_agent` v4 → v5".

Tool calls are also recorded as pipeline steps. So the workflow graph draws the agent's tool
graph, path contracts and shifts apply to tool sequences, Trace shows any run step by step
against its reference, and the measures count tool failures and model cost. A document
pipeline is just an agent with a fixed path.

### When a run is evaluated: when it ends, not on a timer

An agent can take ten seconds or five minutes, so Assay doesn't evaluate after a fixed
delay. Each run goes through a lifecycle:

- **running:** `run.start` (or its first step) has arrived, and `run.end` hasn't. It isn't
  judged half-way.
- **ended:** `run.end` says `completed` or `failed`. A run with no events for 30 minutes
  (`ASSAY_ABANDON_MINUTES`) is marked **abandoned**: its process died. An agent that's
  quiet for longer between steps can be given its own limit, by its runs' `task`:

  ```bash
  curl -X PUT $ASSAY_URL/v1/agents/limits -H "Authorization: Bearer $ASSAY_KEY" \
    -d '{"abandon_minutes": {"deep_research": 180, "*": 10}}'   # "*": this source's other agents; null removes one
  ```
- **evaluated:** in the same request that ended it, once its child runs (`parent_run_id`)
  have ended too.
- **evaluated again:** if more events arrive after that, e.g. a late step.

With OpenTelemetry, a run ends when its root span arrives. Spans are exported as they end,
often over several batches, so the run's steps are added up across batches, and a tool span
that arrives before the root span doesn't end the run early.

Every run gets the checks that need no expectations: it **finished**, it kept the critical
path contracts, it didn't **loop**, and no **tool error** went unrecovered. A test-case run
also gets its case's checks, so an evaluation run fills in as its cases finish. A background
sweep, every 60 seconds (`ASSAY_EVALUATE_SECONDS`) or on each `/v1/cron` call on Vercel,
marks quiet runs abandoned and evaluates anything left over.

| Endpoint | Returns |
|---|---|
| `GET /v1/agents/lifecycle?source=…` | how many runs are running, awaiting evaluation, evaluated, abandoned or failing, and the latest failures |
| `GET /v1/agents/limits?source=…`, `PUT /v1/agents/limits` | each agent's abandon limit, in minutes |
| `POST /v1/events/trajectories` | ingest trajectories (steps inline) |
| `POST /v1/agents/references`, `GET /v1/agents/references?source=…` | what cases expect |
| `GET /v1/agents/runs?source=…` | agent runs, newest first |
| `POST /v1/agents/runs/{run}/evaluate?source=…` | run the five checks, stored as eval results (runs still going are skipped and counted) |
| `GET /v1/agents/runs/{run}?source=…` | pass rate per check, tool precision and recall, first bad steps, and efficiency vs the baseline |
| `GET /v1/agents/trajectories/{id}?source=…` | one run step by step: divergence, end state, contract breaks, cost, and its latest evaluation |

### Plan adherence: did it do what it said it would?

An agent that plans before it acts can record the plan, and the run is checked against it.
No test case or reference is needed:

```python
run.plan(["search_customer", "get_order", {"tool": "refund", "args": {"id": "O-17"}}],
         text="Find the customer, check the order, refund it")
```

The `plan` check fails when the agent:
- skipped a planned call (or it errored and was never made);
- made planned calls out of order;
- made a planned call with other arguments than it planned.

It says where: "Strayed from its plan: called get_order (step 2) after refund, though the plan
put it first." Calls the plan didn't mention are allowed, and so is retrying a call that
errored. Replanning is allowed too: a new plan step replaces the rest of the one before, so
switching from "refund" to "escalate" isn't counted as skipping the refund. A run that failed
or stopped early is reported by **finished**, not here as well. In `pytest --assay` and
`assay test` the check is reported as "Plan adherence", under Planning. Whether the plan
itself was a good one needs a judge; this checks only that it was followed.

### An LLM judge: was the plan a good one, and does the run hang together?

Rules can check that a plan was followed. They can't check whether it was worth following,
or whether the answer agrees with what the tools returned. An LLM judge decides each, PASS or
FAIL, with a critique that names the step it rests on:

| Check | What the judge looks at |
|---|---|
| **Plan quality** | whether the plan, given the request and the tools offered, addresses what was asked, in a workable order, without steps it didn't need. Only for runs that record a plan |
| **Consistency** | whether the reasoning, the tool results and the answer agree: nothing contradicted, nothing stated as fact that no step established |
| **Context retention** | in a conversation, whether later answers and tool calls keep what the user said earlier ("I'm vegan"). Only for runs with more than one user message; each dropped constraint is quoted from what the user said, and a quote that isn't there makes the verdict INVALID |

```bash
pip install anthropic                       # and ANTHROPIC_API_KEY, or `ant auth login`
assay test --judge                          # or: pytest --assay --assay-judge
curl -X POST "$ASSAY_URL/v1/agents/runs/nightly-0924/judge?source=events:acme" -H "Authorization: Bearer $ASSAY_KEY"
```

It costs a model call per run, so it runs only when asked. `[judge] enabled = true` in
`assay.toml` turns it on for every run. The model is `claude-opus-5` by default. `[judge] provider` and `model` (or `ASSAY_JUDGE_PROVIDER`
and `ASSAY_JUDGE_MODEL` on the server) judge with OpenAI, Gemini, Ollama or any OpenAI-compatible
server instead.

Each dimension is PASS or FAIL with a critique: what's wrong, naming the steps, detailed enough
that someone new could act on it. A FAIL names its kind (fabricated, contradicts_source,
unsupported_inference, contradicts_itself, incomplete, policy_refusal, unworkable, inefficient).
A binary verdict is one a person can check and agree with; a 1-5 score hides what a 3 means. The
result reads `expected PASS, actual FAIL` with the critique as its reason. Verdicts recorded with
an earlier judge's 1-5 scores are still read, a score of 3 or more passing.

The judge's results are ordinary evaluation results (evaluator `assay.judge@1`). So:
- a case whose consistency drops from its baseline is a regression, and a judge that disagrees
  with itself across attempts is flaky;
- what the judge was given is recorded, and checked against the trace like any evaluator's;
- a judge that couldn't judge isn't a failure, and never a score of 0. Its verdict is checked
  against its schema. One that isn't a verdict (unparseable, a field missing, a score that
  isn't a whole number from 1 to 5, an answer cut off) is asked for again, then recorded as
  `INVALID` with what the judge actually said. A rate limit is `RATE_LIMITED`, a timeout
  `TIMEOUT`, a 5xx or connection error `INFRA_ERROR`, a refusal or a rejected request
  `EVALUATOR_ERROR`. Each result records how many tries it took.

A test run's runs are judged several at once, within limits, and the run says what judging cost:

```
Judged 40 runs with claude-opus-5 (plan quality, consistency).
43 LLM calls, 3 retries, 1,204,300 tokens in, 18,120 out, estimated $6.47.
```

```toml
[judge]
concurrency = 4     # runs judged at once
rate_limit = 50     # model calls a minute, shared by all of them; a 429 pauses them all (Retry-After)
timeout = 120       # seconds a call may take
retries = 3         # after a 429, a 5xx, a timeout, or a verdict that isn't valid
max_time = 600      # seconds for all the judging
budget_usd = 5      # dollars for all the judging

[judge.prices]      # dollars per million tokens: input, output[, cached]
"claude-opus-5" = [5, 25]
```

There's one retry layer: the provider SDK's own retries are off while judging, so every
request is counted once, with its tokens. When `max_time` or `budget_usd` is reached, the runs
left aren't judged, and they aren't failures. The cost is an estimate from `[judge.prices]`
(else `[prices]`, or `ASSAY_PRICES` as JSON); with no price it says so rather than guessing. On the server,
`ASSAY_JUDGE_CONCURRENCY`, `ASSAY_JUDGE_RATE_LIMIT`, `ASSAY_JUDGE_MAX_TIME` and
`ASSAY_JUDGE_BUDGET_USD` do the same, and the endpoint's answer has the same `summary`. This is
`assay_sdk.EvalRuntime`, which you can run your own evaluators with too
([SDK guide](../sdk/python/README.md#many-samples-at-once-evalruntime)).

**What kind of failure.** A failing score names its kind, and the kinds keep hallucinations
apart, since each needs another fix:

| Kind | The answer… |
|---|---|
| `fabricated` | states a fact no step or source contains |
| `contradicts_source` | says the opposite of a tool result or a source it was given |
| `unsupported_inference` | draws a conclusion the steps it cites don't support |
| `contradicts_itself` | says in one part what another part denies |
| `incomplete`, `policy_refusal`, `unworkable`, `inefficient`, `other` | the rest |

The report counts failures by kind, next to the baseline's (`Failures by kind: 2 contradicts
source (0 before) · 1 fabricated (0 before)`), and so do `assay diff` and the PR comment. Your own
evaluators name kinds the same way (`category` in a verdict, or `run.check(..., category=)`).

**The judge shows its evidence.** A contradiction inside a long answer (paragraph 3 against
paragraph 7) is listed as two exact quotes from the answer. Assay checks the quotes itself, word
for word. A contradiction whose quotes aren't in the answer is left out, and a verdict whose only
evidence is made up is asked for again, then recorded as `INVALID`, never as a score. The same
goes for a reason that cites a step the trace doesn't have. The judge's claim has to be backed by
the text, not taken on trust.

Requests the model declines are retried on another model server-side (`fallbacks: "default"`).
The trace is shown to the judge as data, marked as such, so instructions inside a tool result
don't steer the score. Long tool results are cut, and the cut is marked.

### Step by step: where an agentic workflow fails, and why

First the whole task: did the run meet the user's goal (the answer, the end state, `success=` for
a simulated user, `must_resolve()`, or a calibrated judge)? Then, where error analysis says a
workflow fails most, the steps.

**Tool calls in parts.** `"split": true` on a case's reference (or `assay.expect(case, split=True)`)
checks the tool calls in parts, each passing or failing on its own: `tool_choice` (the expected
tools, and no others), `tool_args` (called with the right arguments), `tool_results` (every expected
call worked). The resulting state is `end_state`. Without it, `tool_calls` is one check for all three.

**Well-formed arguments.** The input schema of each tool a model was offered is recorded with the
run (`assay.instrument()` takes it from the request; `run.llm(tools=[definitions])` otherwise).
Every call is checked against it (`arguments`): required fields missing, wrong types, values outside
an enum, fields the tool doesn't take. It needs no reference, so production traces are checked too:
a malformed call is a failure signal in `assay learn`. `expect(run).well_formed_arguments()` in pytest.

**A claimed success that didn't happen.** When the answer says an action was done ("Your order has
been cancelled"), `claimed_success` fails if the tool for it failed and was never retried
successfully, if the end state the reference expects doesn't hold, or if no tool ran at all. Also a
production signal, and `expect(run).no_false_success()`.

**Goal checkpoints.** A long workflow's milestones, each checked on its own:

```python
assay.expect("berkeley-viewings", checkpoints=[
    {"name": "listings retrieved", "tool": "search_listings", "args": {"city": "Berkeley"}, "result": "nonempty"},
    {"name": "availability checked", "tool": "check_availability"},
    {"name": "invites sent", "state": {"object": "invite:*", "exists": True}},
    {"name": "told the user", "answer": "scheduled"}])
```

Each is a check (`checkpoint.listings retrieved`). In pytest: `expect(run).checkpoint("invites sent",
tool="send_invite")`, or `checkpoint(name, rule=lambda run: ...)`.

**Error handling, by breaking tools on purpose.**

```python
with assay.faults(get_order="empty", search="error", calendar="timeout", pay="error:1"):
    reply = my_agent("Cancel O-17", run=assay_case)
expect(assay_case).handles_failure(max_retries=2)
```

For a tool recorded with `@assay.tool` or `run.call`: `empty` returns `[]`, `error` raises, `timeout`
raises `TimeoutError`, `error:N` fails the first N calls and then calls it for real (does it retry?),
`{"return": value}` returns that, and `lost` calls it for real, then raises `TimeoutError` (it
went through, but the reply never arrived). The step records which fault it was. `handles_failure()` passes when
the answer doesn't claim success, no tool is retried more than `max_retries` times, and the user is
told (or the run escalated, or a retry worked).

**Charged twice for one operation.** A payment that went through, with its reply lost, is the
retry that charges twice. `lost` makes it happen; `at_most_once` checks the agent didn't do it
again:

```python
with assay.faults(charge="lost"):
    reply = my_agent("Pay for order 17", run=assay_case)
expect(assay_case).at_most_once("charge", key="order_id")
```

It fails when `charge` went through more than once for the same `order_id` (more than once in
all, without `key`): "charge went through 2 times for order_id='O-17' (steps 3, 5); once
expected." A call counts when it ran: it succeeded, or `lost` dropped its reply. One that failed
before it ran (`error`, `timeout`) doesn't. The same check fits a booking, an email or anything
else that mustn't happen twice.

**The world changing while the agent works.** An approval checked when the agent plans can be stale
by the time it acts. `{"returns": [a, b, ...]}` gives a tool one value per call, the last one from
then on, and `assay.REAL` in the list calls the tool for real. The invoice stays approved, and the
supplier, in another system, goes on hold between the agent reading it and paying:

```python
with assay.faults(get_supplier={"returns": [assay.REAL, {"status": "on_hold"}]}):
    reply = my_agent("Pay invoice 17", run=assay_case)
assert_not_called(assay_case, "pay")   # it read the supplier again before paying, and stopped
```

An agent that pays on what it read when it planned fails: only a fresh read just before the write
sees the hold. Each tool gets its own
list, so several sources can change in the same run, and a related record can change while the
approved one stays the same. The substituted call is recorded as `fault="return"`, so
`handles_failure()` also checks the agent told the user it didn't go ahead.

**Where runs broke.** One line per place agent runs broke, in plain words: the last step that
worked, then the first that went wrong ("gen_sql worked, then exec_sql went wrong"). Where a case
has goal checkpoints, the line names them instead ("Reached “order found”, then missed “order
cancelled”"). Each line has how many runs broke there, how, what went wrong in one of them, and
every run, to open. Against a baseline, what got worse comes first, and places that no longer
break are listed below.

```
assay matrix                          # the latest test run; --run, --task, --format json
assay matrix --baseline t-0924-1100   # what got worse since the baseline
```

```
4 of 5 runs failed (r0: 2). Agent runs only: 1 plain tests left out.

Where runs broke (the last step that worked, then the first that went wrong):
    3 (+2)  gen_sql worked, then exec_sql went wrong: Tool error, not recovered 3
            e.g. a: exec_sql failed: syntax error
         1  exec_sql worked, then the answer went wrong: Wrong answer 1
            e.g. d: …
```

Plain tests recorded with `pytest --assay-all` aren't agents: they're left out, and the count says
how many. It's in the Agents tab, under **Details** for the run, and at
`GET /v1/agents/matrix?source=…&run=…&baseline=…` (each cell has its `sentence` and its `runs`).
There, it checks itself: if a run it counts passed every check under **What failed**, it says the
two disagree. Without `run`, it's built from the first failures people marked in the Review tab.

### Simulated users: the whole conversation, not one message

A single prompt and answer don't show how an agent handles someone who gives the order number
only when asked, pushes back, or changes their mind. `simulate` plays that user:

```python
from assay_sdk import Judge, Persona, simulate

upset = Persona(goal="get a refund for order O-17, which arrived broken",
                traits="impatient; gives the order number only when asked; pushes back once",
                facts={"order_id": "O-17"})

def test_refund_when_upset(assay_case):
    sim = simulate(my_agent, upset, user=Judge("anthropic", "claude-opus-5"), run=assay_case,
                   success=lambda run: run.state_of("order:17").get("status") == "refunded")
    assert sim.goal_met, sim.reason
```

- **An LLM plays the user,** in character, with facts it gives only when asked. Each turn it
  writes what the user would type next, or ends the conversation. It runs at temperature 0, so an
  unchanged agent gets the same conversation.
- **One conversation is one run:** each user message is a `user` step, each reply the agent's
  answer, and every tool call is recorded in the turn it happened in. Contracts and expectations
  hold over the whole conversation: a refund without approval fails on turn 4 as it would on turn 1.
- **Whether the goal was met** is decided by your `success` check on the run (its recorded state,
  its tool calls) when you give one, and only otherwise by the simulated user's own view, which is
  recorded as an opinion. It's the check `goal`, and the length is `turns`. Past `max_turns` (8)
  without the goal, it fails.
- **Scripted personas** (`Persona(script=["hi", "O-17", ...])`) play fixed messages with no model,
  for tests that must be reproducible to the letter.
- A simulator answer that isn't the JSON asked for is asked for again, then the result is
  `INVALID`: the simulator's failure is never counted against the agent.

The agent is `agent(message)` or `agent(message, history)`, sync or async.

### Conversations and MCP

A chat is several runs, one per turn. `assay.run(..., conversation="chat-1", turn=2)` (or
`conversation_id` and `turn` on `run.start`, or `gen_ai.conversation.id` with OpenTelemetry)
links them. `GET /v1/agents/conversations?source=…` lists conversations, and
`GET /v1/agents/conversations/{id}?source=…` shows one turn by turn: what each turn was asked
and answered, its steps, and its evaluation. A case saved from a later turn keeps the turns
before it in full (`trajectory.conversation`), so it can be replayed with what was said before.

MCP steps are recorded as what they are, not as tool calls:

```python
run.tool("refund", {"id": "O-17"}, {"ok": True}, server="shop")              # an MCP tool
run.resource("file:///policies/refunds.md", policy_text, server="docs")     # a resource read
run.mcp_prompt("refund_policy_check", {"order": "O-17"}, messages, server="docs")
```

A resource's contents count as what the agent retrieved, so a judge given that policy as its
context passes the context check.

An MCP server's tools change under the agent: a description reworded, an argument made required.
Offer the model the server's `tools/list` as it is, and record it with the call:

```python
tools = (await session.list_tools()).model_dump()["tools"]   # [{"name", "description", "inputSchema"}]
run.llm(model=..., tools=tools)
```

Each tool's definition is kept once per run, and `assay diff` compares it with the baseline's. A
change is shown next to the regressions it may have caused, for example *refund: description
reworded; `reason` now required* ([Behavior diff](diff.md)).

## Beyond the final answer: how the agent behaved

"Agent score: 0.87" doesn't say what changed. Assay checks what the agent did, and compares how it
behaved with each test's last passing run.

```python
from assay_sdk.testing import expect

def test_refund(assay_case):
    expect(assay_case).must_call("get_order").must_not_call("delete_order").max_steps(8) \
        .must_get_approval_before("refund").max_cost(0.05).max_latency(8) \
        .max_tools_exposed(10).max_context_tokens(8000).must_resolve()
    my_agent("Refund O-17", run=assay_case)
```

Expectations can be declared before the agent runs. They're checked when the test ends, and every
one that fails is reported together. Each is also recorded as a check (`expect.must_call(get_order)`),
so it's compared with its baseline like any other. Outside pytest, call `.verify()` or use
`with expect(run):`.

For that, the SDK records three things beyond tool calls and the answer:

| Call | Records |
|---|---|
| `run.llm(..., tokens_in=, tools=[...])` | the tools the model was offered (names, or the definitions you passed it), and how big its input was |
| `run.approval("refund", "approved" \| "rejected" \| "pending", by="manager", reason=)` | a decision to allow an action |
| `run.outcome("resolved" \| "unresolved" \| "escalated")` | whether the run did what was asked |

`pytest --assay` and `assay test` then compare each case's behavior with its baseline, and a
case fails when it got worse:

```
⚠ 3 cases behaved worse than their baseline
  tests/test_behavior.py::test_hard_case
    Cost: $0.0040 → $0.0120 (3.0×)
    Context: 1,200 tokens → 9,000 tokens (7.5×)
    Tools exposed: 8 tools → 30 tools (3.8×)
    Outcome: resolved → unresolved
  tests/test_behavior.py::test_policy_edge
    Approval for refund: approved → rejected
```

A number counts as worse when it grew 1.5× and by at least a minimum ($0.001, 1 s, 2 steps, 500
tokens, 2 tools), so small moves aren't news. An outcome counts as worse when a resolved case
stops resolving, and an approval when its decision changes. `[behavior]` in `assay.toml` sets
the ratios (0 turns one off), and `fail = false` only reports it. With repeats, a case's number
is the median of its attempts. The `requires_approval` contract puts an approval rule in
`assay.toml` instead of in every test.

### The input side: what went in before the model did anything

A system prompt that quietly grew 3,000 tokens, tool definitions that doubled, half as many video
frames: output metrics move, and nothing traces the move back to its cause. So every model call
records what its input was made of. `assay.instrument()` reads it from the request (Anthropic,
OpenAI chat and Responses, Gemini, Ollama, LiteLLM); `run.llm(context=, media=, settings=)` takes
it for calls it didn't see:

| Recorded | What |
|---|---|
| `context` | tokens by part, estimated from the text: `system` (the system prompt and standing instructions), `tools` (tool definitions), `history`, `user`, `retrieved` |
| `media` | images and video: how many, their bytes, their size (read from the image's own header when it's inline), `detail` |
| `settings` | temperature, top_p, max tokens, reasoning effort, thinking, seed |

**Fixed context** is the system prompt plus the tool definitions: paid on every call, whatever was
asked, and usually the cheapest input to trim. The report says what it is and how it moved:

```
Fixed context per call
  4,700 tokens (system prompt 4,300, tool definitions 400), 1,600 before (+3,100)
```

A case whose fixed context grew 1.25× (and by 200 tokens) behaved worse, the whole run's fixed
context is a total like input tokens and cost, and `[behavior] max_fixed_context_tokens` is a
limit. Next to a regression, the input that moved is listed with the prompt and model:

```
   Changed around it:
     context system prompt 1,200 → 4,300 tokens (+3,100) per call
     media   8 per call at 1280x720 → 4 per call at 1920x1080
     settings temperature 0.2 → 0.7
```

So a faithfulness dip that's really "someone added 40 lines to the instructions" says so, and
two setups with the same model but a different frame sampling are told apart.

### Faithfulness: is the answer backed by what was retrieved?

A judge that reads only the final answer scores fluency. A RAG system that retrieves the wrong
documents, or writes past them, still reads well. So for a run that retrieved (`run.retrieve()`),
`assay test --judge` also checks two things apart:

| Check | What it says |
|---|---|
| `faithfulness` | the answer's claims, one by one: **supported** by a fragment, **contradicted** by one, **fabricated** (no fragment says anything of it), or **inferred** beyond what the fragments say. The score is the share supported; below 0.9 fails, and the kind of failure is the worst claim's (`contradicts_source`, `fabricated`, `unsupported_inference`) |
| `context_relevance` | which of the fragments that went into the prompt address the question. Low relevance with high faithfulness is a retrieval problem, not a generation one |

```
Faithfulness   0.33: 1 of 3 claims supported; contradicted: “Refunds take 5 days” vs kb-1 “Refunds take 10 business days”; fabricated: “You will also get a coupon”
```

The judge has to show its evidence, and Assay checks it rather than trusting it. Each claim is a
quote of the answer, and each supported or contradicted claim quotes the fragment it rests on.
A claim that isn't in the answer, or whose evidence isn't in the fragment it names, isn't
counted, and the reason says how many. When most of it doesn't check out, the result is
`INVALID`, never a score. The fragments are the judge's recorded context, so the judge-input
audit checks them against what the run retrieved.

Outside `assay test`, on your own pipeline:

```python
from assay_sdk import Judge, faithfulness

out = faithfulness(Judge("anthropic", "claude-opus-5"), question, answer, fragments, run=case)
out["faithfulness"].score, out["context_relevance"].score, out["claims"]
```

### Retrieved context: what RAG puts in the prompt, and what it costs

Adding retrieval to an app often multiplies its bill. Every fragment retrieved is added to the
prompt, and each query is only a few hundred tokens bigger. Nothing looks wrong on its own until
the invoice arrives. Assay records what retrieval put into the prompt and checks it three ways.

```python
run.retrieve(question, docs, used=4)   # the fragments found; the top 4 went into the prompt
```

`fragments` are text, dicts (`text`, `id`, `tokens`, `score`, `source`), LangChain Documents or
LlamaIndex nodes. `used` says which went into the prompt: all of them (the default), the first n,
or ids. A fragment without `tokens` is estimated at four characters a token. With
`assay.instrument()` on, LangChain and LlamaIndex retrievers are recorded without this call
(what the outermost retriever returned).

**Per query, against the baseline.** A case whose queries put more fragments or more tokens into
the prompt (1.5× and at least 2 fragments or 200 tokens) behaved worse. The line names the
retriever and the share of the prompt it took:

```
  tests/test_support.py::test_refund_policy
    Retrieved context (kb_search): 3 fragments, 420 tokens → 12 fragments, 2,900 tokens (68% of the prompt)
    Context: 1,300 tokens → 4,260 tokens (3.3×)
```

**The whole run, against the baseline.** Each case can stay under its own ratio while the
whole run triples. So the run's totals over the cases both runs have (input tokens, cost,
retrieved tokens) are compared too, at `suite = 1.25` (and at least 1,000 tokens, or a cent):

```
⚠ The whole run grew against its baseline
  Input tokens for the whole run: 41,000 → 118,000 (2.9×), over the 40 cases in both runs
    most: test_refund_policy (+6,100), test_warranty (+5,800), test_returns (+5,200)
```

**Limits, whatever the baseline.** A query over a limit fails its case, from the first run:

```toml
[behavior]
max_fragments = 8           # fragments one query puts into the prompt
max_retrieved_tokens = 3000 # tokens of fragments one query puts into the prompt
max_context_tokens = 8000   # input one model call gets
suite = 1.25                # the whole run's totals (0 turns it off)

[prices]                    # dollars per million tokens: recorded calls get their cost
"claude-opus-5" = [5, 25]
```

With `[prices]` (or `ASSAY_PRICES`), model calls recorded without a cost get one from their
tokens, so the cost checks have numbers to compare. On a pull request, the limits and `suite`
are held to the base branch's, like every other check: raising a limit is reported as
loosening it.
