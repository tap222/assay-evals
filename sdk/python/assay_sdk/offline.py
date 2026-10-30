"""Record what tests touch (commands, non-local sockets, outside file writes) and enforce opt-in offline mode.

Features:
- Records commands started (scp, rsync, psql, ...).
- Records connections to hosts that aren't local at the socket level.
- Records file writes outside the repository and temporary folders.
- Opt-in offline mode: refuses non-local socket connections and fails outside file writes loudly.
"""
from __future__ import annotations

import builtins
import io
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import assay_sdk as assay

_lock = threading.Lock()
_installed = False
_offline_active = False
_allow_hosts: Set[str] = set()
_allow_commands: Set[str] = set()
_repo_root: Optional[Path] = None

# Commands typically used for networking, remote execution, or database calls
NETWORK_COMMANDS = {
    "scp", "rsync", "ssh", "sftp", "psql", "mysql", "mongosh",
    "curl", "wget", "nc", "netcat", "telnet", "ftp",
}

# Standard local hosts
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# Original unpatched functions
_orig_popen_init = subprocess.Popen.__init__
_orig_socket_connect = socket.socket.connect
_orig_socket_connect_ex = socket.socket.connect_ex
_orig_open = builtins.open


def is_local_host(host: str) -> bool:
    if not host:
        return True
    h = host.lower().strip("[]").split("%")[0]
    if h in LOCAL_HOSTS or h.startswith("127."):
        return True
    return False


def _get_temp_dirs() -> List[str]:
    dirs = [tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/tmp"]
    try:
        dirs.append(str(Path(tempfile.gettempdir()).resolve()))
    except Exception:
        pass
    try:
        t = tempfile.TemporaryDirectory()
        dirs.append(os.path.dirname(t.name))
        dirs.append(str(Path(t.name).resolve()))
        t.cleanup()
    except Exception:
        pass
    return [os.path.abspath(d) for d in dirs if os.path.exists(d)]


def is_outside_repo_and_temp(path_or_file: Any, repo_root: Optional[Path] = None) -> bool:
    """True if path is outside both the repository and temporary folders."""
    if isinstance(path_or_file, int):
        return False  # file descriptor (stdin, stdout, stderr)
    try:
        raw_path = os.fspath(path_or_file)
        if not isinstance(raw_path, str):
            return False
        abs_path = os.path.abspath(os.path.expanduser(raw_path))
    except Exception:
        return False

    # 1. Check temp directories
    for td in _get_temp_dirs():
        try:
            if os.path.commonpath([abs_path, td]) == td:
                return False
        except Exception:
            continue
    if "pytest-of-" in abs_path or "/tmp/" in abs_path or "/private/tmp/" in abs_path:
        return False

    # 2. Check repository root and working directory
    root = repo_root or _repo_root or Path.cwd()
    try:
        root_abs = os.path.abspath(str(root))
        if os.path.commonpath([abs_path, root_abs]) == root_abs:
            return False
    except Exception:
        pass

    return True


def _record_touch(category: str, name: str, details: Dict[str, Any]) -> None:
    run = assay.current()
    if run is None:
        return
    try:
        tool_name = f"{category} {name}"[:128]
        run.tool(tool_name, details, result={"touched": True}, server="",
                 started=datetime.now(timezone.utc), ended=datetime.now(timezone.utc))
    except Exception:
        pass


def _patched_popen_init(self, args, *a, **kw):
    cmd_name = ""
    cmd_summary = ""
    try:
        if isinstance(args, (list, tuple)):
            cmd_name = os.path.basename(str(args[0])) if args else ""
            cmd_summary = " ".join(str(x) for x in args)
        else:
            parts = str(args).split()
            cmd_name = os.path.basename(parts[0]) if parts else ""
            cmd_summary = str(args)
    except Exception:
        pass

    # Check offline enforcement
    if _offline_active or os.environ.get("ASSAY_OFFLINE") in ("1", "true", "yes"):
        if cmd_name in NETWORK_COMMANDS and cmd_name not in _allow_commands:
            raise PermissionError(
                f"Offline mode: command '{cmd_name}' refused ({cmd_summary[:120]}). "
                f"Fake this tool or allow it in offline settings."
            )

    # Record touch
    if cmd_name:
        _record_touch("CMD", cmd_name, {"command": cmd_summary[:256], "cmd": cmd_name})

    orig = getattr(offline, "_orig_popen_init", _orig_popen_init)
    return orig(self, args, *a, **kw)


def _patched_socket_connect(self, address):
    host, port = "", 0
    is_unix = getattr(self, "family", None) == getattr(socket, "AF_UNIX", None)
    if not is_unix and isinstance(address, tuple) and len(address) >= 2:
        host, port = str(address[0]), int(address[1])

    if host and not is_local_host(host):
        # Check offline mode
        if _offline_active or os.environ.get("ASSAY_OFFLINE") in ("1", "true", "yes"):
            if host not in _allow_hosts and not any(host.endswith("." + a) for a in _allow_hosts):
                raise ConnectionRefusedError(
                    f"Offline mode: connection to {host}:{port} refused. "
                    f"Fake this host or add it to allowlist."
                )

        # Record touch
        _record_touch("SOCKET", f"{host}:{port}", {"host": host, "port": port})

    orig = getattr(offline, "_orig_socket_connect", _orig_socket_connect)
    return orig(self, address)


def _patched_socket_connect_ex(self, address):
    try:
        _patched_socket_connect(self, address)
        return 0
    except ConnectionRefusedError:
        return 111  # ECONNREFUSED
    except Exception as exc:
        orig = getattr(offline, "_orig_socket_connect_ex", _orig_socket_connect_ex)
        return orig(self, address)


def _patched_open(file, mode="r", *a, **kw):
    is_write = any(m in mode for m in ("w", "a", "x", "+"))
    if is_write and is_outside_repo_and_temp(file, _repo_root):
        norm = os.path.abspath(os.path.expanduser(str(file)))
        if _offline_active or os.environ.get("ASSAY_OFFLINE") in ("1", "true", "yes"):
            raise PermissionError(
                f"Offline mode: write to '{norm}' is outside the repository and temporary folders."
            )
        _record_touch("FILE write", os.path.basename(norm), {"path": norm, "mode": mode})

    orig = getattr(offline, "_orig_open", _orig_open)
    return orig(file, mode, *a, **kw)


def record(repo_root: Optional[Path] = None) -> None:
    """Installs hooks to monitor commands started, socket connections, and outside writes."""
    global _installed, _repo_root
    with _lock:
        if repo_root is not None:
            _repo_root = repo_root
        if _installed:
            return
        _installed = True
        subprocess.Popen.__init__ = _patched_popen_init
        socket.socket.connect = _patched_socket_connect
        socket.socket.connect_ex = _patched_socket_connect_ex
        builtins.open = _patched_open


def enable_offline(allow_hosts: Optional[Iterable[str]] = None,
                   allow_commands: Optional[Iterable[str]] = None,
                   repo_root: Optional[Path] = None) -> None:
    """Enables opt-in offline mode: refuses non-local sockets and blocks outside file writes."""
    global _offline_active, _allow_hosts, _allow_commands, _repo_root
    record(repo_root)
    with _lock:
        _offline_active = True
        if allow_hosts:
            _allow_hosts.update(allow_hosts)
        if allow_commands:
            _allow_commands.update(allow_commands)
        if repo_root is not None:
            _repo_root = repo_root


def disable_offline() -> None:
    """Disables offline mode enforcement."""
    global _offline_active
    with _lock:
        _offline_active = False


@contextmanager
def offline(allow_hosts: Optional[Iterable[str]] = None,
            allow_commands: Optional[Iterable[str]] = None,
            repo_root: Optional[Path] = None):
    """Context manager running code in offline mode."""
    enable_offline(allow_hosts, allow_commands, repo_root)
    try:
        yield
    finally:
        disable_offline()


# Attach module functions to offline object so both `assay.offline()` and
# `offline.record()` / `offline.is_local_host()` work cleanly whether imported as
# module or function.
offline.is_local_host = is_local_host
offline.is_outside_repo_and_temp = is_outside_repo_and_temp
offline.record = record
offline.enable_offline = enable_offline
offline.disable_offline = disable_offline
offline._patched_socket_connect = _patched_socket_connect
offline._orig_socket_connect = _orig_socket_connect
offline._orig_open = _orig_open
offline._record_touch = _record_touch
offline.NETWORK_COMMANDS = NETWORK_COMMANDS
offline.LOCAL_HOSTS = LOCAL_HOSTS

