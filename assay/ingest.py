"""Event ingest: validated models, idempotent writes, and OpenTelemetry mapping.

Every write is an upsert keyed by (tenant, id), so a pipeline can retry a
batch as often as it likes without creating duplicates. Records that don't
carry an id get one derived from their content (a stage run from document,
stage and start time; an extraction from document and field).
"""
from __future__ import annotations

import hashlib
import re
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from assay import store

MAX_BATCH = 5000  # records per request, across all types
MAX_OUTPUTS_BYTES = 64 * 1024


class Event(BaseModel):
    # Unknown fields are refused, so a typo like "documentType" fails loudly
    # instead of silently dropping data.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _utc(self):
        """Store every timestamp as naive UTC, whatever offset it was sent with."""
        for name in type(self).model_fields:
            v = getattr(self, name)
            if isinstance(v, datetime) and v.tzinfo is not None:
                object.__setattr__(self, name, v.astimezone(timezone.utc).replace(tzinfo=None))
        return self


class DocumentEvent(Event):
    document_id: str = Field(..., max_length=128)
    received_at: datetime
    completed_at: Optional[datetime] = None
    status: Optional[str] = None
    processing_mode: Optional[str] = Field(None, description="e.g. realtime or batch")
    file_hash: Optional[str] = Field(None, description="Used to check delivery to the downstream system")
    segment: Optional[str] = Field(None, description="What to break failures out by: customer, region, …")
    document_type: Optional[str] = None
    page_count: Optional[int] = Field(None, ge=0)
    delivered_downstream: Optional[bool] = None
    facets: Optional[Dict[str, Union[str, bool]]] = Field(
        None, description="What the document is like, to slice robustness by: source (digital, scanned), quality, "
                          "stamps, handwriting, language, currency, template (the supplier's layout), template_seen")

    @field_validator("facets")
    @classmethod
    def _facets(cls, v):
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("at most 20 facets")
        out = {}
        for k, x in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k):
                raise ValueError(f"facet {k!r}: lowercase letters, digits and _, e.g. template_seen")
            if isinstance(x, bool):
                x = ("seen" if x else "unseen") if k == "template_seen" else ("yes" if x else "no")
            out[k] = str(x)[:64]
        return out


class StageRunEvent(Event):
    run_id: Optional[str] = Field(None, max_length=128,
                                  description="Omit to derive one from document_id, stage and started_at")
    document_id: str
    stage: str
    status: str = Field(..., description="success, failed, error, timeout, …")
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    did_work: Optional[bool] = Field(None, description="false if the stage reported success but did nothing")
    outputs: Optional[Dict[str, Any]] = Field(
        None, description="What the step produced, as named values (nested allowed). Long strings, e.g. "
                          "OCR text under '_text', are used as evidence. Max 64 KB.")
    sequence: Optional[int] = Field(None, ge=0, description="Step position in the pipeline; else start time orders steps")
    prompt_id: Optional[str] = Field(None, max_length=128, description="Which prompt, e.g. extract_invoice_fields")
    prompt_version: Optional[str] = Field(None, max_length=64,
                                          description="A label (v13) or content hash; see prompt_version() in the client")

    @model_validator(mode="after")
    def _outputs_size(self):
        if self.outputs is not None and len(json.dumps(self.outputs, default=str)) > MAX_OUTPUTS_BYTES:
            raise ValueError(f"outputs is larger than {MAX_OUTPUTS_BYTES // 1024} KB; send the fields, "
                             "and trim long text")
        return self


class ErrorEvent(Event):
    error_id: Optional[str] = Field(None, max_length=128,
                                    description="Omit to derive one from document, field and expected value")
    document_id: str
    field: str = Field(..., description="The output field that was wrong; dotted paths like line_items.0.total work")
    expected: Optional[str] = Field(None, description="The correct value (omit when kind is extra)")
    observed: Optional[str] = Field(None, description="What the pipeline output")
    kind: str = Field("wrong", pattern="^(wrong|missing|extra)$")
    reported_at: Optional[datetime] = None
    reporter: Optional[str] = None
    source: Optional[str] = Field(None, description="review, qa, customer, …")


class EvalResultEvent(Event):
    """One test outcome from an evaluation run. Send passes too: they're what
    failures are compared against to find what the failures have in common."""
    result_id: Optional[str] = Field(None, max_length=128,
                                     description="Omit to derive one from run, case, field, evaluator and attempt")
    run_id: str = Field(..., max_length=128, description="The evaluation run, e.g. cert-2026-09-23")
    case_id: str = Field(..., max_length=128, description="The test case; the same id across runs")
    document_id: Optional[str] = Field(None, max_length=128,
                                       description="The document id the pipeline used for this case, so failures "
                                                   "can be traced step by step")
    field: Optional[str] = Field(None, description="The field checked; omit for a whole-case check")
    expected: Optional[str] = Field(None, max_length=4096)
    actual: Optional[str] = Field(None, max_length=4096)
    status: str = Field(..., pattern="^(pass|fail|error)$",
                        description="error: the check itself couldn't run (judge timeout, harness crash)")
    evaluator: Optional[str] = Field(None, max_length=128, description="name@version, e.g. exact_match@2")
    score: Optional[float] = None
    reason: Optional[str] = Field(None, max_length=2048, description="The evaluator's explanation, or the error")
    ts: Optional[datetime] = None
    attempt: Optional[int] = Field(None, ge=0, description="For repeated judgements of the same output")
    inputs: Optional[Dict[str, Any]] = Field(
        None, description='What the evaluator saw, by role: {"query", "output", "context", "expected", '
                          '"instructions", "messages"}. Checked against the trace (assay/audit.py).')
    lineage: Optional[Dict[str, str]] = Field(
        None, description='What produced the output: {"prompt": "extract_fields@v13", "model": "...", "build": "..."}')
    error_kind: Optional[str] = Field(None, pattern="^(invalid|timeout|rate_limited|unavailable|error)$",
                                      description="status error: why it couldn't be judged. invalid (the evaluator answered, but not with a verdict: unparseable, off-schema, a score that isn't a number), timeout, rate_limited, unavailable (connection error, 5xx), error (anything else)")
    tries: Optional[int] = Field(None, ge=1, description="How many times the evaluator was asked")
    raw_output: Optional[str] = Field(None, max_length=16384, description="What the evaluator returned, as it did")
    judge_model: Optional[str] = Field(None, max_length=128, description="The model that judged: a result judged by another model isn't compared with its baseline as like for like")
    judge_prompt: Optional[str] = Field(None, max_length=192, description="The judge's prompt or rubric, as id@version: changing it makes a new judge, like its model")
    judgements: Optional[int] = Field(None, ge=1, description="How many times the judge was asked about this same output")
    judgements_passed: Optional[int] = Field(None, ge=0, description="How many of those judgements passed it: a split vote is the judge disagreeing with itself, not the AI")
    category: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9 _./-]+$", description="fail: the kind of failure, as the evaluator names it (grounding, policy_refusal, ...). An acknowledged failure wakes when it changes")

    @model_validator(mode="after")
    def _validity(self):
        if self.error_kind and self.status != "error":
            raise ValueError("error_kind is for status error: a result that couldn't be judged")
        if self.score is not None and (self.score != self.score or abs(self.score) == float("inf")):
            raise ValueError("score is NaN or infinite: send status error with error_kind invalid instead")
        if self.judgements_passed is not None and (self.judgements is None or self.judgements_passed > self.judgements):
            raise ValueError("judgements_passed needs judgements, and can't be more than it")
        return self


class StepEvent(Event):
    kind: str = Field(..., pattern="^(reason|tool|state|answer|resource|mcp_prompt|plan|retrieval|user|log)$",
                      description="reason (model thinking or planning), tool (a call and its result), state "
                                  "(a change to the world), answer (the final reply), resource (an MCP resource "
                                  "read: args {\"uri\"}, result its contents), mcp_prompt (an MCP prompt fetched: "
                                  "args its arguments, result its messages), plan (the tools the agent means to call: "
                                  "args {\"steps\": [name, or {\"tool\", \"args\"}]}, text the plan as said)")
    name: Optional[str] = Field(None, max_length=128,
                                description="tool: the tool's name; state: the object changed, e.g. order:1001")
    args: Optional[Dict[str, Any]] = Field(None, description='tool: its arguments; state: {"op": "create|update|delete"}')
    result: Optional[Any] = Field(None, description="tool: what it returned; state: the object after the change")
    error: Optional[str] = Field(None, max_length=1024, description="tool: the error it raised, if any")
    server: Optional[str] = Field(None, max_length=128, description="tool, resource, mcp_prompt: the MCP server")

    @field_validator("args", mode="before")
    @classmethod
    def _any_args(cls, v):
        """Tool arguments in any shape: a JSON string parsed, anything else kept under "_raw"."""
        from assay_sdk.llm import normalize_args
        return None if v is None else normalize_args(v)
    text: Optional[str] = Field(None, max_length=16384, description="reason / answer: the text")
    model: Optional[str] = Field(None, max_length=128)
    tokens: Optional[int] = Field(None, ge=0)
    tokens_in: Optional[int] = Field(None, ge=0)
    tokens_out: Optional[int] = Field(None, ge=0)
    cost_usd: Optional[float] = Field(None, ge=0)
    prompt: Optional[str] = Field(None, max_length=192, description="reason: the prompt it ran, id@version")
    context: Optional[Dict[str, int]] = Field(None, description="reason: its input by part, in tokens")
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class TrajectoryEvent(Event):
    """One run of an agent on one task, with its steps in order. Also recorded as a
    document, so volume, cost, the workflow graph and path contracts cover agents."""
    trajectory_id: str = Field(..., max_length=128)
    run_id: Optional[str] = Field(None, max_length=128, description="The evaluation run, for a test case")
    case_id: Optional[str] = Field(None, max_length=128, description="The test case, the same id across runs")
    attempt: Optional[int] = Field(None, ge=0)
    task: Optional[str] = Field(None, max_length=128, description="The kind of task, e.g. refund_request")
    segment: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    answer: Optional[str] = Field(None, max_length=16384)
    status: Optional[str] = Field(None, description="completed, failed, max_steps, …")
    lineage: Optional[Dict[str, str]] = None
    input: Optional[Any] = Field(None, description="What the agent was asked, so a failure can become a test case")
    conversation_id: Optional[str] = Field(None, max_length=128, description="The conversation this run is a turn of")
    turn: Optional[int] = Field(None, ge=0, description="This run's place in the conversation, from 0")
    user: Optional[str] = Field(None, max_length=128, description="Who asked, as a pseudonymous id")
    steps: List[StepEvent] = Field(..., max_length=500)

    @model_validator(mode="after")
    def _size(self):
        if len(json.dumps([s.model_dump() for s in self.steps], default=str)) > 512 * 1024:
            raise ValueError("steps are larger than 512 KB; trim long tool results")
        return self


class InputEvent(Event):
    """What a trace (document or trajectory) was given, so its failures can be replayed as tests."""
    trace_id: str = Field(..., max_length=128)
    input: Optional[Any] = Field(None, description="The request itself: text or structured")
    input_ref: Optional[str] = Field(None, max_length=1024, description="Or where to fetch it, e.g. s3://inbox/a.pdf")

    @model_validator(mode="after")
    def _one(self):
        if self.input is None and not self.input_ref:
            raise ValueError("Send input or input_ref.")
        if self.input is not None and len(json.dumps(self.input, default=str)) > 64 * 1024:
            raise ValueError("input is larger than 64 KB; send input_ref instead")
        return self


class FeedbackEvent(Event):
    """What a user did about a trace: the strongest label-free signal that it went wrong."""
    feedback_id: Optional[str] = Field(None, max_length=128, description="Omit to derive one")
    trace_id: str = Field(..., max_length=128)
    kind: str = Field(..., pattern="^(thumbs_down|thumbs_up|retry|escalation|complaint|edited|redone)$")
    ts: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=1024)


class ReferenceEvent(Event):
    """What a test case expects of the agent's trajectory."""
    case_id: str = Field(..., max_length=128)
    calls: List[Dict[str, Any]] = Field(
        default_factory=list, description='Expected tool calls in order: {"tool": "get_order", "args": {"order_id": '
                                          '"1001"}, "optional": false, "any_order": false}. args match partially.')
    allow_extra: List[str] = Field(default_factory=list, description="Tools that may be called beyond these, "
                                                                     "e.g. read-only lookups")
    answer: Optional[str] = Field(None, description="The expected answer, or a value it must contain")
    answer_match: str = Field("contains", pattern="^(contains|equals)$")
    state: List[Dict[str, Any]] = Field(
        default_factory=list, description='End-state assertions: {"object": "refund:*", "exists": false} or '
                                          '{"object": "order:1001", "field": "qty", "equals": 2}')
    max_steps: Optional[int] = Field(None, ge=1)
    split: bool = Field(False, description="Check the tool calls in parts too: tool_choice, tool_args, tool_results")
    checkpoints: List[Dict[str, Any]] = Field(default_factory=list, max_length=50, description='Goal checkpoints of a long workflow, in order, each passing or failing on its own: {"name": "availability checked", "tool": "check_availability", "args": {...}, "result": "ok" | "nonempty"}, or {"name", "state": {"object", "exists" | "field" + "equals"}}, or {"name", "answer": "text it contains"}')


class CallEvent(Event):
    call_id: str = Field(..., max_length=128)
    stage: str
    ts: datetime
    document_id: Optional[str] = None
    model_declared: Optional[str] = Field(None, description="The model the pipeline asked for")
    model_served: Optional[str] = Field(None, description="The model that actually answered")
    resolving_layer: Optional[str] = Field(None, description="primary, fallback_1, …")
    gate_reason: Optional[str] = Field(None, description="Why this layer answered")
    cost_usd: Optional[float] = Field(None, ge=0)
    code_revision: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    latency_ms: Optional[float] = Field(None, ge=0)
    status: Optional[str] = None
    prompt_id: Optional[str] = Field(None, max_length=128, description="Which prompt, e.g. extract_invoice_fields")
    prompt_version: Optional[str] = Field(None, max_length=64,
                                          description="A label (v13) or content hash; see prompt_version() in the client")


class ReviewEvent(Event):
    review_id: str = Field(..., max_length=128)
    document_id: str
    ts: datetime
    kind: str = Field("review", pattern="^(review|rework)$", description="review, or rework to fix an error")
    minutes: Optional[float] = Field(None, ge=0, description="Priced at the rate card's hourly rate")
    cost_usd: Optional[float] = Field(None, ge=0, description="Use instead of minutes if you know the cost")
    reviewer: Optional[str] = None
    stage: Optional[str] = None


class ExtractionEvent(Event):
    extraction_id: Optional[str] = Field(None, max_length=128,
                                         description="Omit to derive one from document_id and field")
    document_id: str
    field: Optional[str] = Field(None, description="Field name; makes retries idempotent without an id")
    has_positions: bool = Field(..., description="True if the value carries a source location")
    segment: Optional[str] = None
    document_type: Optional[str] = None


class PromptEvent(Event):
    """Register a prompt version (from CI, when it's released). Optional: versions
    are also discovered from traffic. Registering adds the template, for diffs."""
    prompt_id: str = Field(..., max_length=128)
    version: Optional[str] = Field(None, max_length=64, description="Omit to use a hash of the template")
    template: Optional[str] = Field(None, max_length=65536)
    note: Optional[str] = Field(None, max_length=1024, description="What changed")
    author: Optional[str] = None

    @model_validator(mode="after")
    def _version(self):
        if not self.version:
            if not self.template:
                raise ValueError("Give a version, or a template to derive one from.")
            object.__setattr__(self, "version", content_version(self.template))
        return self


def content_version(template: str) -> str:
    """A stable version for prompt text: the first 12 hex digits of its SHA-256."""
    return hashlib.sha256(template.encode()).hexdigest()[:12]


class EventBatch(Event):
    documents: List[DocumentEvent] = []
    stage_runs: List[StageRunEvent] = []
    calls: List[CallEvent] = []
    reviews: List[ReviewEvent] = []
    extractions: List[ExtractionEvent] = []
    errors: List[ErrorEvent] = []
    eval_results: List[EvalResultEvent] = []
    trajectories: List[TrajectoryEvent] = []
    inputs: List[InputEvent] = []
    feedback: List[FeedbackEvent] = []
    prompts: List[PromptEvent] = []


def _derive(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:32]


TABLES = {
    "documents": (store.event_documents, "document_id"),
    "stage_runs": (store.event_stage_runs, "run_id"),
    "calls": (store.event_calls, "call_id"),
    "reviews": (store.event_reviews, "review_id"),
    "extractions": (store.event_indexed, "extraction_id"),
    "errors": (store.event_errors, "error_id"),
    "eval_results": (store.eval_results, "result_id"),
    "inputs": (store.trace_inputs, "trace_id"),
    "feedback": (store.trace_feedback, "feedback_id"),
}


def _rows(kind: str, events: List[Event], tenant: str) -> List[dict]:
    out = []
    for e in events:
        # Documents update only the fields sent (a completion mustn't blank the type);
        # other records are written whole.
        r = e.model_dump(exclude_unset=True) if kind == "documents" else e.model_dump()
        if kind == "stage_runs" and not r.get("run_id"):
            r["run_id"] = _derive(r["document_id"], r["stage"], r.get("started_at"))
        if kind == "errors":
            r.setdefault("reported_at", None)
            r["reported_at"] = r["reported_at"] or datetime.utcnow()
            if not r.get("error_id"):
                r["error_id"] = _derive(r["document_id"], r["field"], r.get("expected"), r.get("kind"))
        if kind == "eval_results":
            r["ts"] = r.get("ts") or datetime.utcnow()
            if not r.get("result_id"):
                r["result_id"] = _derive(r["run_id"], r["case_id"], r.get("field"), r.get("evaluator"), r.get("attempt"))
        if kind == "inputs":
            r["captured_at"] = datetime.utcnow()
        if kind == "feedback":
            r["ts"] = r.get("ts") or datetime.utcnow()
            if not r.get("feedback_id"):
                r["feedback_id"] = _derive(r["trace_id"], r["kind"], r["ts"])
        if kind == "extractions" and not r.get("extraction_id"):
            r["extraction_id"] = _derive(r["document_id"], r["field"]) if r.get("field") else uuid.uuid4().hex
        out.append(r | {"tenant": tenant})
    return out


def upsert(engine: Engine, table, rows: List[dict], key: str) -> int:
    """Insert, or update only the columns each row carries."""
    if not rows:
        return 0
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    with engine.begin() as conn:
        for cols, batch in groups.items():
            stmt = insert(table)
            updates = {c: stmt.excluded[c] for c in cols if c not in (key, "tenant")}
            conflict = ["tenant", key]
            stmt = stmt.on_conflict_do_update(index_elements=conflict, set_=updates) if updates \
                else stmt.on_conflict_do_nothing(index_elements=conflict)
            conn.execute(stmt, batch)
    return len(rows)


def write(engine: Engine, kind: str, events: List[Event], tenant: str) -> int:
    table, key = TABLES[kind]
    rows = _rows(kind, events, tenant)
    n = upsert(engine, table, rows, key)
    if kind in ("calls", "stage_runs"):
        discover_prompts(engine, rows, tenant, "ts" if kind == "calls" else "started_at")
    return n


def _step_key(s: dict) -> tuple:
    return (s["kind"], s.get("name"), s.get("started_at"), s.get("text"))


def write_trajectories(engine: Engine, events: List["TrajectoryEvent"], tenant: str, merge: bool = False) -> int:
    """Store trajectories with their steps, and each as a document. By default a trajectory replaces
    any earlier copy. merge=True adds to it instead (OTLP: a run's spans arrive over several
    batches): steps are combined and put in time order, repeats dropped, and a batch that doesn't
    end the run doesn't reopen one that has ended."""
    if not events:
        return 0
    t, st = store.agent_trajectories, store.agent_steps
    now = datetime.utcnow()
    before, earlier = {}, defaultdict(list)
    if merge:
        ids = [e.trajectory_id for e in events]
        with engine.connect() as conn:
            before = {r.trajectory_id: dict(r._mapping) for r in conn.execute(
                select(t).where((t.c.tenant == tenant) & t.c.trajectory_id.in_(ids)))}
            for r in conn.execute(select(st).where((st.c.tenant == tenant) & st.c.trajectory_id.in_(ids))):
                earlier[r.trajectory_id].append({k: v for k, v in r._mapping.items()
                                                 if k not in ("tenant", "trajectory_id", "seq")})
    heads, steps, docs, inputs = [], [], [], []
    for e in events:
        head = {"tenant": tenant, "trajectory_id": e.trajectory_id, "run_id": e.run_id, "case_id": e.case_id,
                "attempt": e.attempt, "task": e.task, "started_at": e.started_at, "finished_at": e.finished_at,
                "answer": e.answer, "status": e.status, "lineage": e.lineage, "updated_at": now,
                "conversation_id": e.conversation_id, "turn": e.turn, "user_id": e.user}
        new = [s.model_dump() for s in e.steps]
        old = before.get(e.trajectory_id)
        if old:
            for k in ("run_id", "case_id", "attempt", "task", "lineage", "conversation_id", "turn", "user_id"):
                head[k] = head[k] if head[k] is not None else old[k]
            head["started_at"] = min(head["started_at"], old["started_at"])
            if e.status == "running" and old["status"] != "running":  # a late span: still ended
                head.update(status=old["status"], finished_at=old["finished_at"], answer=old["answer"])
            seen = {_step_key(s) for s in new}
            new = sorted([s for s in earlier[e.trajectory_id] if _step_key(s) not in seen] + new,
                         key=lambda s: (s.get("started_at") is None, s.get("started_at") or datetime.min))
        heads.append(head)
        blank = {c.name: None for c in st.columns}  # stored steps and new ones carry different keys
        for i, s in enumerate(new):
            steps.append({**blank, **s, "tenant": tenant, "trajectory_id": e.trajectory_id, "seq": i})
        if e.input is not None:
            inputs.append(InputEvent(trace_id=e.trajectory_id, input=e.input))
        docs.append(DocumentEvent(document_id=e.trajectory_id, received_at=head["started_at"],
                                  completed_at=head["finished_at"] if (head["status"] or "completed") == "completed"
                                  else None, status=head["status"] or "completed", document_type=head["task"],
                                  segment=e.segment))
    ids = [e.trajectory_id for e in events]
    with engine.begin() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.execute(st.delete().where((st.c.tenant == tenant) & st.c.trajectory_id.in_(chunk)))
        if steps:
            conn.execute(st.insert(), steps)
    upsert(engine, t, heads, "trajectory_id")
    write(engine, "documents", docs, tenant)
    write(engine, "inputs", inputs, tenant)
    return len(events)


def write_references(engine: Engine, refs: List["ReferenceEvent"], tenant: str) -> int:
    rows = [r.model_dump() | {"tenant": tenant, "updated_at": datetime.utcnow()} for r in refs]
    return upsert(engine, store.agent_references, rows, "case_id")


def write_batch(engine: Engine, batch: EventBatch, tenant: str, merge_trajectories: bool = False) -> Dict[str, int]:
    # Documents first, so everything else has something to attach to.
    out = {kind: write(engine, kind, getattr(batch, kind), tenant) for kind in TABLES}
    out["trajectories"] = write_trajectories(engine, batch.trajectories, tenant, merge=merge_trajectories)
    out["prompts"] = register_prompts(engine, batch.prompts, tenant)
    return out


def register_prompts(engine: Engine, prompts: List["PromptEvent"], tenant: str) -> int:
    t = store.prompt_versions
    now = datetime.utcnow()
    rows = [dict(tenant=tenant, prompt_id=p.prompt_id, version=p.version, template=p.template,
                 content_hash=hashlib.sha256(p.template.encode()).hexdigest() if p.template else None,
                 note=p.note, author=p.author, registered_at=now) for p in prompts]
    if not rows:
        return 0
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    with engine.begin() as conn:
        for r in rows:
            stmt = insert(t).values(**r)
            keep = lambda col: func.coalesce(stmt.excluded[col], t.c[col])  # don't erase what we know
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["tenant", "prompt_id", "version"],
                set_={c: keep(c) for c in ("template", "content_hash", "note", "author", "registered_at")}))
    return len(rows)


def discover_prompts(engine: Engine, rows: List[dict], tenant: str, ts_field: str) -> None:
    """Add prompt versions seen in traffic to the registry, widening first/last seen."""
    seen: Dict[tuple, list] = {}
    for r in rows:
        if r.get("prompt_id") or r.get("prompt_version"):
            key = (r.get("prompt_id") or "(unnamed)", r.get("prompt_version") or "(unversioned)")
            ts = r.get(ts_field)
            span = seen.setdefault(key, [ts, ts])
            if ts is not None:
                span[0] = ts if span[0] is None else min(span[0], ts)
                span[1] = ts if span[1] is None else max(span[1], ts)
    if not seen:
        return
    t = store.prompt_versions
    sqlite = engine.dialect.name == "sqlite"
    insert = sqlite_insert if sqlite else pg_insert
    lo, hi = (func.min, func.max) if sqlite else (func.least, func.greatest)  # scalar min/max per dialect
    with engine.begin() as conn:
        for (pid, ver), (first, last) in seen.items():
            stmt = insert(t).values(tenant=tenant, prompt_id=pid, version=ver, first_seen=first, last_seen=last)
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["tenant", "prompt_id", "version"],
                set_={"first_seen": lo(func.coalesce(t.c.first_seen, stmt.excluded.first_seen), stmt.excluded.first_seen),
                      "last_seen": hi(func.coalesce(t.c.last_seen, stmt.excluded.last_seen), stmt.excluded.last_seen)}))


# ---------- OpenTelemetry (OTLP/HTTP JSON) ----------

OTEL_MAPPING = """\
One trace is one document unless a span says otherwise.

Document   from each root span (no parent). id: assay.document_id or document.id
           attribute, else the trace id. received_at/completed_at from the span's
           start/end; completed only if the span didn't error. Also reads
           assay.document_type, assay.segment, assay.page_count, assay.processing_mode.
Model call from any span with a gen_ai.* attribute. model_declared =
           gen_ai.request.model, model_served = gen_ai.response.model (else the
           request model), latency from the span, status error if the span errored.
           stage = assay.stage, else the parent span's assay.stage or name, else the span name.
           Optional: assay.cost_usd, assay.resolving_layer, assay.gate_reason.
Stage run  from any other span with an assay.stage attribute. Optional assay.did_work,
           assay.sequence (step position), and assay.output.<field> attributes
           for what the step produced (used to localize errors).
Prompts    assay.prompt_id / assay.prompt_version on a model-call span or its
           parent stage span.
code_revision comes from the resource's service.version.

Agents     a trace with any tool span (gen_ai.operation.name = execute_tool, or a
           gen_ai.tool.name attribute) becomes a trajectory. Tool spans are tool
           steps: gen_ai.tool.call.arguments and gen_ai.tool.call.result (JSON or
           text), and the span's error. Model spans are reasoning steps (model,
           gen_ai.usage.input_tokens + output_tokens, assay.cost_usd). Spans with
           assay.state.object are state changes (assay.state.op,
           assay.state.value as JSON). The root span may carry assay.answer,
           assay.task, and for test cases assay.run_id, assay.case_id, assay.attempt.
           A run is a turn of a conversation with gen_ai.conversation.id (or session.id,
           or assay.conversation_id on the root), its place in it assay.turn.
           Spans are exported as they end, so a run's spans often come over several
           batches: they're added to the run, not replacing it. The run is running until
           its root span arrives (completed, or failed if the root span errored), then
           it's evaluated (see GET /v1/agents/lifecycle).
"""


OPENINFERENCE = """\
OpenInference spans (Arize Phoenix, OpenLLMetry-style and other instrumentors that follow it) are read
too: openinference.span.kind LLM, TOOL, RETRIEVER, AGENT or CHAIN.

LLM        llm.model_name, llm.provider, llm.token_count.prompt / completion, llm.cost.total,
           llm.prompt_template.version, and llm.input_messages (roles, for the input by part)
TOOL       tool.name; input.value is its arguments, output.value its result
RETRIEVER  retrieval.documents.N.document.{id, content, score}: a retrieval step, the query from input.value
AGENT/CHAIN a root one is the run: input.value what it was asked, output.value its answer, its name the task
session.id is the conversation, user.id the user. A trace with a tool or retriever span, or an AGENT or
CHAIN root, is an agent run.
"""


def _indexed(a: Dict[str, Any], prefix: str) -> Dict[int, Dict[str, Any]]:
    """prefix.N.rest attributes as {N: {rest: value}} (OpenInference flattens lists this way)."""
    out: Dict[int, Dict[str, Any]] = defaultdict(dict)
    for k, v in a.items():
        if k.startswith(prefix + "."):
            head, _, rest = k[len(prefix) + 1:].partition(".")
            if head.isdigit():
                out[int(head)][rest] = v
    return dict(sorted(out.items()))


def openinference(a: Dict[str, Any], root: bool = False, name: Optional[str] = None) -> Dict[str, Any]:
    """An OpenInference span's attributes with the gen_ai.* and assay.* ones they mean added (never
    overwriting any the span has), so the rest of the mapping reads it like any other."""
    kind = str(a.get("openinference.span.kind") or "").upper()
    if not kind and not any(k.startswith(("llm.", "tool.name", "retrieval.documents")) for k in a):
        return a
    out = dict(a)
    put = lambda k, v: out.setdefault(k, v) if v is not None else None
    if kind == "LLM" or "llm.model_name" in a:
        put("gen_ai.request.model", a.get("llm.model_name"))
        put("gen_ai.response.model", a.get("llm.model_name"))
        put("gen_ai.system", a.get("llm.provider") or a.get("llm.system"))
        put("gen_ai.usage.input_tokens", a.get("llm.token_count.prompt"))
        put("gen_ai.usage.output_tokens", a.get("llm.token_count.completion"))
        put("gen_ai.operation.name", "chat")
        put("assay.cost_usd", a.get("llm.cost.total"))
        if a.get("llm.prompt_template.version"):
            put("assay.prompt_id", name or "prompt")
            put("assay.prompt_version", str(a["llm.prompt_template.version"]))
        msgs = _indexed(a, "llm.input_messages")
        if msgs:
            from assay_sdk.inputs import tokens
            roles = [(m.get("message.role"), m.get("message.content")) for m in msgs.values()]
            last = max((i for i, (r, _) in enumerate(roles) if r == "user"), default=None)
            ctx = {"system": 0, "history": 0, "user": 0}
            for i, (r, c) in enumerate(roles):
                ctx["system" if r in ("system", "developer") else "user" if i == last else "history"] += tokens(c)
            out["assay._context"] = {k: v for k, v in ctx.items() if v}
        text = next((m.get("message.content") for m in _indexed(a, "llm.output_messages").values()
                     if m.get("message.content")), None)
        put("assay._text", text)
    if kind == "TOOL" or ("tool.name" in a and kind not in ("LLM",)):
        put("gen_ai.operation.name", "execute_tool")
        put("gen_ai.tool.name", a.get("tool.name") or name)
        put("gen_ai.tool.call.arguments", a.get("input.value"))
        put("gen_ai.tool.call.result", a.get("output.value"))
    if kind == "RETRIEVER" or "retrieval.documents.0.document.content" in a:
        docs = [{"id": d.get("document.id"), "text": d.get("document.content"), "score": d.get("document.score")}
                for d in _indexed(a, "retrieval.documents").values()]
        out["assay._retrieval"] = {"query": a.get("input.value"), "fragments": docs}
    if root and kind in ("AGENT", "CHAIN"):
        put("assay.answer", a.get("output.value"))
        put("assay._input", a.get("input.value"))
        put("assay.task", name)
        out["assay._agent"] = True
    put("assay.user", a.get("user.id"))
    return out


def _attr_value(v: Dict[str, Any]):
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    return None


def _attrs(items) -> Dict[str, Any]:
    return {a.get("key"): _attr_value(a.get("value") or {}) for a in items or []}


def _str(v) -> Optional[str]:
    return None if v is None else str(v)


def _ts(nanos) -> Optional[datetime]:
    return datetime.utcfromtimestamp(int(nanos) / 1e9) if nanos else None


def from_otlp(payload: Dict[str, Any]) -> EventBatch:
    """Map an OTLP ExportTraceServiceRequest (JSON) onto Assay records."""
    spans_by_id, trace_doc = {}, {}
    collected = []
    for rs in payload.get("resourceSpans") or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                a = openinference(_attrs(sp.get("attributes")), not sp.get("parentSpanId"), sp.get("name"))
                collected.append((sp, a, res))
                spans_by_id[sp.get("spanId")] = (sp, a)
                doc = a.get("assay.document_id") or a.get("document.id")
                if doc:
                    trace_doc[sp.get("traceId")] = str(doc)

    def doc_of(sp, a):
        d = a.get("assay.document_id") or a.get("document.id")
        return str(d) if d else trace_doc.get(sp.get("traceId"), sp.get("traceId"))

    batch = EventBatch()
    for sp, a, res in collected:
        errored = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
        start, end = _ts(sp.get("startTimeUnixNano")), _ts(sp.get("endTimeUnixNano"))
        doc_id = doc_of(sp, a)
        parent = spans_by_id.get(sp.get("parentSpanId"))
        if not sp.get("parentSpanId"):
            batch.documents.append(DocumentEvent(
                document_id=doc_id, received_at=start or datetime.utcnow(),
                completed_at=None if errored else end, status="failed" if errored else "completed",
                document_type=a.get("assay.document_type"), segment=a.get("assay.segment"),
                page_count=a.get("assay.page_count"), processing_mode=a.get("assay.processing_mode")))
        if any(k.startswith("gen_ai.") for k in a):
            stage = (a.get("assay.stage") or (parent[1].get("assay.stage") or parent[0].get("name") if parent else None)
                     or sp.get("name"))
            batch.calls.append(CallEvent(
                call_id=sp.get("spanId") or uuid.uuid4().hex, stage=str(stage), ts=start or datetime.utcnow(),
                document_id=doc_id, model_declared=a.get("gen_ai.request.model"),
                model_served=a.get("gen_ai.response.model") or a.get("gen_ai.request.model"),
                latency_ms=(end - start).total_seconds() * 1000 if start and end else None,
                cost_usd=a.get("assay.cost_usd"), resolving_layer=a.get("assay.resolving_layer"),
                gate_reason=a.get("assay.gate_reason"), code_revision=res.get("service.version"),
                status="error" if errored else "success",
                segment=a.get("assay.segment"), document_type=a.get("assay.document_type"),
                prompt_id=_str(a.get("assay.prompt_id") or (parent[1].get("assay.prompt_id") if parent else None)),
                prompt_version=_str(a.get("assay.prompt_version")
                                    or (parent[1].get("assay.prompt_version") if parent else None))))
        elif a.get("assay.stage"):
            outputs = {k[len("assay.output."):]: v for k, v in a.items() if k.startswith("assay.output.")}
            batch.stage_runs.append(StageRunEvent(
                run_id=sp.get("spanId"), document_id=doc_id, stage=str(a["assay.stage"]),
                status="failed" if errored else "success", started_at=start, finished_at=end,
                did_work=a.get("assay.did_work"), outputs=outputs or None, sequence=a.get("assay.sequence"),
                prompt_id=_str(a.get("assay.prompt_id")), prompt_version=_str(a.get("assay.prompt_version"))))
    _agent_trajectories(batch, collected, doc_of)
    return batch


def otlp_root_ends(payload: Dict[str, Any]) -> List[dict]:
    """Every root span in the payload: the end of its trace's run. A run whose tool spans came in an
    earlier batch ends when its root span arrives, even in a batch with nothing else."""
    out = []
    for rs in payload.get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                if sp.get("parentSpanId"):
                    continue
                a = openinference(_attrs(sp.get("attributes")), True, sp.get("name"))
                doc = a.get("assay.document_id") or a.get("document.id") or sp.get("traceId")
                errored = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
                out.append({"trajectory_id": str(doc), "finished_at": _ts(sp.get("endTimeUnixNano")),
                            "status": "failed" if errored else "completed", "answer": _str(a.get("assay.answer")),
                            "task": _str(a.get("assay.task") or a.get("assay.document_type"))})
    return out


def end_trajectories(engine: Engine, tenant: str, ends: List[dict]) -> List[str]:
    """Close runs still open that these root spans end. Returns their ids."""
    t = store.agent_trajectories
    done = []
    with engine.begin() as conn:
        for e in ends:
            vals = {"status": e["status"], "finished_at": e["finished_at"], "updated_at": datetime.utcnow()}
            if e["answer"]:
                vals["answer"] = e["answer"]
            if e["task"]:
                vals["task"] = func.coalesce(t.c.task, e["task"])
            n = conn.execute(t.update().where((t.c.tenant == tenant) & (t.c.trajectory_id == e["trajectory_id"])
                                              & (t.c.status == "running")).values(**vals)).rowcount
            if n:
                done.append(e["trajectory_id"])
    return done


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _args(v):
    from assay_sdk.llm import normalize_args
    return normalize_args(_json(v))


def _conversation(spans) -> Optional[str]:
    """The conversation a trace belongs to, from the OpenTelemetry attributes that name it."""
    return next((a[k] for _, a, _ in spans for k in ("gen_ai.conversation.id", "session.id") if a.get(k)), None)


def _agent_trajectories(batch: EventBatch, collected, doc_of) -> None:
    """Turn traces with tool spans into trajectories (see OTEL_MAPPING)."""
    by_doc = defaultdict(list)
    for sp, a, res in collected:
        by_doc[doc_of(sp, a)].append((sp, a, res))
    agent_docs = set()
    for doc, spans in by_doc.items():
        is_tool = lambda a: a.get("gen_ai.operation.name") == "execute_tool" or "gen_ai.tool.name" in a
        if not any(is_tool(a) or a.get("assay._retrieval") or a.get("assay._agent") for _, a, _ in spans):
            continue
        agent_docs.add(doc)
        # Spans are exported as they end, so the root (the whole run) usually comes last, often in a
        # later batch. Until it arrives, the run is still going (assay/lifecycle.py).
        root = next(((sp, a) for sp, a, _ in spans if not sp.get("parentSpanId")), None)
        steps = []
        for sp, a, res in sorted(spans, key=lambda x: int(x[0].get("startTimeUnixNano") or 0)):
            errored = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
            start, end = _ts(sp.get("startTimeUnixNano")), _ts(sp.get("endTimeUnixNano"))
            if is_tool(a):
                steps.append(StepEvent(kind="tool", name=str(a.get("gen_ai.tool.name") or sp.get("name")),
                                       args=None if a.get("gen_ai.tool.call.arguments") is None
                                       else _args(a.get("gen_ai.tool.call.arguments")),  # never dropped
                                       result=_json(a.get("gen_ai.tool.call.result")),
                                       error=((sp.get("status") or {}).get("message") or "error") if errored else None,
                                       started_at=start, finished_at=end))
            elif a.get("assay._retrieval"):
                from assay.schema import retrieval_args
                from assay_sdk.retrieval import fragments
                r = a["assay._retrieval"]
                frags = fragments([d for d in r["fragments"] if d.get("text") or d.get("id")])
                steps.append(StepEvent(kind="retrieval", name=str(sp.get("name") or "retrieve")[:128],
                                       args=retrieval_args(_str(r["query"]), frags), result=frags,
                                       started_at=start, finished_at=end))
            elif a.get("assay.state.object"):
                steps.append(StepEvent(kind="state", name=str(a["assay.state.object"]),
                                       args={"op": a.get("assay.state.op") or "update"},
                                       result=_json(a.get("assay.state.value")), started_at=start, finished_at=end))
            elif any(k.startswith("gen_ai.") for k in a) and sp.get("parentSpanId"):
                tokens = (a.get("gen_ai.usage.input_tokens") or 0) + (a.get("gen_ai.usage.output_tokens") or 0)
                pv = a.get("assay.prompt_version")
                steps.append(StepEvent(kind="reason", model=a.get("gen_ai.response.model") or a.get("gen_ai.request.model"),
                                       tokens=tokens or None, tokens_in=a.get("gen_ai.usage.input_tokens"),
                                       tokens_out=a.get("gen_ai.usage.output_tokens"), cost_usd=a.get("assay.cost_usd"),
                                       prompt=f"{a.get('assay.prompt_id')}@{pv}" if pv else None,
                                       context=a.get("assay._context"), text=_str(a.get("assay._text")),
                                       started_at=start, finished_at=end))
        rsp, ra = root or (None, {})
        if not root:  # the run's own attributes are on the root; take what a child carries meanwhile
            for _, a, _ in spans:
                ra = {**{k: v for k, v in a.items() if k.startswith("assay.")}, **ra}
        if ra.get("assay.answer") and root:
            steps.append(StepEvent(kind="answer", text=str(ra["assay.answer"]), started_at=_ts(rsp.get("endTimeUnixNano"))))
        errored = root is not None and (rsp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
        first = min((int(sp.get("startTimeUnixNano") or 0) for sp, _, _ in spans), default=0)
        batch.trajectories.append(TrajectoryEvent(
            trajectory_id=doc, run_id=_str(ra.get("assay.run_id")), case_id=_str(ra.get("assay.case_id")),
            attempt=ra.get("assay.attempt"), task=_str(ra.get("assay.task") or ra.get("assay.document_type")),
            segment=_str(ra.get("assay.segment")),
            conversation_id=_str(ra.get("assay.conversation_id") or _conversation(spans)),
            turn=ra.get("assay.turn"), user=_str(ra.get("assay.user") or next(
                (a.get("assay.user") for _, a, _ in spans if a.get("assay.user")), None)),
            input=_json(ra.get("assay._input")) if ra.get("assay._input") is not None else None,
            started_at=_ts(rsp.get("startTimeUnixNano") if root else first) or datetime.utcnow(),
            finished_at=_ts(rsp.get("endTimeUnixNano")) if root else None,
            answer=_str(ra.get("assay.answer")) if root else None,
            status=("failed" if errored else "completed") if root else "running", steps=steps[:500]))
    # Their model spans are reasoning steps now; don't count them again as calls.
    batch.calls = [c for c in batch.calls if c.document_id not in agent_docs]
