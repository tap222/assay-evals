"""pytest plugin: each test that takes the `assay_case` fixture is a test case for Assay.

    def test_refund(assay_case):
        assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
        reply = my_agent("Refund O-17", run=assay_case)   # record steps on it: run.call, run.answer, ...
        assert "27.61" in reply

The fixture wraps the test in assay.run(<test name>, test=<test id>), and records the test's own
outcome (its asserts) as a check on the field "pytest", so `assay test` counts them. Tests that
don't take the fixture are left alone. Installed with assay-evals; nothing to configure.

With assay-server installed, the test also fails when the run does: after the test body, the run
gets the checks `assay test` makes (the case's expectations, and the contracts and PII rules in
assay.toml), so pytest's own pass/fail is the answer. `[pytest] checks = false` in assay.toml
turns that off. assay_sdk.testing has assertions for the test body: assert_called, ...

`pytest --assay` also compares the session with each test's last passing run, like `assay test`:
Assay's report is in pytest's summary, and the exit code says whether anything got worse. A test
that failed before too doesn't fail the session; one that doesn't take the fixture fails it as
usual. Exit 6 means inconclusive: nothing got worse, but some results couldn't be judged, or some checks
could be worse and need more attempts to tell.

A session that hangs (a test that never returns, e.g. an async evaluation stuck in a race) is
stopped after `timeout` seconds (assay.toml, --assay-timeout or ASSAY_TIMEOUT): what it recorded
is still compared, the stuck test is reported as never finished, and the process exits. A
session that finished but whose process doesn't exit (a thread or event loop still running)
exits EXIT_GRACE seconds later, with the session's own result.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from typing import Optional

import pytest

import assay_sdk as assay

MAX_CASE = 128  # the event schema's limit on a case id


def case_id(nodeid: str) -> str:
    """The test id, e.g. tests/test_agent.py::test_refund[O-17]; long ids keep a hash of the rest."""
    if len(nodeid) <= MAX_CASE:
        return nodeid
    return nodeid[:MAX_CASE - 9] + "~" + hashlib.sha1(nodeid.encode()).hexdigest()[:8]


def _config(config):
    """assay.toml's settings, read once per session; None without assay-server."""
    if not hasattr(config, "_assay_cfg"):
        try:
            from assay import local
            config._assay_cfg = local.find_config(config.rootpath)
        except ImportError:
            config._assay_cfg = None
    return config._assay_cfg


INCONCLUSIVE = 6  # pytest uses 0-5; 3 would read as its "internal error"
EXIT_GRACE = float(os.environ.get("ASSAY_EXIT_GRACE", 30))  # seconds a finished session's process gets to exit


def pytest_addoption(parser):
    g = parser.getgroup("assay")
    g.addoption("--assay", action="store_true",
                help="Compare with each test's last passing run (needs assay-server); the exit code says "
                     "whether anything got worse")
    g.addoption("--assay-baseline", metavar="RUN", help="Compare with this run instead; 'none' for no baseline")
    g.addoption("--assay-upload", action="store_true", help="Also send the run to ASSAY_URL (with ASSAY_KEY)")
    g.addoption("--assay-timeout", type=float, metavar="SECONDS",
                help="Stop a session still running after this long, and report what it recorded "
                     "(default: timeout in assay.toml, or ASSAY_TIMEOUT; 0: no limit)")
    g.addoption("--assay-judge", action="store_true",
                help="Also have an LLM judge each test's plan quality and consistency (needs anthropic)")
    g.addoption("--assay-rerun", choices=["failed"],
                help="failed: run only the tests that didn't pass last time (regressed, new failures, flaky, "
                     "couldn't be judged, known failures)")


def _rerun_list(config) -> Optional[set]:
    """The tests to rerun, from the last run's .assay/state.json; None when not rerunning."""
    if (config.getoption("assay_rerun", None) or os.environ.get("ASSAY_RERUN")) != "failed":
        return None
    path = config.rootpath / ".assay" / "state.json"
    try:
        return set(json.loads(path.read_text()).get("rerun") or [])
    except (OSError, ValueError):
        raise pytest.UsageError("--assay-rerun failed needs a run to rerun: nothing in .assay/state.json yet")


def pytest_collection_modifyitems(config, items):
    wanted = _rerun_list(config)
    if wanted is None:
        return
    keep = [i for i in items if case_id(i.nodeid) in wanted]
    drop = [i for i in items if case_id(i.nodeid) not in wanted]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    config._assay_rerun_nothing = not keep


def pytest_report_header(config):
    wanted = _rerun_list(config) if config.getoption("assay_rerun", None) or os.environ.get("ASSAY_RERUN") else None
    if wanted is not None:
        return f"assay: rerunning the {len(wanted)} test(s) that didn't pass last time" if wanted else \
            "assay: nothing to rerun, the last run passed"


_pytest_config = None  # the session's config, for hooks that aren't given it


def pytest_configure(config):
    global _pytest_config
    _pytest_config = config
    config._assay_session = None
    if not config.getoption("assay", False):
        return
    if os.environ.get("ASSAY_TEST_RUN") and not os.environ.get("ASSAY_PYTEST_SESSION"):
        return  # under `assay test`, which compares the run itself
    if hasattr(config, "workerinput"):  # a pytest-xdist worker: records into the session's file
        return
    try:
        from assay import local
    except ImportError:
        raise pytest.UsageError("pytest --assay compares runs with assay-server: pip install assay-server")
    problem = local.sdk_problem()
    if problem:
        raise pytest.UsageError(problem)
    home = local.ensure_home(config.rootpath)
    (home / "runs").mkdir(exist_ok=True)
    run_id = local.new_run_id()
    # Workers and anything the tests start inherit these: one recording for the whole session.
    os.environ.update(ASSAY_PATH=str(home / "runs" / f"{run_id}.jsonl"), ASSAY_TEST_RUN=run_id,
                      ASSAY_PYTEST_SESSION="1")
    os.environ.pop("ASSAY_URL", None)  # record locally; --assay-upload sends it afterwards
    try:
        prices = (_config(config) or {}).get("prices")
    except Exception:  # a broken assay.toml is reported where it's read for the checks
        prices = None
    if prices and not os.environ.get("ASSAY_PRICES"):  # [prices]: model calls are recorded with their cost
        import json
        os.environ["ASSAY_PRICES"] = json.dumps(prices)
    if assay._client is not None:  # init() already ran, e.g. in a conftest: record to the session's file
        assay.init()
    config._assay_session = {"run_id": run_id, "other_failures": 0, "report": None, "done": threading.Event(),
                             "lock": threading.Lock(), "code": None}
    timeout = _timeout(config)
    if timeout:
        threading.Thread(target=_watchdog, args=(config, timeout), name="assay-timeout", daemon=True).start()


def _timeout(config) -> Optional[float]:
    t = config.getoption("assay_timeout", None)
    if t is None and os.environ.get("ASSAY_TIMEOUT"):
        t = float(os.environ["ASSAY_TIMEOUT"])
    if t is None:
        t = (_config(config) or {}).get("timeout")
    return t or None


def _exit(code: int) -> None:
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    os._exit(code)


def _watchdog(config, timeout: float) -> None:
    """The session is still running at `timeout`: judge what it recorded, say so, and exit."""
    s = _session(config)
    if s["done"].wait(timeout):
        return
    with s["lock"]:  # the session may be finishing right now; if it is, let it
        if s["code"] is not None:
            return
        s["code"] = "timed out"
    from assay import local
    capman = config.pluginmanager.getplugin("capturemanager")
    try:  # the stuck test's output capture would swallow the report
        if capman:
            capman.suspend_global_capture(in_=True)
    except Exception:
        pass
    try:
        assay.flush()
        why = f"the pytest session timed out after {timeout:g}s"
        if not os.path.exists(os.environ["ASSAY_PATH"]):
            code, text = 2, "Nothing was recorded before the session timed out."
        else:
            code, text = local.finish(config.rootpath, local.find_config(config.rootpath), s["run_id"], 1,
                                      [], config.getoption("assay_baseline"), abandoned_why=why)
        sys.stderr.write(f"\n{'=' * 30} assay {'=' * 30}\n{text}\n\npytest was still running after {timeout:g}s "
                         "and was stopped: a test that never returned is reported as never finished above.\n")
    except Exception as exc:  # the session must still end
        code = 2
        sys.stderr.write(f"\nassay: the pytest session timed out after {timeout:g}s, and its report failed: {exc}\n")
    _exit(1 if s["other_failures"] and code != 2 else INCONCLUSIVE if code == 3 else code or 0)


def pytest_unconfigure(config):
    """A finished session whose process doesn't exit, e.g. an event loop or thread still running:
    give it EXIT_GRACE seconds, then exit with the session's result."""
    s = _session(config)
    if not s:
        return
    s["done"].set()
    # Only when pytest is the program: a process that runs pytest.main() itself goes on afterwards.
    if not EXIT_GRACE or tuple(config.invocation_params.args) != tuple(sys.argv[1:]):
        return
    status = int(getattr(config, "_assay_exitstatus", 0))

    def guard():
        sys.stderr.write(f"\nassay: pytest finished, but its process was still running {EXIT_GRACE:g}s later (a "
                         "thread or event loop that never stopped); exiting with the session's result.\n")
        _exit(status)
    t = threading.Timer(EXIT_GRACE, guard)
    t.daemon = True
    t.start()


def pytest_runtest_logreport(report):
    """A failing test Assay knows nothing about still fails the session. (Called in the main
    process for every test, including those xdist workers ran.)"""
    s = _session(_pytest_config) if _pytest_config is not None else None
    if s and report.failed and not any(k == "assay_case" for k, _ in report.user_properties):
        s["other_failures"] += 1


def _session(config):
    return getattr(config, "_assay_session", None)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if getattr(session.config, "_assay_rerun_nothing", False):
        session.exitstatus = 0  # nothing failed last time: that's a pass, not "no tests collected"
        return
    s = _session(session.config)
    if not s:
        return
    with s["lock"]:
        if s["code"] is not None:  # the watchdog is reporting a timeout
            return
        s["code"] = "finishing"
    s["done"].set()
    try:
        _finish(session, s, exitstatus)
    finally:
        session.config._assay_exitstatus = session.exitstatus


def _finish(session, s, exitstatus):
    from assay import local
    assay.shutdown()  # everything recorded is on disk before it's read
    if not os.path.exists(os.environ["ASSAY_PATH"]):
        s["report"] = "Nothing was recorded: no test took the assay_case fixture."
        return
    cfg = local.find_config(session.config.rootpath)
    if session.config.getoption("assay_judge", False):
        cfg = {**cfg, "judge": {**cfg["judge"], "enabled": True}}
    code, text = local.finish(session.config.rootpath, cfg, s["run_id"], 1, [],
                              session.config.getoption("assay_baseline"))
    if session.config.getoption("assay_upload") and code != 2:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            sent = local.upload(session.config.rootpath, s["run_id"], None, None, None)
        text += "\n\n" + buf.getvalue().strip()
        code = code or sent
    if s["other_failures"]:
        n = s["other_failures"]
        text += (f"\n{n} failing test{'s' * (n != 1)} {'don' if n != 1 else 'doesn'}'t take the assay_case fixture: "
                 f"{'they fail' if n != 1 else 'it fails'} the session as usual.")
    s["report"] = text
    if int(exitstatus) not in (0, 1):  # interrupted, usage error, no tests: pytest's own word stands
        return
    if code == 2:  # nothing Assay could check: pytest's result stands
        return
    code = INCONCLUSIVE if code == 3 else code
    session.exitstatus = 1 if s["other_failures"] else code


def pytest_terminal_summary(terminalreporter, config):
    s = _session(config)
    if s and s["report"]:
        terminalreporter.write_sep("=", "assay")
        terminalreporter.write_line(s["report"])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    out = yield  # a failing test raises here, and fails as it would anyway
    run = getattr(item, "_assay_run", None)
    if run is None:
        return out
    problems = []
    for e in run.expectations:  # expect(run): checked now that the test body is done
        if not e.verified:
            problems += e.failures()
            try:
                e.verify()
            except AssertionError:
                pass
    cfg = _config(item.config)
    if cfg and cfg["pytest"]["checks"]:
        from assay import local
        problems += local.check_run(run.steps, run.expected, run.answer_text, cfg, run.request)
    if problems:
        item._assay_checks_failed = True  # the test's own asserts passed: record them as such
        pytest.fail("The run failed Assay's checks:\n  " + "\n  ".join(problems), pytrace=False)
    return out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item._assay_report = report


@pytest.fixture
def assay_case(request):
    if assay._client is None:
        assay.init()
    node = request.node
    node.user_properties.append(("assay_case", case_id(node.nodeid)))
    with assay.run(node.originalname or node.name, test=case_id(node.nodeid),
                   tags={"pytest": node.nodeid[:200]}) as run:
        node._assay_run = run
        yield run
    report = getattr(node, "_assay_report", None)
    if report is None or report.skipped:
        return
    if getattr(node, "_assay_checks_failed", False):  # Assay's checks failed it; they're recorded as themselves
        run.check("pytest", "pass")
        assay.flush()
        return
    reason = None
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        reason = (crash.message if crash else str(report.longrepr)).strip()[:2000]
    run.check("pytest", "pass" if report.passed else "fail", reason=reason)
    assay.flush()
