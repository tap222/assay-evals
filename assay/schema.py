"""The Assay event schema, v1: models, JSON Schema, and how events land in Assay's tables.

See docs/event-schema.md for the design. A run is a stream: `run.start`, then
`step`s as they happen (each keyed by (run_id, seq), so they can arrive out of
order or be resent), then `run.end`. Outcomes (`feedback`, `check`,
`correction`, `expect`) arrive whenever they're known.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from assay import store

VERSION = 1
MAX_EVENTS = 5000
MAX_JSON = 256 * 1024  # per free-form field: args, result, outputs, value, input


class _E(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: Literal[1] = Field(..., description="Schema version")
    id: str = Field(..., min_length=1, max_length=128, description="Unique per event; retries reuse it")
    ts: datetime = Field(..., description="When it happened (a step: when it started)")

    @model_validator(mode="after")
    def _checks(self):
        for name in type(self).model_fields:
            v = getattr(self, name)
            if isinstance(v, datetime) and v.tzinfo is not None:
                object.__setattr__(self, name, v.astimezone(timezone.utc).replace(tzinfo=None))
            if name in ("args", "result", "outputs", "value", "input", "inputs") and v is not None \
                    and len(json.dumps(v, default=str)) > MAX_JSON:
                raise ValueError(f"{name} is larger than {MAX_JSON // 1024} KB; trim it or send a reference")
        return self


class Test(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str = Field(..., max_length=128, description="The test run, e.g. nightly-2026-09-24")
    case: str = Field(..., max_length=128, description="The test case; the same across test runs")
    attempt: Optional[int] = Field(None, ge=0)


Scalar = Union[str, int, float, bool, None]


class RunStart(_E):
    type: Literal["run.start"]
    run_id: str = Field(..., max_length=128)
    kind: Literal["agent", "pipeline"] = Field("agent", description="agent: llm/tool/state/answer steps; "
                                                                    "pipeline: stage steps with model calls")
    task: Optional[str] = Field(None, max_length=128)
    segment: Optional[str] = Field(None, max_length=128)
    input: Optional[Any] = None
    input_ref: Optional[str] = Field(None, max_length=1024)
    version: Optional[Dict[str, str]] = None
    test: Optional[Test] = None
    parent_run_id: Optional[str] = Field(None, max_length=128)
    tags: Optional[Dict[str, Scalar]] = None
    conversation_id: Optional[str] = Field(None, max_length=128,
                                           description="The conversation this run is a turn of: the turns share it")
    turn: Optional[int] = Field(None, ge=0, description="This run's place in the conversation, from 0")
    user: Optional[str] = Field(None, max_length=128, description="Who asked, as a pseudonymous id: the same "
                                "person asking again in a new conversation is a sign the first answer didn't do")
    duplicate: Optional[bool] = Field(None, description="True if this request was delivered again as a duplicate (double click, retried webhook)")
    original_run_id: Optional[str] = Field(None, max_length=128, description="The original run_id if this run was delivered as a duplicate")
    agent_id: Optional[str] = Field(None, max_length=128, description="The identity of the agent performing this run")
    owner: Optional[str] = Field(None, max_length=128, description="The team or user owning/responsible for this agent")
    requested_by: Optional[str] = Field(None, max_length=128, description="The person or system who initiated this request")
    credential: Optional[str] = Field(None, max_length=128, description="The credential used for this run")
    credential_type: Optional[str] = Field(None, max_length=64, description="Type of credential (user_oauth, service_account, api_key)")
    credential_owner: Optional[str] = Field(None, max_length=128, description="The person whose credential is being delegated/used")


class Step(_E):
    type: Literal["step"]
    run_id: str = Field(..., max_length=128)
    seq: int = Field(..., ge=0)
    kind: Literal["llm", "tool", "state", "answer", "stage", "approval", "resource", "mcp_prompt", "plan",
                  "retrieval", "user", "log"]
    name: Optional[str] = Field(None, max_length=128)
    parent_seq: Optional[int] = Field(None, ge=0)
    ended_at: Optional[datetime] = None
    status: Literal["ok", "error"] = "ok"
    error: Optional[str] = Field(None, max_length=2048)
    # log
    level: Optional[str] = Field(None, max_length=16)
    message: Optional[str] = Field(None, max_length=4096)
    template: Optional[str] = Field(None, max_length=4096)
    logger: Optional[str] = Field(None, max_length=256)
    file: Optional[str] = Field(None, max_length=512)
    line: Optional[int] = Field(None, ge=0)
    function: Optional[str] = Field(None, max_length=256)
    # llm
    model: Optional[str] = Field(None, max_length=128)
    tokens_in: Optional[int] = Field(None, ge=0)
    tokens_out: Optional[int] = Field(None, ge=0)
    cost_usd: Optional[float] = Field(None, ge=0)
    prompt: Optional[str] = Field(None, max_length=192, description="id@version")
    text: Optional[str] = Field(None, max_length=32768)
    tools: Optional[List[str]] = Field(None, max_length=500, description="llm: the tools the model was offered")
    tool_schemas: Optional[Dict[str, Dict[str, Any]]] = Field(
        None, description="llm: the input schema of each tool offered, sent once per run: tool calls are checked "
                          "against it")
    fault: Optional[Literal["empty", "error", "timeout", "lost", "return"]] = Field(
        None, description="tool or retrieval: a fault assay.faults() injected in place of the call")
    finish_reason: Optional[str] = Field(None, max_length=24, description="llm: stop, length, tool_call, refusal, "
                                                                          "content_filter or error")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(None, max_length=200, description="llm: the tool calls the "
                                                                                          "model asked for")
    tool_collisions: Optional[List[Dict[str, Any]]] = Field(None, description="llm: colliding tool names offered by multiple MCP servers")
    raw_tools: Optional[List[Any]] = Field(None, description="llm: raw tool definitions offered to the model")
    tokens_cached: Optional[int] = Field(None, ge=0)
    tokens_reasoning: Optional[int] = Field(None, ge=0)
    context: Optional[Dict[str, int]] = Field(None, description='llm: what the input was made of, in tokens: '
                                              '{"system", "tools", "history", "user", "retrieved"}')
    media: Optional[Dict[str, Any]] = Field(None, description='llm: images and video: {"images", "videos", "bytes", '
                                            '"size", "detail"}')
    settings: Optional[Dict[str, Union[str, int, float, bool]]] = Field(
        None, description="llm: temperature, max_tokens, reasoning effort, ...")
    # tool; mcp_prompt: args are the prompt's arguments, result the messages it returned
    args: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    server: Optional[str] = Field(None, max_length=128, description="tool, resource, mcp_prompt: the MCP server")
    # resource: an MCP resource read; result is its contents
    uri: Optional[str] = Field(None, max_length=2048)
    # plan: the tools the agent means to call, in order; text is the plan as it said it
    plan: Optional[List[Union[str, Dict[str, Any]]]] = Field(
        None, max_length=100, description='plan: ["search_customer", {"tool": "refund", "args": {"id": "O-17"}}]')
    # retrieval: what was searched for, and the fragments found (which went into the prompt: used)
    query: Optional[str] = Field(None, max_length=32768)
    fragments: Optional[List[Dict[str, Any]]] = Field(
        None, max_length=1000, description='retrieval: [{"id", "tokens", "used", "score", "source", "text"}]')
    # state
    op: Optional[Literal["create", "update", "delete"]] = None
    value: Optional[Any] = None
    # stage
    outputs: Optional[Dict[str, Any]] = None
    did_work: Optional[bool] = None
    # approval: name is the action, e.g. refund; text is the reason
    decision: Optional[Literal["approved", "rejected", "pending"]] = None
    by: Optional[str] = Field(None, max_length=128, description="approval: who decided (a person, a policy)")
    source: Optional[str] = Field(None, max_length=64, description="approval: origin of approval (e.g. human, approval_system)")
    arg_hash: Optional[str] = Field(None, max_length=64, description="approval: bound hash of arguments")
    time_to_approve_ms: Optional[float] = Field(None, ge=0, description="approval: time taken to approve in ms")
    rule: Optional[str] = Field(None, max_length=256, description="approval: policy rule or contract permitting action")
    evidence: Optional[str] = Field(None, max_length=4096, description="approval: evidence or context supporting approval")
    # identity & credentials
    credential: Optional[str] = Field(None, max_length=128, description="The credential used for this step")
    credential_type: Optional[str] = Field(None, max_length=64, description="Type of credential (user_oauth, service_account, api_key)")
    credential_owner: Optional[str] = Field(None, max_length=128, description="The person whose credential is being used")
    requested_by: Optional[str] = Field(None, max_length=128, description="The person or system who initiated this request")

    @field_validator("args", mode="before")
    @classmethod
    def _any_args(cls, v):
        """Tool arguments in any shape: a JSON string parsed, anything else kept under "_raw"."""
        from assay_sdk.llm import normalize_args
        return None if v is None else normalize_args(v)

    @field_validator("fragments", mode="before")
    @classmethod
    def _fragments(cls, v):
        """Fragments sent as text or partial dicts: made whole, with tokens estimated where missing."""
        if v is None:
            return None
        from assay_sdk.retrieval import fragments
        if all(isinstance(f, dict) and "position" in f and "tokens" in f and "used" in f for f in v):
            return v
        return fragments(v, [i for i, f in enumerate(v) if not isinstance(f, dict) or f.get("used", True)])

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _calls(cls, v):
        from assay_sdk.llm import normalize_args
        return None if v is None else [{**c, "arguments": normalize_args(c.get("arguments"))} if isinstance(c, dict)
                                       else {"_raw": c} for c in v]

    @model_validator(mode="after")
    def _kind_fields(self):
        allowed = {"llm": {"model", "tokens_in", "tokens_out", "cost_usd", "prompt", "text", "tools", "finish_reason",
                           "tool_calls", "tokens_cached", "tokens_reasoning", "context", "media", "settings"},
                   "tool": {"args", "result", "server"}, "state": {"op", "value"}, "answer": {"text"},
                   "stage": {"outputs", "did_work", "prompt"}, "approval": {"decision", "by", "text"},
                   "resource": {"uri", "result", "server"}, "mcp_prompt": {"args", "result", "server"},
                   "plan": {"plan", "text"}, "retrieval": {"query", "fragments", "server"},
                   "user": {"text"}}[self.kind]
        specific = {"model", "tokens_in", "tokens_out", "cost_usd", "prompt", "text", "args", "result", "op",
                    "value", "outputs", "did_work", "tools", "decision", "by", "server", "uri", "plan",
                    "finish_reason", "tool_calls", "tokens_cached", "tokens_reasoning", "query", "fragments",
                    "context", "media", "settings"}
        wrong = [f for f in specific - allowed if getattr(self, f) is not None]
        if wrong:
            raise ValueError(f"a {self.kind} step doesn't take {', '.join(sorted(wrong))}")
        if self.kind in ("tool", "stage", "state", "approval", "mcp_prompt") and not self.name:
            raise ValueError(f"a {self.kind} step needs a name")
        if self.kind == "resource" and not self.uri:
            raise ValueError("a resource step needs the uri it read")
        if self.kind == "plan" and (not self.plan or not all(
                isinstance(x, str) or (isinstance(x, dict) and isinstance(x.get("tool"), str)) for x in self.plan)):
            raise ValueError('a plan step needs plan: tool names, or {"tool": name, "args": {...}}')
        if self.kind == "retrieval" and self.fragments is None:
            raise ValueError("a retrieval step needs its fragments (an empty list if it found none)")
        if self.kind == "approval" and not self.decision:
            raise ValueError("an approval step needs a decision: approved, rejected or pending")
        return self


def retrieval_args(query: Optional[str], fragments: Optional[list]) -> dict:
    """A retrieval step as stored: args are the query and its counts, result the fragments."""
    from assay_sdk.retrieval import summary
    return {"query": query, **summary(fragments)}


class RunEnd(_E):
    type: Literal["run.end"]
    run_id: str = Field(..., max_length=128)
    status: Literal["completed", "failed", "abandoned"] = "completed"
    answer: Optional[str] = Field(None, max_length=32768)
    error: Optional[str] = Field(None, max_length=2048)
    outcome: Optional[Literal["resolved", "unresolved", "escalated"]] = Field(
        None, description="Whether the run did what was asked: resolved, unresolved, or handed to a person")


class Feedback(_E):
    type: Literal["feedback"]
    run_id: str = Field(..., max_length=128)
    kind: Literal["thumbs_up", "thumbs_down", "retry", "escalation", "complaint", "edited", "redone"] = Field(
        ..., description="edited: the user changed the answer before using it; redone: they did it themselves")
    note: Optional[str] = Field(None, max_length=1024)


ERROR_KINDS = ("invalid", "timeout", "rate_limited", "unavailable", "error")


class Check(_E):
    type: Literal["check"]
    test: Test
    status: Literal["pass", "fail", "error"]
    error_kind: Optional[Literal[ERROR_KINDS]] = Field(
        None, description="status error: why it couldn't be judged. invalid (the evaluator answered, but not with a verdict: unparseable, off-schema, a score that isn't a number), timeout, rate_limited, unavailable (connection error, 5xx), error (anything else)")
    tries: Optional[int] = Field(None, ge=1, description="How many times the evaluator was asked")
    raw_output: Optional[str] = Field(None, max_length=16384, description="What the evaluator returned, as it did")
    judge_model: Optional[str] = Field(None, max_length=128, description="The model that judged: a result judged by another model isn't compared with its baseline as like for like")
    judge_prompt: Optional[str] = Field(None, max_length=192, description="The judge's prompt or rubric, as id@version: changing it makes a new judge, like its model")
    duration_ms: Optional[float] = Field(None, ge=0, description="How long the evaluator took: whether it could run in the request path")
    cost_usd: Optional[float] = Field(None, ge=0, description="What the evaluator cost to run (a judge's model call)")
    judgements: Optional[int] = Field(None, ge=1, description="How many times the judge was asked about this same output (evaluate(rejudge=))")
    judgements_passed: Optional[int] = Field(None, ge=0, description="How many of those judgements passed it: a split vote is the judge disagreeing with itself, not the AI")
    category: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9 _./-]+$", description="fail: the kind of failure, as the evaluator names it (grounding, policy_refusal, ...). An acknowledged failure wakes when it changes")
    run_id: Optional[str] = Field(None, max_length=128, description="The run that produced the output")
    field: Optional[str] = Field(None, max_length=256)
    expected: Optional[str] = Field(None, max_length=4096)
    actual: Optional[str] = Field(None, max_length=4096)
    evaluator: Optional[str] = Field(None, max_length=128)
    score: Optional[float] = None
    reason: Optional[str] = Field(None, max_length=2048)
    version: Optional[Dict[str, str]] = None
    inputs: Optional[Dict[str, Any]] = Field(
        None, description='What the evaluator saw, by role: {"query", "output", "context", "expected", '
                          '"instructions", "messages"}. Assay checks it against the trace (assay/audit.py).')

    @model_validator(mode="after")
    def _validity(self):
        if self.error_kind and self.status != "error":
            raise ValueError("error_kind is for status error: a result that couldn't be judged")
        if self.score is not None and (self.score != self.score or abs(self.score) == float("inf")):
            raise ValueError("score is NaN or infinite: send status error with error_kind invalid instead")
        if self.judgements_passed is not None and (self.judgements is None or self.judgements_passed > self.judgements):
            raise ValueError("judgements_passed needs judgements, and can't be more than it")
        return self


class Correction(_E):
    type: Literal["correction"]
    run_id: str = Field(..., max_length=128)
    field: str = Field(..., max_length=256)
    expected: Optional[str] = Field(None, max_length=4096)
    observed: Optional[str] = Field(None, max_length=4096)
    kind: Literal["wrong", "missing", "extra"] = "wrong"
    reporter: Optional[str] = Field(None, max_length=128)


class ClaimReview(_E):
    type: Literal["claim_review"]
    run_id: str = Field(..., max_length=128)
    claim: str = Field(..., max_length=4096, description="The claim, as the answer made it")
    verdict: Literal["supported", "wrong", "conflict_resolved", "unsure"]
    evidence: Optional[List[Dict[str, Any]]] = Field(None, max_length=50,
                                                     description='The sources it was checked against: [{"id", "text" | "uri"}]')
    correction: Optional[str] = Field(None, max_length=4096, description="What the claim should say")
    by: Optional[str] = Field(None, max_length=128, description="Who decided: a pseudonymous id or a role")
    note: Optional[str] = Field(None, max_length=2048)


class Expect(_E):
    type: Literal["expect"]
    case: str = Field(..., max_length=128)
    calls: List[Dict[str, Any]] = Field(default_factory=list)
    allow_extra: List[str] = Field(default_factory=list)
    answer: Optional[str] = None
    answer_match: Literal["contains", "equals"] = "contains"
    state: List[Dict[str, Any]] = Field(default_factory=list)
    max_steps: Optional[int] = Field(None, ge=1)
    split: bool = Field(False, description="Check the tool calls in parts too: tool_choice, tool_args, tool_results")
    checkpoints: List[Dict[str, Any]] = Field(default_factory=list, max_length=50,
                                              description="Goal checkpoints, each checked on its own (see /v1/agents/references)")


class PromptVersion(_E):
    """A prompt version and its text, so a regression can show what changed in it (assay diff)."""
    type: Literal["prompt"]
    prompt_id: str = Field(..., max_length=128)
    version: str = Field(..., max_length=64)
    template: Optional[str] = Field(None, max_length=65536)
    note: Optional[str] = Field(None, max_length=1024, description="What changed")


Event = Annotated[Union[RunStart, Step, RunEnd, Feedback, Check, Correction, ClaimReview, Expect, PromptVersion],
                  Field(discriminator="type")]
EVENTS = TypeAdapter(List[Event])


def json_schema() -> dict:
    s = EVENTS.json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["$id"] = "https://assay.dev/schema/events-v1.json"
    s["title"] = "Assay events, v1"
    s["description"] = "A list of events. See docs/event-schema.md."
    return s


# ---------- landing events in Assay's tables ----------

def _upsert(conn, engine: Engine, table, rows: List[dict], keys: List[str]) -> None:
    if not rows:
        return
    ins = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    by_cols: Dict[tuple, List[dict]] = {}
    for r in rows:
        by_cols.setdefault(tuple(sorted(r)), []).append(r)
    for cols, batch in by_cols.items():
        stmt = ins(table)
        updates = {c: stmt.excluded[c] for c in cols if c not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates) if updates \
            else stmt.on_conflict_do_nothing(index_elements=keys)
        conn.execute(stmt, batch)


def _split(prompt: Optional[str]):
    if not prompt:
        return None, None
    pid, _, ver = prompt.partition("@")
    return pid or None, ver or None


ORDER = {"run.start": 0, "step": 1, "run.end": 2}


def ingest(engine: Engine, events: List[BaseModel], tenant: str) -> Dict[str, int]:
    """Write events; returns counts by type. Idempotent: resending the same events changes nothing."""
    from assay.ingest import discover_prompts
    events = sorted(events, key=lambda e: ORDER.get(e.type, 3))
    counts: Dict[str, int] = {}
    runs_t, heads, steps_t = store.runs, store.agent_trajectories, store.agent_steps
    with engine.begin() as conn:
        run_ids = {getattr(e, "run_id", None) for e in events} - {None}
        known = {r.run_id: dict(r._mapping) for r in conn.execute(
            select(runs_t).where(and_(runs_t.c.tenant == tenant, runs_t.c.run_id.in_(run_ids))))} if run_ids else {}
        rows = {k: [] for k in ("runs", "docs", "inputs", "heads", "steps", "stages", "calls", "feedback", "checks",
                                "errors", "refs")}
        for e in events:
            counts[e.type] = counts.get(e.type, 0) + 1
            if isinstance(e, RunStart):
                run = {"tenant": tenant, "run_id": e.run_id, "kind": e.kind, "task": e.task, "segment": e.segment,
                       "started_at": e.ts, "status": "running", "version": e.version,
                       "test_run": e.test.run if e.test else None, "test_case": e.test.case if e.test else None,
                       "attempt": e.test.attempt if e.test else None, "parent_run_id": e.parent_run_id, "tags": e.tags,
                       "conversation_id": e.conversation_id, "turn": e.turn, "user_id": e.user}
                known[e.run_id] = run
                rows["runs"].append(run)
                rows["docs"].append({"tenant": tenant, "document_id": e.run_id, "received_at": e.ts,
                                     "status": "running", "document_type": e.task, "segment": e.segment})
                if e.input is not None or e.input_ref:
                    rows["inputs"].append({"tenant": tenant, "trace_id": e.run_id, "input": e.input,
                                           "input_ref": e.input_ref, "captured_at": e.ts})
                if e.kind == "agent":
                    rows["heads"].append(_head(run))
            elif isinstance(e, Step):
                run = known.get(e.run_id)
                if run is None:  # a step before its run.start: open the run from what we know
                    run = {"tenant": tenant, "run_id": e.run_id, "kind": "pipeline" if e.kind == "stage" else "agent",
                           "started_at": e.ts, "status": "running"}
                    known[e.run_id] = run
                    rows["runs"].append(run)
                    rows["docs"].append({"tenant": tenant, "document_id": e.run_id, "received_at": e.ts,
                                         "status": "running"})
                    if run["kind"] == "agent":
                        rows["heads"].append(_head(run))
                pid, ver = _split(e.prompt)
                status = "success" if e.status == "ok" else "failed"
                if e.kind == "stage":
                    rows["stages"].append({"tenant": tenant, "run_id": f"{e.run_id}#{e.seq}", "document_id": e.run_id,
                                           "stage": e.name, "status": status, "started_at": e.ts,
                                           "finished_at": e.ended_at, "did_work": e.did_work, "outputs": e.outputs,
                                           "sequence": e.seq, "prompt_id": pid, "prompt_version": ver})
                elif e.kind == "llm" and run.get("kind") == "pipeline":
                    rows["calls"].append({"tenant": tenant, "call_id": f"{e.run_id}#{e.seq}", "stage": e.name or "llm",
                                          "ts": e.ts, "document_id": e.run_id, "model_declared": e.model,
                                          "model_served": e.model, "cost_usd": e.cost_usd,
                                          "latency_ms": (e.ended_at - e.ts).total_seconds() * 1000 if e.ended_at else None,
                                          "status": "success" if e.status == "ok" else "error", "prompt_id": pid,
                                          "prompt_version": ver, "code_revision": (run.get("version") or {}).get("build"),
                                          "segment": run.get("segment"), "document_type": run.get("task")})
                else:
                    tokens = (e.tokens_in or 0) + (e.tokens_out or 0)
                    rows["steps"].append({
                        "tenant": tenant, "trajectory_id": e.run_id, "seq": e.seq,
                        "kind": "reason" if e.kind == "llm" else e.kind,
                        "name": e.name or (e.uri[:128] if e.kind == "resource" else None),
                        "args": {"level": e.level, "template": e.template, "file": e.file, "line": e.line, "function": e.function} if e.kind == "log" else
                        {"op": e.op or "update"} if e.kind == "state" else
                        {"decision": e.decision, "by": e.by} if e.kind == "approval" else
                        {"uri": e.uri} if e.kind == "resource" else
                        {"steps": e.plan} if e.kind == "plan" else
                        retrieval_args(e.query, e.fragments) if e.kind == "retrieval" else e.args, "server": e.server,
                        "tokens_in": e.tokens_in, "tokens_out": e.tokens_out, "prompt": e.prompt, "tools": e.tools,
                        "finish_reason": e.finish_reason, "tool_calls": e.tool_calls, "tokens_cached": e.tokens_cached,
                        "tokens_reasoning": e.tokens_reasoning, "context": e.context, "media": e.media,
                        "settings": e.settings, "tool_schemas": e.tool_schemas, "fault": e.fault,
                        "result": {"template": e.template} if e.kind == "log" else (e.value if e.kind == "state" else e.fragments if e.kind == "retrieval" else e.result),
                        "error": (e.template or e.message) if e.kind == "log" else (e.error if e.status == "error" else None),
                        "text": e.message if e.kind == "log" else e.text, "model": e.model,
                        "tokens": tokens or None, "cost_usd": e.cost_usd, "started_at": e.ts,
                        "finished_at": e.ended_at, "parent_seq": e.parent_seq})
            elif isinstance(e, RunEnd):
                run = known.setdefault(e.run_id, {"tenant": tenant, "run_id": e.run_id, "kind": "agent",
                                                  "started_at": e.ts})
                run.update(ended_at=e.ts, status=e.status, answer=e.answer, error=e.error, outcome=e.outcome)
                rows["runs"].append({k: v for k, v in run.items()})
                rows["docs"].append({"tenant": tenant, "document_id": e.run_id,
                                     "received_at": run.get("started_at") or e.ts,
                                     "completed_at": e.ts if e.status == "completed" else None, "status": e.status})
                if run.get("kind", "agent") == "agent":
                    head = _head(run)
                    head.update(finished_at=e.ts, status=e.status)
                    if e.answer is not None:
                        head["answer"] = e.answer
                    rows["heads"].append(head)
            elif isinstance(e, Feedback):
                rows["feedback"].append({"tenant": tenant, "feedback_id": e.id, "trace_id": e.run_id, "kind": e.kind,
                                         "ts": e.ts, "note": e.note})
            elif isinstance(e, Check):
                rows["checks"].append({"tenant": tenant, "result_id": e.id, "run_id": e.test.run,
                                       "case_id": e.test.case, "attempt": e.test.attempt, "document_id": e.run_id,
                                       "field": e.field, "expected": e.expected, "actual": e.actual,
                                       "status": e.status, "evaluator": e.evaluator, "score": e.score,
                                       "reason": e.reason, "ts": e.ts, "inputs": e.inputs, "error_kind": e.error_kind,
                                       "tries": e.tries, "raw_output": e.raw_output, "category": e.category,
                                       "judge_model": e.judge_model, "judge_prompt": e.judge_prompt,
                                       "duration_ms": e.duration_ms, "cost_usd": e.cost_usd,
                                       "judgements": e.judgements, "judgements_passed": e.judgements_passed,
                                       "lineage": e.version or (known.get(e.run_id) or {}).get("version")})
            elif isinstance(e, Correction):
                rows["errors"].append({"tenant": tenant, "error_id": e.id, "document_id": e.run_id, "field": e.field,
                                       "reported_at": e.ts, "expected": e.expected, "observed": e.observed,
                                       "kind": e.kind, "reporter": e.reporter, "source": "correction"})
            elif isinstance(e, ClaimReview):
                rows.setdefault("claims", []).append({"tenant": tenant, "review_id": e.id, "run_id": e.run_id,
                                                      "claim": e.claim, "verdict": e.verdict, "evidence": e.evidence,
                                                      "correction": e.correction, "by": e.by, "note": e.note, "ts": e.ts})
            elif isinstance(e, PromptVersion):
                rows.setdefault("prompts", []).append(e)
            elif isinstance(e, Expect):
                rows["refs"].append({"tenant": tenant, "case_id": e.case, "calls": e.calls,
                                     "allow_extra": e.allow_extra, "answer": e.answer, "answer_match": e.answer_match,
                                     "state": e.state or None, "max_steps": e.max_steps,
                                     "checkpoints": e.checkpoints or None, "split": e.split or None,
                                     "updated_at": e.ts})
        _upsert(conn, engine, runs_t, _merge(rows["runs"], "run_id"), ["tenant", "run_id"])
        _upsert(conn, engine, store.event_documents, _merge(rows["docs"], "document_id"), ["tenant", "document_id"])
        _upsert(conn, engine, store.trace_inputs, rows["inputs"], ["tenant", "trace_id"])
        _upsert(conn, engine, heads, _merge(rows["heads"], "trajectory_id"), ["tenant", "trajectory_id"])
        _upsert(conn, engine, steps_t, rows["steps"], ["tenant", "trajectory_id", "seq"])
        _upsert(conn, engine, store.event_stage_runs, rows["stages"], ["tenant", "run_id"])
        _upsert(conn, engine, store.event_calls, rows["calls"], ["tenant", "call_id"])
        _upsert(conn, engine, store.trace_feedback, rows["feedback"], ["tenant", "feedback_id"])
        rows["checks"], dup = _drop_duplicate_checks(conn, tenant, rows["checks"])
        if dup:
            counts["duplicate_checks"] = dup
        _upsert(conn, engine, store.eval_results, rows["checks"], ["tenant", "result_id"])
        _upsert(conn, engine, store.event_errors, rows["errors"], ["tenant", "error_id"])
        _upsert(conn, engine, store.claim_reviews, rows.get("claims", []), ["tenant", "review_id"])
        _upsert(conn, engine, store.agent_references, rows["refs"], ["tenant", "case_id"])
        # Server time of the latest event, per trajectory: when to evaluate again (assay/lifecycle.py).
        touched = {r["trajectory_id"] for r in rows["heads"]} | {r["trajectory_id"] for r in rows["steps"]}
        if touched:
            conn.execute(heads.update().where(and_(heads.c.tenant == tenant, heads.c.trajectory_id.in_(list(touched))))
                         .values(updated_at=datetime.utcnow()))
        # An answer step is the run's answer unless run.end said otherwise.
        answered = {r["trajectory_id"]: r["text"] for r in sorted(rows["steps"], key=lambda r: r["seq"])
                    if r["kind"] == "answer"}
        for rid, text in answered.items():
            conn.execute(heads.update().where(and_(heads.c.tenant == tenant, heads.c.trajectory_id == rid,
                                                   heads.c.answer.is_(None))).values(answer=text))
    if rows.get("prompts"):
        from types import SimpleNamespace
        from assay.ingest import register_prompts
        register_prompts(engine, [SimpleNamespace(prompt_id=p.prompt_id, version=p.version, template=p.template,
                                                  note=p.note, author=None) for p in rows["prompts"]], tenant)
    prompts = [{"prompt_id": s["prompt_id"], "prompt_version": s["prompt_version"], "started_at": s["started_at"]}
               for s in rows["stages"] if s["prompt_id"]]
    if prompts:
        discover_prompts(engine, prompts, tenant, "started_at")
    return counts


def _judgement(r) -> Optional[tuple]:
    """What makes two check results the same judgement: the same output (run, or attempt), judged
    by the same evaluator with the same outcome. None when there's nothing to tell attempts apart by."""
    g = r.get if isinstance(r, dict) else lambda k: getattr(r, k)
    if g("document_id") is None and g("attempt") is None:
        return None
    return (g("run_id"), g("case_id"), g("attempt"), g("document_id"), g("field"), g("evaluator"), g("status"),
            g("score"))


def _drop_duplicate_checks(conn, tenant: str, checks: List[dict]) -> tuple:
    """A retried or twice-delivered evaluator job sends the same judgement again under a new id:
    keep the first, so it counts once. (checks to write, how many were dropped). A resent event
    (same id) isn't a duplicate: it just updates its row. Different outcomes are both kept, so
    the contradiction shows (EVALUATOR_ERROR)."""
    if not checks:
        return checks, 0
    t = store.eval_results
    runs = {c["run_id"] for c in checks}
    seen = {}
    for r in conn.execute(select(t.c.result_id, t.c.run_id, t.c.case_id, t.c.attempt, t.c.document_id, t.c.field,
                                 t.c.evaluator, t.c.status, t.c.score)
                          .where(and_(t.c.tenant == tenant, t.c.run_id.in_(list(runs))))):
        key = _judgement(r)
        if key is not None:
            seen.setdefault(key, r.result_id)
    keep = []
    for c in checks:
        key = _judgement(c)
        if key is not None and seen.setdefault(key, c["result_id"]) != c["result_id"]:
            continue
        keep.append(c)
    return keep, len(checks) - len(keep)


def _head(run: dict) -> dict:
    return {"tenant": run["tenant"], "trajectory_id": run["run_id"], "run_id": run.get("test_run"),
            "case_id": run.get("test_case"), "attempt": run.get("attempt"), "task": run.get("task"),
            "started_at": run.get("started_at"), "status": run.get("status") or "running",
            "lineage": run.get("version"), "outcome": run.get("outcome"),
            "conversation_id": run.get("conversation_id"), "turn": run.get("turn"), "user_id": run.get("user_id"),
            "origin": "synthetic" if (run.get("tags") or {}).get("origin") == "synthetic" else None,
            "consent": True if str((run.get("tags") or {}).get("consent", "")).lower() in ("shared", "yes", "true")
            else None}


def _merge(rows: List[dict], key: str) -> List[dict]:
    """Several rows for the same record in one batch (run.start then run.end): later values win,
    missing ones don't erase earlier ones."""
    out: Dict[str, dict] = {}
    for r in rows:
        cur = out.setdefault(r[key], {})
        cur.update({k: v for k, v in r.items() if v is not None or k not in cur})
    return list(out.values())
