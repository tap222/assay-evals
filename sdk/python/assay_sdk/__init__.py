"""Assay SDK: record what your AI system does, and how it went. Standard library only.

    import assay_sdk as assay

    assay.init("https://assay.example.com", key="ak_...")   # or ASSAY_URL / ASSAY_KEY
    assay.init()                                             # no server: record to .assay/events.jsonl

    with assay.run("refund_request", input=message, version={"prompt": "support@v5"}) as run:
        run.llm(model="claude-sonnet-5", tokens_in=620, tokens_out=180, cost_usd=0.0024)
        order = run.call("get_order", get_order, order_id="O-17")   # runs it, records result or error
        run.state("refund:O-17", "create", {"amount": 27.61})
        run.answer("Refunded $27.61.")

    assay.feedback(run.id, "thumbs_up")                       # later, from your UI
    assay.check("nightly-0924", "case-17", "pass", run_id=run.id, field="answer")   # from your tests

Events follow the Assay event schema v1 (docs/event-schema.md) and stream to
POST /v1/ingest in the background: a run that crashes still shows every step up
to that point. With no server configured they go to a local file instead, one
event per line, for `assay load` (or a later upload) to pick up. Every event has an id, so retries never duplicate anything. The
SDK never raises into your code (pass strict=True to init while developing).
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

__all__ = ["init", "run", "tagged", "claim_review", "feedback", "check", "correction", "expect", "prompt", "flush", "shutdown", "Run", "duplicate", "aduplicate", "DuplicateResult", "check_citations", "extract_citations", "Citation",
           "Scope", "ScopeViolation", "PathWithin", "scope", "probe", "ProbeResult", "PolicyGate", "GateDecision", "PolicyGateReport", "evaluate_policy_gate", "scan_security_flags", "SecurityFlag", "check_gateway_bypass", "profile_agent_reach", "compare_agent_reach", "check_markdown_exfiltration", "check_untrusted_taint", "check_fails_closed",
           "Scenario", "HandoffContract", "ScenarioResult", "BrokenTransition", "crash_at", "ProcessCrash", "validate_handoffs", "check_compensations", "find_first_broken_transition",
           "AgentIdentity", "IdentityViolation", "DriftAlert", "AgentBaseline", "AccessAuditReport", "ActionAuditRecord",
           "check_identity_violations", "compute_identity_metrics", "learn_agent_baseline", "detect_runtime_drift",
           "audit_access_held", "audit_all_agents_access", "query_action_audit", "export_audit_csv",
           "export_signed_audit", "verify_signed_audit", "detect_rubber_stamp_approvals", "hash_arguments",
           "verify_approval_for_action", "check_self_claimed_approvals",
           "CallerMove", "VoicePersona", "VoiceCallRecord", "VoiceTurn", "VoiceCallMetrics", "parse_call_record",
           "compute_voice_call_metrics", "simulate_call", "resolve_relative_date", "check_read_back",
           "check_booking_details", "check_spoken_leaks", "check_handoff_summary", "compare_voice_runs",
           "AgentHandoffContract", "AgentTreeNode", "validate_agent_handoffs", "build_agent_tree", "render_agent_tree", "diff_agent_trees",
           "Decision", "DecisionDivergence", "RunReplayResult", "ReplayReport", "classify_tool_risk", "check_tool_definitions_drift", "replay_decision_by_decision",
           "ServingSetup", "StepDiff", "CaseServingDiff", "ServingDiffReport", "find_first_differing_step", "compare_serving_setups",
           "EvidenceItem", "DecisionProvenance", "CaseEvidenceDiff", "EvidenceDiffReport", "is_superseded",
           "extract_evidence_items", "extract_decision_provenance", "compare_case_evidence", "compare_evidence_diffs"]
__version__ = "0.5.0"

log = logging.getLogger("assay_sdk")
SCHEMA = 1


_last_now = [datetime.min.replace(tzinfo=timezone.utc)]
_now_lock = threading.Lock()


def _now() -> str:
    """Now, always later than the last one this process gave: on a coarse clock (Windows), two
    events recorded back to back still keep their order."""
    with _now_lock:
        t = datetime.now(timezone.utc)
        if t <= _last_now[0]:
            t = _last_now[0] + timedelta(microseconds=1)
        _last_now[0] = t
    return t.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ts(t: Optional[datetime]) -> Optional[str]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(v):
    """Make a value JSON-serializable without failing: unknown objects become their repr."""
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        if isinstance(v, dict):
            return {str(k): _json_safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [_json_safe(x) for x in v]
        if isinstance(v, datetime):
            return _ts(v)
        return repr(v)


class _Client:
    def __init__(self, url: str, key: Optional[str], tenant: Optional[str], redact: Optional[Callable],
                 sample: float, flush_interval: float, batch_size: int, max_queue: int, strict: bool,
                 transport: Optional[Callable[[List[dict]], None]], enabled: bool):
        self.url, self.key, self.tenant = url.rstrip("/"), key, tenant
        self.redact, self.sample, self.strict, self.enabled = redact, sample, strict, enabled
        self.batch_size, self.max_queue = batch_size, max_queue
        self._send = transport or self._http
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._loop, args=(flush_interval,), name="assay-evals", daemon=True)
        self._thread.start()

    def emit(self, event: dict) -> None:
        if not self.enabled:
            return
        event = {"v": SCHEMA, "id": uuid.uuid4().hex, "ts": _now(), **event}
        with self._lock:
            if len(self._queue) >= self.max_queue:
                self._queue.pop(0)
                self.dropped += 1
                if self.dropped in (1, 100, 10000):
                    log.warning("Assay: queue full, dropped %d events so far (is the server reachable?)", self.dropped)
            self._queue.append({k: v for k, v in event.items() if v is not None})
            full = len(self._queue) >= self.batch_size
        if full:
            self._wake.set()

    def clean(self, v):
        """Redact (if configured), then make JSON-safe."""
        if v is None:
            return None
        if self.redact:
            try:
                v = self.redact(v)
            except Exception:
                log.exception("Assay: redact() failed; dropping the value rather than sending it unredacted")
                return "<redaction failed>"
        return _json_safe(v)

    def flush(self) -> bool:
        """Send everything queued. True if the queue is empty afterwards."""
        while True:
            with self._lock:
                batch, self._queue = self._queue[:self.batch_size], self._queue[self.batch_size:]
            if not batch:
                return True
            try:
                self._send(batch)
            except Exception:
                with self._lock:  # keep them, in order, for the next try
                    self._queue = batch + self._queue
                if self.strict:
                    raise
                log.warning("Assay: couldn't send %d events; will retry", len(batch), exc_info=True)
                return False

    def _loop(self, interval: float) -> None:
        backoff = interval
        while not self._stop:
            self._wake.wait(backoff)
            self._wake.clear()
            ok = self.flush() if not self.strict else self._flush_quiet()
            backoff = interval if ok else min(backoff * 2, 60.0)

    def _flush_quiet(self) -> bool:
        try:
            return self.flush()
        except Exception:
            return False

    def close(self) -> None:
        self._stop = True
        self._wake.set()
        # Let a send in progress finish first: at exit the daemon thread is killed, and a batch it
        # has taken off the queue would be lost.
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self.flush()

    def _http(self, batch: List[dict]) -> None:
        headers = {"Content-Type": "application/json", "User-Agent": f"assay-evals-python/{__version__}"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.tenant:
            headers["X-Tenant"] = self.tenant
        req = urllib.request.Request(self.url + "/v1/ingest", data=json.dumps({"events": batch}).encode(),
                                     headers=headers, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code == 422:  # a malformed event won't improve with retries: log it and drop the batch
                    log.error("Assay rejected events: %s", e.read().decode()[:1000])
                    return
                if e.code < 500 and e.code != 429 or attempt == 2:
                    raise
            except OSError:
                if attempt == 2:
                    raise
            time.sleep(0.5 * 2 ** attempt)


def _file_transport(path: str) -> Callable[[List[dict]], None]:
    """Append each batch to a JSON Lines file, one event per line, in the body /v1/ingest takes.
    Each batch is one O_APPEND write, so processes recording to the same file (pytest -n) don't
    split each other's lines."""
    path = os.path.abspath(path)  # fixed now, so a later chdir can't scatter events
    lock = threading.Lock()

    def write(batch: List[dict]) -> None:
        data = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in batch).encode()
        with lock:
            folder = os.path.dirname(path)
            if not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, ".gitignore"), "w", encoding="utf-8") as f:  # recorded inputs stay out of git
                    f.write("*\n")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            try:
                if os.name == "nt":  # Windows emulates O_APPEND (seek, then write): hold a lock across processes
                    _locked_append(fd, data)
                else:
                    while data:
                        data = data[os.write(fd, data):]
            finally:
                os.close(fd)
    return write


def _locked_append(fd: int, data: bytes) -> None:
    """Windows: lock the file's first byte (other writers wait, up to 10 s per try), append, unlock."""
    import msvcrt
    for attempt in range(30):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            break
        except OSError:
            if attempt == 29:
                raise
    try:
        os.lseek(fd, 0, os.SEEK_END)
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _tool_names(tools: Optional[List[Any]]) -> Optional[List[str]]:
    """Names from what a model is given: "get_order", {"name": ...}, or {"function": {"name": ...}}."""
    if tools is None:
        return None
    out = []
    for t in tools:
        if isinstance(t, dict):
            t = t.get("name") or (t.get("function") or {}).get("name")
        out.append(str(t)[:128])
    return out


def _test(test: Any) -> Optional[Dict[str, Any]]:
    """test="case-17" or {"case": ...}; `assay test` fills in the run and the attempt."""
    if test is None:
        return None
    test = {"case": test} if isinstance(test, str) else dict(test)
    test.setdefault("run", os.environ.get("ASSAY_TEST_RUN") or "local")
    if "attempt" not in test and os.environ.get("ASSAY_TEST_ATTEMPT"):
        test["attempt"] = int(os.environ["ASSAY_TEST_ATTEMPT"])
    return test


_client: Optional[_Client] = None


def init(url: Optional[str] = None, key: Optional[str] = None, *, tenant: Optional[str] = None,
         redact: Optional[Callable[[Any], Any]] = None, sample: float = 1.0, flush_interval: float = 1.0,
         batch_size: int = 500, max_queue: int = 100_000, strict: bool = False,
         transport: Optional[Callable[[List[dict]], None]] = None, enabled: bool = True,
         path: Optional[str] = None) -> None:
    """Configure the SDK once, at startup.

    url / key      default to the ASSAY_URL / ASSAY_KEY environment variables
    path           with no url, where events are recorded instead: ASSAY_PATH, else .assay/events.jsonl
    redact         applied to inputs, arguments, results, text and outputs before they leave the process
    sample         share of runs to record (0.1 = one in ten); outcomes are always sent
    strict         raise send errors instead of logging them (for development)
    enabled        False turns the SDK into a no-op (e.g. in unit tests)
    """
    global _client
    if _client is not None:
        _client.close()
    url = url or os.environ.get("ASSAY_URL")
    if not url and transport is None:
        path = path or os.environ.get("ASSAY_PATH") or os.path.join(".assay", "events.jsonl")
        transport = _file_transport(path)
        log.info("Assay: no server set (ASSAY_URL); recording to %s", path)
    _client = _Client(url or "http://localhost", key or os.environ.get("ASSAY_KEY"), tenant, redact, sample,
                      flush_interval, batch_size, max_queue, strict, transport, enabled)
    try:
        from assay_sdk.offline import record as record_offline
        record_offline()
    except Exception:
        pass


def _c() -> _Client:
    if _client is None:
        init()
    return _client


class Run:
    """One run of your system on one input. Use through `assay.run(...)`."""

    def __init__(self, client: _Client, run_id: str, recorded: bool):
        self.id, self._c, self._on = run_id, client, recorded
        self._seq, self._lock, self._parents = 0, threading.Lock(), []
        self.answer_text: Optional[str] = None
        self.test: Optional[Dict[str, Any]] = None  # {"run", "case", "attempt"} for a test-case run
        self.steps: List[Dict[str, Any]] = []  # what was recorded, in order: for assertions (assay_sdk.testing)
        self.expected: Optional[Dict[str, Any]] = None  # what run.expect() said
        self.outcome_value: Optional[str] = None  # run.outcome()
        self.started, self.ended = time.monotonic(), None  # for the run's latency
        self.expectations: List[Any] = []  # assay_sdk.testing.expect(run): checked when the test ends
        self.request: Any = None  # what the run was asked (input=): personal data it gave may be said back
        self.tool_schemas: Dict[str, dict] = {}  # the input schema of each tool a model was offered
        self.tool_definitions: Optional[Any] = None
        self.duplicate: bool = False  # True if this run is a duplicate delivery of a request
        self.original_run_id: Optional[str] = None
        self.task: Optional[str] = None
        self.logged_errors: List[Dict[str, Any]] = []
        self.scope_violations: List[Any] = []
        self.agent_id: Optional[str] = None
        self.owner: Optional[str] = None
        self.requested_by: Optional[str] = None
        self.credential: Optional[str] = None
        self.credential_type: Optional[str] = None
        self.credential_owner: Optional[str] = None
        self.voice_call_record: Optional[Any] = None
        self.serving: Optional[Any] = None

    def _case(self) -> Dict[str, Any]:
        if not self.test:
            raise ValueError("This run isn't a test case: start it with assay.run(..., test=\"<case>\").")
        return self.test

    def expect(self, **kwargs) -> None:
        """assay.expect() for this run's test case: calls=, answer=, state=, allow_extra=, max_steps=."""
        expect(self._case()["case"], **kwargs)
        self.expected = kwargs

    def check(self, field: str, status: str, **kwargs) -> None:
        """assay.check() for this run's test case, e.g. run.check("total", "fail", expected=..., actual=...)."""
        t = self._case()
        check(t["run"], t["case"], status, attempt=t.get("attempt"), run_id=self.id, field=field, **kwargs)

    def _step(self, kind: str, started: Optional[datetime] = None, **fields) -> int:
        with self._lock:
            seq, self._seq = self._seq, self._seq + 1
            parent = self._parents[-1] if self._parents else None
        step = {"seq": seq, "kind": kind, "parent_seq": parent, "ts": _ts(started) or _now(), **fields}
        self.steps.append(step)
        if self._on:
            self._c.emit({"type": "step", "run_id": self.id, **step})
        return seq

    def llm(self, model: Optional[str] = None, tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
            cost_usd: Optional[float] = None, prompt: Optional[str] = None, text: Optional[str] = None,
            started: Optional[datetime] = None, ended: Optional[datetime] = None, error: Optional[str] = None,
            tools: Optional[List[Any]] = None, name: Optional[str] = None, finish_reason: Optional[str] = None,
            tool_calls: Optional[List[dict]] = None, tokens_cached: Optional[int] = None,
            tokens_reasoning: Optional[int] = None, context: Optional[Dict[str, int]] = None,
            media: Optional[Dict[str, Any]] = None, settings: Optional[Dict[str, Any]] = None,
            serving: Optional[Union[Dict[str, Any], Any]] = None) -> None:
        """A model call. prompt is "id@version"; text is the output (or a summary of it); tools are the
        tools the model was offered (names, or the tool definitions you passed the model). name: the
        step it belongs to, for a pipeline (the calls of a step are its cost and its models). Without
        cost_usd, the cost comes from ASSAY_PRICES ([prices] in assay.toml) when the model has one.
        context: what the input was made of, in tokens ({"system", "tools", "history", "user",
        "retrieved"}); media: images and video ({"images", "videos", "bytes", "size", "detail"});
        settings: temperature, max_tokens, reasoning effort, ... assay.instrument() fills all three;
        serving: quantization, inference engine, engine version, hardware accelerator, batch size."""
        if cost_usd is None and model and (tokens_in or tokens_out):
            from assay_sdk.runtime import env_prices, price_of
            p = price_of(env_prices(), model)
            if p:
                cost_usd = round(((tokens_in or 0) * p[0] + (tokens_out or 0) * p[1]) / 1e6, 8)
        from assay_sdk.checks import tool_collisions, tool_schemas
        schemas = {k: v for k, v in tool_schemas(tools).items() if self.tool_schemas.get(k) != v}
        self.tool_schemas.update(schemas)  # sent once per run and tool, not with every call
        collisions = tool_collisions(tools)
        collisions_list = [{"name": k, "servers": v} for k, v in collisions.items()] if collisions else None
        serv = serving if serving is not None else getattr(self, "serving", None)
        serv_dict = serv.to_dict() if hasattr(serv, "to_dict") else serv
        self._step("llm", started, name=name, finish_reason=finish_reason, tool_calls=self._c.clean(tool_calls),
                   tool_schemas=schemas or None, tool_collisions=collisions_list, raw_tools=self._c.clean(tools),
                   tokens_cached=tokens_cached, tokens_reasoning=tokens_reasoning, model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                   prompt=prompt, text=self._c.clean(text), ended_at=_ts(ended),
                   status="error" if error else "ok", error=error, tools=_tool_names(tools),
                   context=context, media=media, settings=settings, serving=serv_dict)

    def approval(self, action: str, decision: str = "approved", by: Optional[str] = None,
                 reason: Optional[str] = None, server: Optional[str] = None,
                 source: Optional[str] = None, arg_hash: Optional[str] = None,
                 args: Optional[Dict[str, Any]] = None, time_to_approve_ms: Optional[float] = None,
                 evidence: Optional[str] = None, rule: Optional[str] = None,
                 started: Optional[datetime] = None) -> None:
        """A decision to allow an action, e.g. approval("refund", "approved", by="manager"). decision:
        approved, rejected or pending. Supports server.action syntax or server=.
        Contracts (requires_approval) and expect(run) check it."""
        if "." in action and server is None:
            server, action = action.split(".", 1)
        from assay_sdk.llm import normalize_args
        cleaned_args = self._c.clean(normalize_args(args)) if args is not None else None
        if arg_hash is None and cleaned_args is not None:
            from assay_sdk.production_security import hash_arguments
            arg_hash = hash_arguments(cleaned_args)
        self._step("approval", started, name=action, decision=decision, by=by, text=self._c.clean(reason),
                   server=server, source=source, arg_hash=arg_hash, args=cleaned_args,
                   time_to_approve_ms=time_to_approve_ms, evidence=evidence, rule=rule)

    def outcome(self, value: str) -> None:
        """Whether the run did what was asked: resolved, unresolved, or escalated (handed to a person)."""
        self.outcome_value = value

    def tool(self, name: str, args: Optional[Dict[str, Any]] = None, result: Any = None, error: Optional[str] = None,
             started: Optional[datetime] = None, ended: Optional[datetime] = None, server: Optional[str] = None,
             fault: Optional[str] = None, credential: Optional[str] = None, credential_type: Optional[str] = None,
             credential_owner: Optional[str] = None, requested_by: Optional[str] = None,
             agent: Optional[str] = None, agent_id: Optional[str] = None) -> None:
        """A tool call you've already made. server: the MCP server it went to, if any. fault: the fault
        assay.faults() injected in its place (empty, error, timeout, return)."""
        if "." in name and server is None and not name.startswith(("HTTP ", "SOCKET ", "CMD ", "FILE ")):
            server, name = name.split(".", 1)
        from assay_sdk.llm import normalize_args  # a JSON string, a list, a value: never rejected
        cleaned_args = self._c.clean(normalize_args(args))
        from assay_sdk.scopes import _current_scope
        sc = _current_scope.get()
        if sc is not None:
            v = sc.check_call(name, cleaned_args, seq=len(self.steps))
            if v:
                self.scope_violations.extend(v)
        cred = credential if credential is not None else getattr(self, "credential", None)
        cred_type = credential_type if credential_type is not None else getattr(self, "credential_type", None)
        cred_owner = credential_owner if credential_owner is not None else getattr(self, "credential_owner", None)
        req_by = requested_by if requested_by is not None else getattr(self, "requested_by", None)
        extra = {}
        if agent or agent_id:
            extra["agent"] = agent or agent_id
        self._step("tool", started, name=name, args=cleaned_args, result=self._c.clean(result),
                   ended_at=_ts(ended), status="error" if error else "ok", error=error, server=server, fault=fault,
                   credential=cred, credential_type=cred_type, credential_owner=cred_owner, requested_by=req_by,
                   **extra)

    def gate(self, tool: str, decision: str, reason: str, policy_version: str = "v1",
             args: Optional[Dict[str, Any]] = None) -> None:
        """Record a policy gate decision (allow, block, needs_approval) in front of a tool call."""
        self._step("approval", None, name=tool, decision=decision, error=reason if decision == "block" else None,
                   args={"reason": reason, "policy_version": policy_version, **(args or {})})

    def resource(self, uri: str, contents: Any = None, server: Optional[str] = None, error: Optional[str] = None,
                 started: Optional[datetime] = None, ended: Optional[datetime] = None) -> None:
        """An MCP resource the agent read, e.g. resource("file:///policies/refunds.md", text, server="docs").
        Its contents count as what the agent retrieved, for checking a judge's context."""
        self._step("resource", started, uri=uri, result=self._c.clean(contents), server=server,
                   ended_at=_ts(ended), status="error" if error else "ok", error=error)

    def retrieve(self, query: Any, fragments: Any, used: Any = None, name: str = "retrieve",
                 server: Optional[str] = None, error: Optional[str] = None,
                 started: Optional[datetime] = None, ended: Optional[datetime] = None,
                 fault: Optional[str] = None) -> None:
        """What a retrieval found and put into the prompt, e.g. retrieve(question, docs, used=4) for
        the top 4 after re-ranking. fragments: text, dicts (text, id, tokens, score, source),
        LangChain Documents or LlamaIndex nodes. used: which went into the prompt (None: all; an
        int: the first n; a list: ids or positions). Fragments per query, their tokens and their
        share of the prompt are compared with the baseline, and [behavior] limits apply to them.
        fault: the fault assay.faults() injected in its place (empty, error, timeout, return)."""
        from assay_sdk.retrieval import fragments as _fragments
        frags = [{**f, "text": self._c.clean(f["text"])} if f.get("text") else f
                 for f in _fragments(fragments, used)]
        self._step("retrieval", started, name=name[:128], query=self._c.clean(query if isinstance(query, str) else
                                                                          str(query))[:32768] if query is not None else None,
                   fragments=frags, server=server, ended_at=_ts(ended), status="error" if error else "ok", error=error,
                   fault=fault)

    def search(self, name: str, fn: Callable, query: Any, *positional, used: Any = None, **args):
        """Call fn(query, *positional, **args), record what it returned as a retrieval named `name`
        (or its error), and return it. assay.faults(name="empty") breaks it like a tool."""
        from assay_sdk.faults import call as fault_call, of as fault_of
        started = datetime.now(timezone.utc)
        try:
            out, fault = fault_call(name, fn, (query, *positional), args)
        except Exception as e:
            self.retrieve(query, [], name=name, error=f"{type(e).__name__}: {e}"[:2000], started=started,
                          ended=datetime.now(timezone.utc), fault=fault_of(e))
            raise
        self.retrieve(query, list(out or []), used=used, name=name, started=started,
                      ended=datetime.now(timezone.utc), fault=fault)
        return out

    def plan(self, steps: List[Any], text: Optional[str] = None) -> None:
        """What the agent means to do, before doing it: the tools it will call, in order, each a name
        or {"tool": name, "args": {...}}. The run is checked against it (plan adherence). Call it
        again to replan: the new plan replaces what was left of the old one."""
        self._step("plan", plan=[s if isinstance(s, str) else {**s, "args": self._c.clean(s.get("args"))}
                                 if s.get("args") else s for s in steps], text=self._c.clean(text))

    def mcp_prompt(self, name: str, args: Optional[Dict[str, Any]] = None, messages: Any = None,
                   server: Optional[str] = None, error: Optional[str] = None) -> None:
        """An MCP prompt the agent fetched: its name, the arguments, and the messages it returned."""
        self._step("mcp_prompt", name=name, args=self._c.clean(args or {}), result=self._c.clean(messages),
                   server=server, status="error" if error else "ok", error=error)

    def log(self, level: str, message: str, logger: Optional[str] = None, file: Optional[str] = None,
            line: Optional[int] = None, function: Optional[str] = None,
            started: Optional[datetime] = None) -> None:
        """Record an error or warning logged during this run. Turned into a canonical template
        (with IDs, numbers, and PII stripped) to detect regressions across test runs."""
        from assay_sdk.logged_errors import template_log
        tmpl = template_log(message)
        lvl = str(level).upper()
        entry = {
            "level": lvl,
            "message": message,
            "template": tmpl,
            "logger": logger or "",
            "file": file,
            "line": line,
            "function": function,
        }
        self.logged_errors.append(entry)
        self._step("log", started, name=logger or lvl, level=lvl,
                   message=self._c.clean(message), template=tmpl, logger=logger or "",
                   file=file, line=line, function=function,
                   error=tmpl, text=self._c.clean(message))

    def call(self, name: str, fn: Callable, *positional, server: Optional[str] = None, **args):
        """Call fn(*positional, **args), record it as a tool call (result or error, and timing), and
        return its result. Exceptions are recorded, then re-raised."""
        if "." in name and server is None and not name.startswith(("HTTP ", "SOCKET ", "CMD ", "FILE ")):
            server, name = name.split(".", 1)
        from assay_sdk.faults import call as fault_call, of as fault_of
        from assay_sdk.scenarios import check_crash_point
        check_crash_point(name, "before")
        started = datetime.now(timezone.utc)
        try:
            out, fault = fault_call(name, fn, positional, args)
        except Exception as e:
            self.tool(name, args, error=f"{type(e).__name__}: {e}"[:2000], started=started,
                      ended=datetime.now(timezone.utc), server=server, fault=fault_of(e))
            raise
        self.tool(name, args, out, started=started, ended=datetime.now(timezone.utc), server=server, fault=fault)
        check_crash_point(name, "after")
        return out

    def handoff(self, to_agent: str, kind: str = "transfer", payload: Optional[Dict[str, Any]] = None,
                from_agent: Optional[str] = None, owner: Optional[str] = None,
                accepted: Optional[bool] = None, allowed_tools: Optional[List[str]] = None,
                allowed_approvals: Optional[List[str]] = None) -> None:
        """Record an agent handoff (take over task or request advice)."""
        self._step("stage", None, name=f"handoff:{to_agent}",
                   args={"from": from_agent or getattr(self, "agent_name", "agent"),
                         "to": to_agent, "kind": kind, "payload": payload or {}, "owner": owner,
                         "accepted": accepted, "allowed_tools": allowed_tools,
                         "allowed_approvals": allowed_approvals})

    def accept_handoff(self, from_agent: str, accepted: bool = True, reason: Optional[str] = None) -> None:
        """Record acceptance (or refusal) of a task transfer by the receiving agent."""
        self._step("stage", None, name=f"handoff_accept:{from_agent}",
                   args={"from": from_agent, "accepted": accepted, "reason": reason})

    def state(self, obj: str, op: str = "update", value: Any = None) -> None:
        """A change to the world, e.g. state("order:17", "update", {"qty": 3}). op: create, update, delete."""
        self._step("state", name=obj, op=op, value=self._c.clean(value))

    def state_of(self, obj: str) -> Dict[str, Any]:
        """The last value this run recorded for a state object (run.state), or {}: for success checks."""
        for s in reversed(self.steps):
            if s["kind"] == "state" and s.get("name") == obj:
                v = s.get("value")
                return v if isinstance(v, dict) else {"value": v}
        return {}

    def user(self, text: str) -> None:
        """What the user said next, in a conversation recorded as one run (assay_sdk.simulate does it)."""
        self._step("user", text=self._c.clean(text))

    def answer(self, text: str, agent: Optional[str] = None) -> None:
        self.answer_text = text
        extra = {"agent": agent} if agent else {}
        self._step("answer", text=self._c.clean(text), **extra)

    def call_record(self, data: Dict[str, Any], platform: Optional[str] = None) -> None:
        """Attach a voice platform call record (Retell, Vapi, or generic telephony) to this run."""
        from dataclasses import asdict
        from assay_sdk.voice import parse_call_record
        rec = parse_call_record(data, platform=platform)
        self.voice_call_record = rec
        self._step("voice_call", None, call_id=rec.call_id, platform=rec.platform,
                   metrics=asdict(rec.metrics) if hasattr(rec.metrics, "__dataclass_fields__") else rec.metrics)

    @contextmanager
    def stage(self, name: str, prompt: Optional[str] = None):
        """A pipeline stage. Record what it produced in .outputs; llm/tool calls inside are nested under it.

            with run.stage("extract") as s:
                s.outputs.update(extract(text))
        """
        handle = _Stage()
        started = datetime.now(timezone.utc)
        with self._lock:
            seq, self._seq = self._seq, self._seq + 1
            parent = self._parents[-1] if self._parents else None  # a stage inside a stage
            self._parents.append(seq)
        error = None
        try:
            yield handle
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:2000]
            raise
        finally:
            with self._lock:
                self._parents.remove(seq)
            if self._on:
                self._c.emit({"type": "step", "run_id": self.id, "seq": seq, "kind": "stage", "name": name,
                              "parent_seq": parent, "ts": _ts(started), "ended_at": _now(), "status": "error" if error else "ok",
                              "error": error, "outputs": self._c.clean(handle.outputs) or None,
                              "did_work": handle.did_work, "prompt": prompt})


class _Stage:
    def __init__(self):
        self.outputs: Dict[str, Any] = {}
        self.did_work: Optional[bool] = True


@contextmanager
def run(task: Optional[str] = None, *, run_id: Optional[str] = None, kind: str = "agent", input: Any = None,
        input_ref: Optional[str] = None, version: Optional[Dict[str, str]] = None, segment: Optional[str] = None,
        test: Any = None, parent: Optional[Run] = None, tags: Optional[Dict[str, Any]] = None,
        conversation: Optional[str] = None, turn: Optional[int] = None, user: Optional[str] = None,
        duplicate: Optional[bool] = None, original_run_id: Optional[str] = None,
        agent_id: Optional[str] = None, owner: Optional[str] = None, requested_by: Optional[str] = None,
        credential: Optional[str] = None, credential_type: Optional[str] = None,
        credential_owner: Optional[str] = None, tool_definitions: Optional[Any] = None,
        serving: Optional[Union[Dict[str, Any], Any]] = None):
    """Record one run. kind is "agent" (llm/tool/state/answer steps) or "pipeline" (stages).
    conversation="c-1", turn=2: this run is one turn of a conversation (turns from 0).
    user: who asked, as a pseudonymous id (a hash, not an email), so the same person asking again
    in a new conversation is seen.
    test="case-17" (or {"run": "nightly-0924", "case": "case-17", "attempt": 0}) marks a test-case run;
    under `assay test`, the run and attempt are filled in.
    duplicate=True: a duplicate delivery of a previous request (webhooks, double clicks).
    agent_id, owner, requested_by, credential: identity and credential tracking for production security.
    An exception inside the block ends the run as failed (and is re-raised)."""
    c = _c()
    rid = run_id or uuid.uuid4().hex
    scope = _tags.get()
    if scope is not None:  # inside tagged(): this run carries its tags too
        tags = {**scope["tags"], **(tags or {})}
        scope["runs"].append(rid)
    recorded = c.sample >= 1 or random.random() < c.sample
    r = Run(c, rid, recorded)
    r.test = _test(test)
    r.request = input
    r.task = task
    r.duplicate = bool(duplicate) if duplicate is not None else False
    r.original_run_id = original_run_id
    r.tool_definitions = tool_definitions
    r.serving = serving
    r.agent_id = agent_id or task
    r.owner = owner
    r.requested_by = requested_by or user
    r.credential = credential
    r.credential_type = credential_type
    r.credential_owner = credential_owner
    token = _current.set(r)  # assay.current(): what @assay.step, @assay.tool and assay.instrument() record into
    if recorded:
        c.emit({"type": "run.start", "run_id": rid, "kind": kind, "task": task, "segment": segment,
                "input": c.clean(input), "input_ref": input_ref, "version": version, "test": r.test,
                "parent_run_id": parent.id if parent else None, "tags": tags, "conversation_id": conversation,
                "turn": turn, "user": None if user is None else str(user)[:128],
                "duplicate": bool(duplicate) if duplicate is not None else None,
                "original_run_id": original_run_id,
                "agent_id": r.agent_id, "owner": owner, "requested_by": r.requested_by,
                "credential": credential, "credential_type": credential_type,
                "credential_owner": credential_owner})
    from assay_sdk.logged_errors import capture_logs
    status, error = "completed", None
    try:
        with capture_logs(r):
            yield r
    except GeneratorExit:
        # Not from inside a `with` block: the run was dropped without being exited, so how it ended
        # is unknown. Leave it open; the server marks it abandoned once it goes quiet.
        status = None
        raise
    except BaseException as e:
        status, error = "failed", f"{type(e).__name__}: {e}"[:2000]
        raise
    finally:
        try:
            _current.reset(token)
        except ValueError:  # exited in another context (a generator closed elsewhere): nothing to restore
            pass
        r.ended = time.monotonic()
        if recorded and status:
            c.emit({"type": "run.end", "run_id": rid, "status": status, "error": error, "outcome": r.outcome_value})


_current: "contextvars.ContextVar[Optional[Run]]" = contextvars.ContextVar("assay_run", default=None)
_tags: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("assay_tags", default=None)


@contextmanager
def tagged(**tags):
    """Every run started inside carries these tags, including runs the code under test opens itself
    (@assay.agent, assay.run). Yields the list of run ids started inside. assay synth runs generated
    queries with tagged(origin="synthetic", ...), so they're never counted as production."""
    scope = {"tags": {k: v for k, v in tags.items() if v is not None}, "runs": []}
    token = _tags.set(scope)
    try:
        yield scope["runs"]
    finally:
        _tags.reset(token)


def current() -> Optional[Run]:
    """The run being recorded here (this thread or task), or None."""
    return _current.get()


def prompt(prompt_id: str, version: str, template: Optional[str] = None, note: Optional[str] = None) -> str:
    """Register a prompt version and its text; returns "id@version" for run.llm(prompt=...). With the
    text recorded, a regression shows what changed in the prompt next to it (assay test, assay diff)."""
    _c().emit({"type": "prompt", "prompt_id": prompt_id, "version": str(version), "template": template, "note": note})
    return f"{prompt_id}@{version}"


def feedback(run_id: str, kind: str, note: Optional[str] = None) -> None:
    """What a user did: thumbs_up, thumbs_down, retry, escalation, complaint, edited (changed the
    answer before using it) or redone (did it themselves). The last two are the quiet failures:
    the transcript looks fine, and the work was done by hand anyway."""
    _c().emit({"type": "feedback", "run_id": run_id, "kind": kind, "note": note})


def check(test_run: Optional[str], case: str, status: str, *, attempt: Optional[int] = None, run_id: Optional[str] = None,
          field: Optional[str] = None, expected: Any = None, actual: Any = None, evaluator: Optional[str] = None,
          score: Optional[float] = None, reason: Optional[str] = None,
          version: Optional[Dict[str, str]] = None, inputs: Optional[Dict[str, Any]] = None,
          error_kind: Optional[str] = None, tries: Optional[int] = None, raw_output: Optional[str] = None,
          category: Optional[str] = None, judge_model: Optional[str] = None,
          judge_prompt: Optional[str] = None, duration_ms: Optional[float] = None,
          cost_usd: Optional[float] = None, judgements: Optional[int] = None,
          judgements_passed: Optional[int] = None) -> None:
    """One result from a test run: status pass, fail, or error (the check couldn't run). Send passes too.
    test_run None: the run `assay test` is doing (else "local").
    inputs: what the evaluator saw, e.g. {"query": ..., "output": ..., "context": ...}, so Assay can
    check a judge got the right data (the output really is the run's answer, and so on).
    error_kind, with status "error": why it couldn't be judged (invalid, timeout, rate_limited,
    unavailable, error). tries: how many times the evaluator was asked; raw_output: what it said.
    category, with status fail: the kind of failure in your evaluator's words (grounding,
    policy_refusal, ...). An acknowledged failure wakes when its category changes, at any score.
    judge_model, judge_prompt ("rubric@3"): which judge it was. When either differs from the
    baseline's, the check isn't compared with it as if nothing changed but the AI. duration_ms and
    cost_usd: what the evaluator took to run (`assay evals guardrails` asks whether it could run in the
    request path). judgements and judgements_passed: the same output judged that many times, and
    how many passed it (evaluate(rejudge=)): a split vote is the judge, not the AI."""
    s = lambda v: None if v is None else v if isinstance(v, str) else json.dumps(_json_safe(v))
    t = _test({"case": case, **({"run": test_run} if test_run else {}),
               **({"attempt": attempt} if attempt is not None else {})})
    _c().emit({"type": "check", "test": t,
               "status": status, "run_id": run_id, "field": field, "expected": s(expected), "actual": s(actual),
               "evaluator": evaluator, "score": score, "reason": reason, "version": version,
               "inputs": _c().clean(inputs), "error_kind": error_kind, "tries": tries,
               "raw_output": None if raw_output is None else str(raw_output)[:16384],
               "category": _category(category),
               "judge_model": None if judge_model is None else str(judge_model)[:128],
               "judge_prompt": None if judge_prompt is None else str(judge_prompt)[:192],
               "duration_ms": None if duration_ms is None else round(float(duration_ms), 3),
               "cost_usd": cost_usd,
               **({"judgements": judgements, "judgements_passed": judgements_passed} if judgements else {})})


def _category(v: Any) -> Optional[str]:
    """A category as the schema takes it: words, _ - . /, at most 64 characters."""
    if v is None:
        return None
    s = re.sub(r"[^A-Za-z0-9 _./-]+", "_", str(v)).strip(" _")[:64]
    return s or None


def correction(run_id: str, field: str, expected: Any = None, observed: Any = None, kind: str = "wrong",
               reporter: Optional[str] = None) -> None:
    """Someone found a wrong value: Assay traces it to the step it started at."""
    s = lambda v: None if v is None else str(v)
    _c().emit({"type": "correction", "run_id": run_id, "field": field, "expected": s(expected),
               "observed": s(observed), "kind": kind, "reporter": reporter})


def claim_review(run_id: str, claim: str, verdict: str, *, evidence: Optional[List[Dict[str, Any]]] = None,
                 correction: Optional[str] = None, by: Optional[str] = None, note: Optional[str] = None) -> None:
    """An expert's decision on one claim in an answer, made while using the product: verdict is
    "supported", "wrong", "conflict_resolved" (sources disagreed, and they picked) or "unsure".
    evidence: the sources they checked it against, [{"id", "text" | "uri"}]. correction: what the
    claim should say. Redacted like every other event (init(redact=...)) before it leaves."""
    if verdict not in ("supported", "wrong", "conflict_resolved", "unsure"):
        raise ValueError("verdict is supported, wrong, conflict_resolved or unsure")
    c = _c()
    c.emit({"type": "claim_review", "run_id": run_id, "claim": c.clean(claim), "verdict": verdict,
            "evidence": c.clean(evidence), "correction": c.clean(correction), "by": by, "note": c.clean(note)})


def expect(case: str, *, calls: Optional[List[Dict[str, Any]]] = None, answer: Optional[str] = None,
           state: Optional[List[Dict[str, Any]]] = None, allow_extra: Optional[List[str]] = None,
           max_steps: Optional[int] = None, answer_match: str = "contains",
           checkpoints: Optional[List[Dict[str, Any]]] = None, split: bool = False) -> None:
    """What a test case should do: the tool calls, the answer, the end state, and goal checkpoints
    ([{"name": "listings retrieved", "tool": "search_listings", "result": "nonempty"}, ...]), each
    checked on its own. split=True checks the tool calls in parts too: the tools chosen, their arguments,
    and whether they worked."""
    _c().emit({"type": "expect", "case": case, "calls": calls or [], "answer": answer, "answer_match": answer_match,
               "state": state or [], "allow_extra": allow_extra or [], "max_steps": max_steps,
               "checkpoints": checkpoints or [], "split": split})


def flush() -> bool:
    """Send everything now (e.g. before a short-lived process exits). True if nothing is left."""
    return _c().flush() if _client else True


def shutdown() -> None:
    if _client:
        _client.close()


atexit.register(lambda: _client and _client.close())


# Your own evaluators, with results whose validity is explicit (assay_sdk/evaluation.py).
from assay_sdk.evaluation import Result, aevaluate, evaluate  # noqa: E402
# Attach to code with a few lines: @assay.step, @assay.tool, assay.instrument() (assay_sdk/auto.py).
from assay_sdk.auto import agent, instrument, pipeline, step, tool  # noqa: E402
# Any model provider, one shape (assay_sdk/llm.py).
from assay_sdk.llm import Judge, Response, normalize, normalize_args  # noqa: E402
from assay_sdk.runtime import EvalRuntime, Report, Sample  # noqa: E402
from assay_sdk.faithfulness import faithfulness  # noqa: E402
from assay_sdk.simulate import Persona, Simulation, load_personas, simulate  # noqa: E402
from assay_sdk.golden import golden_examples  # noqa: E402
# Breaking tools on purpose, to test error handling (assay_sdk/faults.py).
from assay_sdk.faults import REAL, ToolFault, faults  # noqa: E402
# Simulating duplicate requests (webhooks, double clicks) to test idempotency (assay_sdk/duplicate.py).
from assay_sdk.duplicate import DuplicateResult, aduplicate, duplicate  # noqa: E402
# Checking citations in RAG answers (assay_sdk/citations.py).
from assay_sdk.citations import Citation, check_citations, extract_citations  # noqa: E402
# Opt-in offline mode and monitoring real resource access (assay_sdk/offline.py).
from assay_sdk.offline import disable_offline, enable_offline, offline  # noqa: E402
# Agent security scopes, fails-closed, probes, gates, taint, and flags (assay_sdk/scopes.py).
from assay_sdk.scopes import (  # noqa: E402
    GateDecision,
    PathWithin,
    PolicyGate,
    PolicyGateReport,
    ProbeResult,
    Scope,
    ScopeViolation,
    SecurityFlag,
    check_fails_closed,
    check_gateway_bypass,
    check_markdown_exfiltration,
    check_untrusted_taint,
    compare_agent_reach,
    evaluate_policy_gate,
    probe,
    profile_agent_reach,
    scan_security_flags,
    scope,
)
# Cross-cutting agent reliability scenarios (assay_sdk/scenarios.py).
from assay_sdk.scenarios import (  # noqa: E402
    BrokenTransition,
    HandoffContract,
    ProcessCrash,
    Scenario,
    ScenarioResult,
    check_compensations,
    crash_at,
    find_first_broken_transition,
    validate_handoffs,
)
# Agent production security: identity, drift, access, and action audits (assay_sdk/production_security.py).
from assay_sdk.production_security import (  # noqa: E402
    AccessAuditReport,
    ActionAuditRecord,
    AgentBaseline,
    AgentIdentity,
    DriftAlert,
    IdentityViolation,
    audit_access_held,
    audit_all_agents_access,
    check_identity_violations,
    check_self_claimed_approvals,
    compute_identity_metrics,
    detect_rubber_stamp_approvals,
    detect_runtime_drift,
    export_audit_csv,
    export_signed_audit,
    hash_arguments,
    learn_agent_baseline,
    query_action_audit,
    verify_approval_for_action,
    verify_signed_audit,
)
# Voice and phone agents (assay_sdk/voice.py).
from assay_sdk.voice import (  # noqa: E402
    CallerMove,
    VoiceCallMetrics,
    VoiceCallRecord,
    VoicePersona,
    VoiceTurn,
    check_booking_details,
    check_handoff_summary,
    check_read_back,
    check_spoken_leaks,
    compare_voice_runs,
    compute_voice_call_metrics,
    parse_call_record,
    resolve_relative_date,
    simulate_call,
)
# Multi-agent handoff contracts, single ownership, permission containment, and tree diffs (assay_sdk/handoffs.py).
from assay_sdk.handoffs import (  # noqa: E402
    AgentHandoffContract,
    AgentTreeNode,
    build_agent_tree,
    diff_agent_trees,
    render_agent_tree,
    validate_agent_handoffs,
)
# Replay recorded runs decision by decision (assay_sdk/replay.py).
from assay_sdk.replay import (  # noqa: E402
    Decision,
    DecisionDivergence,
    ReplayReport,
    RunReplayResult,
    check_tool_definitions_drift,
    classify_tool_risk,
    replay_decision_by_decision,
)
# Model serving setups and serving diffs (assay_sdk/serving.py).
from assay_sdk.serving import (  # noqa: E402
    CaseServingDiff,
    ServingDiffReport,
    ServingSetup,
    StepDiff,
    compare_serving_setups,
    find_first_differing_step,
)
# Evidence diffs and decision provenance (assay_sdk/evidence.py).
from assay_sdk.evidence import (  # noqa: E402
    CaseEvidenceDiff,
    DecisionProvenance,
    EvidenceDiffReport,
    EvidenceItem,
    compare_case_evidence,
    compare_evidence_diffs,
    extract_decision_provenance,
    extract_evidence_items,
    is_superseded,
)
