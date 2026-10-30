"""Tests for voice and phone agents:
1. Caller moves (hold, silence, interrupt, correction, compound, escalate) in simulate_call
2. Outcome checks (final corrected details, read-back verification, fixed clock relative dates,
   spoken instruction leak prevention, human handoff summary)
3. Timing and platform metrics (Retell, Vapi, TTFR latency, talk-overs, hold gaps, dead air)
4. Voice regressions and change report integration
"""
from datetime import datetime, timezone
import pytest

import assay_sdk as assay
from assay_sdk.voice import (
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
from assay_sdk.testing import (
    assert_booking_holds,
    assert_no_spoken_leaks,
    assert_no_talk_overs,
    assert_read_back_details,
    expect,
)
from assay.changes import items as change_items


# ============================================================================
# 1. Caller Moves and Voice Call Simulation
# ============================================================================

def test_caller_moves_simulation():
    """Simulates a voice call where the caller corrects their date and puts the agent on hold."""
    def booking_phone_agent(message, history=None):
        m = message.lower()
        if "haircut" in m:
            return "Sure! What day works for you?"
        if "wednesday" in m:
            return "I have Wednesday at 2pm available. Shall I book that?"
        if "actually" in m or "thursday" in m:
            return "Got it, switching to Thursday at 2pm instead."
        if "[hold" in m:
            return "Take your time, I will hold."
        if "confirm" in m or "yes" in m or "thanks" in m:
            return "You are all set and confirmed for Thursday at 2:00 PM!"
        return "How can I help you today?"

    persona = VoicePersona(
        goal="Book a haircut for Thursday",
        moves=[
            CallerMove.speak("Hi, I would like to book a haircut."),
            CallerMove.speak("Wednesday at 2pm please."),
            CallerMove.correct("Actually, Thursday at 2pm instead, Wednesday does not work.",
                               field="date", old_value="Wednesday", new_value="Thursday"),
            CallerMove.hold(seconds=3.0),
            CallerMove.speak("Yes, Thursday at 2pm is confirmed, thanks!"),
        ],
    )

    with assay.run("case_voice_haircut") as run:
        sim = simulate_call(booking_phone_agent, persona, run=run)
        run.state("booking", "create", {"service": "haircut", "date": "Thursday", "time": "2:00 PM"})

    assert sim["goal_met"] is True
    assert len(sim["transcript"]) == 8  # 4 user speech turns + 4 assistant replies

    # Check booking holds final corrected detail, not initial Wednesday
    expect(run).booking_holds(
        expected={"date": "Thursday", "time": "2:00 PM"},
        initial_rejected={"date": "Wednesday"},
    )
    assert_booking_holds(
        run,
        expected={"date": "Thursday", "time": "2:00 PM"},
        initial_rejected={"date": "Wednesday"},
    )


def test_caller_interruption_and_compound_moves():
    """Tests interruption and compound multi-intent answers."""
    persona = VoicePersona(
        goal="Book dental exam",
        moves=[
            CallerMove.compound("Tomorrow at 3pm, my name is Alice Smith and phone is 555-0199",
                                date="tomorrow", name="Alice Smith", phone="555-0199"),
            CallerMove.interrupt("Wait, sorry to cut you off, make that 4pm!"),
        ],
    )
    assert len(persona.moves) == 2
    assert persona.moves[0].kind == "compound"
    assert persona.moves[0].metadata["name"] == "Alice Smith"
    assert persona.moves[1].kind == "interrupt"
    assert persona.moves[1].interrupted is True


# ============================================================================
# 2. Outcome Checks (Read-back, Fixed Clock, Leaks, Handoff)
# ============================================================================

def test_read_back_details_verification():
    # 1. Good call: agent reads back the confirmed details
    with assay.run("case_good_read_back") as run_ok:
        run_ok.user("Book appointment for Friday at 10am")
        run_ok.answer("I have you scheduled and confirmed for Friday at 10:00 AM. See you then!")

    expect(run_ok).read_back_details(fields=("date", "time"))
    assert_read_back_details(run_ok, fields=("date", "time"))

    # 2. Bad call: agent just says "ok bye" without reading back details
    with assay.run("case_missing_read_back") as run_bad:
        run_bad.user("Book appointment for Friday at 10am")
        run_bad.answer("Okay done, bye.")

    failures = expect(run_bad).read_back_details().failures()
    assert len(failures) == 1
    assert "stopped reading the confirmed details back" in failures[0]

    with pytest.raises(AssertionError, match="stopped reading the confirmed details back"):
        assert_read_back_details(run_bad)


def test_relative_date_resolution_against_fixed_clock():
    # Fixed clock: Friday Oct 30, 2026
    clock = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)

    # "tomorrow" -> Saturday Oct 31, 2026
    assert resolve_relative_date("tomorrow", clock) == "2026-10-31"

    # "next Tuesday" -> Nov 3, 2026
    assert resolve_relative_date("next Tuesday", clock) == "2026-11-03"

    with assay.run("case_date_resolution") as run:
        run.user("Can I book for next Tuesday?")
        run.tool("save_booking", {"date": "2026-11-03"})

    expect(run).resolved_relative_date("next Tuesday", "2026-11-03", fixed_clock=clock)

    # Failure when wrong date resolved
    failures = expect(run).resolved_relative_date("next Tuesday", "2026-11-10", fixed_clock=clock).failures()
    assert len(failures) == 1
    assert "Relative date 'next Tuesday' resolved to 2026-11-03, expected 2026-11-10" in failures[0]


def test_spoken_instruction_leak_prevention():
    forbidden = ["3:30 Pacific", "system prompt", "tier 2 pricing", "internal sku"]

    # 1. Clean run: no leaks
    with assay.run("case_clean_call") as run_clean:
        run_clean.user("When is your next slot?")
        run_clean.answer("Our next slot is at 3:30 PM local time.")

    expect(run_clean).no_spoken_leaks(forbidden)
    assert_no_spoken_leaks(run_clean, forbidden)

    # 2. Leaky run: agent read internal timezone or instruction aloud
    with assay.run("case_leaky_call") as run_leaky:
        run_leaky.user("When is your next slot?")
        run_leaky.answer("Our internal system says 3:30 Pacific according to the system prompt.")

    failures = expect(run_leaky).no_spoken_leaks(forbidden).failures()
    assert len(failures) == 1
    assert "Spoken instruction leak" in failures[0]
    assert "3:30 Pacific" in failures[0]

    with pytest.raises(AssertionError, match="Spoken instruction leak"):
        assert_no_spoken_leaks(run_leaky, forbidden)


def test_handoff_carried_summary():
    # 1. Good handoff: carries summary of call
    with assay.run("case_good_handoff") as run_good:
        run_good.user("Can I speak to a real person? I want to cancel my flight.")
        run_good.handoff(
            to_agent="human_agent",
            kind="transfer",
            payload={"summary": "Caller wants to cancel flight UA-101", "booking_ref": "B-99"},
        )
        run_good.answer("Transferring you to an agent now.")

    expect(run_good).handoff_carried_summary(required_keys=["summary", "booking_ref"])

    # 2. Bad handoff: empty payload, no summary
    with assay.run("case_empty_handoff") as run_bad:
        run_bad.user("Transfer me")
        run_bad.handoff(to_agent="human_agent", kind="transfer", payload={})

    failures = expect(run_bad).handoff_carried_summary().failures()
    assert len(failures) == 1
    assert "empty payload" in failures[0] or "missing call summary" in failures[0]


# ============================================================================
# 3. Timing and Call Record Metrics (Retell, Vapi, Telephony)
# ============================================================================

def test_parse_retell_call_record():
    retell_data = {
        "call_id": "retell_call_101",
        "transcript_object": [
            {
                "role": "user",
                "content": "Hi there, I need an appointment.",
                "words": [{"word": "Hi", "start": 0.5, "end": 0.8}, {"word": "appointment", "start": 1.5, "end": 2.0}],
            },
            {
                "role": "agent",
                "content": "Hello! I can help you with that.",
                "words": [{"word": "Hello", "start": 2.8, "end": 3.2}, {"word": "that", "start": 4.0, "end": 4.5}],
            },
        ],
    }

    rec = parse_call_record(retell_data)
    assert rec.platform == "retell"
    assert rec.call_id == "retell_call_101"
    assert len(rec.turns) == 2

    # TTFR = agent start (2800ms) - user end (2000ms) = 800ms
    assert rec.metrics.time_to_first_response_ms == 800.0
    assert rec.metrics.talk_over_count == 0


def test_parse_vapi_call_record():
    vapi_data = {
        "id": "vapi_call_202",
        "messages": [
            {"role": "user", "message": "I'd like to check store hours", "secondsFromStart": 1.0, "duration": 1.5},
            {"role": "assistant", "message": "We are open from 9 to 5", "secondsFromStart": 2.9, "duration": 2.0},
        ],
    }

    rec = parse_call_record(vapi_data)
    assert rec.platform == "vapi"
    assert len(rec.turns) == 2
    # User ended at 1.0 + 1.5 = 2.5s (2500ms). Assistant started at 2.9s (2900ms) -> latency 400ms
    assert rec.metrics.time_to_first_response_ms == 400.0


def test_voice_talk_over_and_hold_answers_detection():
    # Call data where agent talked over the caller and answered while on hold
    call_data = {
        "call_id": "call_bad_timing",
        "hold_intervals": [{"start_ms": 5000, "end_ms": 10000}],
        "transcript_object": [
            {
                "role": "user",
                "content": "I am speaking right now...",
                "words": [{"word": "I", "start": 1.0, "end": 3.0}],
            },
            {
                "role": "agent",
                "content": "Let me interrupt you!",
                # Agent started at 2.0s while user was speaking until 3.0s -> TALK-OVER!
                "words": [{"word": "Let", "start": 2.0, "end": 4.0}],
            },
            {
                "role": "agent",
                "content": "Speaking during hold...",
                # Started at 6.0s (inside 5.0s-10.0s hold) -> SPOKE DURING HOLD!
                "words": [{"word": "Speaking", "start": 6.0, "end": 8.0}],
            },
        ],
    }

    with assay.run("case_bad_timing") as run:
        run.call_record(call_data)

    expect(run).voice_latency(max_ttfr_ms=2000.0)

    # 1. Talk over expectation fails
    failures_to = expect(run).no_talk_overs(max_allowed=0).failures()
    assert len(failures_to) == 1
    assert "talked over caller" in failures_to[0]

    with pytest.raises(AssertionError, match="talked over caller"):
        assert_no_talk_overs(run)

    # 2. Answers during hold expectation fails
    failures_hold = expect(run).no_answers_during_hold().failures()
    assert len(failures_hold) == 1
    assert "Agent spoke while caller placed call on hold" in failures_hold[0]


# ============================================================================
# 4. Voice Regressions & Change Reports
# ============================================================================

def test_compare_voice_runs_regressions():
    # Baseline run read back time
    with assay.run("base_run") as run_base:
        run_base.user("Confirm Tuesday at 3pm")
        run_base.answer("I have you confirmed for Tuesday at 3:00 PM.")

    # Candidate run stopped reading time back
    with assay.run("cand_run") as run_cand:
        run_cand.user("Confirm Tuesday at 3pm")
        run_cand.answer("Okay got it.")

    regressions = compare_voice_runs(run_base, run_cand)
    assert len(regressions) == 1
    assert "stopped reading the time back" in regressions[0]["text"]


def test_change_report_voice_regression_item():
    run_with_voice_issue = {
        "voice_regressions": [
            {"text": "Voice regression: stopped reading the time back", "detail": "omitted confirmed details", "key": "voice:rb", "cases": ["case_phone"]}
        ]
    }
    report_items = change_items({}, run_with_voice_issue, [])
    kinds = [it["kind"] for it in report_items]
    assert "voice_regression" in kinds
