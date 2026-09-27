"""Source over events pushed to Assay's ingest API (the multi-tenant path)."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.models import CallRecord, DocumentRecord, ErrorReport, IndexedRecord, ReviewRecord, StageRun, Window


class EventsSource:
    def __init__(self, engine: Engine, tenant: str):
        self.engine = engine
        self.tenant = tenant
        self.name = f"events:{tenant}"

    def _rows(self, table, time_col=None, window: Optional[Window] = None):
        cond = [table.c.tenant == self.tenant]
        if time_col is not None and window is not None:
            cond += [time_col >= window.start, time_col < window.end]
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(table).where(and_(*cond)))]

    # --- agents: a trajectory's steps, seen as stage runs and model calls ---

    def _agent_steps(self, window: Optional[Window] = None, ids: Optional[List[str]] = None):
        t, st = store.agent_trajectories, store.agent_steps
        cond = [t.c.tenant == self.tenant]
        if window is not None:
            cond += [t.c.started_at >= window.start, t.c.started_at < window.end]
        if ids is not None:
            cond.append(t.c.trajectory_id.in_(ids))
        q = (select(st, t.c.started_at.label("t_start"), t.c.lineage).join(
            t, and_(t.c.tenant == st.c.tenant, t.c.trajectory_id == st.c.trajectory_id)).where(and_(*cond)))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    @staticmethod
    def _as_runs(steps) -> List[StageRun]:
        """Tool calls and the answer as pipeline steps: the workflow graph, path contracts and
        error tracing then work on agents unchanged. Arguments and results go in outputs."""
        from datetime import timedelta
        out = []
        for s in steps:
            if s["kind"] not in ("tool", "answer"):
                continue
            start = s["started_at"] or s["t_start"] + timedelta(seconds=s["seq"])
            outputs = {"_args": s["args"], "_result": s["result"]} if s["kind"] == "tool" else {"answer": s["text"]}
            out.append(StageRun(document_id=s["trajectory_id"], stage=s["name"] or s["kind"],
                                status="error" if s["error"] else "success", started_at=start,
                                finished_at=s["finished_at"] or start, did_work=True, outputs=outputs,
                                sequence=s["seq"]))
        return out

    @staticmethod
    def _as_calls(steps) -> List[CallRecord]:
        from datetime import timedelta
        out = []
        for s in steps:
            if not s["model"]:
                continue
            start = s["started_at"] or s["t_start"] + timedelta(seconds=s["seq"])
            lin = s.get("lineage") or {}
            pid, _, ver = (s.get("prompt") or lin.get("prompt") or "").partition("@")  # the call's own, else the run's
            out.append(CallRecord(call_id=f"{s['trajectory_id']}#{s['seq']}", stage=s["kind"], ts=start,
                                  document_id=s["trajectory_id"], model_declared=s["model"], model_served=s["model"],
                                  cost_usd=s["cost_usd"], status="error" if s["error"] else "success",
                                  latency_ms=(s["finished_at"] - s["started_at"]).total_seconds() * 1000
                                  if s["started_at"] and s["finished_at"] else None,
                                  prompt_id=pid or None, prompt_version=ver or None, code_revision=lin.get("build")))
        return out

    def trajectory(self, trajectory_id: str) -> Optional[dict]:
        t, st = store.agent_trajectories, store.agent_steps
        with self.engine.connect() as conn:
            head = conn.execute(select(t).where(and_(t.c.tenant == self.tenant,
                                                     t.c.trajectory_id == trajectory_id))).first()
            if head is None:
                return None
            steps = conn.execute(select(st).where(and_(st.c.tenant == self.tenant, st.c.trajectory_id == trajectory_id))
                                 .order_by(st.c.seq)).all()
        return {**{k: v for k, v in head._mapping.items() if k != "tenant"},
                "steps": [{k: v for k, v in r._mapping.items() if k not in ("tenant", "trajectory_id")} for r in steps]}

    def trajectories(self, ids: List[str]) -> Dict[str, dict]:
        """Many trajectories with their steps, in two queries."""
        t, st = store.agent_trajectories, store.agent_steps
        out = {}
        with self.engine.connect() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for h in conn.execute(select(t).where(and_(t.c.tenant == self.tenant, t.c.trajectory_id.in_(chunk)))):
                    out[h.trajectory_id] = {**{k: v for k, v in h._mapping.items() if k != "tenant"}, "steps": []}
                for r in conn.execute(select(st).where(and_(st.c.tenant == self.tenant, st.c.trajectory_id.in_(chunk)))
                                      .order_by(st.c.trajectory_id, st.c.seq)):
                    if r.trajectory_id in out:
                        out[r.trajectory_id]["steps"].append(
                            {k: v for k, v in r._mapping.items() if k not in ("tenant", "trajectory_id")})
        return out

    def calls(self, window: Window) -> Iterable[CallRecord]:
        t = store.event_calls
        return [CallRecord(**{k: v for k, v in r.items() if k != "tenant"})
                for r in self._rows(t, t.c.ts, window)] + self._as_calls(self._agent_steps(window))

    def field_scores(self, window: Window) -> Optional[List[dict]]:
        """Extracted fields scored against their correct values (assay_sdk.documents), with their
        document's type and segment: {"document_id", "document_type", "segment", "field", "weight",
        "share"}. None if this tenant has never sent one. Line-item columns are left out: their
        table is counted whole."""
        import json
        t, d = store.eval_results, store.event_documents
        from assay.local import BASELINE  # copies of passing runs' results: counted once, as themselves
        cond = [t.c.tenant == self.tenant, t.c.evaluator == "assay.documents@1", t.c.status.in_(("pass", "fail")),
                t.c.field != "document", ~t.c.field.startswith("rule: "), t.c.run_id != BASELINE]
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.result_id).where(and_(*cond[:2])).limit(1)).first() is None:
                return None
            if window is not None:
                cond += [t.c.ts >= window.start, t.c.ts < window.end]
            rows = conn.execute(select(t.c.document_id, t.c.case_id, t.c.field, t.c.raw_output, d.c.document_type,
                                       d.c.segment).select_from(t.outerjoin(
                d, and_(d.c.tenant == t.c.tenant, d.c.document_id == t.c.document_id))).where(and_(*cond))).all()
        out = []
        for r in rows:
            try:
                raw = json.loads(r.raw_output or "{}")
            except ValueError:
                raw = {}
            if raw.get("part_of"):
                continue
            out.append({"document_id": r.document_id or r.case_id, "document_type": r.document_type,
                        "segment": r.segment, "field": r.field, "weight": float(raw.get("weight") or 1.0),
                        "share": float(raw.get("share", 1.0 if raw.get("kind") == "correct" else 0.0))})
        return out

    def document_checks(self, window: Window, kind: str, evaluator: str = "assay.documents@1") -> Optional[List[dict]]:
        """Checks of one kind recorded by assay_sdk.documents ("ocr", "location", "table",
        "spot_check"), with their counts and their document's type and segment: {"document_id",
        "document_type", "segment", "field", "passed", "raw"}. None if this tenant has never sent one
        of that kind; copies of passing runs kept as the baseline are left out."""
        import json
        from assay.local import BASELINE
        t, d = store.eval_results, store.event_documents
        cond = [t.c.tenant == self.tenant, t.c.evaluator == evaluator, t.c.raw_output.like(f'%"kind": "{kind}"%')]
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.result_id).where(and_(*cond)).limit(1)).first() is None:
                return None
            cond += [t.c.status.in_(("pass", "fail")), t.c.run_id != BASELINE]
            if window is not None:
                cond += [t.c.ts >= window.start, t.c.ts < window.end]
            rows = conn.execute(select(t.c.document_id, t.c.case_id, t.c.field, t.c.status, t.c.raw_output,
                                       d.c.document_type, d.c.segment).select_from(t.outerjoin(
                d, and_(d.c.tenant == t.c.tenant, d.c.document_id == t.c.document_id))).where(and_(*cond))).all()
        out = []
        for r in rows:
            try:
                raw = json.loads(r.raw_output or "{}")
            except ValueError:
                continue
            if raw.get("kind") == kind:
                out.append({"document_id": r.document_id or r.case_id, "document_type": r.document_type,
                            "segment": r.segment, "field": r.field, "passed": r.status == "pass", "raw": raw})
        return out

    def split_scores(self, window: Window) -> Optional[List[dict]]:
        """Files split into documents, scored against their correct boundaries (assay_sdk.documents.
        score_split): {"document_id", "segment", "document_type", "documents", "right"}. None if this
        tenant has never sent one."""
        import json
        from assay.local import BASELINE
        t, d = store.eval_results, store.event_documents
        cond = [t.c.tenant == self.tenant, t.c.evaluator == "assay.documents@1", t.c.field == "split"]
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.result_id).where(and_(*cond)).limit(1)).first() is None:
                return None
            cond += [t.c.status.in_(("pass", "fail")), t.c.run_id != BASELINE]
            if window is not None:
                cond += [t.c.ts >= window.start, t.c.ts < window.end]
            rows = conn.execute(select(t.c.document_id, t.c.case_id, t.c.status, t.c.raw_output, d.c.document_type,
                                       d.c.segment).select_from(t.outerjoin(
                d, and_(d.c.tenant == t.c.tenant, d.c.document_id == t.c.document_id))).where(and_(*cond))).all()
        out = []
        for r in rows:
            try:
                raw = json.loads(r.raw_output or "{}")
            except ValueError:
                raw = {}
            out.append({"document_id": r.document_id or r.case_id, "segment": r.segment,
                        "document_type": r.document_type, "documents": int(raw.get("documents") or 0),
                        "right": r.status == "pass"})
        return out

    def documents(self, window: Window) -> Iterable[DocumentRecord]:
        t = store.event_documents
        return [DocumentRecord(**{k: v for k, v in r.items() if k not in ("tenant", "delivered_downstream")})
                for r in self._rows(t, t.c.received_at, window)]

    def stage_runs(self, window: Window) -> Iterable[StageRun]:
        t = store.event_stage_runs
        return [StageRun(**{k: v for k, v in r.items() if k not in ("tenant", "run_id")})
                for r in self._rows(t, t.c.started_at, window)] + self._as_runs(self._agent_steps(window))

    def indexed(self, window: Window) -> Iterable[IndexedRecord]:
        t = store.event_indexed
        return [IndexedRecord(**{k: v for k, v in r.items() if k not in ("tenant", "extraction_id", "field")})
                for r in self._rows(t)]

    def reviews(self, window: Window) -> Optional[Iterable[ReviewRecord]]:
        """None if the tenant has never sent a review, so people cost reads as
        "not recorded" rather than "zero"."""
        t = store.event_reviews
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.review_id).where(t.c.tenant == self.tenant).limit(1)).first() is None:
                return None
        return [ReviewRecord(**{k: v for k, v in r.items() if k != "tenant"})
                for r in self._rows(t, t.c.ts, window)]

    def errors(self, window: Optional[Window], document_id: Optional[str] = None) -> Optional[List[ErrorReport]]:
        """Reported wrong outputs. None if this tenant has never reported one."""
        t = store.event_errors
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.error_id).where(t.c.tenant == self.tenant).limit(1)).first() is None:
                return None
            cond = [t.c.tenant == self.tenant]
            if window is not None:
                cond += [t.c.reported_at >= window.start, t.c.reported_at < window.end]
            if document_id is not None:
                cond.append(t.c.document_id == document_id)
            rows = conn.execute(select(t).where(and_(*cond)).order_by(t.c.reported_at)).all()
        return [ErrorReport(**{k: v for k, v in r._mapping.items() if k != "tenant"}) for r in rows]

    def document_detail(self, document_id: str) -> Optional[Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        d, r, c = store.event_documents, store.event_stage_runs, store.event_calls
        with self.engine.connect() as conn:
            doc = conn.execute(select(d).where(and_(d.c.tenant == self.tenant, d.c.document_id == document_id))).first()
            if not doc:
                return None
            runs = conn.execute(select(r).where(and_(r.c.tenant == self.tenant, r.c.document_id == document_id))
                                .order_by(r.c.started_at)).all()
            calls = conn.execute(select(c).where(and_(c.c.tenant == self.tenant, c.c.document_id == document_id))
                                 .order_by(c.c.ts)).all()
        drop = ("tenant", "run_id", "delivered_downstream")
        clean = lambda row: {k: v for k, v in row._mapping.items() if k not in drop}
        agent = self._agent_steps(ids=[document_id])
        return (DocumentRecord(**clean(doc)), [StageRun(**clean(x)) for x in runs] + self._as_runs(agent),
                [CallRecord(**clean(x)) for x in calls] + self._as_calls(agent))

    def document_details(self, document_ids: List[str]) -> Dict[str, Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        """document_detail for many documents in three queries."""
        if not document_ids:
            return {}
        d, r, c = store.event_documents, store.event_stage_runs, store.event_calls
        drop = ("tenant", "run_id", "delivered_downstream")
        clean = lambda row: {k: v for k, v in row._mapping.items() if k not in drop}
        out: Dict[str, list] = {}
        with self.engine.connect() as conn:
            for i in range(0, len(document_ids), 500):
                ids = document_ids[i:i + 500]
                for row in conn.execute(select(d).where(and_(d.c.tenant == self.tenant, d.c.document_id.in_(ids)))):
                    out[row.document_id] = [DocumentRecord(**clean(row)), [], []]
                for row in conn.execute(select(r).where(and_(r.c.tenant == self.tenant, r.c.document_id.in_(ids)))
                                        .order_by(r.c.started_at)):
                    if row.document_id in out:
                        out[row.document_id][1].append(StageRun(**clean(row)))
                for row in conn.execute(select(c).where(and_(c.c.tenant == self.tenant, c.c.document_id.in_(ids)))
                                        .order_by(c.c.ts)):
                    if row.document_id in out:
                        out[row.document_id][2].append(CallRecord(**clean(row)))
        agent = defaultdict(list)
        for i in range(0, len(document_ids), 500):
            for s in self._agent_steps(ids=document_ids[i:i + 500]):
                agent[s["trajectory_id"]].append(s)
        for d, steps in agent.items():
            if d in out:
                out[d][1].extend(self._as_runs(steps))
                out[d][2].extend(self._as_calls(steps))
        return {k: tuple(v) for k, v in out.items()}

    def downstream_hashes(self) -> Optional[Set[str]]:
        """Hashes of documents the tenant reported as delivered downstream.

        None if the tenant has never reported delivery at all, so the handoff
        measure says "unmeasured" instead of "everything was lost".
        """
        rows = self._rows(store.event_documents)
        reported = [r for r in rows if r["delivered_downstream"] is not None]
        if not reported:
            return None
        return {r["file_hash"] for r in reported if r["delivered_downstream"] and r["file_hash"]}
