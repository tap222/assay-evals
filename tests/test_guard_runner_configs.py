import json
import os
import subprocess
from pathlib import Path

import pytest

from assay import changes, weakening
from assay.__main__ import main


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Developer")
    (tmp_path / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("from app import add\ndef test_add(): assert add(1, 2) == 3\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "package.json").write_text('{\n  "name": "demo",\n  "scripts": {\n    "build": "echo build"\n  }\n}\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "initial commit")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_conftest_addition_or_modification_is_detected(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    # Add conftest.py
    (repo / "conftest.py").write_text("import pytest\n\n@pytest.fixture\ndef db():\n    return {}\n", encoding="utf-8")
    git(repo, "add", "conftest.py")
    
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert len(configs) == 1
    assert configs[0]["file"] == "conftest.py"
    assert configs[0]["lines_text"] == "1-5"
    assert "changes test runner setup or fixtures" in configs[0]["detail"]
    assert weakening.describe(configs[0]) == "changes how tests run in conftest.py (lines 1-5): changes test runner setup or fixtures"

    # assay guard fails with exit code 1
    code, out = weakening.guard(repo, "main")
    assert code == 1
    assert "1 test runner configuration change (for a person to confirm):" in out
    assert "changes how tests run in conftest.py (lines 1-5)" in out

    # CLI invocation
    assert main(["guard", "--base", "main"]) == 1
    cli_out = capsys.readouterr().out
    assert "1 test runner configuration change" in cli_out


def test_untracked_conftest_is_detected(repo):
    # conftest.py created but not added to git
    (repo / "conftest.py").write_text("# new fixture\ndef setup_all(): pass\n", encoding="utf-8")
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert len(configs) == 1
    assert configs[0]["file"] == "conftest.py"
    assert configs[0]["lines_text"] == "1-2"
    assert configs[0]["before"] == "untracked"


def test_pytest_ini_and_jest_config_are_detected(repo):
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -ra -q\n", encoding="utf-8")
    (repo / "jest.config.js").write_text("module.exports = { testEnvironment: 'node' };\n", encoding="utf-8")
    git(repo, "add", ".")
    found = weakening.scan(repo, "main")
    files = {f["file"] for f in found if f["kind"] == "runner_config"}
    assert files == {"pytest.ini", "jest.config.js"}


def test_pyproject_toml_pytest_options_detected_but_unrelated_ignored(repo):
    # Unrelated change: version bump in pyproject.toml
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.2.0"\n', encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert configs == []  # version change is not a runner config change

    # Adding pytest options in pyproject.toml
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.2.0"\n\n[tool.pytest.ini_options]\naddopts = "-v"\n',
        encoding="utf-8"
    )
    git(repo, "add", "pyproject.toml")
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert len(configs) == 1
    assert configs[0]["file"] == "pyproject.toml"
    assert "changes pytest or vitest options in pyproject.toml" in configs[0]["detail"]


def test_package_json_test_script_detected_but_unrelated_ignored(repo):
    # Unrelated change: author field
    (repo / "package.json").write_text('{\n  "name": "demo",\n  "author": "Alice",\n  "scripts": {\n    "build": "echo build"\n  }\n}\n', encoding="utf-8")
    git(repo, "add", "package.json")
    found = weakening.scan(repo, "main")
    assert [f for f in found if f["kind"] == "runner_config"] == []

    # Changing test script
    (repo / "package.json").write_text('{\n  "name": "demo",\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n', encoding="utf-8")
    git(repo, "add", "package.json")
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert len(configs) == 1
    assert configs[0]["file"] == "package.json"
    assert "changes test script or jest config in package.json" in configs[0]["detail"]


def test_github_workflow_ci_test_command_detected(repo):
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "ci.yml"
    wf.write_text("name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n", encoding="utf-8")
    git(repo, "add", ".github")
    found = weakening.scan(repo, "main")
    configs = [f for f in found if f["kind"] == "runner_config"]
    assert len(configs) == 1
    assert configs[0]["file"] == ".github/workflows/ci.yml"
    assert "changes CI test command in workflow" in configs[0]["detail"]


def test_assay_changes_lists_runner_config_and_stops_merge_until_intended(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    (repo / "conftest.py").write_text("import pytest\n@pytest.fixture\ndef user(): return 'guest'\n", encoding="utf-8")
    git(repo, "add", "conftest.py")

    # In assay changes, runner_config stops merge
    r = changes.report(repo, {"base": "main", "files": 1, "deploy": [], "coverage": None, "untested": None})
    assert "Not safe to merge: 1 thing broke or stopped being checked" in r["verdict"]
    assert len(r["items"]) == 1
    assert r["items"][0]["kind"] == "runner_config"
    assert r["items"][0]["key"] == "runner_config:conftest.py:1-3"

    # Mark it intended
    assert main(["changes", "--base", "main", "--intended", "1", "--reason", "added guest user fixture"]) == 0
    out = capsys.readouterr().out
    assert "Marked intended in assay.intended.toml: changes how tests run in conftest.py (lines 1-3)" in out

    # Now verify it is left out and safe
    r2 = changes.report(repo, {"base": "main", "files": 1, "deploy": [], "coverage": None, "untested": None})
    assert len(r2["items"]) == 0
    assert r2["intended"] == 1
    assert "Nothing to check" in r2["verdict"]


def test_intended_stats_computes_count_and_shares(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    # Seed assay.intended.toml with multiple entries
    toml_content = """
[[intended]]
kind = "deploy"
key = "deploy:Dockerfile:"
what = "Dockerfile: no job on pull requests builds the image"
by = "dev"
reason = "local dev image only"
on = "2026-10-29"

[[intended]]
kind = "deploy"
key = "deploy:docker-compose.yml:"
what = "docker-compose.yml: no job deploys with it"
by = "dev"
reason = "compose is for integration tests"
on = "2026-10-29"

[[intended]]
kind = "untested"
key = "untested:helpers.py:clean:abc123"
what = "not run by any test: helpers.py: clean"
by = "dev"
reason = "tested manually"
on = "2026-10-29"

[[intended]]
kind = "runner_config"
key = "runner_config:conftest.py:1-5"
what = "changes how tests run in conftest.py (lines 1-5)"
by = "dev"
reason = "standard fixtures"
on = "2026-10-29"
"""
    (repo / "assay.intended.toml").write_text(toml_content.strip() + "\n", encoding="utf-8")

    current_items = [
        {"kind": "deploy", "key": "deploy:release.yml:", "text": "release.yml: no job"},
        {"kind": "untested", "key": "untested:app.py:main:xyz", "text": "not run by any test: main"},
        {"kind": "untested", "key": "untested:app.py:calc:xyz", "text": "not run by any test: calc"},
        {"kind": "untested", "key": "untested:app.py:fmt:xyz", "text": "not run by any test: fmt"},
    ]

    stats = changes.intended_stats(repo, current_items)
    # deploy: 2 intended, 1 active -> total 3, share 2/3 = 66.7%, ignored=True (>=50% and >=2)
    assert stats["deploy"]["intended"] == 2
    assert stats["deploy"]["active"] == 1
    assert stats["deploy"]["total"] == 3
    assert stats["deploy"]["share_percent"] == "67%"
    assert stats["deploy"]["ignored"] is True

    # untested: 1 intended, 3 active -> total 4, share 1/4 = 25%, ignored=False
    assert stats["untested"]["intended"] == 1
    assert stats["untested"]["active"] == 3
    assert stats["untested"]["total"] == 4
    assert stats["untested"]["share_percent"] == "25%"
    assert stats["untested"]["ignored"] is False

    # runner_config: 1 intended, 0 active -> total 1, share 100%
    assert stats["runner_config"]["intended"] == 1
    assert stats["runner_config"]["active"] == 0
    assert stats["runner_config"]["total"] == 1
    assert stats["runner_config"]["share_percent"] == "100%"

    text = changes.intended_stats_text(stats)
    assert "Intended flags by kind (share marked intended):" in text
    assert "- deploy: 2 of 3 marked intended (67%) (high ignore rate: consider fixing or dropping these checks)" in text
    assert "- untested: 1 of 4 marked intended (25%)" in text
    assert "- runner_config: 1 of 1 marked intended (100%)" in text

    # Test CLI flag --intended-stats
    assert main(["changes", "--base", "main", "--intended-stats"]) == 0
    cli_out = capsys.readouterr().out
    assert "Intended flags by kind (share marked intended):" in cli_out
    assert "deploy: 2 of 2 marked intended (100%)" in cli_out
    assert "runner_config: 1 of 1 marked intended (100%)" in cli_out
