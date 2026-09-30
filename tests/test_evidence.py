"""Hallucination kinds, a judge that must show its evidence, and a regression with nothing changed on
your side (assay/judge.py evidence, assay/local.py failure_kinds and revision)."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from assay import judge
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
ANSWER = "Your refund is approved and will arrive in 5 days. Unfortunately no refund can be issued for this order."
TRAJ = {"answer": ANSWER, "status": "completed", "steps": [
    {"seq": 0, "kind": "tool", "name": "get_order", "args": {"id": "O-17"}, "result": {"status": "delivered"}},
    {"seq": 1, "kind": "answer", "text": ANSWER}]}


class Fake:
    def __init__(self, *verdicts):
        self.verdicts, self.messages = list(verdicts), self

    def create(self, **kw):
        return N(model="claude-opus-5", stop_reason="end_turn",
                 content=[N(type="text", text=json.dumps(self.verdicts.pop(0)))])


def verdict(reason="The answer contradicts itself (step 1).", score=1, category="contradicts_itself", pairs=None):
    return {"consistency": {"applicable": True, "score": score, "reason": reason, "category": category,
                            **({"contradictions": pairs} if pairs is not None else {})},
            "plan_quality": {"applicable": False, "score": 1, "reason": "no plan"}}


def test_a_self_contradiction_is_quoted_and_the_quotes_are_checked():
    real = [{"first": "Your refund is approved", "second": "no refund can be issued for this order"}]
    out = judge.judge(TRAJ, "refund", client=Fake(verdict(pairs=real)))["consistency"]
    assert out["status"] == "fail" and out["category"] == "contradicts_itself"
    assert "Contradicts itself: “Your refund is approved” vs “no refund can be issued for this order”" in out["reason"]

    made_up = [{"first": "the order was cancelled", "second": "the order shipped"}]
    out = judge.judge(TRAJ, "refund", client=Fake(verdict(pairs=made_up), verdict(pairs=real)))["consistency"]
    assert out["status"] == "fail" and out["tries"] == 2  # asked again: its first evidence wasn't in the answer

    out = judge.judge(TRAJ, "refund", client=Fake(verdict(pairs=made_up), verdict(pairs=made_up)))["consistency"]
    assert (out["status"], out["error_kind"]) == ("error", "invalid")  # never a score on made-up evidence
    assert "the contradictions consistency quotes aren't in the answer" in out["reason"]

    both = real + made_up
    out = judge.judge(TRAJ, "refund", client=Fake(verdict(pairs=both)))["consistency"]
    assert "(1 quoted contradiction not in the answer: left out)" in out["reason"]


def test_a_reason_that_cites_a_step_the_trace_doesnt_have_is_not_a_verdict():
    out = judge.judge(TRAJ, "refund", client=Fake(verdict("Step 7 shows it.", 2, "fabricated"),
                                                  verdict("Step 7 shows it.", 2, "fabricated")))["consistency"]
    assert out["error_kind"] == "invalid" and "cites step 7, which the trace doesn't have" in out["reason"]


def test_hallucination_kinds_are_distinct_and_the_old_names_still_read():
    assert {"fabricated", "contradicts_source", "unsupported_inference", "contradicts_itself"} <= set(judge.CATEGORIES)
    out = judge.judge(TRAJ, "refund", client=Fake(verdict("Contradicts step 0.", 2, "contradiction")))["consistency"]
    assert out["category"] == "contradicts_source"


KINDS = '''
import json, os
import assay_sdk as assay
assay.init()
for case, cat in json.loads(os.environ["KINDS"]).items():
    with assay.run("support", test=case) as r:
        r.answer("x")
        r.check("helpful", "fail" if cat else "pass", score=0.2 if cat else 0.9, category=cat or None)
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY",
              "GITHUB_SHA"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "kinds.py").write_text(KINDS)
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} kinds.py"\n')
    return tmp_path


def test_failures_by_kind_next_to_the_baselines(project, monkeypatch, capsys):
    monkeypatch.setenv("KINDS", json.dumps({"q1": "", "q2": "", "q3": ""}))
    assert main(["test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("KINDS", json.dumps({"q1": "fabricated", "q2": "contradicts_source", "q3": "contradicts_source"}))
    main(["test"])
    out = capsys.readouterr().out
    assert "Failures by kind\n  2 contradicts source (0 before) · 1 fabricated (0 before)" in out


def test_a_regression_with_nothing_changed_on_your_side(project, monkeypatch, capsys):
    git = lambda *a: subprocess.run(["git", *a], cwd=project, check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.email=a@b.c", "-c", "user.name=a", "add", "-A")
    git("-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "x")
    monkeypatch.setenv("KINDS", json.dumps({"q1": "", "q2": ""}))
    assert main(["test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("KINDS", json.dumps({"q1": "fabricated", "q2": ""}))  # the provider's model moved; no code did
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "Nothing on your side changed since this case's baseline: the same commit" in out
    assert "The model underneath changed, or a service a tool calls did." in out
    (project / "kinds.py").write_text(KINDS + "\n# a change\n")  # now something did change
    assert main(["test"]) == 1
    assert "Nothing on your side changed" not in capsys.readouterr().out


PROMPTED = '''
import os
import assay_sdk as assay
assay.init()
with assay.run("support", test="q1") as r:
    r.llm(model="m", prompt=assay.prompt("support", os.environ["PROMPT"]))
    r.answer("x")
    r.check("helpful", "fail" if os.environ["PROMPT"] == "2" else "pass")
'''


def test_a_new_prompt_version_is_a_change_on_your_side(project, monkeypatch, capsys):
    """A prompt picked at run time (an env var, a registry) changes no file, and is still your change."""
    (project / "prompted.py").write_text(PROMPTED)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} prompted.py"\n')
    git = lambda *a: subprocess.run(["git", *a], cwd=project, check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.email=a@b.c", "-c", "user.name=a", "add", "-A")
    git("-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "x")
    monkeypatch.setenv("PROMPT", "1")
    assert main(["test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("PROMPT", "2")
    assert main(["test"]) == 1
    assert "Nothing on your side changed" not in capsys.readouterr().out
