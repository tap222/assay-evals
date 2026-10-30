"""Tests that stopped checking: assertions a change removed or loosened, compared with the base branch.

A failing test can be made to pass without fixing anything: `assert total == 1200` becomes
`assert total > 0`, `expect(total).toBe(1200)` becomes `toBeDefined()`, or the assertion goes.
Coding agents do it when told to make the tests pass, and in a big pull request nobody reads
every test. Deleting, skipping or filtering out a test is caught elsewhere (the suite a pull
request runs is held to the base branch's); this catches the quieter kind, inside the test.

Each test file the change touched is compared with its version at the merge base, test by test
(matched by name), assertion by assertion (matched by what they check):

  loosened   an exact check of a value became a looser one on the same value
  removed    a test has fewer assertions than before, and the ones gone check something no
             remaining assertion does
  rewritten  an exact check stayed exact, but its expected value changed. Often that's the
             change you meant; it's listed to confirm, never a failure. An expected dict, list,
             tuple or set that kept every entry and only gained more checks more, not less, so
             it isn't listed

pytest files are read with Python's own parser. Jest and Vitest files are read by a scanner
that follows `expect(...)` chains: a parser for every TypeScript dialect isn't worth a
dependency for this. What isn't understood is skipped, never guessed.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PY_TEST = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
JS_TEST = re.compile(r"(^|/)([^/]+\.(test|spec)\.[cm]?[jt]sx?|__tests__/[^/]+\.[cm]?[jt]sx?)$")
CODE = re.compile(r"\.(py|[cm]?[jt]sx?|go|rb|java|kt|rs|cs|php|swift|scala)$")
BASE_ENV = "ASSAY_BASE_REF"

# An assertion: (target, exact, expected, line, text, tolerance). target and expected are normalized
# source text, so reformatting changes nothing; tolerance is set for approximate checks.
Check = Tuple[str, bool, Optional[str], int, str, Optional[float]]


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def base_ref(root: Path, given: Optional[str] = None) -> Optional[str]:
    """The ref to compare with: the one given, else ASSAY_BASE_REF. None: nothing to compare with."""
    ref = given or os.environ.get(BASE_ENV) or None
    if ref and _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is None:
        return None
    return ref


def default_base(root: Path) -> Optional[str]:
    """origin's default branch, else main or master: what `assay guard` compares with by default."""
    head = (_git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") or "").strip()
    for ref in ([head.replace("refs/remotes/", "", 1)] if head else []) + ["origin/main", "origin/master", "main", "master"]:
        if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is not None:
            return ref
    return None


# ---------- pytest ----------

EXACT_CALLS = {"assertEqual", "assertEquals", "assertDictEqual", "assertListEqual", "assertTupleEqual",
               "assertSetEqual", "assertSequenceEqual", "assertMultiLineEqual", "assertCountEqual",
               "assertAlmostEqual", "assertIs", "assertIsNone"}
LOOSE_CALLS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertGreater", "assertGreaterEqual",
               "assertLess", "assertLessEqual", "assertIn", "assertNotIn", "assertIsInstance",
               "assertNotEqual", "assertIsNot", "assertRegex"}


def _const(node: ast.AST) -> bool:
    """A literal value: the expected side of an exact check."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _const(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_const(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _const(k) for k in node.keys) and all(_const(v) for v in node.values)
    return _approx(node) is not None


def _approx(node: ast.AST) -> Optional[float]:
    """The tolerance of a pytest.approx(...) call (relative, its default 1e-6), else None."""
    if isinstance(node, ast.Call) and ast.unparse(node.func) in ("pytest.approx", "approx"):
        tol = 1e-6
        for k in node.keywords:
            if k.arg in ("rel", "abs") and isinstance(k.value, ast.Constant) and isinstance(k.value.value, (int, float)):
                tol = float(k.value.value)
        return tol
    return None


def _py_check(node: ast.AST) -> Optional[Check]:
    if isinstance(node, ast.Assert):
        t = node.test
        text = "assert " + ast.unparse(t)
        if isinstance(t, ast.Compare) and len(t.ops) == 1:
            left, right, op = t.left, t.comparators[0], t.ops[0]
            if isinstance(op, (ast.Eq, ast.Is)):
                target, expected = (right, left) if _const(left) and not _const(right) else (left, right)
                tol = _approx(expected)
                return (ast.unparse(target), True, ast.unparse(expected), node.lineno, text, tol)
            return (ast.unparse(left), False, None, node.lineno, text, None)
        if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
            return (ast.unparse(t.operand), False, None, node.lineno, text, None)
        if isinstance(t, ast.Call) and ast.unparse(t.func) in ("isinstance", "len", "bool") and t.args:
            return (ast.unparse(t.args[0]), False, None, node.lineno, text, None)
        return (ast.unparse(t), False, None, node.lineno, text, None)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        name = node.func.attr
        text = ast.unparse(node)
        if name in EXACT_CALLS:
            first = node.args[0]
            second = node.args[1] if len(node.args) > 1 else None
            if second is not None and _const(first) and not _const(second):
                first, second = second, first
            return (ast.unparse(first), True, ast.unparse(second) if second is not None else "None", node.lineno, text, None)
        if name in LOOSE_CALLS:
            return (ast.unparse(node.args[0]), False, None, node.lineno, text, None)
    return None


def py_tests(source: str) -> Optional[Dict[str, List[Check]]]:
    """{test name: its assertions, in order} for a pytest or unittest file. None: it doesn't parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: Dict[str, List[Check]] = {}

    def add(fn, prefix=""):
        checks = [c for n in ast.walk(fn) for c in [_py_check(n)] if c]
        out[prefix + fn.name] = sorted(checks, key=lambda c: c[3])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            add(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test"):
                    add(m, node.name + ".")
    return out


# ---------- Jest and Vitest ----------

JS_EXACT = {"toBe", "toEqual", "toStrictEqual", "toMatchObject", "toHaveBeenCalledWith", "toHaveBeenLastCalledWith",
            "toHaveBeenNthCalledWith", "toHaveBeenCalledTimes", "toHaveLength", "toBeCloseTo", "toMatchSnapshot",
            "toMatchInlineSnapshot", "toBeNull", "toBeUndefined", "toBeNaN", "toHaveReturnedWith",
            "toHaveReturnedTimes", "toHaveLastReturnedWith", "toThrowErrorMatchingSnapshot",
            "toThrowErrorMatchingInlineSnapshot", "toHaveTextContent", "toHaveValue", "toHaveAttribute"}
JS_NEEDS_ARG = {"toThrow", "toThrowError", "toHaveProperty"}  # exact with an argument (two, for toHaveProperty)
TEST_CALL = re.compile(r"\b(?:it|test|xit)(?:\.(?:only|skip|todo|concurrent|failing))?\s*\(\s*(['\"`])((?:\\.|(?!\1).)*)\1", re.S)


def _blank_comments(src: str) -> str:
    """The source with comments replaced by spaces (newlines kept), so offsets and lines still match."""
    out, i, n, quote = list(src), 0, len(src), None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in src[i:j]]
            i = j
            continue
        i += 1
    return "".join(out)


def _balanced(src: str, i: int) -> Optional[int]:
    """src[i] is an opening bracket: the index just past its match, skipping strings. None if unmatched."""
    pairs, stack, quote, n = {"(": ")", "[": "]", "{": "}"}, [], None, len(src)
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack.pop() != c:
                return None
            if not stack:
                return i + 1
        i += 1
    return None


def _norm(text: str) -> str:
    """Whitespace out, and every plain string in double quotes: a formatter's quote style isn't a change."""
    text = re.sub(r"'((?:[^'\\\"\n]|\\.)*)'|`((?:[^`\\\"$\n]|\\.)*)`", lambda m: f'"{m.group(1) if m.group(1) is not None else m.group(2)}"', text)
    return re.sub(r"\s+", "", text).rstrip(",")


def _js_checks(block: str, offset: int, src: str) -> List[Check]:
    out = []
    for m in re.finditer(r"\bexpect(?:\.soft)?\s*\(", block):
        start = m.end() - 1
        end = _balanced(block, start)
        if end is None:
            continue
        target = block[start + 1:end - 1]
        i, chain, matcher, args = end, [], None, ""
        while True:  # .not .resolves .rejects, then the matcher and its arguments
            mm = re.match(r"\s*\.\s*([A-Za-z_$][\w$]*)", block[i:])
            if not mm:
                break
            name = mm.group(1)
            i += mm.end()
            if re.match(r"\s*\(", block[i:]):
                p = i + block[i:].index("(")
                e = _balanced(block, p)
                if e is None:
                    break
                matcher, args = name, block[p + 1:e - 1]
                i = e
                break
            chain.append(name)
        if not matcher:
            continue
        negated = "not" in chain
        has_args = bool(args.strip())
        loose_args = bool(re.search(r"\bexpect\.(anything|any)\s*\(", args))
        exact = (not negated and not loose_args and
                 (matcher in JS_EXACT or (matcher in JS_NEEDS_ARG and has_args and
                                          (matcher != "toHaveProperty" or args.count(",") >= 1))))
        if negated and matcher in ("toHaveBeenCalled", "toThrow", "toThrowError"):
            exact = True  # "never called", "doesn't throw": as strict as it gets
        tol = None
        if matcher == "toBeCloseTo":
            digits = args.split(",")[1].strip() if "," in args else "2"
            tol = 10 ** -float(digits) / 2 if re.fullmatch(r"\d+", digits) else None
        line = src.count("\n", 0, offset + m.start()) + 1
        text = re.sub(r"\s+", " ", block[m.start():i]).strip()
        expected = _norm(args) if exact else None  # toBe(1200) and toEqual(1200) check the same thing
        out.append((_norm(target), exact, expected, line, text, tol))
    return out


def js_tests(source: str) -> Dict[str, List[Check]]:
    """{test title: its expect() checks, in order} for a Jest or Vitest file."""
    src = _blank_comments(source)
    calls = list(TEST_CALL.finditer(src))
    out: Dict[str, List[Check]] = {}
    for k, m in enumerate(calls):
        end = calls[k + 1].start() if k + 1 < len(calls) else len(src)
        name, n = m.group(2), 2
        while name in out:  # the same title twice: keep both, in order
            name, n = f"{m.group(2)} ({n})", n + 1
        out[name] = _js_checks(src[m.start():end], m.start(), src)
    return out


# ---------- comparing ----------

def _entries(text: Optional[str]) -> Optional[Tuple[str, list]]:
    """An expected value written as a dict, list, tuple or set literal: (its kind, its entries as
    comparable text). None for anything else. JS literals parse too, once normalized."""
    try:
        node = ast.parse(text or "", mode="eval").body
    except (SyntaxError, ValueError):
        return None
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            return None  # {**other}: what it holds isn't written here
        return "dict", [(ast.dump(k), ast.dump(v)) for k, v in zip(node.keys, node.values)]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        if any(isinstance(e, ast.Starred) for e in node.elts):
            return None
        return type(node).__name__.lower(), [ast.dump(e) for e in node.elts]
    return None


def only_gained(before: Optional[str], after: Optional[str]) -> bool:
    """The new expected value keeps every old entry unchanged and adds more: a dict with new keys,
    a list or tuple with new items (the old ones still in order), a set with new members."""
    b, a = _entries(before), _entries(after)
    if not b or not a or b[0] != a[0] or len(a[1]) <= len(b[1]):
        return False
    kind, old, new = b[0], b[1], a[1]
    if kind == "dict":
        return dict(old).items() <= dict(new).items()
    if kind == "set":
        return set(old) <= set(new)
    rest = iter(new)
    return all(any(x == y for y in rest) for x in old)  # a subsequence: the old items, in order


def compare(before: Dict[str, List[Check]], after: Dict[str, List[Check]]) -> List[dict]:
    """What weakened, test by test: [{"test", "kind", "line", "before", "after"}]."""
    out = []
    for name in sorted(before.keys() & after.keys()):
        b, a = before[name], after[name]
        left = list(a)  # the new checks not yet paired with an old one
        for tgt, exact, exp, _, text, tol in b:
            if not exact:
                continue
            same = [c for c in left if c[0] == tgt and c[1]]
            if same:
                c = next((c for c in same if c[2] == exp), same[0])
                left.remove(c)
                if tol is not None and c[5] is not None:  # approximate: what matters is the tolerance
                    if c[5] > tol:
                        out.append({"test": name, "kind": "loosened", "line": c[3], "before": text, "after": c[4]})
                    elif c[5] == tol and c[2] != exp and not only_gained(exp, c[2]):
                        out.append({"test": name, "kind": "rewritten", "line": c[3], "before": text, "after": c[4]})
                elif c[2] != exp and not only_gained(exp, c[2]):
                    out.append({"test": name, "kind": "rewritten", "line": c[3], "before": text, "after": c[4]})
                continue
            looser = [c for c in left if c[0] == tgt and not c[1]]
            if looser:
                left.remove(looser[0])
                out.append({"test": name, "kind": "loosened", "line": looser[0][3], "before": text, "after": looser[0][4]})
        if len(a) < len(b):
            still = {c[0] for c in a}
            gone = [c[4] for c in b if c[0] not in still]
            if gone:
                line = a[0][3] if a else 0
                out.append({"test": name, "kind": "removed", "line": line, "before": "; ".join(gone[:3]) +
                            (f" and {len(gone) - 3} more" if len(gone) > 3 else ""), "after": f"{len(b)} → {len(a)} assertions"})
    return out


def _tests(path: str, source: str) -> Optional[Dict[str, List[Check]]]:
    return py_tests(source) if path.endswith(".py") else js_tests(source)


def is_test(path: str) -> bool:
    return bool(PY_TEST.search(path) or JS_TEST.search(path))


RUNNER_CONFIG = re.compile(
    r"(?:^|/)(?:conftest\.py|pytest\.ini|setup\.cfg|tox\.ini|"
    r"jest\.config\.[cm]?[jt]s|jest\.config\.json|jest\.setup\.[jt]s|setupTests\.[jt]s|"
    r"vitest\.config\.[cm]?[jt]s|vitest\.config\.mts)$",
    re.I
)


def is_runner_config(path: str) -> bool:
    if RUNNER_CONFIG.search(path):
        return True
    p = path.lower()
    if p.endswith("pyproject.toml") or p.endswith("package.json"):
        return True
    if ".github/workflows/" in p and (p.endswith(".yml") or p.endswith(".yaml")):
        return True
    return False


def _parse_diff_lines(diff: str) -> List[int]:
    lines = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                lines.extend(range(start, start + n))
    return sorted(set(lines))


def _spans(lines: List[int]) -> str:
    if not lines:
        return ""
    spans = []
    start = lines[0]
    prev = start
    for x in lines[1:]:
        if x == prev + 1:
            prev = x
        else:
            spans.append(f"{start}-{prev}" if prev > start else f"{start}")
            start = prev = x
    spans.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ", ".join(spans)


def runner_config_matters(path: str, diff_text: str) -> Optional[str]:
    """Returns why the runner config change matters, or None if unrelated."""
    if RUNNER_CONFIG.search(path):
        return "changes test runner setup or fixtures"
    p = path.lower()
    if p.endswith("pyproject.toml"):
        if any(k in diff_text for k in ("pytest", "vitest", "addopts", "testpaths")):
            return "changes pytest or vitest options in pyproject.toml"
        return None
    if p.endswith("package.json"):
        if any(k in diff_text for k in ('"test"', '"jest"', '"vitest"')):
            return "changes test script or jest config in package.json"
        return None
    if ".github/workflows/" in p:
        if any(k in diff_text.lower() for k in ("pytest", "jest", "vitest", "npm test", "yarn test", "assay test")):
            return "changes CI test command in workflow"
        return None
    return None


def scan(root: Path, ref: Optional[str]) -> List[dict]:
    """Assertions weakened and test runner configurations changed in the working tree against ref:
    [{"file", "test", "kind", "line", "before", "after", "related", ...}]. [] when there's no ref or no git."""
    if not ref:
        return []
    base = (_git(root, "merge-base", ref, "HEAD") or "").strip() or ref
    top = (_git(root, "rev-parse", "--show-toplevel") or "").strip()
    diff = _git(root, "diff", "--name-status", "-M", base, "--", ".")  # this project's files, paths from the top
    if diff is None or not top:
        return []
    tests, code, configs = [], [], []
    tracked_configs = set()
    for line in diff.splitlines():
        parts = line.split("\t")
        status, old, new = parts[0][:1], parts[1], parts[-1]
        if status == "D":
            if is_runner_config(old):
                configs.append((status, old, new))
                tracked_configs.add(old)
            continue
        if is_test(new):
            if status in ("M", "R"):
                tests.append((old, new))
        elif is_runner_config(new):
            configs.append((status, old, new))
            tracked_configs.add(new)
        elif CODE.search(new):
            code.append(new)
    for u in (_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines():
        if is_runner_config(u) and u not in tracked_configs:
            configs.append(("?", u, u))
    out = []
    for status, old, new in configs:
        if status == "D":
            out.append({
                "file": old,
                "kind": "runner_config",
                "test": old,
                "line": 1,
                "lines": [],
                "lines_text": "deleted",
                "detail": f"deleted test runner config {old}",
                "before": old,
                "after": "deleted",
                "related": []
            })
            continue
        if status == "?":
            try:
                content = (root / new).read_text(encoding="utf-8", errors="replace")
                lines_count = len(content.splitlines())
            except OSError:
                content = ""
                lines_count = 0
            why = runner_config_matters(new, content)
            if why:
                lines = list(range(1, max(1, lines_count) + 1)) if lines_count else [1]
                lines_str = _spans(lines)
                out.append({
                    "file": new,
                    "kind": "runner_config",
                    "test": new,
                    "line": lines[0] if lines else 1,
                    "lines": lines,
                    "lines_text": lines_str,
                    "detail": why,
                    "before": "untracked",
                    "after": f"new {new}",
                    "related": []
                })
            continue
        hunk = _git(root, "diff", "-U0", base, "--", new) or ""
        why = runner_config_matters(new, hunk)
        if why:
            lines = _parse_diff_lines(hunk)
            lines_str = _spans(lines)
            out.append({
                "file": new,
                "kind": "runner_config",
                "test": new,
                "line": lines[0] if lines else 1,
                "lines": lines,
                "lines_text": lines_str,
                "detail": why,
                "before": f"base:{old}",
                "after": f"modified {new}",
                "related": []
            })
    for old, new in tests:
        before = _git(root, "show", f"{base}:{old}")
        try:
            after = (Path(top) / new).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        b, a = _tests(old, before or ""), _tests(new, after)
        if not b or not a:
            continue
        found = compare(b, a)
        if not found:
            continue
        related = [c for c in code if _mentions(after, c)]
        for f in found:
            out.append({"file": new, **f, "related": related})
    return out


def _mentions(test_source: str, code_path: str) -> bool:
    """Does the test refer to this file? Its module name or its file name (without the extension)."""
    p = Path(code_path)
    stem = p.name.split(".")[0] if p.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs") else p.stem
    names = {re.sub(r"\.[^.]+$", "", p.name), stem}
    if p.suffix == ".py":
        names.add(".".join(p.with_suffix("").parts))
    return any(n and len(n) > 2 and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", test_source) for n in names)


def weakening(found: Iterable[dict]) -> List[dict]:
    """The ones that loosen the checks (a failure until accepted), not the rewritten values."""
    return [f for f in found if f["kind"] in ("loosened", "removed")]


def describe(f: dict) -> str:
    """One line for the report."""
    if f.get("kind") == "runner_config":
        where = f"lines {f['lines_text']}" if f.get("lines_text") else (f"{f['file']}:{f['line']}" if f.get("line") else f["file"])
        return f"changes how tests run in {f['file']} ({where}): {f.get('detail', 'for a person to confirm')}"
    where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
    why = f", while {', '.join(f['related'][:2])} changed" if f.get("related") else ""
    if f["kind"] == "removed":
        return f"removes assertions from {f['test']} ({where}): {f['before']} ({f['after']}){why}"
    if f["kind"] == "loosened":
        return f"loosens an assertion in {f['test']} ({where}): {f['before']} → {f['after']}{why}"
    return f"changes an expected value in {f['test']} ({where}): {f['before']} → {f['after']}{why}"


def guard(root: Path, ref: Optional[str]) -> Tuple[int, str]:
    """`assay guard`: (exit code, report). 1 when a test was weakened or a runner config changed,
    0 when none was, 2 with nothing to compare with."""
    if not ref:
        return 2, ("Nothing to compare with: pass --base <branch>, or set ASSAY_BASE_REF. "
                   "In a pull request, that's the branch it merges into.")
    found = scan(root, ref)
    loose = weakening(found)
    configs = [f for f in found if f["kind"] == "runner_config"]
    notes = [f for f in found if f["kind"] == "rewritten"]
    lines = [f"Compared with {ref} (merge base).", ""]
    if loose:
        lines += [f"{len(loose)} assertion change{'s' * (len(loose) != 1)} weaken{'s' * (len(loose) == 1)} the tests:",
                  *(f"  - {describe(f)}" for f in loose), ""]
    if configs:
        lines += [f"{len(configs)} test runner configuration change{'s' * (len(configs) != 1)} (for a person to confirm):",
                  *(f"  - {describe(f)}" for f in configs), ""]
    if notes:
        lines += [f"{len(notes)} expected value{'s' * (len(notes) != 1)} changed (check they're the change you meant):",
                  *(f"  ~ {describe(f)}" for f in notes), ""]
    if not (loose or configs or notes):
        lines.append("No test was weakened.")
    return (1 if (loose or configs) else 0), "\n".join(lines).rstrip()
