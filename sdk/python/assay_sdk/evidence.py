"""Evidence diffs and decision provenance: detect when the same answer rested on different evidence.

Two runs can both say "approved" while the new one read an older policy, or the
wrong customer's record.
Extracts decision provenance:
- Retrieved fragments (id, source, version, date, entity, whether used in prompt)
- Tool records (entity, record id, tool outputs)
- Resources read (URIs, policy docs)

Per-case evidence diffs identify:
- Evidence dropped, added, or swapped
- Superseded evidence (older policy version or date than baseline)
- Records of another entity (wrong customer/account)
- Changed basis under an unchanged answer (a finding, not a pass)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union


@dataclass
class EvidenceItem:
    """An individual piece of evidence (retrieved fragment, database record, or resource)
    that an agent decision or answer rested upon.
    """
    kind: str  # "fragment", "record", "resource"
    id: str  # unique id (doc_id, record_id, uri)
    source: Optional[str] = None  # file, table, or endpoint name
    version: Optional[str] = None  # policy/doc version (e.g. "v1.0", "v2")
    date: Optional[str] = None  # timestamp or date string (e.g. "2026-01-15")
    entity: Optional[str] = None  # associated entity/customer/user id
    text: Optional[str] = None  # excerpt or snippet
    step_seq: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
            "version": self.version,
            "date": self.date,
            "entity": self.entity,
            "text": self.text,
            "step_seq": self.step_seq,
            "metadata": self.metadata,
        }

    def key(self) -> str:
        """Normalized identity key for matching across runs."""
        s = self.source or ""
        return f"{self.kind}:{s}:{self.id}"


@dataclass
class DecisionProvenance:
    """The complete provenance and evidentiary basis for an agent's run and answer."""
    case_name: str
    run_id: str
    answer: Optional[str]
    evidence_items: List[EvidenceItem]
    policy_versions: Dict[str, str]  # source -> version
    entities_consulted: Set[str]  # e.g. {"cust_123"}
    records_consulted: Set[str]  # e.g. {"order:100", "invoice:200"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_name": self.case_name,
            "run_id": self.run_id,
            "answer": self.answer,
            "evidence_items": [e.to_dict() for e in self.evidence_items],
            "policy_versions": self.policy_versions,
            "entities_consulted": sorted(self.entities_consulted),
            "records_consulted": sorted(self.records_consulted),
        }


def _parse_version_tuple(v: Optional[str]) -> Optional[Tuple[int, ...]]:
    if not v:
        return None
    # Strip leading 'v' or 'ver'
    clean = re.sub(r"^[vV](?:er(?:sion)?)?\.?", "", v.strip())
    parts = []
    for chunk in re.split(r"[.\-_]", clean):
        if chunk.isdigit():
            parts.append(int(chunk))
    return tuple(parts) if parts else None


def is_superseded(
    cand_version: Optional[str],
    base_version: Optional[str],
    cand_date: Optional[str] = None,
    base_date: Optional[str] = None,
) -> bool:
    """Return True if cand_version/cand_date is older than base_version/base_date."""
    # Check version tuples
    c_vt = _parse_version_tuple(cand_version)
    b_vt = _parse_version_tuple(base_version)
    if c_vt and b_vt:
        if c_vt < b_vt:
            return True
        if c_vt > b_vt:
            return False

    # Check date strings if versions are inconclusive
    if cand_date and base_date:
        if str(cand_date).strip() < str(base_date).strip():
            return True

    return False


def _extract_entity_from_obj(obj: Any) -> Optional[str]:
    """Helper to detect entity / customer IDs in arguments or results."""
    if not isinstance(obj, dict):
        return None
    for k in ("customer_id", "customer", "user_id", "uid", "account_id", "entity_id", "client_id", "entity"):
        if k in obj and obj[k]:
            return str(obj[k])
    return None


def extract_evidence_items(run_or_dict: Any) -> List[EvidenceItem]:
    """Extracts all evidence items that an agent run rested upon."""
    steps = getattr(run_or_dict, "steps", None) or (run_or_dict.get("steps") if isinstance(run_or_dict, dict) else []) or []
    items: List[EvidenceItem] = []

    # Also check run-level requested entity
    run_req_entity = None
    req = getattr(run_or_dict, "request", None) or (run_or_dict.get("input") if isinstance(run_or_dict, dict) else None)
    if isinstance(req, dict):
        run_req_entity = _extract_entity_from_obj(req)

    for s in steps:
        kind = s.get("kind")
        seq = s.get("seq")

        if kind == "retrieval":
            # Extract fragments that went into the prompt (used != False)
            frags = s.get("fragments") or []
            for f in frags:
                if f.get("used", True):
                    fid = str(f.get("id") or f.get("position") or len(items))
                    source = f.get("source")
                    version = f.get("version")
                    date = f.get("date")
                    entity = f.get("entity") or run_req_entity
                    text = f.get("text")
                    meta = {k: v for k, v in f.items() if k not in ("id", "source", "version", "date", "entity", "text")}
                    items.append(EvidenceItem(
                        kind="fragment",
                        id=fid,
                        source=source,
                        version=version,
                        date=date,
                        entity=entity,
                        text=text,
                        step_seq=seq,
                        metadata=meta,
                    ))

        elif kind == "resource":
            uri = s.get("uri") or f"resource_{len(items)}"
            contents = s.get("result")
            text = contents if isinstance(contents, str) else json.dumps(contents, default=str)
            items.append(EvidenceItem(
                kind="resource",
                id=uri,
                source=s.get("server") or uri,
                version=s.get("version"),
                date=s.get("date"),
                text=text[:500] if text else None,
                step_seq=seq,
            ))

        elif kind == "tool":
            tool_name = s.get("name") or "tool"
            args = s.get("args") or {}
            result = s.get("result")
            # Only consider read/fetch tools that returned data
            is_read = not any(tool_name.lower().startswith(v) for v in ("write", "delete", "drop", "refund", "charge", "send"))
            if is_read and result:
                entity = _extract_entity_from_obj(args) or _extract_entity_from_obj(result) or run_req_entity
                # Extract record id or construct from tool and args
                record_id = None
                for k in ("id", "order_id", "invoice_id", "case_id", "account_id", "doc_id"):
                    if k in args:
                        record_id = f"{k}:{args[k]}"
                        break
                    if isinstance(result, dict) and k in result:
                        record_id = f"{k}:{result[k]}"
                        break
                record_id = record_id or f"{tool_name}:{json.dumps(args, sort_keys=True)}"

                version = None
                date = None
                if isinstance(result, dict):
                    version = result.get("version") or result.get("ver") or result.get("policy_version")
                    date = result.get("date") or result.get("updated_at") or result.get("created_at")

                items.append(EvidenceItem(
                    kind="record",
                    id=record_id,
                    source=tool_name,
                    version=str(version) if version else None,
                    date=str(date) if date else None,
                    entity=entity,
                    text=json.dumps(result, default=str)[:500],
                    step_seq=seq,
                    metadata={"args": args},
                ))

    return items


def extract_decision_provenance(run_or_dict: Any) -> DecisionProvenance:
    """Builds the full provenance structure for a run."""
    run_id = getattr(run_or_dict, "id", None) or (run_or_dict.get("run_id") or run_or_dict.get("id") if isinstance(run_or_dict, dict) else "unknown_run")
    case_name = getattr(run_or_dict, "task", None) or (run_or_dict.get("task") or run_or_dict.get("case") if isinstance(run_or_dict, dict) else "unknown_case")

    # Extract answer
    answer = None
    steps = getattr(run_or_dict, "steps", None) or (run_or_dict.get("steps") if isinstance(run_or_dict, dict) else []) or []
    for s in reversed(steps):
        if s.get("kind") in ("answer", "llm") and s.get("text"):
            answer = str(s["text"]).strip()
            break
    if not answer:
        answer = getattr(run_or_dict, "answer_text", None) or (run_or_dict.get("answer") if isinstance(run_or_dict, dict) else None)

    evidence_items = extract_evidence_items(run_or_dict)

    policy_versions: Dict[str, str] = {}
    entities_consulted: Set[str] = set()
    records_consulted: Set[str] = set()

    for item in evidence_items:
        if item.version and item.source:
            policy_versions[item.source] = item.version
        if item.entity:
            entities_consulted.add(item.entity)
        if item.kind == "record":
            records_consulted.add(item.id)

    return DecisionProvenance(
        case_name=case_name,
        run_id=run_id,
        answer=answer,
        evidence_items=evidence_items,
        policy_versions=policy_versions,
        entities_consulted=entities_consulted,
        records_consulted=records_consulted,
    )


@dataclass
class CaseEvidenceDiff:
    """Differences in evidence basis between baseline and candidate for a test case."""
    case_name: str
    baseline_answer: Optional[str]
    candidate_answer: Optional[str]
    answers_match: bool
    basis_matches: bool
    is_changed_basis_finding: bool  # Changed evidence under identical / passing answer!
    dropped_evidence: List[EvidenceItem]
    added_evidence: List[EvidenceItem]
    swapped_evidence: List[Tuple[EvidenceItem, EvidenceItem]]  # (baseline_item, candidate_item)
    superseded_evidence: List[Tuple[EvidenceItem, EvidenceItem]]  # (baseline_item, candidate_item_older)
    wrong_entity_records: List[EvidenceItem]
    findings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_name": self.case_name,
            "baseline_answer": self.baseline_answer,
            "candidate_answer": self.candidate_answer,
            "answers_match": self.answers_match,
            "basis_matches": self.basis_matches,
            "is_changed_basis_finding": self.is_changed_basis_finding,
            "dropped_evidence": [e.to_dict() for e in self.dropped_evidence],
            "added_evidence": [e.to_dict() for e in self.added_evidence],
            "swapped_evidence": [(b.to_dict(), c.to_dict()) for b, c in self.swapped_evidence],
            "superseded_evidence": [(b.to_dict(), c.to_dict()) for b, c in self.superseded_evidence],
            "wrong_entity_records": [e.to_dict() for e in self.wrong_entity_records],
            "findings": self.findings,
        }


def compare_case_evidence(
    base_run: Any,
    cand_run: Any,
    target_entity: Optional[str] = None,
) -> CaseEvidenceDiff:
    """Compares the evidentiary basis between baseline and candidate runs for a single case."""
    base_prov = extract_decision_provenance(base_run)
    cand_prov = extract_decision_provenance(cand_run)

    case_name = cand_prov.case_name or base_prov.case_name
    base_ans = (base_prov.answer or "").strip()
    cand_ans = (cand_prov.answer or "").strip()
    answers_match = (base_ans == cand_ans) and bool(base_ans)

    # Determine target expected entity
    expected_entity = target_entity or next(iter(base_prov.entities_consulted), None)

    # Index evidence by source / key
    base_by_source: Dict[str, List[EvidenceItem]] = {}
    cand_by_source: Dict[str, List[EvidenceItem]] = {}
    base_by_key: Dict[str, EvidenceItem] = {}
    cand_by_key: Dict[str, EvidenceItem] = {}

    for e in base_prov.evidence_items:
        base_by_key[e.key()] = e
        if e.source:
            base_by_source.setdefault(e.source, []).append(e)

    for e in cand_prov.evidence_items:
        cand_by_key[e.key()] = e
        if e.source:
            cand_by_source.setdefault(e.source, []).append(e)

    dropped: List[EvidenceItem] = []
    added: List[EvidenceItem] = []
    swapped: List[Tuple[EvidenceItem, EvidenceItem]] = []
    superseded: List[Tuple[EvidenceItem, EvidenceItem]] = []
    wrong_entity: List[EvidenceItem] = []
    findings: List[str] = []

    # Check dropped items
    for k, b_item in base_by_key.items():
        if k not in cand_by_key:
            # Check if it was swapped with another version from same source
            cand_same_source = cand_by_source.get(b_item.source or "", [])
            if cand_same_source:
                c_item = cand_same_source[0]
                swapped.append((b_item, c_item))
                # Check if candidate used superseded (older) evidence
                if is_superseded(c_item.version, b_item.version, c_item.date, b_item.date):
                    superseded.append((b_item, c_item))
                    findings.append(
                        f"Superseded evidence: answer rested on older policy '{b_item.source}' "
                        f"({c_item.version or c_item.date} vs baseline {b_item.version or b_item.date})"
                    )
                else:
                    findings.append(
                        f"Swapped evidence: source '{b_item.source}' changed from {b_item.version or b_item.id} "
                        f"to {c_item.version or c_item.id}"
                    )
            else:
                dropped.append(b_item)
                findings.append(f"Evidence dropped: missing '{b_item.source or b_item.id}'")

    # Check added items (not part of a swap)
    swapped_cand_keys = {c.key() for _, c in swapped}
    for k, c_item in cand_by_key.items():
        if k not in base_by_key and k not in swapped_cand_keys:
            # Check if it replaced an item from the same source
            if c_item.source not in base_by_source:
                added.append(c_item)
                findings.append(f"Evidence added: new source '{c_item.source or c_item.id}'")

    # Check for wrong / cross-entity records consulted
    for c_item in cand_prov.evidence_items:
        if c_item.entity and expected_entity and c_item.entity != expected_entity:
            wrong_entity.append(c_item)
            findings.append(
                f"Wrong entity record: consulted record for entity '{c_item.entity}' "
                f"instead of expected '{expected_entity}'"
            )

    basis_matches = not (dropped or added or swapped or superseded or wrong_entity)

    # A changed basis under an unchanged answer is a finding, not a pass!
    is_changed_basis_finding = (answers_match and not basis_matches) or bool(superseded or wrong_entity)

    return CaseEvidenceDiff(
        case_name=case_name,
        baseline_answer=base_ans,
        candidate_answer=cand_ans,
        answers_match=answers_match,
        basis_matches=basis_matches,
        is_changed_basis_finding=is_changed_basis_finding,
        dropped_evidence=dropped,
        added_evidence=added,
        swapped_evidence=swapped,
        superseded_evidence=superseded,
        wrong_entity_records=wrong_entity,
        findings=findings,
    )


@dataclass
class EvidenceDiffReport:
    """The aggregate evidence diff report across all cases."""
    total_cases: int
    matching_basis_count: int
    changed_basis_count: int
    changed_basis_under_same_answer_count: int
    superseded_evidence_count: int
    wrong_entity_count: int
    case_diffs: List[CaseEvidenceDiff]

    def has_findings(self) -> bool:
        return self.changed_basis_under_same_answer_count > 0 or self.superseded_evidence_count > 0 or self.wrong_entity_count > 0

    def summary(self) -> str:
        lines = [
            f"Evidence diff: {self.total_cases} case(s) compared",
            f"  Matching basis: {self.matching_basis_count} / {self.total_cases}",
            f"  Changed evidence basis: {self.changed_basis_count} total "
            f"({self.changed_basis_under_same_answer_count} under unchanged answer)",
            f"  Superseded evidence (older policy/date): {self.superseded_evidence_count}",
            f"  Cross-entity / wrong records: {self.wrong_entity_count}",
        ]
        if self.has_findings():
            lines.append("  Findings:")
            for cd in self.case_diffs:
                if cd.findings:
                    same_tag = " [SAME ANSWER]" if cd.answers_match else ""
                    lines.append(f"    • {cd.case_name}{same_tag}:")
                    for f in cd.findings:
                        lines.append(f"        - {f}")
        return "\n".join(lines)


def compare_evidence_diffs(
    baseline_runs: Union[Sequence[Any], Dict[str, Any]],
    candidate_runs: Union[Sequence[Any], Dict[str, Any]],
    target_entities: Optional[Dict[str, str]] = None,
) -> EvidenceDiffReport:
    """Compares evidence provenance across baseline and candidate test runs."""
    def map_runs(runs: Union[Sequence[Any], Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(runs, dict):
            return runs
        out: Dict[str, Any] = {}
        for idx, r in enumerate(runs):
            name = getattr(r, "task", None) or (r.get("task") or r.get("case") if isinstance(r, dict) else None) or f"case_{idx}"
            out[name] = r
        return out

    base_map = map_runs(baseline_runs)
    cand_map = map_runs(candidate_runs)
    target_entities = target_entities or {}

    shared_cases = sorted(set(base_map.keys()) & set(cand_map.keys()))
    case_diffs: List[CaseEvidenceDiff] = []

    matching_count = 0
    changed_count = 0
    changed_under_same_count = 0
    superseded_count = 0
    wrong_entity_count = 0

    for c_name in shared_cases:
        b_run = base_map[c_name]
        c_run = cand_map[c_name]
        expected_entity = target_entities.get(c_name)

        diff = compare_case_evidence(b_run, c_run, target_entity=expected_entity)
        case_diffs.append(diff)

        if diff.basis_matches:
            matching_count += 1
        else:
            changed_count += 1
            if diff.answers_match:
                changed_under_same_count += 1
            if diff.superseded_evidence:
                superseded_count += len(diff.superseded_evidence)
            if diff.wrong_entity_records:
                wrong_entity_count += len(diff.wrong_entity_records)

    # Sort diffs: changed basis under same answer and wrong entity first
    case_diffs.sort(key=lambda x: (
        not x.is_changed_basis_finding,
        not bool(x.superseded_evidence or x.wrong_entity_records),
        x.case_name
    ))

    return EvidenceDiffReport(
        total_cases=len(shared_cases),
        matching_basis_count=matching_count,
        changed_basis_count=changed_count,
        changed_basis_under_same_answer_count=changed_under_same_count,
        superseded_evidence_count=superseded_count,
        wrong_entity_count=wrong_entity_count,
        case_diffs=case_diffs,
    )
