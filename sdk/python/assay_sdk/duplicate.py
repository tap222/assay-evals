"""Simulating duplicate request delivery (double click, webhook delivered twice) and verifying idempotency.

    from assay_sdk import duplicate, expect

    def test_payment_double_click(assay_case):
        res = duplicate(handle_payment_webhook, payload, run=assay_case, key="order_id")
        res.verify()

The second delivery must:
  1. Change nothing: no repeated side-effecting tool calls (charging cards, sending emails, database writes).
  2. Answer success to the sender: returning an HTTP 409 Conflict causes the sender to retry, piling up retries.
  3. Be recorded as a duplicate: the second run is tagged with duplicate=True and original_run_id.
"""
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import assay_sdk as assay

MUTATING_VERBS = (
    "pay", "charge", "refund", "send", "email", "post", "write", "insert",
    "update", "delete", "drop", "create", "book", "cancel", "transfer", "mutate", "bill"
)

READ_VERBS = ("get", "read", "fetch", "search", "lookup", "find", "check", "list")


def _is_mutating_tool(name: str) -> bool:
    n = name.lower()
    if any(n.startswith(r) or f"_{r}" in n for r in READ_VERBS):
        return False
    return any(n.startswith(v) or f"_{v}" in n or f"{v}_" in n for v in MUTATING_VERBS)


def _format_args(args: Optional[dict]) -> str:
    if not args:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def check_duplicate_safety(first_run: Any, duplicate_run: Any, duplicate_result: Any = None,
                           duplicate_error: Optional[BaseException] = None,
                           key: Optional[str] = None) -> List[str]:
    """Check that the duplicate delivery satisfies the roadmap rules:
    - Change nothing (no duplicate side effects or tool calls with key)
    - Still answer success to sender (no 409 Conflict, no errors)
    - Be recorded as a duplicate
    """
    failures: List[str] = []

    # 1. Be recorded as a duplicate
    if not getattr(duplicate_run, "duplicate", False):
        failures.append("The second delivery was not recorded as a duplicate.")

    # 2. Still answer success to the sender (a 409 makes the sender retry, and retries pile up)
    if duplicate_error is not None:
        err_str = str(duplicate_error)
        err_name = type(duplicate_error).__name__
        status_code = getattr(duplicate_error, "status_code", None)
        if status_code == 409 or "409" in err_str or "conflict" in err_name.lower():
            failures.append("The duplicate delivery returned status 409 (Conflict). "
                            "A 409 makes the sender retry, and retries pile up. It should answer success to the sender.")
        else:
            failures.append(f"The duplicate delivery failed with {err_name}: {duplicate_error}. "
                            "It should answer success to the sender.")
    elif duplicate_result is not None:
        status = getattr(duplicate_result, "status_code", None)
        if status is None and isinstance(duplicate_result, dict):
            status = duplicate_result.get("status_code") or duplicate_result.get("status")
        if status in (409, "409", "conflict", "Conflict"):
            failures.append("The duplicate delivery returned status 409 (Conflict). "
                            "A 409 makes the sender retry, and retries pile up. It should answer success to the sender.")
        elif isinstance(status, int) and status >= 400:
            failures.append(f"The duplicate delivery returned HTTP {status}. "
                            "It should answer success to the sender.")
        elif isinstance(duplicate_result, dict) and duplicate_result.get("error") and not duplicate_result.get("ok", True):
            failures.append(f"The duplicate delivery answered error: {duplicate_result['error']!r}. "
                            "It should answer success to the sender.")

    # 3. Change nothing (side effects / idempotency)
    first_tools = [s for s in getattr(first_run, "steps", []) if s.get("kind") == "tool"]
    dup_tools = [s for s in getattr(duplicate_run, "steps", []) if s.get("kind") == "tool"]

    for c in dup_tools:
        name = c.get("name") or "tool"
        args = c.get("args") or {}
        # If key is specified, check if any first call used that key and this call repeated it
        if key is not None:
            k_val = args.get(key)
            if k_val is not None:
                match = any((f.get("args") or {}).get(key) == k_val and f.get("name") == name for f in first_tools)
                if match:
                    failures.append(f"{name} was called again with {key}={k_val!r} (step {c.get('seq')}); "
                                    f"duplicate delivery should change nothing.")
                    continue

        # Check mutating verbs
        if _is_mutating_tool(name):
            failures.append(f"The duplicate delivery called side-effecting tool {name}({_format_args(args)}) "
                            f"(step {c.get('seq')}); duplicate delivery should change nothing.")
        else:
            # Check identical repeated call
            same_call = any(f.get("name") == name and f.get("args") == args for f in first_tools)
            if same_call and not any(name.lower().startswith(r) for r in READ_VERBS):
                failures.append(f"The duplicate delivery repeated {name}({_format_args(args)}) (step {c.get('seq')}); "
                                f"duplicate delivery should change nothing.")

    # State mutations recorded
    dup_states = [s for s in getattr(duplicate_run, "steps", []) if s.get("kind") == "state"]
    if dup_states:
        for s in dup_states:
            failures.append(f"The duplicate delivery recorded state change {s.get('name')!r} (step {s.get('seq')}); "
                            f"duplicate delivery should change nothing.")

    return failures


@dataclass
class DuplicateResult:
    """The result of simulating a request sent twice."""
    first: Any
    second: Any
    first_run: Any
    duplicate_run: Any
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.failures) == 0

    def verify(self) -> None:
        """Raise AssertionError if duplicate delivery did not meet expectations."""
        __tracebackhide__ = True
        if self.failures:
            raise AssertionError("Duplicate delivery failed expectations:\n  " + "\n  ".join(self.failures))

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"<DuplicateResult ok={self.ok} failures={len(self.failures)}>"


def duplicate(fn: Callable, *args: Any, run: Optional[Any] = None, key: Optional[str] = None,
              task: Optional[str] = None, **kwargs: Any) -> DuplicateResult:
    """Simulate the same request sent twice (e.g. client double-click or webhook retried by sender).

    The first delivery executes `fn(*args, **kwargs)` normally.
    The second delivery executes `fn(*args, **kwargs)` in a duplicate run context.

    Verifies:
      - The second delivery changes nothing (no repeated charges, payments, mutations).
      - The second delivery answers success to sender (not 409 Conflict, which causes retry storms).
      - The second delivery is recorded as a duplicate.
    """
    if inspect.iscoroutinefunction(fn):
        raise TypeError("duplicate() was given an async coroutine function. Use 'await assay.aduplicate(...)' instead.")

    own_first = run is None
    req_input = args[0] if args else kwargs.get("input")
    task_name = task or (getattr(run, "task", None) if run else None) or "delivery"

    first_ctx = assay.run(task=task_name, input=req_input) if own_first else None
    first_run = first_ctx.__enter__() if own_first else run

    first_err = None
    first_result = None
    try:
        first_result = fn(*args, **kwargs)
    except BaseException as e:
        first_err = e
        if own_first:
            first_ctx.__exit__(*sys.exc_info())
        raise
    else:
        if own_first:
            first_ctx.__exit__(None, None, None)

    # Delivery 2 (Duplicate delivery)
    dup_ctx = assay.run(
        task=task_name,
        input=req_input,
        test=getattr(first_run, "test", None),
        duplicate=True,
        original_run_id=first_run.id
    )
    dup_run = dup_ctx.__enter__()
    dup_err = None
    dup_result = None
    try:
        dup_result = fn(*args, **kwargs)
    except BaseException as e:
        dup_err = e
        dup_ctx.__exit__(*sys.exc_info())
    else:
        dup_ctx.__exit__(None, None, None)

    failures = check_duplicate_safety(first_run, dup_run, duplicate_result=dup_result,
                                      duplicate_error=dup_err, key=key)

    if getattr(first_run, "test", None):
        first_run.check("expect.duplicate_safe()", "pass" if not failures else "fail",
                        evaluator="assay.duplicate@1", reason="; ".join(failures) if failures else None)

    return DuplicateResult(
        first=first_result,
        second=dup_result,
        first_run=first_run,
        duplicate_run=dup_run,
        failures=failures
    )


async def aduplicate(fn: Callable, *args: Any, run: Optional[Any] = None, key: Optional[str] = None,
                     task: Optional[str] = None, **kwargs: Any) -> DuplicateResult:
    """Async version of duplicate() for coroutine functions."""
    own_first = run is None
    req_input = args[0] if args else kwargs.get("input")
    task_name = task or (getattr(run, "task", None) if run else None) or "delivery"

    first_ctx = assay.run(task=task_name, input=req_input) if own_first else None
    first_run = first_ctx.__enter__() if own_first else run

    first_err = None
    first_result = None
    try:
        first_result = await fn(*args, **kwargs)
    except BaseException as e:
        first_err = e
        if own_first:
            first_ctx.__exit__(*sys.exc_info())
        raise
    else:
        if own_first:
            first_ctx.__exit__(None, None, None)

    # Delivery 2 (Duplicate delivery)
    dup_ctx = assay.run(
        task=task_name,
        input=req_input,
        test=getattr(first_run, "test", None),
        duplicate=True,
        original_run_id=first_run.id
    )
    dup_run = dup_ctx.__enter__()
    dup_err = None
    dup_result = None
    try:
        dup_result = await fn(*args, **kwargs)
    except BaseException as e:
        dup_err = e
        dup_ctx.__exit__(*sys.exc_info())
    else:
        dup_ctx.__exit__(None, None, None)

    failures = check_duplicate_safety(first_run, dup_run, duplicate_result=dup_result,
                                      duplicate_error=dup_err, key=key)

    if getattr(first_run, "test", None):
        first_run.check("expect.duplicate_safe()", "pass" if not failures else "fail",
                        evaluator="assay.duplicate@1", reason="; ".join(failures) if failures else None)

    return DuplicateResult(
        first=first_result,
        second=dup_result,
        first_run=first_run,
        duplicate_run=dup_run,
        failures=failures
    )
