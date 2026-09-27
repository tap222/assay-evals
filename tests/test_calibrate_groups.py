"""A judge that recognizes the topic, not the answer (calibrate.group_check): a golden set whose tags
differ in typical quality gives a topic-only judge a healthy Spearman overall and none within a tag."""
import json
import random
import sys

import pytest

from assay import calibrate
from assay.__main__ import main

from test_calibrate import SDK, project  # noqa: F401  the setup; this file brings its own golden set

TYPICAL = {"refund": 2, "shipping": 3, "billing": 2, "greeting": 5}  # refunds rated low, greetings high

JUDGES = '''
import re
TYPICAL = {"refund": 2, "shipping": 3, "billing": 2, "greeting": 5}
def topic(question, answer):  # knows which kind of question it is, and nothing about the answer
    return {"score": TYPICAL[question.split()[0]]}
def good(question, answer):
    return {"score": int(re.search(r"quality=(\\\\d)", answer).group(1))}
'''


def golden(n=12, seed=0):
    rnd = random.Random(seed)
    out = []
    for tag, mu in TYPICAL.items():
        for i in range(n):
            q = min(5, max(1, mu + rnd.choice([-2, -1, -1, 0, 0, 1, 1, 2])))
            out.append({"id": f"{tag}-{i}", "input": f"{tag} question {i}", "output": f"answer {i} quality={q}",
                        "score": q, "by": "sam", "tags": [tag]})
    return out


def judged(items, judge):
    return [{**x, "label": x["score"], "judged": judge(x), "tags": x["tags"]} for x in items]


def test_a_topic_only_judge_is_told_apart_from_a_good_one():
    items = golden()
    topic = calibrate.group_check(judged(items, lambda x: TYPICAL[x["tags"][0]]), "tags")
    assert topic["groups"] == 4 and topic["spearman"] > 0.5 and topic["baseline"] >= calibrate.TOPIC
    assert topic["within"] == 0.0 and topic["topic"]  # the same score within a tag: nothing ranked
    rnd = random.Random(1)
    good = calibrate.group_check(judged(items, lambda x: x["score"] + rnd.gauss(0, 0.5)), "tags")
    assert good["within"] > 0.7 and not good["topic"] and good["within_interval"][0] > 0.5


def test_topic_needs_groups_that_differ_and_enough_of_them():
    flat = [{**x, "tags": [x["tags"][0]], "score": x["score"]} for x in golden()]
    rnd = random.Random(2)
    for x in flat:  # every tag the same typical quality: knowing the tag says nothing
        x["score"] = rnd.randint(1, 5)
    g = calibrate.group_check(judged(flat, lambda x: TYPICAL[x["tags"][0]]), "tags")
    assert g["baseline"] < calibrate.TOPIC and not g["topic"]
    two = [x for x in golden() if x["tags"][0] in ("refund", "greeting")]
    assert calibrate.group_check(judged(two, lambda x: x["score"]), "tags") is None  # 2 groups can't be resampled
    assert calibrate.group_check(judged(golden(), lambda x: x["score"]), "none") is None


def test_answers_to_the_same_input_are_a_group():
    items = []
    for i in range(5):  # five questions, four answers each, of different quality
        for q in (1, 2, 4, 5):
            items.append({"id": f"q{i}-{q}", "input": f"question {i}", "output": f"quality={q}", "score": q + (i % 2),
                          "tags": []})
    g = calibrate.group_check(judged(items, lambda x: x["score"]), "input")
    assert g["by"] == "input" and g["groups"] == 5 and g["within"] > 0.9


@pytest.fixture
def tagged(project):
    (project / "evals" / "judges.py").write_text(JUDGES)
    (project / "golden.jsonl").write_text("\n".join(json.dumps(x) for x in golden()) + "\n")
    return project


def test_calibration_says_so_and_so_does_every_judged_number(tagged, monkeypatch, capsys):
    project = tagged
    toml = (project / "assay.toml").read_text()
    (project / "assay.toml").write_text(toml.replace("judges.py:grade", "judges.py:topic")
                                        .replace('command = "true"', f'command = "{sys.executable} record.py"')
                                        + 'field = "helpful"\n')
    (project / "record.py").write_text("""
import assay_sdk as assay
assay.init()
with assay.run("support", test="q1") as r:
    r.answer("ok")
    r.check("helpful", "pass", score=4)
""")
    monkeypatch.setenv("PYTHONPATH", SDK)
    assert main(["calibrate", "--repeat", "1"]) == 0  # not a regression: a trust problem
    out = capsys.readouterr().out
    assert "Within tags  Spearman" in out and "knowing only each tag's average label" in out
    assert "it tracks the topic, not the answer" in out
    main(["test"])
    out = capsys.readouterr().out
    assert "helpful  calibrated today: Spearman" in out and "but it tracks the topic, not the answer" in out

    (project / "assay.toml").write_text((project / "assay.toml").read_text().replace("judges.py:topic", "judges.py:good"))
    assert main(["calibrate", "--repeat", "1"]) == 0
    assert "tracks the topic" not in capsys.readouterr().out
    main(["test"])
    assert "tracks the topic" not in capsys.readouterr().out


def test_group_by_is_checked(tagged, capsys):
    (tagged / "assay.toml").write_text((tagged / "assay.toml").read_text() + 'group_by = "topic"\n')
    assert main(["calibrate"]) == 2 and "group_by: one of tags, input, none" in capsys.readouterr().err


def test_split_by_tags_keeps_each_tag_in_one_split(tagged, capsys):
    assert main(["golden", "split", "--by", "tags", "--train", "0.25", "--dev", "0.25"]) == 0
    assert "Whole tags go to one split" in capsys.readouterr().out
    rows = [json.loads(x) for x in (tagged / "golden.jsonl").read_text().splitlines() if x.strip()]
    by_tag = {}
    for x in rows:
        by_tag.setdefault(x["tags"][0], set()).add(x["split"])
    assert all(len(s) == 1 for s in by_tag.values()) and len({s for v in by_tag.values() for s in v}) == 3
    # A new item of a tag that's already split joins its tag's split.
    rows.append({"id": "refund-new", "input": "refund question x", "output": "quality=3", "score": 3, "by": "sam",
                 "tags": ["refund"]})
    (tagged / "golden.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    assert main(["golden", "split", "--by", "tags"]) == 0
    rows = {x["id"]: x for x in (json.loads(y) for y in (tagged / "golden.jsonl").read_text().splitlines() if y.strip())}
    assert rows["refund-new"]["split"] == rows["refund-0"]["split"]
