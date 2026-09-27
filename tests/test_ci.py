"""CI: the one-glance summary, the PR comment, rerunning what failed, timeouts, the GitHub Action."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assay import github, local
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
REPO = str(Path(__file__).resolve().parents[1])
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "assay_sdk.pytest_plugin"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "GITHUB_STEP_SUMMARY", "MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SDK, REPO]))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.syspath_prepend(SDK)
    return tmp_path


SUITE = '''
import os
MODE = os.environ.get("MODE", "")

def test_steady(assay_case):
    assay_case.answer("ok")

def test_regresses(assay_case):
    assay_case.answer("ok")
    assert MODE != "after", "the answer changed"

def test_improves(assay_case):
    assay_case.answer("ok")
    assert MODE == "after", "not fixed yet"
'''


def run(project, *args, env=None):
    return subprocess.run([*PYTEST, "--assay", "tests", *args], capture_output=True, text=True, cwd=project,
                          env={**os.environ, **(env or {})})


def test_summary_counts_each_case_once_and_the_markdown_says_what_changed(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(SUITE)
    assert run(project).returncode == 1  # test_improves fails, and there's no baseline yet
    assert main(["accept"]) == 0
    step_summary = project / "step.md"
    out = run(project, env={"MODE": "after", "GITHUB_STEP_SUMMARY": str(step_summary)})
    assert out.returncode == 1
    for line in ("✗ 1 regressed", "✓ 2 passed", "↑ 1 improved (failing in their baseline, passing now)",
                 "Output quality  2/3"):
        assert line in out.stdout
    md = (project / ".assay" / "summary.md").read_text()
    assert md.startswith(local.MARKER) and "## AI regression detected" in md
    assert "**3 cases** · 2 passed · 1 regressed · 1 improved" in md
    assert "- `test_regresses` → Your asserts: AssertionError: the answer changed" in md
    assert "| Output quality | 2/3 |" in md
    assert step_summary.read_text().strip() == md.strip()  # GitHub Actions' job summary
    state = json.loads((project / ".assay" / "state.json").read_text())
    assert state["rerun"] == ["tests/test_suite.py::test_regresses"]


def test_rerun_only_what_didnt_pass(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(SUITE)
    run(project)
    first = run(project, "--assay-rerun", "failed")
    assert "1 failed, 2 deselected" in first.stdout  # test_improves, the only one that failed
    fixed = run(project, "--assay-rerun", "failed", env={"MODE": "after"})
    assert fixed.returncode == 0 and "1 passed, 2 deselected" in fixed.stdout
    nothing = run(project, "--assay-rerun", "failed")
    assert nothing.returncode == 0 and "3 deselected" in nothing.stdout  # a pass, not "no tests ran"


def test_a_command_that_hangs_is_stopped_and_reported(project, capsys):
    (project / "slow.py").write_text('''
import time, assay_sdk as assay
assay.init()
with assay.run("t", test="quick") as r:
    r.answer("ok")
run = assay.run("t", test="hangs").__enter__()
run.tool("lookup", {"id": 1}, {"ok": True})
assay.flush()
time.sleep(60)
''')
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} slow.py"\ntimeout = 2\n')
    started = time.time()
    assert main(["test"]) == 1
    assert time.time() - started < 20
    out = capsys.readouterr().out
    assert "Never finished after step 0: the command timed out after 2s." in out
    assert "Your command timed out (1 of 1 attempts)" in out


def test_pr_comment_creates_then_updates_one_comment():
    comments, calls = [], []

    def http(method, url, body, headers):
        calls.append((method, url.split("?")[0]))
        assert headers["Authorization"] == "Bearer tok"
        if method == "GET":
            page = int(url.rsplit("&page=", 1)[1])
            chunk = comments[(page - 1) * 100:page * 100]
            return 200, chunk
        if method == "POST":
            comments.append({"id": len(comments) + 1, "body": body["body"]})
            return 201, comments[-1]
        if method == "PATCH":
            cid = int(url.rsplit("/", 1)[1])
            next(c for c in comments if c["id"] == cid)["body"] = body["body"]
            return 200, {}
    comments += [{"id": i, "body": f"someone else {i}"} for i in range(1, 151)]  # two pages of others
    assert github.comment(f"{local.MARKER}\nfirst", "o/r", 7, "tok", local.MARKER, http) == "created"
    assert github.comment(f"{local.MARKER}\nsecond", "o/r", 7, "tok", local.MARKER, http) == "updated"
    mine = [c for c in comments if local.MARKER in c["body"]]
    assert len(mine) == 1 and mine[0]["body"].endswith("second")
    assert ("POST", "https://api.github.com/repos/o/r/issues/7/comments") in calls

    def refused(method, url, body, headers):
        return (200, []) if method == "GET" else (403, {"message": "Resource not accessible by integration"})
    with pytest.raises(RuntimeError, match="pull-requests: write"):
        github.comment("x", "o/r", 7, "tok", local.MARKER, refused)


def test_pr_number_from_the_github_event(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 184}}))
    assert github.pr_number({"GITHUB_EVENT_PATH": str(event)}) == 184
    assert github.pr_number({"GITHUB_REF": "refs/pull/42/merge"}) == 42
    assert github.pr_number({"GITHUB_REF": "refs/heads/main"}) is None


def test_the_github_action_is_well_formed():
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((Path(REPO) / "action.yml").read_text())
    steps = action["runs"]["steps"]
    assert action["runs"]["using"] == "composite"
    restore = next(s for s in steps if s.get("uses", "").startswith("actions/cache/restore"))
    assert steps[-2]["uses"].startswith("actions/cache/save")
    assert "github.base_ref" in restore["with"]["restore-keys"]  # a PR restores its base branch's baseline
    assert "assay pr-comment" in next(s["run"] for s in steps if "pr-comment" in s.get("run", ""))
    guard = steps[0]  # never with a write token and secrets on a PR's code
    assert guard["if"] == "github.event_name == 'pull_request_target'" and "exit 1" in guard["run"]
    policy = next(s for s in steps if "ASSAY_POLICY=" in s.get("run", ""))
    assert "${{" not in policy["run"]  # values reach the script as variables, never spliced into it
    # the exit-code mapping in the test step matches assay's codes
    script = next(s for s in steps if s.get("id") == "test")["run"]
    assert all(part in script for part in ("0) result=passed", "1) result=regressed", "3|6) result=inconclusive"))


HANGS = '''
import threading

def test_quick(assay_case):
    assay_case.answer("ok")

def test_hangs(assay_case):
    assay_case.tool("evaluate", {"run_async": True}, {"started": 2})
    threading.Event().wait()  # like evaluate(run_async=True) that never returns
'''


def test_a_pytest_session_that_hangs_is_stopped_and_judged(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(HANGS)
    started = time.time()
    out = run(project, "--assay-timeout", "3")
    assert time.time() - started < 30
    assert out.returncode == 1  # a test that never finished isn't a pass
    assert "Never finished after step 0: the pytest session timed out after 3s." in out.stderr
    assert "pytest was still running after 3s and was stopped" in out.stderr
    md = (project / ".assay" / "summary.md").read_text()  # the PR comment still gets written
    assert "1 passed" in md and "test_hangs" in md


LINGERS = '''
import threading

def test_done(assay_case):
    assay_case.answer("ok")
    threading.Thread(target=threading.Event().wait).start()  # never stops: the process can't exit
'''


def test_a_finished_session_whose_process_wont_exit_exits_with_its_result(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(LINGERS)
    started = time.time()
    out = run(project, env={"ASSAY_EXIT_GRACE": "2"})
    assert time.time() - started < 30
    assert out.returncode == 0 and "1 passed" in out.stdout
    assert "pytest finished, but its process was still running 2s later" in out.stderr


def test_pytest_fails_a_test_whose_agent_strayed_from_its_plan(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_plan.py").write_text('''
def test_refund(assay_case):
    assay_case.plan(["get_order", "refund"], text="Check the order, then refund it")
    assay_case.tool("refund", {"id": "O-17"}, {"ok": True})
    assay_case.tool("get_order", {"id": "O-17"}, {"status": "delivered"})
    assay_case.answer("Refunded.")
''')
    out = run(project)
    assert out.returncode == 1
    assert "Plan adherence: Strayed from its plan: called get_order (step 2) after refund, though the plan put it " \
           "first." in out.stdout


def test_personal_data_in_the_answer_that_the_request_didnt_give(project):
    from assay import local
    traj = lambda answer: {"answer": answer, "steps": [{"seq": 0, "kind": "answer", "text": answer}]}
    req = "I'm ana@example.com, where is my order?"
    assert local.pii_findings(traj("We emailed ANA@example.com."), {}, req) == []  # their own, said back
    assert local.pii_findings(traj("Bob's is bob@corp.io, card 4111 1111 1111 1111."), {}, req) == [
        "email (bob…io) in the answer, which the request didn't give",
        "card (411…11) in the answer, which the request didn't give"]
    assert local.pii_findings(traj("bob@corp.io"), {}, None) == []  # the request isn't known: can't tell whose
    assert local.pii_findings(traj("bob@corp.io"), {}, req, answer_allow={"email"}) == []


def test_the_pr_comment_is_text_never_markdown(project):
    from assay import local
    bad = "@octocat <img src=x onerror=alert(1)> ![p](http://t/i.png) [x](http://e) `x` | # h"
    md = local.summary_markdown("r1", {
        "summary": {"cases": 1, "buckets": {"passed": [], "regressed": ["t::test_`evil`"], "new failure": [],
                                            "flaky": [], "couldn't be judged": [], "known failure": []},
                    "improved": [], "categories": {}},
        "failing": {("t::test_`evil`", "pytest"): {"reason": bad, "expected": None, "actual": None}},
        "fields": [], "not_judged": [], "behavior": []}, 1, None)
    line = next(x for x in md.splitlines() if x.startswith("- "))
    assert "@​octocat" in line and "<img" not in line and "&lt;img" in line
    assert "![p](" not in line and "[x](" not in line and "\\[x\\]" in line  # link syntax escaped
    assert "`test_'evil'`" in line  # a backtick can't end the code span


DELETES = '''
def test_cleanup(assay_case):
    assay_case.tool("delete_order", {"id": "O-17"}, {"ok": True})
    assay_case.answer("Done.")
'''
BASE_TOML = '[test]\ncommand = "pytest -q tests"\n\n[[contracts]]\nkind = "never"\nstep = "delete_order"\n'


def test_a_pr_cant_loosen_the_checks_that_judge_it(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(DELETES)
    (project / "base.toml").write_text(BASE_TOML)  # the base branch's, as the action fetches it
    (project / "assay.toml").write_text('[test]\ncommand = "pytest -q tests"\n\n[pii]\ncheck = false\n')  # the PR's
    assert run(project).returncode == 0  # judged by its own weakened config, the PR would pass

    out = run(project, env={"ASSAY_POLICY": str(project / "base.toml")})
    assert out.returncode == 1
    assert "delete_order never runs" in out.stdout  # the contract it removed still holds
    assert "This PR loosens the checks that judge it" in out.stdout
    assert "- removes the contract" in out.stdout and "- turns the PII check off" in out.stdout
    md = (project / ".assay" / "summary.md").read_text()
    assert "## Checks weakened: this PR loosens the checks that judge it" in md
    assert "- loosens: removes the contract" in md

    accepted = run(project, env={"ASSAY_POLICY": str(project / "base.toml"), "ASSAY_POLICY_CHANGE": "accepted"})
    assert accepted.returncode == 0 and "the change is accepted" in accepted.stdout  # a person said so


THREE = '''
def test_refund(assay_case):
    assay_case.answer("ok")

def test_greeting(assay_case):
    assay_case.answer("ok")

def test_cancel(assay_case):
    assay_case.answer("ok")
'''


def test_a_pr_cant_drop_the_test_its_change_breaks(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(THREE)
    (project / "assay.toml").write_text('[test]\ncommand = "pytest -q tests"\n')
    (project / "base.toml").write_text('[test]\ncommand = "pytest -q tests"\n')
    assert run(project).returncode == 0  # the default branch: its suite is these three
    policy = {"ASSAY_POLICY": str(project / "base.toml")}

    (project / "tests" / "test_suite.py").write_text(THREE.split("def test_cancel")[0])  # the PR deletes one
    out = run(project, env=policy)
    assert out.returncode == 1 and "✓ 2 passed" in out.stdout  # what's left passes; the run doesn't
    assert "- stops running 1 test case the base branch runs: test_cancel" in out.stdout
    assert "- loosens: stops running 1 test case the base branch runs: test\\_cancel" in \
        (project / ".assay" / "summary.md").read_text()
    out = run(project, "-k", "not greeting", env=policy)  # or filters it out of the command
    assert out.returncode == 1 and "stops running 2 test cases the base branch runs: test_cancel, test_greeting" \
        in out.stdout
    accepted = run(project, env={**policy, "ASSAY_POLICY_CHANGE": "accepted"})  # a person removed it on purpose
    assert accepted.returncode == 0 and "the change is accepted" in accepted.stdout

    # Outside a PR it's said, not failed: running a subset is how you work on one file.
    out = run(project, "-k", "refund")
    assert out.returncode == 0 and "2 cases that ran last time didn't run now: test_cancel, test_greeting." in out.stdout
    # And a rerun of what failed leaves the rest out on purpose.
    (project / "tests" / "test_suite.py").write_text(THREE)
    assert run(project).returncode == 0
    out = run(project, "--assay-rerun", "failed", env=policy)
    assert "stops running" not in out.stdout


def test_what_counts_as_loosening():
    from assay import local
    base = local.load_config(Path("."), _toml(BASE_TOML + '\n[pii]\nallow = { send_receipt = ["email"] }\n'
                                          '\n[behavior]\ncost_usd = 1.5\n'), policy=False)
    pr = local.load_config(Path("."), _toml('[test]\ntolerance = 0.2\n\n[[contracts]]\nkind = "never"\n'
                                            'step = "delete_order"\n\n[[contracts]]\nkind = "never"\nstep = "drop_db"\n'
                                            '\n[pii]\nallow = { send_receipt = ["email", "phone"] }\n'
                                            '\n[behavior]\ncost_usd = 0\nsteps = 1.2\n'), policy=False)
    changes = {c["text"]: c["weakens"] for c in local.policy_changes(base, pr)}
    assert changes == {"adds the contract “drop_db never runs”": False, "lets send_receipt receive phone": True,
                       "turns the cost regression check off": True,
                       "lets steps grow 1.2x over the baseline, not 1.5x": False,
                       "raises the tolerated pass-rate drop from 0.01 to 0.2": True}
    merged = local.strictest(base, pr)
    assert len(merged["contracts"]) == 2 and merged["pii"]["allow"] == {"send_receipt": {"email"}}
    assert merged["behavior"]["ratios"]["cost_usd"] == 1.5 and merged["behavior"]["ratios"]["steps"] == 1.2
    assert merged["tolerance"] == 0.01


def _toml(text):
    import tempfile
    f = Path(tempfile.mkdtemp()) / "assay.toml"
    f.write_text(text)
    return f
