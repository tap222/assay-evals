"""Voice and phone agents: caller moves, outcome checks (read-back, fixed clock dates,
spoken instruction leak detection, corrected details), platform timing metrics (Retell, Vapi,
talk-overs, latency, hold gaps), and voice diff regressions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Common days and relative date mappings
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ============================================================================
# 1. Caller Moves and Voice Persona
# ============================================================================

@dataclass
class CallerMove:
    """A move made by a telephone caller during a simulated voice call."""
    kind: str  # "speak", "silence", "hold", "interrupt", "correction", "compound", "escalate"
    text: str = ""
    duration_seconds: float = 0.0
    interrupted: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def speak(cls, text: str) -> "CallerMove":
        return cls(kind="speak", text=text)

    @classmethod
    def silence(cls, seconds: float = 3.0) -> "CallerMove":
        return cls(kind="silence", text="...", duration_seconds=seconds)

    @classmethod
    def hold(cls, seconds: float = 5.0) -> "CallerMove":
        return cls(kind="hold", text="[Caller places agent on hold]", duration_seconds=seconds)

    @classmethod
    def interrupt(cls, text: str = "Wait, sorry to interrupt...") -> "CallerMove":
        return cls(kind="interrupt", text=text, interrupted=True)

    @classmethod
    def correct(cls, text: str, field: Optional[str] = None, old_value: Any = None, new_value: Any = None) -> "CallerMove":
        meta = {}
        if field:
            meta["field"] = field
            meta["old_value"] = old_value
            meta["new_value"] = new_value
        return cls(kind="correction", text=text, metadata=meta)

    @classmethod
    def compound(cls, text: str, **facts) -> "CallerMove":
        return cls(kind="compound", text=text, metadata=facts)

    @classmethod
    def escalate(cls, text: str = "Can I speak to a real person please?") -> "CallerMove":
        return cls(kind="escalate", text=text)


@dataclass
class VoicePersona:
    """A simulated caller persona with scripted or dynamic moves."""
    goal: str = ""
    traits: str = "caller"
    facts: Dict[str, Any] = field(default_factory=dict)
    moves: List[Union[CallerMove, str]] = field(default_factory=list)
    name: str = "caller"


# ============================================================================
# 2. Timing and Call Records (Retell, Vapi, Generic telephony)
# ============================================================================

@dataclass
class VoiceTurn:
    role: str  # "user" | "assistant"
    text: str
    start_ms: Optional[float] = None
    end_ms: Optional[float] = None
    duration_ms: Optional[float] = None
    words: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class VoiceCallMetrics:
    total_duration_seconds: float = 0.0
    time_to_first_response_ms: Optional[float] = None
    median_latency_ms: Optional[float] = None
    max_latency_ms: Optional[float] = None
    gap_after_hold_ms: Optional[float] = None
    talk_over_count: int = 0
    talk_over_rate: float = 0.0
    dead_air_seconds: float = 0.0
    spoke_during_hold: bool = False
    turns_count: int = 0


@dataclass
class VoiceCallRecord:
    call_id: str
    platform: str  # "retell", "vapi", "bland", "generic"
    turns: List[VoiceTurn]
    metrics: VoiceCallMetrics
    raw_data: Dict[str, Any] = field(default_factory=dict)


def parse_call_record(call_data: Dict[str, Any], platform: Optional[str] = None) -> VoiceCallRecord:
    """Parses transcript timings and call metadata from voice platforms (Retell, Vapi, or generic JSON)."""
    detected_platform = platform or "generic"
    turns: List[VoiceTurn] = []
    call_id = call_data.get("call_id") or call_data.get("id") or "call-unknown"

    # Retell detection: transcript_object with role and words
    if "transcript_object" in call_data or "transcript" in call_data and isinstance(call_data.get("transcript_object"), list):
        detected_platform = "retell"
        t_objs = call_data.get("transcript_object") or []
        for obj in t_objs:
            role = "assistant" if obj.get("role") in ("agent", "assistant", "bot") else "user"
            words = obj.get("words") or []
            start_ms = (words[0].get("start") * 1000) if words and "start" in words[0] else None
            end_ms = (words[-1].get("end") * 1000) if words and "end" in words[-1] else None
            dur_ms = (end_ms - start_ms) if (start_ms is not None and end_ms is not None) else None
            turns.append(VoiceTurn(
                role=role,
                text=obj.get("content") or "",
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=dur_ms,
                words=words,
            ))

    # Vapi detection: messages list with secondsFromStart, duration
    elif "messages" in call_data and isinstance(call_data.get("messages"), list):
        detected_platform = "vapi"
        for m in call_data["messages"]:
            role_raw = m.get("role")
            if role_raw not in ("user", "assistant", "bot"):
                continue
            role = "assistant" if role_raw in ("assistant", "bot") else "user"
            sec_start = m.get("secondsFromStart") or (m.get("time") / 1000 if "time" in m else None)
            dur_sec = m.get("duration")
            start_ms = (sec_start * 1000) if sec_start is not None else None
            dur_ms = (dur_sec * 1000) if dur_sec is not None else None
            end_ms = (start_ms + dur_ms) if (start_ms is not None and dur_ms is not None) else None
            turns.append(VoiceTurn(
                role=role,
                text=m.get("message") or m.get("content") or "",
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=dur_ms,
            ))

    # Generic turns format
    elif "turns" in call_data and isinstance(call_data.get("turns"), list):
        detected_platform = "generic"
        for t in call_data["turns"]:
            role = "assistant" if t.get("speaker") in ("assistant", "agent", "bot") or t.get("role") in ("assistant", "agent", "bot") else "user"
            turns.append(VoiceTurn(
                role=role,
                text=t.get("text") or t.get("content") or "",
                start_ms=t.get("start_ms"),
                end_ms=t.get("end_ms"),
                duration_ms=t.get("duration_ms"),
            ))

    metrics = compute_voice_call_metrics(turns, call_data)
    return VoiceCallRecord(
        call_id=call_id,
        platform=detected_platform,
        turns=turns,
        metrics=metrics,
        raw_data=call_data,
    )


def compute_voice_call_metrics(turns: List[VoiceTurn], raw_data: Optional[Dict[str, Any]] = None) -> VoiceCallMetrics:
    """Computes time to first response, hold gaps, talk-overs, and dead air."""
    latencies: List[float] = []
    talk_overs = 0
    dead_air_ms = 0.0
    spoke_during_hold = False
    gap_after_hold: Optional[float] = None
    ttfr_ms: Optional[float] = None

    last_user_turn: Optional[VoiceTurn] = None
    last_turn: Optional[VoiceTurn] = None

    # Check if raw_data mentions hold intervals
    holds = (raw_data or {}).get("hold_intervals") or []  # [{"start_ms": ..., "end_ms": ...}]

    for i, t in enumerate(turns):
        if t.role == "assistant":
            # Check if this assistant turn occurred inside a hold interval
            if t.start_ms is not None:
                for h in holds:
                    h_start = h.get("start_ms", 0)
                    h_end = h.get("end_ms", float("inf"))
                    if h_start <= t.start_ms <= h_end:
                        spoke_during_hold = True

            # Latency from previous user turn
            if last_user_turn and last_user_turn.end_ms is not None and t.start_ms is not None:
                lat = t.start_ms - last_user_turn.end_ms
                if lat >= 0:
                    latencies.append(lat)
                    if ttfr_ms is None:
                        ttfr_ms = lat

            # Talk-over detection: assistant started before user finished
            if last_user_turn and last_user_turn.end_ms is not None and t.start_ms is not None:
                if t.start_ms < last_user_turn.end_ms:
                    talk_overs += 1

        elif t.role == "user":
            last_user_turn = t
            # Talk-over check if user started before assistant finished
            if last_turn and last_turn.role == "assistant" and last_turn.end_ms is not None and t.start_ms is not None:
                if t.start_ms < last_turn.end_ms:
                    talk_overs += 1

        # Dead air between consecutive turns
        if last_turn and last_turn.end_ms is not None and t.start_ms is not None:
            gap = t.start_ms - last_turn.end_ms
            if gap > 3000:  # silence > 3s
                dead_air_ms += gap

        last_turn = t

    total_sec = 0.0
    if turns and turns[-1].end_ms and turns[0].start_ms:
        total_sec = max(0.0, (turns[-1].end_ms - turns[0].start_ms) / 1000.0)

    med_lat = None
    max_lat = None
    if latencies:
        sorted_lat = sorted(latencies)
        med_lat = sorted_lat[len(sorted_lat) // 2]
        max_lat = max(latencies)

    # Post hold gap if present
    if holds and turns:
        for h in holds:
            h_end = h.get("end_ms", 0)
            # Find next assistant turn after hold ended
            next_agent = next((t for t in turns if t.role == "assistant" and t.start_ms and t.start_ms >= h_end), None)
            if next_agent and next_agent.start_ms:
                gap_after_hold = next_agent.start_ms - h_end
                break

    turn_count = len(turns)
    talk_over_rate = (talk_overs / turn_count) if turn_count > 0 else 0.0

    return VoiceCallMetrics(
        total_duration_seconds=round(total_sec, 2),
        time_to_first_response_ms=round(ttfr_ms, 1) if ttfr_ms is not None else None,
        median_latency_ms=round(med_lat, 1) if med_lat is not None else None,
        max_latency_ms=round(max_lat, 1) if max_lat is not None else None,
        gap_after_hold_ms=round(gap_after_hold, 1) if gap_after_hold is not None else None,
        talk_over_count=talk_overs,
        talk_over_rate=round(talk_over_rate, 3),
        dead_air_seconds=round(dead_air_ms / 1000.0, 2),
        spoke_during_hold=spoke_during_hold,
        turns_count=turn_count,
    )


# ============================================================================
# 3. Outcome Checks: Read-back, Fixed Clock Dates, Leaks, Handoff Summaries
# ============================================================================

def resolve_relative_date(relative_phrase: str, fixed_clock: datetime) -> str:
    """Resolves relative date expressions ('next Tuesday', 'tomorrow', 'next week')
    against a fixed clock, returning an ISO date string (YYYY-MM-DD).
    """
    p = relative_phrase.lower().strip()
    base_date = fixed_clock.date() if isinstance(fixed_clock, datetime) else fixed_clock

    if p == "today":
        return base_date.isoformat()
    if p == "tomorrow":
        return (base_date + timedelta(days=1)).isoformat()
    if p in ("day after tomorrow", "in two days"):
        return (base_date + timedelta(days=2)).isoformat()
    if p in ("next week", "in a week"):
        return (base_date + timedelta(days=7)).isoformat()

    # Match "next <weekday>" or "this <weekday>" or just "<weekday>"
    for i, day_name in enumerate(_WEEKDAYS):
        if day_name in p:
            current_weekday = base_date.weekday()  # Monday=0, Sunday=6
            target_weekday = i
            days_ahead = target_weekday - current_weekday
            if "next" in p:
                if days_ahead <= 0:
                    days_ahead += 7
                # "next Tuesday" when today is Friday adds days_ahead
                if days_ahead < 3 and "next" in p:
                    days_ahead += 7
            else:
                if days_ahead <= 0:
                    days_ahead += 7
            return (base_date + timedelta(days=days_ahead)).isoformat()

    return base_date.isoformat()


def check_read_back(
    run_or_transcript: Any,
    required_fields: Sequence[str] = ("date", "time"),
    expected_values: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """Verifies that the agent read back the final confirmation details (time, date, name)
    to the caller before concluding the call.
    """
    transcript = _extract_agent_transcript(run_or_transcript)
    if not transcript:
        return False, "Call transcript is empty; agent gave no spoken replies"

    # Inspect the final 1 or 2 assistant turns
    assistant_turns = [m["content"] for m in transcript if m.get("role") in ("assistant", "agent")]
    if not assistant_turns:
        return False, "Agent made no assistant statements"

    concluding_text = " ".join(assistant_turns[-2:]).lower()

    # Look for read-back indicators
    read_back_indicators = [
        "confirm", "scheduled", "booked", "i have you", "you're all set", "put you down",
        "appointment for", "reservation for", "see you on", "registered for",
    ]
    has_indicator = any(ind in concluding_text for ind in read_back_indicators)

    if not has_indicator:
        return False, "Agent stopped reading the confirmed details back before ending the call"

    # If specific expected values are given, ensure they appear in the concluding text
    if expected_values:
        for k, v in expected_values.items():
            str_v = str(v).lower()
            if str_v not in concluding_text:
                return False, f"Agent did not read back confirmed {k} '{v}' in final confirmation"

    return True, None


def check_booking_details(
    run_or_state: Any,
    expected_details: Dict[str, Any],
    initial_rejected: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """Checks that the final booking state holds the final, corrected details,
    NOT the initial details that were later corrected.
    """
    actual_state: Dict[str, Any] = {}

    # 1. State from Run instance
    if hasattr(run_or_state, "steps"):
        for s in run_or_state.steps:
            if s.get("kind") == "state" and s.get("value"):
                val = s.get("value")
                if isinstance(val, dict):
                    actual_state.update(val)
            elif s.get("kind") == "tool" and s.get("name") in ("book_appointment", "create_booking", "save_reservation", "update_booking"):
                args = s.get("args") or {}
                if isinstance(args, dict):
                    actual_state.update(args)
    elif isinstance(run_or_state, dict):
        actual_state = run_or_state

    # Verify initial rejected values were NOT stored
    if initial_rejected:
        for k, bad_val in initial_rejected.items():
            if str(actual_state.get(k)).lower() == str(bad_val).lower():
                return False, f"Booking holds initial uncorrected {k} '{bad_val}' instead of corrected detail"

    # Verify expected details
    for k, exp_val in expected_details.items():
        act_val = actual_state.get(k)
        if act_val is None:
            return False, f"Booking is missing detail '{k}' (expected '{exp_val}')"
        if str(act_val).lower() != str(exp_val).lower():
            return False, f"Booking detail '{k}' mismatch: expected '{exp_val}', got '{act_val}'"

    return True, None


def check_spoken_leaks(
    run_or_transcript: Any,
    forbidden_phrases: Sequence[str],
) -> List[str]:
    """Inspects assistant utterances for leaks of internal instructions, unannounced rules,
    or internal notes that should never be spoken to the caller.
    """
    transcript = _extract_agent_transcript(run_or_transcript)
    leaks = []

    for turn in transcript:
        if turn.get("role") in ("assistant", "agent"):
            text = turn.get("content", "")
            for phrase in forbidden_phrases:
                pattern = re.compile(rf"\b{re.escape(phrase)}\b", re.I)
                if pattern.search(text):
                    leaks.append(f"Spoken instruction leak: agent said '{phrase}' in turn: \"{text[:80]}...\"")

    return leaks


def check_handoff_summary(
    run: Any,
    required_keys: Optional[Sequence[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Verifies that when a caller was handed off / escalated to a human, the handoff
    payload carried a summary of the call.
    """
    handoff_step = None
    for s in getattr(run, "steps", []):
        if s.get("kind") == "stage" and str(s.get("name", "")).startswith("handoff:"):
            handoff_step = s
            break
        if s.get("kind") == "tool" and "transfer" in str(s.get("name", "")).lower():
            handoff_step = s
            break

    if not handoff_step:
        return False, "No handoff step recorded in run"

    args = handoff_step.get("args") or {}
    payload = args.get("payload") if "payload" in args else args

    if not payload:
        return False, "Handoff to human carried an empty payload with no summary"

    # Check for summary
    has_summary = any(k in payload for k in ("summary", "issue_summary", "notes", "context", "transcript"))
    if not has_summary:
        return False, "Handoff payload missing call summary"

    if required_keys:
        for k in required_keys:
            if k not in payload:
                return False, f"Handoff payload missing required key '{k}'"

    return True, None


def _extract_agent_transcript(run_or_transcript: Any) -> List[Dict[str, str]]:
    if isinstance(run_or_transcript, list):
        return run_or_transcript
    if hasattr(run_or_transcript, "steps"):
        out = []
        for s in run_or_transcript.steps:
            if s.get("kind") == "user" and s.get("text"):
                out.append({"role": "user", "content": s["text"]})
            elif s.get("kind") == "answer" and s.get("text"):
                out.append({"role": "assistant", "content": s["text"]})
        return out
    return []


# ============================================================================
# 4. Simulation of Voice Calls (`simulate_call`)
# ============================================================================

def simulate_call(
    agent: Callable,
    persona: VoicePersona,
    *,
    run=None,
    fixed_clock: Optional[datetime] = None,
    max_turns: int = 10,
    success: Optional[Callable[[Any], bool]] = None,
    task: str = "voice_call",
) -> Dict[str, Any]:
    """Simulates a voice call testing caller moves: holds, silences, interruptions,
    corrections, multi-intent compound answers, and escalations.
    """
    import assay_sdk as assay
    from assay_sdk.simulate import _call

    own = run is None
    ctx = assay.run(task, input=None) if own else None
    r = ctx.__enter__() if own else run

    transcript: List[Dict[str, str]] = []
    moves_history: List[CallerMove] = []
    clock = fixed_clock or datetime.now(timezone.utc)
    setattr(r, "fixed_clock", clock)

    hold_intervals = []
    current_time_ms = 0.0

    try:
        moves = list(persona.moves)
        for turn_idx, raw_move in enumerate(moves[:max_turns]):
            move = raw_move if isinstance(raw_move, CallerMove) else CallerMove.speak(str(raw_move))
            moves_history.append(move)

            # Record caller move
            if move.kind == "hold":
                hold_start = current_time_ms
                hold_end = current_time_ms + (move.duration_seconds * 1000)
                hold_intervals.append({"start_ms": hold_start, "end_ms": hold_end})
                current_time_ms = hold_end
                r.user(f"[HOLD {move.duration_seconds}s]")
            elif move.kind == "silence":
                current_time_ms += move.duration_seconds * 1000
                r.user(f"[SILENCE {move.duration_seconds}s]")
            else:
                r.user(move.text)
                if turn_idx == 0 and hasattr(r, "request") and r.request is None:
                    r.request = move.text

                history = list(transcript)
                transcript.append({"role": "user", "content": move.text})

                # Agent responds
                reply = _call(agent, move.text, history)
                r.answer(reply)
                transcript.append({"role": "assistant", "content": reply})

                # Estimate turn speech duration ~150 words per min
                words_count = len(reply.split())
                reply_dur_ms = max(500, (words_count / 2.5) * 1000)
                current_time_ms += reply_dur_ms

                # If caller interrupted, flag on run
                if move.interrupted:
                    setattr(r, "interrupted_turn", turn_idx)

    finally:
        if own:
            ctx.__exit__(None, None, None)

    setattr(r, "voice_transcript", transcript)
    setattr(r, "caller_moves", moves_history)
    setattr(r, "hold_intervals", hold_intervals)

    goal_met = True
    reason = "Voice call simulation finished"
    if success:
        try:
            goal_met = bool(success(r))
            reason = "Success check passed" if goal_met else "Success check failed"
        except Exception as exc:
            goal_met = False
            reason = f"Success check raised {exc}"

    return {
        "transcript": transcript,
        "turns": len(transcript),
        "goal_met": goal_met,
        "reason": reason,
        "run": r,
    }


# ============================================================================
# 5. Voice Regressions & Diff Analysis
# ============================================================================

def compare_voice_runs(baseline_run: Any, candidate_run: Any) -> List[Dict[str, Any]]:
    """Compares baseline vs candidate voice runs to identify voice regressions:
    - 'stopped reading the time back'
    - 'now answers during hold'
    - 'talked over caller {n} times'
    - 'failed to resolve relative date against fixed clock'
    - 'booking holds initial uncorrected detail'
    """
    regressions = []

    # 1. Read-back regression
    base_rb, _ = check_read_back(baseline_run)
    cand_rb, cand_rb_why = check_read_back(candidate_run)
    if base_rb and not cand_rb:
        regressions.append({
            "text": "Voice regression: stopped reading the time back",
            "detail": cand_rb_why or "agent omitted confirmed details in final turn",
            "key": "voice:stopped_read_back",
        })

    # 2. Hold behavior regression
    base_holds = getattr(baseline_run, "hold_intervals", [])
    cand_holds = getattr(candidate_run, "hold_intervals", [])
    if cand_holds:
        # Check if candidate spoke during hold
        cand_spoke_during_hold = getattr(candidate_run, "spoke_during_hold", False)
        base_spoke_during_hold = getattr(baseline_run, "spoke_during_hold", False)
        if cand_spoke_during_hold and not base_spoke_during_hold:
            regressions.append({
                "text": "Voice regression: now answers during hold",
                "detail": "agent spoke while caller had placed call on hold",
                "key": "voice:answers_during_hold",
            })

    return regressions
