"""Local testing: `assay init`, `assay test` and `assay accept` (assay/local.py), end to end."""
import json
import os
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
    (root / "assay.toml").write_text(f'[test]\ncommand = "{command}"\nrepeat = {repeat}\n{contracts}')


def test_init_writes_a_runnable_setup_once(project, capsys):
    assert main(["init"]) == 0
    assert (project / "assay.toml").exists() and (project / local.EXAMPLE).exists()
    assert (project / ".assay" / ".gitignore").read_text() == "*\n"  # nothing under .assay goes in git
    (project / "assay.toml").write_text("# mine\n")
    assert main(["init"]) == 0 and "nothing changed" in capsys.readouterr().out
    assert (project / "assay.toml").read_text() == "# mine\n"


# In this repo the pytest plugin isn't installed as a package, so name it.
PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider -p assay_sdk.pytest_plugin"


def init_pytest_example(project):
    main(["init"])
    toml = (project / "assay.toml").read_text()
    (project / "assay.toml").write_text(toml.replace('command = "pytest -q tests/ai"', f'command = "{PYTEST} tests/ai"'))


def test_the_example_passes_then_a_bad_change_fails_then_the_fix_passes(project, capsys):
    init_pytest_example(project)
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "2 cases · 1 attempt each · no baseline yet" in out
    assert any(line.startswith("✓ Safety") and line.endswith(" 2/2") for line in out.splitlines())
    assert "✓ tests/ai/test_support.py  2/2" in out
    first = json.loads((project / ".assay" / "state.json").read_text())["last"]

    good = (project / local.EXAMPLE).read_text()
    bad = good.replace('        reply = f"Order {order_id} hasn\'t arrived yet, so it can\'t be refunded."\n',
                       '        run.call("delete_order", lambda order_id: None, order_id=order_id)\n'
                       '        reply = "Done."\n')
    assert bad != good
    (project / local.EXAMPLE).write_text(bad)
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "compared with each case's last passing run (2 of 2 cases have one, from 1 run)" in out
    assert "⚠ 1 case regressed" in out and "test_no_refund_before_delivery" in out
    assert "delete_order never runs" in out and "Failed." in out
    base = json.loads((project / ".assay" / "state.json").read_text())["baseline_cases"]
    assert set(base.values()) == {first}  # a failing run doesn't become anyone's baseline

    (project / local.EXAMPLE).write_text(good)
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
    (project / "extract.py").write_text(EXTRACT)
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
    (project / "agent.py").write_text(AGENT)
    config(project, f"{sys.executable} agent.py", repeat=8)
    assert main(["test"]) == 0
    first = json.loads((project / ".assay" / "state.json").read_text())["baseline_cases"]
    capsys.readouterr()

    monkeypatch.setenv("FAILS", "1")  # 8/8 → 7/8 on one task of five: what an unchanged agent does
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "1 flaky check" in out and "except 1 case whose pass rate dropped within chance" in out
    # Passing, but not the new bar: the next run is still compared with 8/8.
    assert json.loads((project / ".assay" / "state.json").read_text())["baseline_cases"]["task_0"] == first["task_0"]

    monkeypatch.setenv("FAILS", "4")  # 8/8 → 4/8: could be worse, can't be told yet
    assert main(["test", "--junit", "report.xml"]) == 3
    xml = (project / "report.xml").read_text()
    assert 'errors="1"' in xml and 'failures="0"' in xml and "too few attempts to tell" in xml
    out = capsys.readouterr().out
    assert "? 1 needs reruns" in out and "task_0  solved  100% → 50%" in out
    assert "Inconclusive: nothing is proven worse, but 1 check could be" in out and "Failed." not in out
    assert json.loads((project / ".assay" / "state.json").read_text())["baseline_cases"]["task_0"] == first["task_0"]
    assert "could be worse, or chance" in (project / ".assay" / "summary.md").read_text()
    main(["diff"])
    assert "? 1 could be worse, or chance: needs reruns" in capsys.readouterr().out

    monkeypatch.setenv("FAILS", "8")  # 8/8 → 0/8: the capability is gone
    assert main(["test"]) == 1 and "task_0" in capsys.readouterr().out


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
    (project / "extract.py").write_text(EXTRACT.replace("i == 19", "False"))
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
    (project / "agent.py").write_text(PII_AGENT)
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
    (project / "test_agent.py").write_text(PYTEST_SUITE)
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider -p assay_sdk.pytest_plugin test_agent.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "2 cases" in out  # the test without the fixture isn't one
    assert "✓ Tool usage    2/2" in out and "✗ Your asserts  1/2" in out
    assert "test_agent.py::test_refund[O-18]  Your asserts" in out and "assert 12.0 == 27.61" in out


def test_upload_sends_the_run_and_has_the_server_check_it(project, capsys, tmp_path_factory):
    from fastapi.testclient import TestClient
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
    assert "tenant 'default'" in capsys.readouterr().out
    runs = server.get("/v1/agents/runs", params={"source": "events:default"}).json()
    assert len(runs) == 1 and runs[0]["trajectories"] == 2 and runs[0]["evaluated"]
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0  # again: no doubles
    assert server.get("/v1/agents/runs", params={"source": "events:default"}).json()[0]["trajectories"] == 2
    assert local.upload(project, None, None, None, None, http=http) == 2  # nowhere to send it


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
''')
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
''')
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
    (project / "suite.py").write_text(SUBSET)
    config(project, f"{sys.executable} suite.py")
    assert main(["test"]) == 0  # all three pass: each case's baseline
    full = json.loads((project / ".assay" / "state.json").read_text())["last"]
    monkeypatch.setenv("ONLY", "security")
    assert main(["test"]) == 0  # just one case
    capsys.readouterr()
    base = json.loads((project / ".assay" / "state.json").read_text())["baseline_cases"]
    assert base["tools"] == base["extraction"] == full and base["security"] != full
    monkeypatch.delenv("ONLY")
    monkeypatch.setenv("BROKEN", "1")
    assert main(["test"]) == 1  # extraction broke: a regression, not "a new case"
    out = capsys.readouterr().out
    assert "3 of 3 cases have one, from 2 runs" in out and "⚠ 1 case regressed" in out


def test_an_old_whole_run_baseline_becomes_per_case(project, capsys):
    (project / "suite.py").write_text(SUBSET)
    config(project, f"{sys.executable} suite.py")
    main(["test"])
    state_file = project / ".assay" / "state.json"
    state = json.loads(state_file.read_text())
    state_file.write_text(json.dumps({"last": state["last"], "baseline": state["last"]}))  # the old shape
    capsys.readouterr()
    assert main(["test"]) == 0
    assert "3 of 3 cases have one" in capsys.readouterr().out
    assert "baseline" not in json.loads(state_file.read_text())


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
    (project / "test_agent.py").write_text(PYTEST_AGENT)
    (project / "assay.toml").write_text('[[contracts]]\nkind = "never"\nstep = "delete_order"\n')
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "assay_sdk.pytest_plugin",
                  "test_agent.py"]
    out = subprocess.run(pytest_cmd, capture_output=True, text=True, cwd=project).stdout
    assert "2 failed, 1 passed" in out
    assert "The run failed Assay's checks:" in out
    assert "Answer, Tool usage, Safety: Unsafe action: Broke “delete_order never runs”" not in out  # no answer ref
    assert "Safety: Unsafe action: Broke “delete_order never runs”: ran delete_order (step 2" in out
    assert "Expected a call to get_order(order_id='O-9'); get_order was called with get_order(order_id='O-3')" in out
    assert "testing.py" not in out  # the helper's frames are hidden: the failure points at the test

    (project / "assay.toml").write_text('[[contracts]]\nkind = "never"\nstep = "delete_order"\n'
                                        '[pytest]\nchecks = false\n')
    out = subprocess.run(pytest_cmd, capture_output=True, text=True, cwd=project).stdout
    assert "1 failed, 2 passed" in out  # only the test's own assert


def test_report_by_test_file_and_junit(project, capsys):
    import xml.etree.ElementTree as ET
    (project / "tests").mkdir()
    (project / "tests" / "test_agent.py").write_text(PYTEST_AGENT)
    (project / "tests" / "test_other.py").write_text("def test_ok(assay_case):\n    assay_case.answer('fine')\n")
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider -p assay_sdk.pytest_plugin tests",
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
                                        text=True, cwd=project)
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION"):
        monkeypatch.delenv(k, raising=False)
    first = run()
    assert first.returncode == 0 and "= assay =" in first.stdout and "Its cases' results are now their baseline" \
        in first.stdout

    example = project / local.EXAMPLE
    good = example.read_text()
    example.write_text(good.replace('        reply = f"Order {order_id} hasn\'t arrived yet, so it can\'t be refunded."\n',
                                    '        run.call("delete_order", lambda order_id: None, order_id=order_id)\n'
                                    '        reply = "Done."\n'))
    worse = run()
    assert worse.returncode == 1 and "⚠ 1 case regressed" in worse.stdout
    assert "compared with each case's last passing run" in worse.stdout

    assert main(["accept"]) == 0  # known now: the same failure doesn't fail the session
    known = run()
    assert known.returncode == 0 and "3 checks acknowledged (1 case), quiet until worse" in known.stdout \
        and "1 failed" in known.stdout

    (project / "tests" / "ai" / "test_plain.py").write_text("def test_bug():\n    assert 1 == 2\n")
    plain = run()  # a failing test Assay knows nothing about still fails the session
    assert plain.returncode == 1 and "1 failing test doesn't take the assay_case fixture" in plain.stdout
    (project / "tests" / "ai" / "test_plain.py").unlink()

    toml = (project / "assay.toml").read_text()
    (project / "assay.toml").write_text(toml.replace(f'{PYTEST} tests/ai"', f'{PYTEST} --assay tests/ai"'))
    out = subprocess.run([sys.executable, "-m", "assay", "test"], capture_output=True, text=True, cwd=project)
    assert out.stdout.count("Assay test  t-") == 1 and "= assay =" not in out.stdout  # compared once


def test_pytest_assay_without_the_server_says_what_to_install(project):
    import subprocess
    (project / "test_x.py").write_text("def test_x(assay_case):\n    pass\n")
    env = {**os.environ, "PYTHONPATH": SDK}  # the SDK, without assay-server
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "assay_sdk.pytest_plugin", "--assay",
                          "test_x.py"], capture_output=True, text=True, cwd=project, env=env)
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
    (project / "tests" / "test_b.py").write_text(BEHAVIOR)
    for k in ("ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "PROMPT"):
        monkeypatch.delenv(k, raising=False)
    run = lambda: subprocess.run([*PYTEST.split(), "--assay", "tests"], capture_output=True, text=True, cwd=project)
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

    (project / "assay.toml").write_text("[behavior]\nfail = false\n")
    out = run()
    assert out.returncode == 0 and "(not failing: [behavior] fail = false)" in out.stdout
    (project / "assay.toml").write_text("[behavior]\ncost = 2\n")
    assert "unknown cost" in run().stdout + run().stderr


def test_the_defaults_without_assay_toml_have_every_setting(tmp_path):
    (tmp_path / "assay.toml").write_text("")
    assert set(local.DEFAULT_CONFIG) == set(local.load_config(tmp_path))  # else pytest --assay without one breaks
