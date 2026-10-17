"""Local testing: `assay init`, `assay test` and `assay accept` (assay/local.py), end to end."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from assay import local
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASSAY_URL", raising=False)
    repo = str(Path(__file__).resolve().parents[1])  # the server package, for in-process checks
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SDK, repo, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.syspath_prepend(SDK)  # `assay test` checks the SDK's version in its own process
    return tmp_path


def config(root, command, repeat=1, contracts=""):
    (root / "assay.toml").write_text(f'[test]\ncommand = "{command}"\nrepeat = {repeat}\n{contracts}', encoding="utf-8")


def test_init_writes_a_runnable_setup_once(project, capsys):
    assert main(["init"]) == 0
    assert (project / "assay.toml").exists() and (project / local.EXAMPLE).exists()
    assert (project / ".assay" / ".gitignore").read_text(encoding="utf-8") == "*\n"  # nothing under .assay goes in git
    (project / "assay.toml").write_text("# mine\n", encoding="utf-8")
    assert main(["init"]) == 0 and "nothing changed" in capsys.readouterr().out
    assert (project / "assay.toml").read_text(encoding="utf-8") == "# mine\n"


# In this repo the pytest plugin isn't installed as a package, so name it.
PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


def init_pytest_example(project):
    main(["init"])
    toml = (project / "assay.toml").read_text(encoding="utf-8")
    (project / "assay.toml").write_text(toml.replace('command = "pytest -q tests/ai"', f'command = "{PYTEST} tests/ai"'), encoding="utf-8")


def test_the_example_passes_then_a_bad_change_fails_then_the_fix_passes(project, capsys):
    init_pytest_example(project)
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "2 cases · 1 attempt each · no baseline yet" in out
    assert any(line.startswith("✓ Safety") and line.endswith(" 2/2") for line in out.splitlines())
    assert "✓ tests/ai/test_support.py  2/2" in out
    first = json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["last"]

    good = (project / local.EXAMPLE).read_text(encoding="utf-8")
    bad = good.replace('        reply = f"Order {order_id} hasn\'t arrived yet, so it can\'t be refunded."\n',
                       '        run.call("delete_order", lambda order_id: None, order_id=order_id)\n'
                       '        reply = "Done."\n')
    assert bad != good
    (project / local.EXAMPLE).write_text(bad, encoding="utf-8")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "compared with each case's last passing run (2 of 2 cases have one, from 1 run)" in out
    assert "⚠ 1 case regressed" in out and "test_no_refund_before_delivery" in out
    assert "delete_order never runs" in out and "Failed." in out
    base = json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["baseline_cases"]
    assert set(base.values()) == {first}  # a failing run doesn't become anyone's baseline

    (project / local.EXAMPLE).write_text(good, encoding="utf-8")
    assert main(["test"]) == 0 and "Its cases' results are now their baseline." in capsys.readouterr().out


EXTRACT = '''
import os
import assay_sdk as assay
assay.init()
after, attempt = os.environ.get("MODE") == "after", int(os.environ["ASSAY_TEST_ATTEMPT"])
for i in range(20):
    date_ok = not (after and i < 4)
    vendor_ok = attempt % 2 == 0 if i == 19 else True   # flaky the same way in every run
    assay.check(None, f"inv_{i:02d}", "pass", field="total", expected="1", actual="1")
    assay.check(None, f"inv_{i:02d}", "pass" if date_ok else "fail", field="invoice_date",
                expected="2026-09-01", actual="2026-09-01" if date_ok else "2026-01-09")
    assay.check(None, f"inv_{i:02d}", "pass" if vendor_ok else "fail", field="vendor", expected="Acme",
                actual="Acme" if vendor_ok else "Globex")
'''


def test_fields_known_failures_accept_and_flakiness(project, capsys, monkeypatch):
    (project / "extract.py").write_text(EXTRACT, encoding="utf-8")
    config(project, f"{sys.executable} extract.py", repeat=4)
    assert main(["test"]) == 1  # inv_19's vendor fails half its attempts, and there's no baseline
    out = capsys.readouterr().out
    assert "no baseline yet" in out and "inv_19" in out and "assay accept" in out
    assert main(["accept"]) == 0

    monkeypatch.setenv("MODE", "after")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ invoice_date  16/20   100% → 80%" in out
    assert "invoice_date  4 cases: inv_00, inv_01, inv_02, …" in out  # one item, not four
    assert "1 flaky check" in out and "not blocking" in out  # inv_19's vendor: flaky before too

    monkeypatch.setenv("MODE", "before")
    assert main(["test"]) == 0  # only the flaky check fails: it doesn't block


AGENT = '''
import os
import assay_sdk as assay
assay.init()
attempt, fails = int(os.environ["ASSAY_TEST_ATTEMPT"]), int(os.environ.get("FAILS", "0"))
for i in range(5):
    ok = not (i == 0 and attempt < fails)  # task_0 fails its first FAILS attempts of 8
    assay.check(None, f"task_{i}", "pass" if ok else "fail", field="solved", expected="yes", actual="yes" if ok else "no")
'''


def test_repeated_attempts_tell_chance_from_a_regression(project, capsys, monkeypatch):
    (project / "agent.py").write_text(AGENT, encoding="utf-8")
    config(project, f"{sys.executable} agent.py", repeat=8)
    assert main(["test"]) == 0
    first = json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["baseline_cases"]
    capsys.readouterr()

    monkeypatch.setenv("FAILS", "1")  # 8/8 → 7/8 on one task of five: what an unchanged agent does
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "1 flaky check" in out and "except 1 case whose pass rate dropped within chance" in out
    # Passing, but not the new bar: the next run is still compared with 8/8.
    assert json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["baseline_cases"]["task_0"] == first["task_0"]

    monkeypatch.setenv("FAILS", "4")  # 8/8 → 4/8: could be worse, can't be told yet
    assert main(["test", "--junit", "report.xml"]) == 3
    xml = (project / "report.xml").read_text(encoding="utf-8")
    assert 'errors="1"' in xml and 'failures="0"' in xml and "too few attempts to tell" in xml
    out = capsys.readouterr().out
    assert "? 1 needs reruns" in out and "task_0  solved  100% → 50%" in out
    assert "Inconclusive: nothing is proven worse, but 1 check could be" in out and "Failed." not in out
    assert json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["baseline_cases"]["task_0"] == first["task_0"]
    assert "could be worse, or chance" in (project / ".assay" / "summary.md").read_text(encoding="utf-8")
    main(["diff"])
    assert "? 1 could be worse, or chance: needs reruns" in capsys.readouterr().out

    monkeypatch.setenv("FAILS", "8")  # 8/8 → 0/8: the capability is gone
    assert main(["test"]) == 1 and "task_0" in capsys.readouterr().out



UNSURE = '''
import json, os
import assay_sdk as assay
assay.init()
attempt, mode = int(os.environ["ASSAY_TEST_ATTEMPT"]), os.environ.get("MODE", "same")
only = set(json.loads(os.environ.get("ASSAY_RERUN_CASES", "null")) or [])
for i in range(5):
    if only and f"task_{i}" not in only:
        continue
    # task_0 after the change: fails every other attempt of the first 6, then keeps failing every other one
    # ("worse") or stops failing ("chance")
    ok = not (mode != "same" and i == 0 and attempt % 2 and (attempt < 6 or mode == "worse"))
    assay.check(None, f"task_{i}", "pass" if ok else "fail", field="solved", expected="yes", actual="yes" if ok else "no")
'''


def _attempts(project, case):
    rows = [json.loads(l) for l in sorted((project / ".assay" / "runs").glob("*.jsonl"))[-1].read_text().splitlines()]
    return sum(1 for r in rows if (r.get("test") or {}).get("case") == case)


def test_rerun_unsure_gives_more_attempts_only_to_the_cases_that_need_them(project, capsys, monkeypatch):
    (project / "agent.py").write_text(UNSURE, encoding="utf-8")
    config(project, f"{sys.executable} agent.py", repeat=6)
    assert main(["test", "--repeat", "16"]) == 0  # the baseline: 16/16 each
    monkeypatch.setenv("MODE", "worse")
    assert main(["test"]) == 3  # 16/16 → 3/6 on its own: inconclusive
    capsys.readouterr()

    assert main(["test", "--rerun-unsure"]) == 1  # 3/6, then 6/12: worse, settled without a human rerun
    out = capsys.readouterr()
    assert "1 case could be worse, too few attempts to tell: 6 more attempts of it (12 each)." in out.err
    assert "5 cases · 6 attempts each, up to 12 for the 1 case that needed more" in out.out and "Failed." in out.out
    assert (_attempts(project, "task_0"), _attempts(project, "task_1")) == (12, 6)  # only the unsure case ran again

    monkeypatch.setenv("MODE", "chance")
    assert main(["test", "--rerun-unsure"]) == 0  # 3/6, then 9/12: no worse than chance
    assert "6 more attempts of it" in capsys.readouterr().err


def test_rerun_unsure_never_passes_a_case_because_it_ran_out(project, capsys, monkeypatch):
    (project / "agent.py").write_text(UNSURE, encoding="utf-8")
    config(project, f"{sys.executable} agent.py", repeat=4)
    assert main(["test", "--repeat", "12"]) == 0  # 12 attempts of baseline can't tell 50% from chance here
    monkeypatch.setenv("MODE", "worse")
    assert main(["test", "--rerun-unsure"]) == 3  # still unclear at 19 attempts: inconclusive, not flaky
    assert _attempts(project, "task_0") == 19 and _attempts(project, "task_1") == 4


def test_more_looks_raise_the_bar_for_got_worse():
    from types import SimpleNamespace as N
    from assay import flaky
    def rows(p, n):
        return [N(status="pass" if i < p else "fail", attempt=i, ts=i, actual="x", case_id="c", field="f",
                  evaluator="", judgements=None, judgements_passed=None) for i in range(n)]
    cand, base = {("c", "f", ""): rows(12, 20)}, {("c", "f", ""): rows(20, 20)}
    assert flaky.assess(cand, base)[("c", "f", "")]["state"] == "got_worse"  # 20/20 → 12/20, one look
    with flaky.looks(4):  # q ≈ 0.002 still clears 0.05/4; the bar moved, the clear case stays clear
        assert flaky.assess(cand, base)[("c", "f", "")]["state"] == "got_worse"
    cand = {("c", "f", ""): rows(15, 20)}
    q = flaky.assess(cand, base)[("c", "f", "")]["q_worse"]
    assert 0.05 / 3 < q <= 0.05  # 20/20 → 15/20: worse on one look, not after three
    assert flaky.assess(cand, base)[("c", "f", "")]["state"] == "got_worse"
    with flaky.looks(3):
        assert flaky.assess(cand, base)[("c", "f", "")]["state"] != "got_worse"


SAVED = '''
import assay_sdk as assay
assay.init()
answers = ["Refunds take 5 days.", "We can refund it.", "Ask support.", "No refund after 30 days."]
for i, a in enumerate(answers):
    passed = "refund" in a.lower()
    assay.check(None, f"q{i}", "pass" if passed else "fail", field="helpful", evaluator="helpful@1",
                inputs={"answer": a}, judge_model="m-1", judge_prompt="rubric@1")
'''

JUDGES = '''
calls = {"n": 0}

def same(answer):
    return "refund" in answer.lower()

def stricter(answer):
    return "refund" in answer.lower() and "days" in answer.lower()

def wobbly(answer):
    calls["n"] += 1
    return same(answer) if "Ask" not in answer else calls["n"] % 2 == 0

def broken(answer):
    return "not a verdict"
'''


def test_rejudge_a_saved_run_without_calling_the_ai(project, capsys):
    (project / "agent.py").write_text(SAVED, encoding="utf-8")
    (project / "judges.py").write_text(JUDGES, encoding="utf-8")
    config(project, f"{sys.executable} agent.py")
    main(["test"])
    capsys.readouterr()

    assert main(["rejudge", "--judge", "judges.py:same"]) == 0  # the same judge, frozen: no flips
    out = capsys.readouterr().out
    assert "Re-judged 4 saved answers from t-" in out and "No calls to the AI." in out
    assert "Saved verdicts came from m-1 / rubric@1." in out
    assert "Agrees with the saved verdicts  4/4 (100%), Cohen's kappa 1.00" in out and "Flipped" not in out

    assert main(["rejudge", "--judge", "judges.py:stricter", "--min-agreement", "0.9"]) == 1  # a new rubric, tried first
    out = capsys.readouterr().out
    assert "Agrees with the saved verdicts  3/4 (75%)" in out and "pass → fail 1   fail → pass 0" in out
    assert "q1  helpful  pass → fail" in out and "Below --min-agreement 90%." in out

    assert main(["rejudge", "--judge", "judges.py:wobbly", "--times", "3", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["split"] == 1 and data["times"] == 3 and data["answers"] == 4

    assert main(["rejudge", "--judge", "judges.py:broken"]) == 0  # no verdict isn't a flip
    out = capsys.readouterr().out
    assert "Couldn't be judged  4: not counted as flips" in out and "Agrees" not in out

    assert main(["rejudge", "--judge", "judges.py:same", "--field", "polite"]) == 2
    assert "Nothing to re-judge" in capsys.readouterr().err
    assert main(["rejudge", "--judge", "judges.py:nope"]) == 2
    assert "--judge: judges.py has no function nope." in capsys.readouterr().err


def test_paired_cases_and_mcnemar():
    from assay import flaky
    assert flaky.mcnemar(0, 3) == 1.0 and abs(flaky.mcnemar(6, 0) - 1 / 64) < 1e-9
    assert abs(flaky.mcnemar(8, 2) - 0.0546875) < 1e-9
    now = {("a", "x"): [True, True, False], ("a", "y"): [True], ("b", "x"): [False, False, True], ("c", "x"): [True, False]}
    before = {("a", "x"): [True], ("b", "x"): [True], ("c", "x"): [True], ("d", "x"): [False]}
    pc = flaky.paired_cases(now, before)  # a: most attempts pass; b: most fail; c: a tie isn't most
    assert pc["cases"] == 3 and pc["lost"] == ["b", "c"] and pc["gained"] == [] and pc["p"] == 0.25
    assert flaky.paired_cases(now, {("z", "x"): [True]}) is None


def test_kappa_discounts_agreement_by_chance():
    from assay.rejudge import kappa
    assert kappa([(True, True)] * 9 + [(False, False)]) == 1.0
    assert kappa([(True, True)] * 9 + [(False, True)]) == 0.0  # 90% agreement, all of it what chance gives
    assert kappa([(True, True)] * 10) is None and kappa([]) is None


MIXED = '''
import os
import assay_sdk as assay
assay.init()
judge_fails, code_fails = os.environ.get("JUDGE_FAILS") == "1", os.environ.get("CODE_FAILS") == "1"
for i in range(4):
    assay.check(None, f"task_{i}", "fail" if code_fails and i == 0 else "pass", field="refund_amount",
                expected="40", actual="40")
    assay.check(None, f"task_{i}", "fail" if judge_fails and i < 2 else "pass", field="helpful",
                judge_model="m-1", judge_prompt="rubric@1")
'''


def test_code_checks_and_judged_checks_are_reported_apart(project, capsys, monkeypatch):
    (project / "agent.py").write_text(MIXED, encoding="utf-8")
    config(project, f"{sys.executable} agent.py")
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "Code checks    4/4 passing" in out and "Judged checks  4/4 passing" in out

    monkeypatch.setenv("JUDGE_FAILS", "1")  # only the judge's verdicts moved
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "Code checks    4/4 passing\nJudged checks  2/4 passing · 2 failing" in out
    assert "Code checks held: what moved is judged. Check the judge before the AI (`assay rejudge`)." in out
    md = (project / ".assay" / "summary.md").read_text(encoding="utf-8")
    assert "Code checks: 4/4 passing · Judged checks: 2/4 passing · 2 failing. Code checks held" in md

    monkeypatch.setenv("CODE_FAILS", "1")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "Code checks moved, and no judge decides those: the AI's behavior changed." in out
    # task_0 and task_1 went pass → fail, none the other way: two can't show it's beyond chance
    assert "4 cases in both runs: 2 pass → fail, 0 fail → pass (exact McNemar p = 0.25, within chance" in out


def test_one_kind_of_check_isnt_split(project, capsys):
    (project / "agent.py").write_text(UNSURE, encoding="utf-8")
    config(project, f"{sys.executable} agent.py")
    main(["test"])
    assert "Code checks" not in capsys.readouterr().out

JUDGED = '''
import os
import assay_sdk as assay
from assay_sdk import evaluate
assay.init()
attempt, fails = int(os.environ["ASSAY_TEST_ATTEMPT"]), int(os.environ.get("FAILS", "0"))
for i in range(5):
    # task_0's answer is borderline on its first FAILS attempts: the judge, asked three times, splits on it.
    votes = iter([False, True, False] if i == 0 and attempt < fails else [True] * 3)
    r = evaluate(lambda: next(votes), rejudge=3, backoff=0)
    assay.check(None, f"task_{i}", r.status.lower(), field="helpful", evaluator="helpful@1",
                judgements=r.votes[1], judgements_passed=r.votes[0])
'''


def test_a_judge_that_disagrees_with_itself_is_inconclusive_not_a_regression(project, capsys, monkeypatch):
    (project / "agent.py").write_text(JUDGED, encoding="utf-8")
    config(project, f"{sys.executable} agent.py", repeat=8)
    assert main(["test"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("FAILS", "1")  # 8/8 → 7/8, and the judge split on the answer it failed: not blocking
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "? 1 judge unstable" in out and "verdict flipped on 1 of 8 answers judged again" in out
    assert "flaky check" not in out and "coin flip" not in out

    monkeypatch.setenv("FAILS", "4")  # 8/8 → 4/8: could be worse, and more attempts won't settle a judge
    assert main(["test", "--junit", "report.xml"]) == 3
    out = capsys.readouterr().out
    assert "(could be worse)" in out and "needs reruns" not in out
    assert "Inconclusive: nothing is proven worse, but 1 check could be, and its judge varies" in out
    assert "the judge varies" in (project / "report.xml").read_text(encoding="utf-8")
    assert "unstable judge" in (project / ".assay" / "summary.md").read_text(encoding="utf-8")
    main(["diff"])
    out = capsys.readouterr().out
    assert "? 1 judge unstable: same answer, another verdict" in out and "JUDGE UNSTABLE" in out


def test_setup_problems_exit_2(project, capsys):
    assert main(["test"]) == 2 and "Run `assay init` first" in capsys.readouterr().err
    config(project, f"{sys.executable} -c pass")
    assert main(["test"]) == 2 and "recorded nothing" in capsys.readouterr().err
    config(project, "x", contracts='[[contracts]]\nkind = "sometimes"\nstep = "a"\n')
    assert main(["test"]) == 2 and "contract 1: Unknown kind 'sometimes'" in capsys.readouterr().err
    assert main(["accept"]) == 2 and "No test run yet" in capsys.readouterr().err


def test_an_old_or_missing_sdk_is_named_with_the_fix(project, monkeypatch, capsys):
    import assay_sdk
    config(project, "true")
    monkeypatch.setattr(assay_sdk, "__version__", "0.1.0")
    assert main(["test"]) == 2 and "pip install -U assay-evals" in capsys.readouterr().err
    monkeypatch.setitem(sys.modules, "assay_sdk", None)  # not installed
    assert main(["test"]) == 2 and "pip install assay-evals" in capsys.readouterr().err


def test_command_after_dashes_overrides_the_config(project, capsys):
    (project / "extract.py").write_text(EXTRACT.replace("i == 19", "False"), encoding="utf-8")
    config(project, "false")
    assert main(["test", "--repeat", "2", "--", sys.executable, "extract.py"]) == 0
    assert "20 cases · 2 attempts each" in capsys.readouterr().out


def test_sdk_fills_in_the_run_and_attempt_under_assay_test(monkeypatch):
    sys.path.insert(0, SDK)
    import assay_sdk
    monkeypatch.setenv("ASSAY_TEST_RUN", "t-1")
    monkeypatch.setenv("ASSAY_TEST_ATTEMPT", "2")
    assert assay_sdk._test("c1") == {"case": "c1", "run": "t-1", "attempt": 2}
    assert assay_sdk._test({"case": "c1", "run": "mine", "attempt": 0}) == {"case": "c1", "run": "mine", "attempt": 0}
    monkeypatch.delenv("ASSAY_TEST_RUN")
    monkeypatch.delenv("ASSAY_TEST_ATTEMPT")
    assert assay_sdk._test("c1") == {"case": "c1", "run": "local"}


PII_AGENT = '''
import assay_sdk as assay
assay.init()
for case in ("c1", "c2"):
    with assay.run("support", test=case) as run:
        run.tool("lookup", {"email": "jo.smith@example.com"}, {"ok": True})
        if case == "c2":
            run.tool("log_event", {"note": "card 4111 1111 1111 1111"}, None)
        run.answer("done")
'''


def test_pii_in_tool_arguments_fails_unless_the_tool_is_allowed_it(project, capsys):
    (project / "agent.py").write_text(PII_AGENT, encoding="utf-8")
    config(project, f"{sys.executable} agent.py", contracts='[pii]\nallow = { lookup = ["email"] }\n')
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ PII         1/2" in out and "c2  PII" in out
    assert "card (411…11) sent to log_event (step 1)" in out and "email" not in out.split("⚠")[1]
    config(project, f"{sys.executable} agent.py", contracts="[pii]\ncheck = false\n")
    assert main(["test"]) == 0 and "PII" not in capsys.readouterr().out
    config(project, "x", contracts='[pii]\nallow = { lookup = ["shoe_size"] }\n')
    assert main(["test"]) == 2 and "[pii] allow.lookup" in capsys.readouterr().err


PYTEST_SUITE = '''
import pytest

@pytest.mark.parametrize("order_id", ["O-17", "O-18"])
def test_refund(assay_case, order_id):
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": order_id}}], max_steps=3)
    price = assay_case.call("get_order", lambda order_id: {"O-17": 27.61, "O-18": 12.0}[order_id],
                            order_id=order_id)
    assay_case.answer(f"Refunded ${price:.2f}.")
    assert price == 27.61

def test_without_the_fixture():
    assert True
'''


def test_pytest_plugin_makes_each_test_a_case_and_counts_its_asserts(project, capsys):
    (project / "test_agent.py").write_text(PYTEST_SUITE, encoding="utf-8")
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider test_agent.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "2 cases" in out  # the test without the fixture isn't one
    assert "✓ Tool usage    2/2" in out and "✗ Your asserts  1/2" in out
    assert "test_agent.py::test_refund[O-18]  Your asserts" in out and "assert 12.0 == 27.61" in out


def test_upload_sends_the_run_and_has_the_server_check_it(project, capsys, tmp_path_factory, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ASSAY_PROJECT", "Shop App")  # an open server files each run under its project
    from assay.api import create_app
    from assay.config import Settings
    init_pytest_example(project)
    main(["test"])
    capsys.readouterr()
    server = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path_factory.mktemp('srv') / 's.db'}")))

    def http(method, url, body, headers):
        r = server.request(method, url.replace("http://assay.test", ""), json=body, headers=headers)
        return r.status_code, r.json()
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0
    assert "tenant 'shop-app'" in capsys.readouterr().out
    runs = server.get("/v1/agents/runs", params={"source": "events:shop-app"}).json()
    assert len(runs) == 1 and runs[0]["trajectories"] == 2 and runs[0]["evaluated"]
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0  # again: no doubles
    assert server.get("/v1/agents/runs", params={"source": "events:shop-app"}).json()[0]["trajectories"] == 2
    assert "events:shop-app" in server.get("/v1/sources").json()["configured"]
    (only,) = server.get("/v1/projects").json()
    assert only["project"] == "shop-app" and only["runs"] == 1 and only["counts"] is None  # nothing before it
    main(["test"])
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0
    (only,) = server.get("/v1/projects").json()
    assert only["runs"] == 2 and only["baseline"] and only["counts"]["unchanged"] == 2 \
        and only["counts"]["regressed"] == 0
    assert local.upload(project, None, None, None, None, http=http) == 2  # nowhere to send it



def test_an_uploaded_run_says_which_repository_and_folder_it_came_from(project, capsys, tmp_path_factory,
                                                                      monkeypatch):
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "GITHUB_ACTIONS"):  # who ran it: nobody in particular
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://bot:s3cret@github.com/acme/shop.git"], cwd=project,
                   check=True)
    monkeypatch.setenv("HOME", str(project.parent))
    monkeypatch.setenv("USERPROFILE", str(project.parent))  # Windows' home
    init_pytest_example(project)
    main(["test"])
    capsys.readouterr()
    server = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path_factory.mktemp('srv') / 's.db'}")))

    def http(method, url, body, headers):
        r = server.request(method, url.replace("http://assay.test", ""), json=body, headers=headers)
        return r.status_code, r.json()
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0
    where = {"repo": "https://github.com/acme/shop.git", "folder": f"~/{project.name}"}  # no token in it
    (run,) = server.get("/v1/evals/runs", params={"source": "events:shop"}).json()
    assert run["origin"] == where
    (only,) = server.get("/v1/projects").json()
    assert only["latest"]["origin"] == where
    assert "s3cret" not in (project / ".assay" / "runs").joinpath(f"{run['run_id']}.jsonl").read_text(encoding="utf-8") + \
        json.dumps(server.get("/v1/projects").json())

def test_a_run_the_command_left_open_is_reported_not_skipped(project, capsys):
    (project / "agent.py").write_text('''
import os
import assay_sdk as assay
assay.init()
with assay.run("t", test="ok") as run:
    run.answer("done")
run = assay.run("t", test="killed").__enter__()
run.tool("lookup", {"id": 1}, {"ok": True})
assay.flush()
os._exit(1)  # killed mid-run: run.end never comes
''', encoding="utf-8")
    config(project, f"{sys.executable} agent.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ Finished    1/2" in out and "killed  Finished" in out
    assert "Never finished after step 0: the command exited first." in out


def test_results_judged_on_the_wrong_data_are_listed_not_counted(project, capsys):
    (project / "agent.py").write_text('''
import assay_sdk as assay
assay.init()
for case, q, reply in (("c1", "Refund O-17 please", "Refunded $27.61."), ("c2", "Where is O-18?", "It ships tomorrow.")):
    with assay.run("support", input=q, test=case) as run:
        run.answer(reply)
    graded = q if case == "c2" else reply   # c2: the judge got the question as the "generation"
    run.check("helpful", "fail" if case == "c2" else "pass", evaluator="helpful@1",
              inputs={"query": q, "generation": graded})
''', encoding="utf-8")
    config(project, f"{sys.executable} agent.py")
    assert main(["test"]) == 3  # c2's fail says nothing about the AI: inconclusive, not failed
    out = capsys.readouterr().out
    assert "? 1 result couldn't be judged (not counted)" in out and "Evaluator error: 1" in out
    assert "c2  helpful  helpful@1" in out and "judged on data that doesn't match the trace" in out
    assert "✓ helpful     1/1" in out and "Inconclusive: nothing got worse" in out


SUBSET = '''
import os, sys
import assay_sdk as assay
assay.init()
broken = os.environ.get("BROKEN") == "1"
only = os.environ.get("ONLY")
for case in ("security", "tools", "extraction"):
    if only and case != only:
        continue
    with assay.run("t", test=case) as run:
        run.answer("ok")
    run.check("result", "fail" if broken and case == "extraction" else "pass")
'''


def test_a_subset_run_only_moves_its_own_cases_baseline(project, capsys, monkeypatch):
    (project / "suite.py").write_text(SUBSET, encoding="utf-8")
    config(project, f"{sys.executable} suite.py")
    assert main(["test"]) == 0  # all three pass: each case's baseline
    full = json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["last"]
    monkeypatch.setenv("ONLY", "security")
    assert main(["test"]) == 0  # just one case
    capsys.readouterr()
    base = json.loads((project / ".assay" / "state.json").read_text(encoding="utf-8"))["baseline_cases"]
    assert base["tools"] == base["extraction"] == full and base["security"] != full
    monkeypatch.delenv("ONLY")
    monkeypatch.setenv("BROKEN", "1")
    assert main(["test"]) == 1  # extraction broke: a regression, not "a new case"
    out = capsys.readouterr().out
    assert "3 of 3 cases have one, from 2 runs" in out and "⚠ 1 case regressed" in out


def test_an_old_whole_run_baseline_becomes_per_case(project, capsys):
    (project / "suite.py").write_text(SUBSET, encoding="utf-8")
    config(project, f"{sys.executable} suite.py")
    main(["test"])
    state_file = project / ".assay" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state_file.write_text(json.dumps({"last": state["last"], "baseline": state["last"]}), encoding="utf-8")  # the old shape
    capsys.readouterr()
    assert main(["test"]) == 0
    assert "3 of 3 cases have one" in capsys.readouterr().out
    assert "baseline" not in json.loads(state_file.read_text(encoding="utf-8"))


PYTEST_AGENT = '''
from assay_sdk.testing import assert_called, assert_not_called, assert_called_before, assert_max_steps, \
    assert_answer_contains, assert_no_pii

def agent(run, order_id, bad=False):
    run.call("get_order", lambda order_id: {"price": 5}, order_id=order_id)
    if bad:
        run.call("delete_order", lambda order_id: None, order_id=order_id)
    run.answer("Refunded $5.00.")

def test_good(assay_case):
    agent(assay_case, "O-1")
    assert_called(assay_case, "get_order", order_id="O-1")
    assert_not_called(assay_case, "delete_order")
    assert_called_before(assay_case, "get_order", "refund")
    assert_max_steps(assay_case, 2)
    assert_answer_contains(assay_case, "refunded")
    assert_no_pii(assay_case)

def test_breaks_a_contract(assay_case):   # its own asserts pass; Assay's checks don't
    agent(assay_case, "O-2", bad=True)

def test_helper(assay_case):
    agent(assay_case, "O-3")
    assert_called(assay_case, "get_order", order_id="O-9")
'''


def test_plain_pytest_fails_a_test_whose_run_fails_assays_checks(project):
    import subprocess
    (project / "test_agent.py").write_text(PYTEST_AGENT, encoding="utf-8")
    (project / "assay.toml").write_text('[[contracts]]\nkind = "never"\nstep = "delete_order"\n', encoding="utf-8")
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  "test_agent.py"]
    out = subprocess.run(pytest_cmd, capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace").stdout
    assert "2 failed, 1 passed" in out
    assert "The run failed Assay's checks:" in out
    assert "Answer, Tool usage, Safety: Unsafe action: Broke “delete_order never runs”" not in out  # no answer ref
    assert "Safety: Unsafe action: Broke “delete_order never runs”: ran delete_order (step 2" in out
    assert "Expected a call to get_order(order_id='O-9'); get_order was called with get_order(order_id='O-3')" in out
    assert "testing.py" not in out  # the helper's frames are hidden: the failure points at the test

    (project / "assay.toml").write_text('[[contracts]]\nkind = "never"\nstep = "delete_order"\n'
                                        '[pytest]\nchecks = false\n', encoding="utf-8")
    out = subprocess.run(pytest_cmd, capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace").stdout
    assert "1 failed, 2 passed" in out  # only the test's own assert


def test_report_by_test_file_and_junit(project, capsys):
    import xml.etree.ElementTree as ET
    (project / "tests").mkdir()
    (project / "tests" / "test_agent.py").write_text(PYTEST_AGENT, encoding="utf-8")
    (project / "tests" / "test_other.py").write_text("def test_ok(assay_case):\n    assay_case.answer('fine')\n", encoding="utf-8")
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider tests",
           contracts='[[contracts]]\nkind = "never"\nstep = "delete_order"\n')
    assert main(["test", "--junit", "out.xml"]) == 1
    out = capsys.readouterr().out
    assert "✗ tests/test_agent.py  1/3" in out and "✓ tests/test_other.py  1/1" in out
    suite = ET.parse(project / "out.xml").getroot()
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("4", "2", "0")
    bad = {tc.get("name"): tc.find("failure").get("message") for tc in suite if tc.find("failure") is not None}
    assert set(bad) == {"test_breaks_a_contract", "test_helper"}
    assert bad["test_breaks_a_contract"].startswith("Safety: Unsafe action")  # not also "Your asserts"
    assert suite[0].get("classname") == "tests.test_agent"


def test_pytest_assay_compares_the_session_like_assay_test(project, monkeypatch):
    import subprocess
    init_pytest_example(project)
    run = lambda *extra: subprocess.run([*PYTEST.split(), "--assay", "tests/ai", *extra], capture_output=True,
                                        text=True, cwd=project, encoding="utf-8", errors="replace")
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION"):
        monkeypatch.delenv(k, raising=False)
    first = run()
    assert first.returncode == 0 and "= assay =" in first.stdout and "Its cases' results are now their baseline" \
        in first.stdout

    example = project / local.EXAMPLE
    good = example.read_text(encoding="utf-8")
    example.write_text(good.replace('        reply = f"Order {order_id} hasn\'t arrived yet, so it can\'t be refunded."\n',
                                    '        run.call("delete_order", lambda order_id: None, order_id=order_id)\n'
                                    '        reply = "Done."\n'), encoding="utf-8")
    worse = run()
    assert worse.returncode == 1 and "⚠ 1 case regressed" in worse.stdout
    assert "compared with each case's last passing run" in worse.stdout

    assert main(["accept"]) == 0  # known now: the same failure doesn't fail the session
    known = run()
    assert known.returncode == 0 and "3 checks acknowledged (1 case), quiet until worse" in known.stdout \
        and "1 failed" in known.stdout

    (project / "tests" / "ai" / "test_plain.py").write_text("def test_bug():\n    assert 1 == 2\n", encoding="utf-8")
    plain = run()  # a failing test Assay knows nothing about still fails the session
    assert plain.returncode == 1 and "1 failing test doesn't take the assay_case fixture" in plain.stdout
    (project / "tests" / "ai" / "test_plain.py").unlink()

    toml = (project / "assay.toml").read_text(encoding="utf-8")
    (project / "assay.toml").write_text(toml.replace(f'{PYTEST} tests/ai"', f'{PYTEST} --assay tests/ai"'), encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "assay", "test"], capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace")
    assert out.stdout.count("Assay test  t-") == 1 and "= assay =" not in out.stdout  # compared once


def test_pytest_assay_without_the_server_says_what_to_install(project):
    import subprocess
    (project / "test_x.py").write_text("def test_x(assay_case):\n    pass\n", encoding="utf-8")
    hidden = project / "no_server" / "assay"  # first on the path: assay-server, as if not installed
    hidden.mkdir(parents=True)
    (hidden / "__init__.py").write_text("raise ImportError('assay-server is not installed')\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(hidden.parent), SDK])}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "--assay",
                          "test_x.py"], capture_output=True, text=True, cwd=project, env=env, encoding="utf-8", errors="replace")
    assert out.returncode == 4 and "pip install assay-server" in out.stderr + out.stdout


BEHAVIOR = '''
import os
V2 = os.environ.get("PROMPT") == "v2"

def test_refund(assay_case):
    assay_case.llm(model="m", tokens_in=9000 if V2 else 1200, cost_usd=0.012 if V2 else 0.004,
                   tools=[f"t{i}" for i in range(30 if V2 else 8)])
    assay_case.approval("refund", "rejected" if V2 else "approved", by="policy")
    assay_case.answer("ok")
    assay_case.outcome("unresolved" if V2 else "resolved")
'''


def test_behavior_that_got_worse_fails_the_session(project, monkeypatch):
    import subprocess
    (project / "tests").mkdir()
    (project / "tests" / "test_b.py").write_text(BEHAVIOR, encoding="utf-8")
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "PROMPT"):
        monkeypatch.delenv(k, raising=False)
    run = lambda: subprocess.run([*PYTEST.split(), "--assay", "tests"], capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace")
    assert run().returncode == 0
    monkeypatch.setenv("PROMPT", "v2")
    out = run()
    assert out.returncode == 1 and "1 passed" in out.stdout  # its own asserts pass; its behavior doesn't
    assert "⚠ 1 case behaved worse than their baseline" in out.stdout
    for line in ("Cost: $0.0040 → $0.0120 (3.0×)", "Context: 1,200 tokens → 9,000 tokens (7.5×)",
                 "Tools exposed: 8 tools → 30 tools (3.8×)", "Outcome: resolved → unresolved",
                 "Approval for refund: approved → rejected"):
        assert line in out.stdout
    assert "✗ tests/test_b.py  0/1" in out.stdout

    (project / "assay.toml").write_text("[behavior]\nfail = false\n", encoding="utf-8")
    out = run()
    assert out.returncode == 0 and "(not failing: [behavior] fail = false)" in out.stdout
    (project / "assay.toml").write_text("[behavior]\ncost = 2\n", encoding="utf-8")
    assert "unknown cost" in run().stdout + run().stderr


def test_the_defaults_without_assay_toml_have_every_setting(tmp_path):
    (tmp_path / "assay.toml").write_text("", encoding="utf-8")
    assert set(local.DEFAULT_CONFIG) == set(local.load_config(tmp_path))  # else pytest --assay without one breaks


def test_pytest_assay_upload_sends_to_assay_url(project, monkeypatch):
    import subprocess
    init_pytest_example(project)
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSAY_URL", "http://127.0.0.1:9")  # the session records locally, then sends it here
    out = subprocess.run([*PYTEST.split(), "--assay", "--assay-upload", "tests/ai"], capture_output=True,
                         text=True, cwd=project, encoding="utf-8", errors="replace").stdout
    assert "Where to?" not in out and "127.0.0.1:9" in out



def test_plain_pytest_sends_nothing_though_assay_url_is_set(project, monkeypatch):
    """ASSAY_URL is there for --assay (set once for every project): pytest without it records
    locally, as with no server, instead of sending each test to the server as it runs."""
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init_pytest_example(project)
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSAY_URL", f"http://127.0.0.1:{server.server_port}")
    out = subprocess.run([*PYTEST.split(), "tests/ai"], capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace")
    server.shutdown()
    assert out.returncode == 0, out.stdout
    assert hits == []
    events = (project / ".assay" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(e)["type"] == "run.start" for e in events)

def test_init_adds_the_claude_code_skill_when_asked(project, capsys):
    skill = project / local.CLAUDE_SKILL
    assert main(["init"]) == 0 and not skill.exists()  # only when asked
    assert main(["init", "--claude-code"]) == 0
    assert local.CLAUDE_SKILL in capsys.readouterr().out
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: assay\ndescription: ") and "Never run `assay accept`" in text
    skill.write_text("mine", encoding="utf-8")
    assert main(["init", "--claude-code"]) == 0 and skill.read_text(encoding="utf-8") == "mine"  # left alone


def test_a_project_is_named_after_its_repository(project, monkeypatch):
    import re
    import subprocess
    monkeypatch.delenv("ASSAY_PROJECT", raising=False)
    assert local.project_name(project) == re.sub(r"[^a-z0-9._-]+", "-", project.name.lower()).strip("-")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    for remote, name in [("git@github.com:acme/Support-Bot.git", "support-bot"),
                         ("https://github.com/acme/billing/", "billing")]:
        subprocess.run(["git", "remote", "remove", "origin"], cwd=project, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=project, check=True)
        assert local.project_name(project) == name
    monkeypatch.setenv("ASSAY_PROJECT", "mine")
    assert local.project_name(project) == "mine"


def test_assay_upload_env_sends_every_run(project, monkeypatch):
    import subprocess
    init_pytest_example(project)
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION"):
        monkeypatch.delenv(k, raising=False)
    run = lambda: subprocess.run([*PYTEST.split(), "--assay", "tests/ai"], capture_output=True, text=True,
                                 cwd=project, encoding="utf-8", errors="replace").stdout
    monkeypatch.setenv("ASSAY_UPLOAD", "1")
    assert "Where to?" not in run()  # no server set: nothing to send, and nothing said
    monkeypatch.setenv("ASSAY_URL", "http://127.0.0.1:9")
    out = run()
    assert "127.0.0.1:9" in out and "passed" in out  # tried to send it; an unreachable server fails nothing


def test_init_global_installs_the_skill_for_every_project(project, monkeypatch, capsys):
    home = project / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    assert main(["init", "--global"]) == 2  # only with --claude-code
    assert main(["init", "--claude-code", "--global"]) == 0
    assert (home / local.CLAUDE_SKILL).exists() and not (project / "assay.toml").exists()  # nothing in the project
    assert "ASSAY_UPLOAD=1" in capsys.readouterr().out
    assert main(["init", "--claude-code", "--global"]) == 0 and "nothing changed" in capsys.readouterr().out

# --- the JavaScript SDK (sdk/js) under `assay test`: a Node project is checked like pytest -----

JS_SDK = Path(__file__).resolve().parents[1] / "sdk" / "js"

JS_AGENT = """
const {{ assayCase }} = require({sdk});
const skipApproval = process.env.SKIP_APPROVAL === "1";

async function support(run, orderId) {{
  run.expect().mustCall("get_order").mustCallBefore("approval", "refund");
  const order = await run.call("get_order", async () => ({{ price: 27.61, status: "delivered" }}), {{ orderId }});
  run.llm({{ model: "demo-model", tokensIn: 850, tokensOut: 60 }});
  if (!skipApproval) run.tool("approval", {{ action: "refund" }}, {{ decision: "approved" }});
  await run.call("refund", async () => ({{ refunded: order.price }}), {{ orderId, amount: order.price }});
  run.answer(`Refunded $${{order.price}}.`);
  run.outcome("resolved");
}}

(async () => {{
  let failed = 0;
  for (const id of ["O-17", "O-18"]) {{
    try {{ await assayCase(`refund ${{id}}`, (run) => support(run, id)); }} catch (e) {{ failed++; }}
  }}
  process.exit(failed ? 1 : 0);
}})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_node_project_is_tested_and_a_regression_caught(project, capsys, monkeypatch):
    (project / "agent.js").write_text(JS_AGENT.format(sdk=json.dumps(str(JS_SDK))), encoding="utf-8")
    config(project, "node agent.js")
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "✓ 2 passed" in out and "✓ Your asserts" in out

    monkeypatch.setenv("SKIP_APPROVAL", "1")  # the refund no longer waits for approval
    assert main(["test"]) == 1
    capsys.readouterr()
    assert main(["diff"]) == 1
    out = capsys.readouterr().out
    assert "2 regressed" in out and "No longer: approval" in out
    assert "expect.must_call_before(approval, refund)" in out


# --- what a plain JavaScript test writes to the database ----------------------------------------

JS_DB = """
const assay = require({sdk});
const {{ recordPg }} = require({pg});
class Client {{ query() {{ return Promise.resolve({{ rows: [] }}); }} }}  // pg's Client, as pg.js sees it
recordPg({{ Client }});
const db = new Client();
const drop = process.env.DROP_BRANCH;
async function plainTest(name, body) {{  // what assay-evals/jest does around each test
  const run = new assay.Run(name, {{ caseId: `bookings.test.js::${{name}}`, tags: {{ assay_record: "plain" }} }});
  assay.setPlainRun(run);
  await body();
  run.check("test", true);
  run.end("completed");
  assay.setPlainRun(null);
}}
(async () => {{
  await plainTest("books a slot", async () => {{
    await db.query('INSERT INTO "bookings"("amount", "locationId") VALUES ($1, $2)', [500, drop ? null : "b-1"]);
    for (let i = 0; i < (process.env.N_PLUS_ONE ? 12 : 1); i++) await db.query("SELECT * FROM services WHERE id = $1", [i]);
  }});
  await plainTest("seeds three slots", async () => {{  // three identical writes: not an agent's loop
    for (let i = 0; i < 3; i++) await db.query("INSERT INTO slots (starts_at) VALUES ($1)", [i]);
  }});
}})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_plain_js_tests_database_writes_are_compared(project, capsys, monkeypatch):
    (project / "db.js").write_text(JS_DB.format(sdk=json.dumps(str(JS_SDK)), pg=json.dumps(str(JS_SDK / "pg.js"))),
                                   encoding="utf-8")
    config(project, "node db.js")
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "Looped" not in out  # plain tests aren't held to agent checks

    monkeypatch.setenv("DROP_BRANCH", "1")
    monkeypatch.setenv("N_PLUS_ONE", "1")
    assert main(["test"]) == 0  # still passing: the diff says what changed
    out = capsys.readouterr().out
    assert "1 case wrote to the database differently than its baseline" in out
    main(["diff"])
    out = capsys.readouterr().out
    assert "DB INSERT bookings: now leaves locationId empty" in out
    assert "reads of services: 12 instead of 1" in out
    assert "seeds three slots" not in out
    main(["diff", "--format", "markdown"])
    assert "now leaves locationId empty" in capsys.readouterr().out


# --- a tool's definition changed: what the model reads to choose it ---------------------------

MCP_AGENT = '''
import os
import assay_sdk as assay
assay.init()
# An MCP server's tools/list, offered to the model as it is.
TOOLS = [{"name": "get_order", "description": "Look up an order by its id.",
          "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
         {"name": "refund", "description": os.environ.get("REFUND_DOC", "Refund an order. Needs approval first."),
          "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"},
                                                          "amount": {"type": "number"}},
                          "required": ["order_id", "amount"]}}]
for case in ("refund_a", "refund_b"):
    with assay.run("refund", test=case) as run:
        run.llm(model="m", tokens_in=500, tokens_out=40, tools=TOOLS)
        run.call("get_order", lambda order_id: {"price": 10}, order_id="O-1")
        if "approval" in TOOLS[1]["description"]:  # the stand-in model follows the description
            run.approval("refund", "approved")
        run.call("refund", lambda order_id, amount: {"ok": True}, order_id="O-1", amount=10)
        run.answer("Refunded.")
with assay.run("lookup", test="lookup") as run:  # a case that isn't offered refund
    run.llm(model="m", tokens_in=300, tokens_out=20, tools=TOOLS[:1])
    run.call("get_order", lambda order_id: {"price": 10}, order_id="O-2")
    run.answer("It's on its way.")
'''


def test_a_reworded_tool_description_shows_next_to_the_regression_it_caused(project, capsys, monkeypatch):
    (project / "agent.py").write_text(MCP_AGENT, encoding="utf-8")
    config(project, f"{sys.executable} agent.py",
           contracts='[[contracts]]\nkind = "requires_approval"\nstep = "refund"\n')
    assert main(["test"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("REFUND_DOC", "Refund an order right away.")
    assert main(["test"]) == 1
    capsys.readouterr()
    assert main(["diff"]) == 1
    out = capsys.readouterr().out
    assert "2 regressed" in out
    assert "tool    refund: description “Refund an order. Needs approval first.” → “Refund an order right away.”" in out
    assert "2 regressions, all in cases offered refund, whose definition changed; " \
           "none of the 1 case not offered it regressed" in out


def test_tool_definition_changes_say_what_the_model_now_sees():
    from assay_sdk.checks import DESCRIPTION, tool_schemas
    before = {"type": "object", DESCRIPTION: "Refund an order.",
              "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"},
                             "reason": {"type": "string", "enum": ["damaged", "late"]}, "note": {"type": "string"}},
              "required": ["order_id"]}
    after = {"type": "object", DESCRIPTION: "Refund an order.",
             "properties": {"order_id": {"type": "integer"}, "amount": {"type": "number"},
                            "reason": {"type": "string", "enum": ["damaged", "lost"]},
                            "currency": {"type": "string"}},
             "required": ["order_id", "amount", "currency"]}
    assert local.tool_definition_changes(before, after) == [
        "`currency` added (required)", "`note` removed", "`amount` now required", "`order_id` string → integer",
        "`reason` allowed values +lost −late"]
    assert local.tool_definition_changes(before, before) == []
    assert local.tool_definition_changes({**before, DESCRIPTION: "Old."}, {**before, DESCRIPTION: "New."}) == \
        ["description “Old.” → “New.”"]
    # Anthropic, OpenAI and MCP definitions all keep their description with the schema.
    for t in ({"name": "f", "description": "d", "input_schema": {"type": "object"}},
              {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}},
              {"name": "f", "description": "d", "inputSchema": {"type": "object"}}):
        assert tool_schemas([t]) == {"f": {"type": "object", DESCRIPTION: "d"}}
    assert tool_schemas([{"name": "f", "description": "no schema"}]) == {}  # as before: no schema, no entry


# --- any language: the recorder in docs/any-language.md, run as it's written ------------------

@pytest.mark.skipif(shutil.which("ruby") is None, reason="needs Ruby")
def test_the_ruby_recorder_in_the_docs_works_under_assay_test(project, capsys, monkeypatch):
    import re
    doc = (Path(__file__).resolve().parents[1] / "docs" / "any-language.md").read_text(encoding="utf-8")
    (project / "refund_test.rb").write_text(re.search(r"```ruby\n(.*?)```", doc, re.S).group(1), encoding="utf-8")
    config(project, "ruby refund_test.rb")
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "✓ 1 passed" in out and "approval before refund" in out

    monkeypatch.setenv("SKIP_APPROVAL", "1")
    assert main(["test"]) == 1
    capsys.readouterr()
    assert main(["diff"]) == 1
    out = capsys.readouterr().out
    assert "Expected: get_order → approval(refund) → refund" in out and "Actual:   get_order → refund" in out


# --- assay init in a JavaScript or TypeScript project -----------------------------------------

def test_init_picks_the_language_and_runner_a_js_project_uses(tmp_path):
    def project(pkg, *also):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        for f in also:
            (root / f).write_text("", encoding="utf-8")
        return root
    dev = lambda *names: {"devDependencies": {n: "*" for n in names}}
    vitest = local.js_project(project(dev("vitest", "typescript")))
    assert (vitest["lang"], vitest["runner"], vitest["command"], vitest["example"]) == \
        ("ts", "vitest", "npx vitest run test/ai", "test/ai/support.assay.test.ts")
    jest = local.js_project(project({**dev("jest", "ts-jest", "typescript"), "jest": {"testRegex": ".*\\.spec\\.ts$"}}))
    assert (jest["lang"], jest["runner"], jest["example"]) == ("ts", "jest", "test/ai/support.assay.spec.ts")
    plain = local.js_project(project({"devDependencies": {"jest": "*", "assay-evals": "*"}}))
    assert (plain["lang"], plain["runner"], plain["example"], plain["installed"]) == \
        ("js", "jest", "test/ai/support.assay.test.js", True)  # TypeScript, but no ts-jest: JavaScript
    bare = local.js_project(project({}))
    assert (bare["runner"], bare["command"]) == ("node", "node --test test/ai/*.test.js")
    assert local.js_project(project({}, "pyproject.toml")) is None  # Python packaging: a Python project
    assert local.js_project(project({}, "pyproject.toml"), "ts")["lang"] == "ts"  # unless asked
    assert local.js_project(tmp_path / "nothing-here") is None
    # The example is typed for TypeScript, and node:test and Vitest name the case by its context.
    assert "run: Run, orderId: string" in local.js_example("jest", True)
    assert "(t) =>\n  assayCase(t, " in local.js_example("node", False)
    assert "(t) =>\n  assayCase(t, " in local.js_example("vitest", True)
    assert "assayCase(async (run)" in local.js_example("jest", False)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_init_in_a_node_project_writes_a_setup_assay_test_runs(project, capsys):
    (project / "package.json").write_text('{"name": "shop"}', encoding="utf-8")
    (project / "node_modules").mkdir()  # the SDK from this commit; copied: Windows needs rights to symlink
    shutil.copytree(JS_SDK, project / "node_modules" / "assay-evals", ignore=shutil.ignore_patterns("test", "node_modules"))
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "Created test/ai/support.assay.test.js, assay.toml." in out and "`assay test`" in out
    assert 'command = "node --test test/ai/*.test.js"' in (project / "assay.toml").read_text(encoding="utf-8")
    assert not (project / local.EXAMPLE).exists()  # no Python example in a Node project
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "✓ 2 passed" in out and "expect.must_get_approval_before(refund)" in out
