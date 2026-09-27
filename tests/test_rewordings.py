"""Rewordings (assay/rewordings.py): the same request in other words gets the same behavior."""
from test_ci import project, run  # noqa: F401  a project dir, and `pytest --assay` in it

SUITE = '''
import os
from assay_sdk.testing import rewordings

def support_agent(run, message, order):
    run.tool("get_order", {"order_id": order}, {"status": "shipped"})
    # The PR's prompt change: a short, casual request now gets refunded without a check.
    if os.environ.get("MODE") == "after" and "pls" in message:
        run.tool("refund", {"order_id": order}, {"ok": True})
    run.answer("It hasn't arrived yet, so no refund for now.")

@rewordings(*os.environ.get("WORDINGS", "Can I get a refund for O-18?|refund O-18 pls").split("|"))
def test_no_refund_before_delivery(assay_case, wording):
    support_agent(assay_case, wording, "O-18")

def test_greeting(assay_case):
    assay_case.answer("Hi!")
'''


def write(project):
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests" / "test_suite.py").write_text(SUITE)


def test_a_change_that_breaks_one_wording_fails(project):
    write(project)
    assert run(project).returncode == 0  # the baseline: every wording does the same
    out = run(project, env={"MODE": "after"})
    assert out.returncode == 1, out.stdout
    assert "test_no_refund_before_delivery[wording1]" in out.stdout
    assert "does something else than the original wording ('Can I get a refund for O-18?'): new: refund" in out.stdout
    assert "Rewordings      0/1" in out.stdout  # its own category in the report
    assert "⚠ 1 case regressed (1 check)" in out.stdout  # the rewording; the original itself didn't change
    assert "[wording0]" not in out.stdout


def test_a_new_wording_is_checked_against_the_original_the_first_time(project):
    write(project)
    assert run(project).returncode == 0
    wordings = "Can I get a refund for O-18?|refund O-18 pls|Where is O-18, refund it"
    assert run(project, env={"WORDINGS": wordings}).returncode == 0  # a new wording that behaves: fine
    out = run(project, env={"WORDINGS": wordings + "|money back O-18 pls", "MODE": "after"})
    assert out.returncode == 1
    assert "test_no_refund_before_delivery[wording3]  (new case)" in out.stdout  # no baseline, and already differs


def test_where_the_original_fails_theres_nothing_to_be_consistent_with(project):
    from assay import local, rewordings, store
    from sqlalchemy import select
    write(project)
    (project / "tests" / "test_suite.py").write_text(SUITE.replace(
        "    support_agent(assay_case, wording, \"O-18\")",
        "    support_agent(assay_case, wording, \"O-18\")\n    assert \"pls\" in wording, \"the original fails\""))
    run(project)
    engine = store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}")
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.case_id).where(t.c.field == rewordings.FIELD)).all()
    assert rows == []  # the rewording passes, the original doesn't: not a rewording failure
    assert local.CHECK_NAMES["rewording"] == "Same behavior reworded"


def test_rewordings_needs_two_wordings():
    import pytest
    from assay_sdk.testing import rewordings
    with pytest.raises(ValueError):
        rewordings("only one")


RANDOM = '''
import os
from assay_sdk.testing import rewordings

@rewordings("Where is O-18?", "where's my order O-18")
def test_where(assay_case, wording):
    # Either order, depending on the attempt, and out of step between the two wordings.
    odd = (int(os.environ["ASSAY_TEST_ATTEMPT"]) + (wording != "Where is O-18?")) % 2
    for tool in (("get_order", "track") if odd else ("track", "get_order")):
        assay_case.tool(tool, {"order_id": "O-18"}, {"ok": True})
    assay_case.answer("On its way.")
'''


def test_an_agent_that_varies_isnt_called_sensitive_to_wording(project):
    import sys
    from assay.__main__ import main
    (project / "tests").mkdir()
    (project / "tests" / "test_suite.py").write_text(RANDOM)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} -m pytest -q -p no:cacheprovider '
                                        f'-p assay_sdk.pytest_plugin tests"\nrepeat = 4\n')
    assert main(["test"]) == 0  # attempt by attempt the two never match; path for path they always do
    from assay import rewordings, store
    from sqlalchemy import select
    t = store.eval_results
    with store.make_engine(f"sqlite:///{project / '.assay' / 'assay.db'}").connect() as conn:
        got = [r.status for r in conn.execute(select(t.c.status).where(t.c.field == rewordings.FIELD))]
    assert len(got) >= 4 and set(got) == {"pass"}  # judged, every attempt, and consistent


PLAIN = '''
import os
import assay_sdk as assay
assay.init()
for i, text in enumerate(["Can I get a refund for O-18?", "refund O-18 pls"]):
    with assay.run("support", test=f"refund-{i}", tags={"rewording_of": "refund", "wording": text,
                                                        "wording_index": i}) as run:
        run.tool("get_order", {"order_id": "O-18"}, {"status": "shipped"})
        if os.environ.get("MODE") == "after" and i == 1:
            run.tool("refund", {"order_id": "O-18"}, {"ok": True})
        run.answer("Not yet.")
        run.check("answer", "pass")
'''


def test_without_pytest_the_runs_are_tagged(project, monkeypatch, capsys):
    import sys
    from assay.__main__ import main
    (project / "agent.py").write_text(PLAIN)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n')
    assert main(["test"]) == 0
    monkeypatch.setenv("MODE", "after")
    assert main(["test"]) == 1
    assert "refund-1  Same behavior reworded" in capsys.readouterr().out
