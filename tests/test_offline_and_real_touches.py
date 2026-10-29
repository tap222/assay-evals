import os
import socket
import subprocess
from pathlib import Path

import pytest

import assay_sdk as assay
from assay import changes
from assay.reach import compare_real_touches
import assay_sdk.offline as offline
from assay_sdk.testing import expect


def test_is_local_host():
    assert offline.is_local_host("localhost")
    assert offline.is_local_host("127.0.0.1")
    assert offline.is_local_host("127.0.0.2")
    assert offline.is_local_host("::1")
    assert offline.is_local_host("0.0.0.0")

    assert not offline.is_local_host("backup.internal")
    assert not offline.is_local_host("192.168.1.50")
    assert not offline.is_local_host("api.stripe.com")
    assert not offline.is_local_host("10.0.0.1")


def test_is_outside_repo_and_temp(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Inside repo is safe
    in_repo = repo_root / "src" / "app.py"
    assert not offline.is_outside_repo_and_temp(in_repo, repo_root=repo_root)

    # In tmp_path is safe
    in_tmp = tmp_path / "scratch" / "data.csv"
    assert not offline.is_outside_repo_and_temp(in_tmp, repo_root=repo_root)

    # Outside both is flagged
    outside = Path("/var/backups/data.tar")
    assert offline.is_outside_repo_and_temp(outside, repo_root=repo_root)


def test_recording_commands_sockets_and_outside_writes(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    assay.init(transport=assay._file_transport(str(events_path)))
    repo_root = tmp_path / "my_project"
    repo_root.mkdir()
    offline.record(repo_root=repo_root)

    with assay.run("test_backup_task") as run:
        # 1. Subprocess command
        subprocess.Popen(["echo", "hello"], stdout=subprocess.PIPE).communicate()

        # 2. Non-local socket connection (mocked underlying socket syscall to avoid network traffic)
        monkeypatch.setattr(offline, "_orig_socket_connect", lambda s, addr: None)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("db.prod.internal", 5432))
        finally:
            sock.close()

        # 3. Outside file write (mock builtins.open to avoid writing to system root)
        real_open = offline._orig_open
        def mock_open_impl(file, mode="r", *a, **kw):
            if any(m in mode for m in ("w", "a", "x", "+")):
                offline._record_touch("FILE write", os.path.basename(str(file)), {"path": str(file), "mode": mode})
            return real_open(os.devnull, mode, *a, **kw)
        monkeypatch.setattr(offline, "_orig_open", mock_open_impl)
        with open("/var/backups/archive.tar", "w") as f:
            f.write("test")

    step_names = [s.get("name") for s in run.steps if s.get("kind") == "tool"]
    assert any(n.startswith("CMD echo") for n in step_names)
    assert any(n == "SOCKET db.prod.internal:5432" for n in step_names)
    assert any(n == "FILE write archive.tar" for n in step_names)

    # expect(run).no_external_touches() fails
    exp = expect(run).no_external_touches()
    failures = exp.failures()
    assert len(failures) == 1
    assert "reached something real:" in failures[0]
    assert "CMD echo" in failures[0]


def test_offline_mode_refuses_non_local_socket(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    offline.enable_offline(allow_hosts={"allowed.api.com"}, repo_root=repo_root)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Connecting to non-local, unallowed host raises ConnectionRefusedError
        with pytest.raises(ConnectionRefusedError) as exc_info:
            sock.connect(("db.prod.internal", 5432))
        assert "Offline mode: connection to db.prod.internal:5432 refused" in str(exc_info.value)
    finally:
        offline.disable_offline()


def test_offline_mode_refuses_networking_command(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    offline.enable_offline(allow_commands={"git"}, repo_root=repo_root)

    try:
        with pytest.raises(PermissionError) as exc_info:
            subprocess.Popen(["scp", "file.txt", "user@backup.server:/data"])
        assert "Offline mode: command 'scp' refused" in str(exc_info.value)
    finally:
        offline.disable_offline()


def test_offline_mode_blocks_outside_file_write(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    offline.enable_offline(repo_root=repo_root)

    try:
        with pytest.raises(PermissionError) as exc_info:
            open("/var/backups/prod.sql", "w")
        assert "outside the repository and temporary folders" in str(exc_info.value)
    finally:
        offline.disable_offline()


def test_compare_real_touches_flags_new_touches_and_ignores_baseline():
    current_touches = {
        "test_backup": {"CMD scp", "SOCKET db.prod.internal:5432"},
        "test_dns": {"FILE write hosts"},
    }
    baseline_touches = {
        "test_dns": {"FILE write hosts"},  # baseline already touched hosts
    }

    regressions = compare_real_touches(current_touches, baseline_touches)
    assert len(regressions) == 2
    by_touch = {r["touch"]: r for r in regressions}

    assert "CMD scp" in by_touch
    assert by_touch["CMD scp"]["cases"] == ["test_backup"]
    assert "command scp run in test_backup" in by_touch["CMD scp"]["text"]

    assert "SOCKET db.prod.internal:5432" in by_touch
    assert by_touch["SOCKET db.prod.internal:5432"]["cases"] == ["test_backup"]
    assert "connection to db.prod.internal:5432 in test_backup" in by_touch["SOCKET db.prod.internal:5432"]["text"]

    # FILE write hosts was touched on baseline too, so left out
    assert "FILE write hosts" not in by_touch


def test_real_touches_in_changes_report_stops_merge_until_intended(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_with_real_touch = {
        "real_touches": [
            {
                "kind": "touched_real",
                "touch": "CMD scp",
                "cases": ["test_backup"],
                "text": "tests reached something real: command scp run in test_backup",
                "key": "touched_real:CMD scp",
            }
        ]
    }
    items = changes.items({"untested": None, "deploy": []}, run_with_real_touch, [])
    assert any(x["kind"] == "touched_real" for x in items)
    assert changes.verdict(items).startswith("Not safe to merge")

    # Mark intended
    item = items[0]
    changes.mark_intended(tmp_path, item, "verified backup test in sandbox")
    saved = changes.load_intended(tmp_path)
    assert len(saved) == 1
    assert saved[0]["key"] == "touched_real:CMD scp"
    assert saved[0]["kind"] == "touched_real"

    # Now verify intended item is filtered out
    marked = {m["key"] for m in saved}
    remaining = [x for x in items if x["key"] not in marked]
    assert remaining == []
