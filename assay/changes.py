"""`assay changes`: the change report. What a change did, whether it's safe to merge, and at most
three things to look at, against the base branch.

    NOT SAFE TO MERGE: 1 THING BROKE OR STOPPED BEING CHECKED
      1. refund_flow: refund ran before its approval (HIGH · changed: prompt refund@3 → refund@4)
      2. not run by any test: app/client.py: upload_run, lines 88-92 (outside calls)
      3. .github/workflows/release.yml: doesn't run on pull requests, only on push, tags: ...
    Also: 2 more untested changes; 1 flaky, as before. `assay changes --all` lists everything.

The items come from every signal, ranked into one list: regressions from the last `assay test`
run (with what changed next to each), weakened tests, new database writes that look wrong, changed
lines no test ran, deploy and dependency changes no CI job checked, behavior that changed but still
passes, and rewritten expected values. Test results from a run on another commit are left out.

Two of those signals are this module's own, from replaying this repository's history
(docs/p0-findings.md): of nine bugs later fixed by hand, changed lines no test ran would have
flagged two, and deploy and dependency changes four. A whole function no test ran would have
flagged one, so it's lines, grouped by the function they're in.

Changed lines come from `git diff` against the merge base. Whether a test ran them comes from the
coverage the test run left behind: coverage.py (`coverage.json` or `.coverage`), Jest or Vitest
(`coverage/coverage-final.json`), Go (`coverage.out` from `go test -coverprofile`), or lcov
(`lcov.info`, which most other languages can write). Lines
coverage doesn't count (comments, blank lines) are left out. A changed code file no test loaded
at all is one item. Each function is ranked by what its unrun lines do: money first, then messages,
logins, database writes and outside calls, then the rest.

A deploy or dependency file (a requirements or lock file, a workflow, a Dockerfile, deploy config)
is listed only when no job in a workflow that runs on pull requests installs, builds or deploys
from it: a change CI already exercised isn't news. Nothing here fails the run.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from assay.weakening import CODE, JS_TEST, PY_TEST

SHOW = 3
COVERAGE_FILES = ("coverage.json", ".coverage", "coverage/coverage-final.json", "coverage/lcov.info", "lcov.info",
                  "coverage/coverage.json", "coverage.out", "cover.out")

# What a line does, by the words in it; the first that matches names the risk.
RISKS = [
    ("money", re.compile(r"\b\w*(charge|refund|payment|payout|invoice|billing|price|amount|stripe|checkout|"
                         r"subscription|currency|wallet)\w*", re.I)),
    ("messages", re.compile(r"\b\w*(send_?mail|sendmail|send_?message|send_?sms|email|sms|whatsapp|notify|"
                            r"notification|slack|webhook|push_?notification)\w*", re.I)),
    ("logins", re.compile(r"\b\w*(login|logout|log_in|log_out|sign_?in|sign_?out|password|passwd|"
                          r"access_?token|refresh_?token|id_?token|auth_?token|bearer|api_?key|secret|jwt|oauth|sso|"
                          r"authenticat|authoriz|permission|csrf)\w*", re.I)),
    ("database writes", re.compile(r"\b\w*(repo|repository|session|db|cursor|conn|connection|objects|collection|"
                                   r"table|manager|orm|knex|prisma|tx|transaction)\w*(\.\w+)*\.(save|insert|"
                                   r"insert_one|insert_many|update|update_one|upsert|delete|delete_one|remove|commit|"
                                   r"execute|executemany|bulk_create|create|destroy|add|merge|flush)\s*\(|"
                                   r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.I)),
    ("outside calls", re.compile(r"\b(requests|httpx|urllib\w*|aiohttp|axios|urlopen|got|superagent)\b\s*[.(]|"
                                 r"\bfetch\s*\(|\b(http|https)\.(request|get)\s*\(|\b\w*(client|session|http|"
                                 r"api)\w*\.(post|put|patch|get|delete|request|send)\s*\(", re.I)),
]
RANK = {name: i for i, (name, _) in enumerate(RISKS)}
OTHER = len(RISKS)


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------- what changed ----------

def changed_lines(root: Path, base: str) -> Dict[str, Set[int]]:
    """{path: changed line numbers} of the working tree against base's merge base; new files whole."""
    mb = (_git(root, "merge-base", base, "HEAD") or "").strip() or base
    diff = _git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", mb) or ""
    out = parse_diff(diff)
    for path in (_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines():
        try:
            n = len((root / path).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        out[path] = set(range(1, n + 1))
    return out


def parse_diff(diff: str) -> Dict[str, Set[int]]:
    """{path: added or changed line numbers in the new version} from a unified diff."""
    out: Dict[str, Set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            if path is not None:
                out.setdefault(path, set())
        elif line.startswith("@@") and path is not None:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                out[path].update(range(start, start + n))
    return out


# ---------- what the tests ran ----------

@dataclass
class FileCoverage:
    measured: Set[int] = field(default_factory=set)  # lines coverage counts
    ran: Set[int] = field(default_factory=set)
    functions: List[Tuple[int, int, str]] = field(default_factory=list)  # (start, end, name); end 0: unknown


def _rel(root: Path, path: str) -> Optional[str]:
    p = Path(path)
    if not (p.is_absolute() or os.path.isabs(path) or bool(p.root) or bool(p.drive)):
        norm = Path(os.path.normpath(path)).as_posix()
        return None if norm.startswith("..") else norm
    try:
        resolved_root = root.resolve()
        if bool(p.root) and not bool(p.drive) and resolved_root.drive:
            resolved_p = Path(resolved_root.drive + os.path.normpath(path)).resolve()
        else:
            resolved_p = p.resolve()
        return resolved_p.relative_to(resolved_root).as_posix()
    except (ValueError, OSError):
        return None


def find_coverage(root: Path) -> Optional[Path]:
    for name in COVERAGE_FILES:
        if (root / name).is_file():
            return root / name
    return None


def read_coverage(root: Path, path: Path) -> Dict[str, FileCoverage]:
    """{repo-relative path: FileCoverage} from any of the formats in the module docstring."""
    if path.name.endswith(".info"):
        return _lcov(root, path.read_text(encoding="utf-8", errors="replace"))
    if path.suffix in (".out", ".txt", ".cov", ".profile") or path.name.endswith("cover.out"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("mode:"):
            return _go(root, text)
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("files"), dict):  # coverage.py's JSON report
            return _coverage_py_json(root, data)
        return _istanbul(root, data)
    return _coverage_py_data(root, path)


def _coverage_py_json(root: Path, data: dict) -> Dict[str, FileCoverage]:
    out = {}
    for name, f in data["files"].items():
        rel = _rel(root, name)
        if rel:
            ran = set(f.get("executed_lines") or [])
            out[rel] = FileCoverage(ran | set(f.get("missing_lines") or []), ran)
    return out


def _coverage_py_data(root: Path, path: Path) -> Dict[str, FileCoverage]:
    try:
        import coverage
    except ImportError:
        raise ValueError(f"{path.name} is coverage.py's data file: install coverage, or run `coverage json` "
                         f"and pass coverage.json")
    cov = coverage.Coverage(data_file=str(path))
    cov.load()
    out = {}
    for name in cov.get_data().measured_files():
        rel = _rel(root, name)
        if not rel:
            continue
        try:
            _, statements, _, missing, _ = cov.analysis2(name)
        except Exception:  # the source is gone, or isn't Python
            continue
        out[rel] = FileCoverage(set(statements), set(statements) - set(missing))
    return out


def _istanbul(root: Path, data: dict) -> Dict[str, FileCoverage]:
    out = {}
    for name, f in data.items():
        if not isinstance(f, dict) or "statementMap" not in f:
            continue
        rel = _rel(root, f.get("path") or name)
        if not rel:
            continue
        cov = FileCoverage()
        hits: Dict[int, int] = {}
        for sid, loc in f["statementMap"].items():
            n = (f.get("s") or {}).get(sid, 0)
            for line in range(loc["start"]["line"], loc["end"]["line"] + 1):
                hits[line] = max(hits.get(line, 0), n)
        cov.measured = set(hits)
        cov.ran = {line for line, n in hits.items() if n > 0}
        for fn in (f.get("fnMap") or {}).values():
            loc = fn.get("loc") or fn.get("decl") or {}
            if loc.get("start"):
                cov.functions.append((loc["start"]["line"], loc["end"]["line"], fn.get("name") or "(anonymous)"))
        out[rel] = cov
    return out


def _go_modules(root: Path) -> List[Tuple[str, str]]:
    """[(module path, its directory in the repository)] from every go.mod, the longest module first."""
    out = []
    listed = (_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "*go.mod") or "").splitlines()
    for mod in listed or (["go.mod"] if (root / "go.mod").exists() else []):
        try:
            m = re.search(r"^module\s+(\S+)", (root / mod).read_text(encoding="utf-8", errors="replace"), re.M)
        except OSError:
            continue
        if m:
            out.append((m.group(1).strip('"'), _dir(mod)))
    return sorted(out, key=lambda x: -len(x[0]))


def _go(root: Path, text: str) -> Dict[str, FileCoverage]:
    """Go's own profile (`go test -coverprofile=coverage.out`): "pkg/file.go:12.2,15.3 4 1" per block,
    named by import path. The module path in go.mod turns it into a path in the repository."""
    modules = _go_modules(root)
    out: Dict[str, FileCoverage] = {}
    for line in text.splitlines()[1:]:
        m = re.match(r"^(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$", line.strip())
        if not m:
            continue
        name, start, end, count = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        rel = None
        for module, d in modules:
            if name == module or name.startswith(module + "/"):
                rest = name[len(module):].lstrip("/")
                rel = f"{d}/{rest}" if d else rest
                break
        if rel is None and (Path(name).is_absolute() or os.path.isabs(name) or bool(Path(name).root) or (root / name).exists()):
            rel = _rel(root, name)
        if not rel:
            continue  # another module's package: a dependency, not this repository
        cov = out.setdefault(rel, FileCoverage())
        for n in range(start, end + 1):
            cov.measured.add(n)
            if count > 0:
                cov.ran.add(n)
    return out


def _lcov(root: Path, text: str) -> Dict[str, FileCoverage]:
    out, cov, rel = {}, None, None
    for line in text.splitlines():
        if line.startswith("SF:"):
            rel, cov = _rel(root, line[3:].strip()), FileCoverage()
        elif cov is None:
            continue
        elif line.startswith("DA:"):
            parts = line[3:].split(",")
            n = int(parts[0])
            cov.measured.add(n)
            if int(float(parts[1])) > 0:
                cov.ran.add(n)
        elif line.startswith("FN:"):  # FN:start,name, or FN:start,end,name (lcov 2)
            parts = line[3:].split(",")
            end = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else 0
            cov.functions.append((int(parts[0]), end, parts[-1]))
        elif line.startswith("end_of_record"):
            if rel:
                out[rel] = cov
            cov = rel = None
    return out


# ---------- grouped by function, ranked ----------

def _py_functions(source: str) -> List[Tuple[int, int, str]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    out.append((child.lineno, child.end_lineno or child.lineno, name))
                walk(child, f"{name}.")
            else:
                walk(child, prefix)
    walk(tree, "")
    return out


JS_FN = re.compile(r"(?:function\s*\*?\s*([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|"
                   r"[A-Za-z_$][\w$]*\s*=>)|^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*"
                   r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::\s*[^={]+)?\{)")
NOT_FN = {"if", "for", "while", "switch", "catch", "return", "function", "constructor"}


def function_of(line: int, functions: List[Tuple[int, int, str]], source_lines: List[str],
                scan: bool = True) -> Optional[str]:
    """The innermost function the line sits in: from spans, else the nearest declaration above it
    (scan: read the source for one when coverage named none)."""
    inside = [(s, name) for s, e, name in functions if e and s <= line <= e]
    if inside:
        return max(inside)[1]
    above = [(s, name) for s, e, name in functions if not e and s <= line]
    if above:
        return max(above)[1]
    if not scan:
        return None
    for i in range(min(line, len(source_lines)) - 1, -1, -1):
        m = JS_FN.search(source_lines[i])
        name = m and next((g for g in m.groups() if g), None)
        if name and name not in NOT_FN:
            return name
    return None


def risk(texts: Iterable[str]) -> str:
    best = OTHER
    for t in texts:
        for name, pattern in RISKS:
            if RANK[name] < best and pattern.search(t):
                best = RANK[name]
    return RISKS[best][0] if best < OTHER else ""


def _spans(lines: List[int]) -> str:
    """"88-92", or for scattered lines "14 lines in 393-436"."""
    lines = sorted(lines)
    lo, hi = lines[0], lines[-1]
    if hi - lo + 1 == len(lines):
        return str(lo) if lo == hi else f"{lo}-{hi}"
    return f"{len(lines)} lines in {lo}-{hi}"


def _digest(*parts) -> str:
    """A short fingerprint of what was flagged: a mark of "intended" matches only the same thing."""
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _test_file(path: str) -> bool:
    return bool(PY_TEST.search(path) or JS_TEST.search(path))


def untested(root: Path, changed: Dict[str, Set[int]], coverage: Dict[str, FileCoverage]) -> dict:
    """{"items": [{"file", "function", "lines", "spans", "risk"}], "ran", "measured"}: changed lines no
    test ran, one item per function (or per file no test loaded), ranked by risk then size."""
    items, ran, measured = [], 0, 0
    kinds = {Path(p).suffix for p in coverage}
    for path, lines in sorted(changed.items()):
        if not lines or not CODE.search(path) or _test_file(path):
            continue
        try:
            source = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue  # deleted
        cov = coverage.get(path)
        if cov is None:
            if Path(path).suffix not in kinds:
                continue  # a language this coverage doesn't measure
            body = [n for n in lines if n <= len(source) and source[n - 1].strip()
                    and not source[n - 1].strip().startswith(("#", "//", "*", "/*"))]
            if body:
                measured += len(body)
                items.append({"file": path, "function": None, "lines": body, "spans": _spans(body),
                              "risk": risk(source[n - 1] for n in body), "unloaded": True,
                              "digest": _digest(*(source[n - 1].strip() for n in body))})
            continue
        counted = lines & cov.measured
        measured += len(counted)
        ran += len(counted & cov.ran)
        missed = sorted(counted - cov.ran)
        if not missed:
            continue
        if not cov.ran:  # nothing in the file ran: one item, not one per function
            items.append({"file": path, "function": None, "lines": missed, "spans": _spans(missed),
                          "risk": risk(source[n - 1] for n in missed if n <= len(source)), "unloaded": True,
                          "digest": _digest(*(source[n - 1].strip() for n in missed if n <= len(source)))})
            continue
        functions = cov.functions or (_py_functions("\n".join(source)) if path.endswith(".py") else [])
        groups: Dict[Optional[str], List[int]] = {}
        for n in missed:
            groups.setdefault(function_of(n, functions, source, scan=not path.endswith(".py")), []).append(n)
        for fn, ns in groups.items():
            items.append({"file": path, "function": fn, "lines": ns, "spans": _spans(ns),
                          "risk": risk(source[n - 1] for n in ns if n <= len(source)),
                          "digest": _digest(*(source[n - 1].strip() for n in ns if n <= len(source)))})
    items.sort(key=lambda x: (RANK.get(x["risk"], OTHER), -len(x["lines"]), x["file"], x["function"] or ""))
    return {"items": items, "ran": ran, "measured": measured}


# ---------- deploy and dependency changes ----------

PY_DEPS = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|constraints[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|"
                     r"Pipfile(\.lock)?|poetry\.lock|uv\.lock|MANIFEST\.in)$")
NODE_DEPS = re.compile(r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|"
                       r"bun\.lockb?|\.npmrc|\.nvmrc|\.node-version)$")
DOCKER = re.compile(r"(^|/)(Dockerfile[^/]*|[^/]*\.Dockerfile|\.dockerignore|docker-compose[^/]*\.ya?ml|"
                    r"compose\.ya?ml)$")
DEPLOY = re.compile(r"(^|/)(vercel\.json|\.vercelignore|netlify\.toml|fly\.toml|render\.ya?ml|Procfile|app\.ya?ml|"
                    r"runtime\.txt|\.python-version|railway\.json|nixpacks\.toml)$")
WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
ACTION = re.compile(r"(^|/)action\.ya?ml$")

PY_INSTALL = re.compile(r"\b(pip3?\s+install|pip\s+wheel|uv\s+(sync|pip\s+install)|poetry\s+install|pipenv\s+install|"
                        r"python3?\s+-m\s+(pip\s+install|build)|tox|nox|hatch)\b[^\n]*")
NODE_INSTALL = re.compile(r"\b(npm\s+(ci|install|i|test|run|pack)|yarn(\s+install)?|pnpm\s+(install|i|test|run)|"
                          r"bun\s+install|npx)\b[^\n]*")
DOCKER_BUILD = re.compile(r"docker\s+(build|buildx|compose\s+build)|docker-compose\s+build|docker/build-push-action|"
                          r"docker/setup-buildx-action")
DEPLOY_RUN = {"vercel": re.compile(r"\bvercel\b"), "netlify": re.compile(r"\bnetlify\b"),
              "fly": re.compile(r"\bfly(ctl)?\s+deploy\b|superfly/"), "render": re.compile(r"\brender\b")}
PR_TRIGGER = re.compile(r"^on:\s*(\[[^\]]*\bpull_request\b|pull_request\b)|^\s{2,}pull_request(_target)?\s*:|"
                        r"^on:\s*\n(\s+-\s*\w+\s*\n)*\s+-\s*pull_request\b", re.M)
SAMPLES = re.compile(r"(^|/)(examples?|samples?|docs?|fixtures?|tests?|__tests__)/")  # nothing deploys from these
TRIGGERS = ("push", "release", "schedule", "workflow_dispatch", "workflow_run", "workflow_call", "tags")


def _dir(path: str) -> str:
    d = Path(path).parent.as_posix()
    return "" if d == "." else d


def _mentions_dir(text: str, d: str) -> bool:
    """Whether a command line points at directory d: a path for a subdirectory, `.` for the root."""
    if d:
        return re.search(rf"(^|[\s\"'=/]){re.escape(d)}(/|\b)", text) is not None
    return re.search(r"(\s|-e\s*|=)[\"']?\.(\[[^\]]*\])?[\"']?(\s|$)", text) is not None


# Lines of a manifest that change nothing a build or install reads: a release's version, the description.
METADATA = re.compile(r"^\s*\"?(version|description|keywords|readme|license|authors?|maintainers|homepage|"
                      r"repository|bugs|urls|classifiers|funding|documentation|changelog|contributors)\"?\s*[=:]",
                      re.I)


def _matters(path: str, lines: Optional[Set[int]], source: Optional[str]) -> bool:
    """Whether the changed lines of a manifest or action can change a build: not only its version or
    description, and in an action.yml, its steps (runs:) rather than its inputs. None: assume so."""
    if lines is None or source is None:
        return True
    text = source.splitlines()
    body = [text[n - 1] for n in lines if 0 < n <= len(text) and text[n - 1].strip()]
    if not body:
        return bool(lines)  # only lines removed: assume it matters
    if ACTION.search(path):
        runs = next((i + 1 for i, t in enumerate(text) if re.match(r"runs\s*:", t)), None)
        return runs is None or any(n > runs for n in lines)
    if PY_DEPS.search(path) or NODE_DEPS.search(path):
        if path.endswith(("pyproject.toml", "package.json", "setup.cfg")):
            return any(not METADATA.match(t) for t in body)
    return True


def unchecked(changed: Iterable[str], read: Callable[[str], Optional[str]], files: Iterable[str],
              lines: Optional[Dict[str, Set[int]]] = None) -> List[dict]:
    """Deploy and dependency files that changed with no pull-request CI job using them: [{"file", "why"}].
    read(path) gives a file at the head, files the paths there (to find the workflows), lines the
    changed line numbers per file (a manifest whose version alone changed isn't listed)."""
    workflows = {p: read(p) or "" for p in files if WORKFLOW.match(p)}
    on_pr = {p: t for p, t in workflows.items() if PR_TRIGGER.search(t)}
    blob = "\n".join(on_pr.values())
    for d in sorted({m.group(1).strip("/") for t in on_pr.values()
                     for m in re.finditer(r"uses:\s*\./([^\s@]*)", t)}):  # a local action the PR runs
        blob += "\n" + (read(f"{d}/action.yml" if d else "action.yml") or read(f"{d}/action.yaml" if d else "action.yaml") or "")
    py = [m.group(0) for m in PY_INSTALL.finditer(blob)]
    node = [m.group(0) for m in NODE_INSTALL.finditer(blob)]
    out = []
    for path in sorted(changed):
        name, d, why = Path(path).name, _dir(path), None
        if SAMPLES.search(path) or (not WORKFLOW.match(path) and not _matters(path, (lines or {}).get(path), read(path))):
            continue
        if WORKFLOW.match(path):
            if path not in on_pr:
                when = [t for t in TRIGGERS if re.search(rf"^\s*-?\s*{t}\s*:|^on:.*\b{t}\b|^\s+-\s*{t}\s*$",
                                                        workflows.get(path, ""), re.M)]
                why = "doesn't run on pull requests" + (f", only on {', '.join(when)}" if when else "") + \
                      ": its first run is after the merge"
        elif ACTION.search(path):
            if not re.search(rf"uses:\s*\./{re.escape(d)}\s*$" if d else r"uses:\s*\./?\s*$", blob, re.M):
                why = "no workflow on pull requests uses this action (uses: ./)"
        elif PY_DEPS.search(path):
            if not any(name in c or path in c or (not name.startswith(("requirements", "constraints"))
                                                  and _mentions_dir(c, d)) for c in py):
                why = "no job on pull requests installs or builds from it"
        elif NODE_DEPS.search(path):
            if not any((not d or d in c) for c in node) and not (d and re.search(
                    rf"working-directory:\s*[\"']?\.?/?{re.escape(d)}", blob)):
                why = "no job on pull requests installs or runs from it"
        elif DOCKER.search(path):
            if not DOCKER_BUILD.search(blob):
                why = "no job on pull requests builds the image"
        elif DEPLOY.search(path):
            if not any(p.search(blob) for p in DEPLOY_RUN.values()):
                why = "no job on pull requests deploys with it (a preview deploy outside CI isn't seen here)"
        if why:
            out.append({"file": path, "why": why, "digest": _digest(read(path) or "")})
    return out


# ---------- the report ----------

def compute(root: Path, base: str, coverage_path: Optional[Path]) -> dict:
    changed = changed_lines(root, base)
    head_files = (_git(root, "ls-files") or "").splitlines() + \
        (_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines()

    def read(p: str) -> Optional[str]:
        try:
            return (root / p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    deploy = unchecked([p for p in changed if (root / p).exists()], read, head_files, changed)
    out = {"base": base, "files": len(changed), "deploy": deploy, "coverage": None, "untested": None}
    if coverage_path is not None:
        out["coverage"] = str(coverage_path)
        out["untested"] = untested(root, changed, read_coverage(root, coverage_path))
    return out


def _item(x: dict) -> str:
    where = f"{x['file']}: " + ("no test loaded this file" if x.get("unloaded") else x["function"] or "(top level)")
    lines = x["spans"] if " in " in x["spans"] else f"line{'s' * (len(x['lines']) > 1)} {x['spans']}"
    return f"{where}, {lines}" + (f" ({x['risk']})" if x["risk"] else "")


# ---------- one report: a verdict and at most three things to look at ----------
#
# Everything a change did that a reviewer should see, from every signal there is, ranked into one
# list: what broke first, then what looks wrong, then what nothing checked. The verdict says
# whether to merge; the first three items say what to look at; the rest is one line of counts.

ORDER = ["regressed", "weakened", "runner_config", "logged_error", "touched_real", "new write", "untested, risky", "deploy", "changed", "untested", "expected value"]
STOPS = {"regressed", "weakened", "runner_config", "logged_error", "touched_real"}  # what makes the verdict "not safe to merge"
NOUN = {"regressed": ("regression", "regressions"), "weakened": ("weakened test", "weakened tests"),
        "runner_config": ("test runner configuration change", "test runner configuration changes"),
        "logged_error": ("logged error regression", "logged error regressions"),
        "touched_real": ("real system access", "real system accesses"),
        "new write": ("new write to check", "new writes to check"),
        "untested, risky": ("untested change", "untested changes"), "untested": ("untested change", "untested changes"),
        "deploy": ("unchecked deploy change", "unchecked deploy changes"),
        "changed": ("behavior change, still passing", "behavior changes, still passing"),
        "expected value": ("rewritten expected value", "rewritten expected values")}


def items(d: dict, run: Optional[dict], weakened: List[dict]) -> List[dict]:
    """Every finding as {"kind", "text", "detail", "key"}, ranked by ORDER, then by each signal's own
    rank. One change seen in many cases is one item ("refund ran before its approval (refund_flow,
    and 4 more cases)"). key names exactly what was flagged, for marking it intended."""
    from assay import weakening as w
    out = []

    def add(kind, text, detail="", key=None, case=None):
        out.append({"kind": kind, "text": text, "detail": detail, "key": key, "cases": [case] if case else []})

    for e in (run or {}).get("regressions", []) + (run or {}).get("new_failures", []):
        if "field" in e:
            add("regressed", f"{e['name']}: {e['change']}", e["severity"])
            continue
        why = (e.get("reasons") or [""])[0] or (e.get("flow_change") or {}).get("text") or "failing now"
        cause = "; ".join(e.get("setup") or [])
        add("regressed", why, " · ".join(x for x in (e.get("severity") or "", f"changed: {cause}" if cause else "") if x),
            case=e["name"])
    for f in w.weakening(weakened):
        add("weakened", w.describe(f))
    for f in weakened:
        if f.get("kind") == "runner_config":
            add("runner_config", w.describe(f),
                key=f"runner_config:{f['file']}:{f.get('lines_text', '')}")
    for e in (run or {}).get("logged_errors") or []:
        add("logged_error", e["text"], detail=e.get("detail", ""),
            key=e.get("key") or f"logged_error:{e['template']}", case=e.get("cases", [None])[0] if e.get("cases") else None)
    for t in (run or {}).get("real_touches") or []:
        add("touched_real", t["text"], detail=t.get("detail", ""),
            key=t.get("key") or f"touched_real:{t['touch']}",
            case=t.get("cases", [None])[0] if t.get("cases") else None)
    for c in (run or {}).get("write_checks") or []:
        names = c.get("names") or c.get("cases") or []
        add("new write", c["text"], key=f"write:{c['kind']}:{c['write']}:{c['field']}")
        out[-1]["cases"] = list(names)
    for x in (d.get("untested") or {}).get("items", []):
        add("untested, risky" if x["risk"] else "untested", f"not run by any test: {_item(x)}",
            key=f"untested:{x['file']}:{x['function'] or ''}:{x.get('digest', '')}")
    for x in d.get("deploy") or []:
        add("deploy", f"{x['file']}: {x['why']}", key=f"deploy:{x['file']}:{x.get('digest', '')}")
    for e in (run or {}).get("changed", []):
        what = (e.get("flow_change") or {}).get("text") or "changed"
        add("changed", what, "still passing", key=f"changed:{e.get('case') or e['name']}:{what}", case=e["name"])
    for f in weakened:
        if f["kind"] == "rewritten":
            add("expected value", w.describe(f), "the change you meant?",
                key=f"expected:{f['file']}:{f['test']}:{f['after']}")
    grouped: Dict[tuple, dict] = {}
    for x in out:  # the same change in many cases: one item
        if x["kind"] in ("regressed", "changed"):
            g = grouped.setdefault((x["kind"], x["text"], x["detail"]), {**x, "cases": [], "keys": []})
            g["cases"] += x["cases"]
            g["keys"].append(x["key"])
        else:
            grouped[(id(x),)] = x
    found = []
    for x in grouped.values():
        n = len(x["cases"])
        if x["kind"] in ("regressed", "changed") and n:
            x["text"] = f"{x['cases'][0]}: {x['text']}" if n == 1 else \
                f"{x['text']} ({x['cases'][0]}, and {n - 1} more case{'s' * (n > 2)})"
        elif x["kind"] == "new write" and n:
            x["text"] += f" ({x['cases'][0]}" + (f", and {n - 1} more test{'s' * (n > 2)})" if n > 1 else ")")
        if x["kind"] == "changed":
            x["key"] = "|".join(sorted(k for k in x.pop("keys") if k))
        else:
            x.pop("keys", None)
        found.append(x)
    return sorted(found, key=lambda x: ORDER.index(x["kind"]))  # stable: each signal keeps its own order


# ---------- marking an item intended ----------

INTENDED = "assay.intended.toml"
CAN_INTEND = {"runner_config", "logged_error", "touched_real", "new write", "untested, risky", "untested", "deploy", "changed", "expected value"}

KEY_TO_KIND = {
    "write": "new write",
    "untested": "untested",
    "deploy": "deploy",
    "changed": "changed",
    "expected": "expected value",
    "runner_config": "runner_config",
    "logged_error": "logged_error",
    "touched_real": "touched_real",
}


def load_intended(root: Path) -> List[dict]:
    from assay.local import tomllib
    path = root / INTENDED
    if not path.exists():
        return []
    return list(tomllib.loads(path.read_text(encoding="utf-8")).get("intended") or [])


def mark_intended(root: Path, item: dict, reason: str) -> Path:
    """Adds item to assay.intended.toml, in the repository, so CI sees it and a reviewer reads it."""
    from datetime import date
    from assay.acks import who
    q = lambda v: json.dumps(str(v), ensure_ascii=False)
    path = root / INTENDED
    head = "" if path.exists() else ("# Change-report items marked intended (`assay changes --intended N`). Each matches\n"
                                     "# only what was flagged then; a different change in the same place shows again.\n")
    entry = (f"\n[[intended]]\nkind = {q(item['kind'])}\nkey = {q(item['key'])}\nwhat = {q(item['text'])}\nby = {q(who())}\n"
             f"reason = {q(reason)}\non = {q(date.today().isoformat())}\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(head + entry)
    return path


def intended_stats(root: Path, current_items: Optional[List[dict]] = None) -> Dict[str, dict]:
    """Counts which flags get marked intended and computes the share marked intended per kind.
    Returns: {kind: {"intended": int, "active": int, "total": int, "share": float, "share_percent": str, "ignored": bool}}"""
    intended_entries = load_intended(root)
    counts: Dict[str, int] = {}
    for entry in intended_entries:
        kind = entry.get("kind")
        if not kind:
            prefix = (entry.get("key") or "").split(":", 1)[0]
            kind = KEY_TO_KIND.get(prefix, prefix or "other")
        counts[kind] = counts.get(kind, 0) + 1

    active_counts: Dict[str, int] = {}
    if current_items is not None:
        marked_keys = {m.get("key") for m in intended_entries if m.get("key")}
        for it in current_items:
            k = it.get("kind")
            if it.get("key") and it["key"] in marked_keys:
                continue
            active_counts[k] = active_counts.get(k, 0) + 1

    all_kinds = sorted(set(list(counts.keys()) + list(active_counts.keys())))
    out = {}
    for k in all_kinds:
        n_intended = counts.get(k, 0)
        n_active = active_counts.get(k, 0)
        total = n_intended + n_active
        share = (n_intended / total) if total > 0 else 0.0
        ignored = (total >= 2 and share >= 0.5)
        out[k] = {
            "intended": n_intended,
            "active": n_active,
            "total": total,
            "share": round(share, 3),
            "share_percent": f"{int(round(share * 100))}%",
            "ignored": ignored,
        }
    return out


def intended_stats_text(stats: Dict[str, dict]) -> str:
    if not stats:
        return "No items marked intended in assay.intended.toml."
    lines = ["Intended flags by kind (share marked intended):"]
    for kind, s in stats.items():
        n_int, tot, pct = s["intended"], s["total"], s["share_percent"]
        note = " (high ignore rate: consider fixing or dropping these checks)" if s.get("ignored") else ""
        lines.append(f"  - {kind}: {n_int} of {tot} marked intended ({pct}){note}")
    return "\n".join(lines)


def verdict(found: List[dict]) -> str:
    stops = sum(x["kind"] in STOPS for x in found)
    if stops:
        return f"Not safe to merge: {stops} {'thing' if stops == 1 else 'things'} broke or stopped being checked"
    if found:
        n = min(len(found), SHOW)
        return f"Check {n} {'thing' if n == 1 else 'things'} before merging"
    return "Nothing to check: everything changed ran in a test, and CI used every deploy change"


def report(root: Path, d: dict) -> dict:
    """The change report: {"verdict", "items", "rest" (counts by kind past the first three), "flaky",
    "run"} over `assay changes`' own findings (d), the latest test run and the weakened tests."""
    from assay import diff as behavior, weakening
    from assay.local import HOME
    if (root / HOME / "assay.db").exists():  # never create .assay/ in a repository that doesn't use Assay
        run, why = behavior.latest(root, quiet=True)
    else:
        run, why = None, None
    if run is not None:
        from assay import local
        commit = ((local._state(local.ensure_home(root)).get("revisions") or {}).get(run["current"]["run_id"]) or {}) \
            .get("commit")
        head = os.environ.get("GITHUB_SHA") or (_git(root, "rev-parse", "HEAD") or "").strip()  # as revision() records it
        if commit and head and commit != head:  # results from other code would mislead
            run, why = None, f"the last test run was on {commit[:7]}, not this commit: run `assay test` to include it"
    weakened = weakening.scan(root, d["base"])
    marked = {m.get("key") for m in load_intended(root)}
    everything = items(d, run, weakened)
    found = [x for x in everything if not (x["key"] and x["key"] in marked)]
    rest: Dict[str, int] = {}
    for x in found[SHOW:]:
        rest[x["kind"]] = rest.get(x["kind"], 0) + 1
    flaky = len((run or {}).get("flaky") or [])
    stats = intended_stats(root, everything)
    return {"verdict": verdict(found), "items": found, "rest": rest, "flaky": flaky,
            "intended": len(everything) - len(found),
            "intended_stats": stats,
            "run": None if run is None else {"current": run["current"]["label"], "baseline": run["baseline"]["label"],
                                             "scenarios": run["scenarios"]},
            "no_run": why if run is None else None, "changes": d}


def _rest_text(rest: Dict[str, int]) -> str:
    by_noun: Dict[tuple, int] = {}
    for k in sorted(rest, key=ORDER.index):  # risky and other untested changes are counted together
        by_noun[NOUN[k]] = by_noun.get(NOUN[k], 0) + rest[k]
    return ", ".join(f"{n} more {noun[n != 1]}" for noun, n in by_noun.items())


def report_text(r: dict, limit: int = SHOW) -> str:
    out = [r["verdict"].upper() if r["verdict"].startswith("Not safe") else r["verdict"]]
    for i, x in enumerate(r["items"][:limit], 1):
        out.append(f"  {i}. {x['text']}" + (f" ({x['detail']})" if x["detail"] else ""))
    rest = {} if limit > SHOW else r["rest"]
    extra = [s for s in (_rest_text(rest), f"{r['flaky']} flaky, as before" if r["flaky"] else "") if s]
    if extra:
        out.append(f"Also: {'; '.join(extra)}." + (" `assay changes --all` lists everything." if rest else ""))
    d = r["changes"]
    where = [f"against {d['base']}, {d['files']} changed file{'s' * (d['files'] != 1)}"]
    if r["run"]:
        where.append(f"tests: {r['run']['current']} vs {r['run']['baseline']}")
    elif r["no_run"]:
        where.append(f"no test results: {r['no_run'][:1].lower()}{r['no_run'][1:].rstrip('.')}")
    if r.get("intended"):
        where.append(f"{r['intended']} marked intended in {INTENDED}")
    if d["untested"] is None:
        where.append("no coverage found, so untested lines weren't checked")
    else:
        u = d["untested"]
        where.append(f"{u['ran']} of {u['measured']} changed lines ran in a test")
    out.append(f"({'; '.join(where)})")
    return "\n".join(out) + "\n"


def report_markdown(r: dict, limit: int = SHOW) -> str:
    from assay.local import _md
    out = [f"### {_md(r['verdict'])}", ""]
    for i, x in enumerate(r["items"][:limit], 1):
        out.append(f"{i}. {_md(x['text'])}" + (f" · {_md(x['detail'])}" if x["detail"] else ""))
    if r["items"]:
        out.append("")
    rest = r["rest"] if limit <= SHOW else {}
    extra = [s for s in (_rest_text(rest), f"{r['flaky']} flaky, as before" if r["flaky"] else "") if s]
    if extra:
        out += [f"Also: {_md('; '.join(extra))}." + (" `assay changes --all` lists everything." if rest else ""), ""]
    d = r["changes"]
    note = f"Against `{_md(d['base'])}`, {d['files']} changed files"
    if d["untested"] is not None:
        note += f"; {d['untested']['ran']} of {d['untested']['measured']} changed lines ran in a test"
    else:
        note += "; no coverage, so untested lines weren't checked"
    if r.get("intended"):
        note += f"; {r['intended']} marked intended in `{INTENDED}`"
    if r["no_run"]:
        note += f"; no test results: {r['no_run'][:1].lower()}{r['no_run'][1:].rstrip('.')}"
    out += [f"<sub>{note}.</sub>", ""]
    return "\n".join(out) + "\n"


def main(root: Path, base: Optional[str], coverage: Optional[str], fmt: str, show_all: bool,
         intended: Optional[int] = None, reason: Optional[str] = None,
         show_intended_stats: bool = False) -> int:
    """`assay changes`: 0 with a report (nothing here fails), 2 when it can't run. With intended,
    marks that item of the report (as `--all` numbers them) intended instead. With show_intended_stats,
    reports the share marked intended per kind."""
    import sys
    from assay import weakening
    ref = weakening.base_ref(root, base) if base or os.environ.get(weakening.BASE_ENV) else weakening.default_base(root)
    if not ref:
        print(f"There's no {base} here to compare with." if base else
              "No base branch to compare with: pass --base main.", file=sys.stderr)
        return 2
    path = Path(coverage) if coverage else find_coverage(root)
    if coverage and not path.is_file():
        print(f"No coverage file at {coverage}.", file=sys.stderr)
        return 2
    try:
        d = compute(root, ref, path)
    except (ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"Couldn't read the coverage in {path}: {exc}", file=sys.stderr)
        return 2
    limit = 10 ** 9 if show_all else SHOW
    r = report(root, d)
    if show_intended_stats:
        stats = r.get("intended_stats") or intended_stats(root, r.get("items"))
        print(json.dumps(stats, indent=2) if fmt == "json" else intended_stats_text(stats))
        return 0
    if intended is not None:
        if not 1 <= intended <= len(r["items"]):
            print(f"There's no item {intended}: the report has {len(r['items'])} (`assay changes --all` numbers "
                  f"them).", file=sys.stderr)
            return 2
        item = r["items"][intended - 1]
        if item["kind"] not in CAN_INTEND or not item["key"]:
            print("A regression or a weakened test can't be marked intended here: accept the run (`assay accept`), "
                  "acknowledge the check (`assay ack`), or label the pull request.", file=sys.stderr)
            return 2
        if not reason:
            print("Say why with --reason: a reviewer reads it in the pull request.", file=sys.stderr)
            return 2
        path = mark_intended(root, item, reason)
        print(f"Marked intended in {path.name}: {item['text']}. Commit it with the change.")
        return 0
    print(json.dumps(r, indent=1, default=str) if fmt == "json" else
          report_markdown(r, limit) if fmt == "markdown" else report_text(r, limit), end="")
    return 0
