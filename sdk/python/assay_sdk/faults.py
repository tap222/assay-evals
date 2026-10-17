"""Breaking tools on purpose, to test how an agent handles it.

    with assay.faults(get_order="empty", search="error", calendar="timeout", pay="error:1"):
        reply = my_agent("Cancel O-17", run=assay_case)
    expect(assay_case).handles_failure()

For each named tool (recorded with @assay.tool or run.call), the fault replaces the call:
  "empty"      returns an empty result ([]), without calling the tool
  "error"      raises ToolFault, recorded as the tool's error
  "timeout"    raises TimeoutError
  "error:N"    fails the first N calls, then calls the tool for real: does the agent retry?
  "lost"       calls the tool for real, then raises TimeoutError: the payment went through, but the
               reply never arrived. Does the agent pay again? (expect(...).at_most_once("pay"))
  {"return": value}   returns value instead, e.g. {"return": {"status": "unknown"}}
  {"returns": [a, b]}  one value per call, the last one from then on: the world changing while the
                       agent works. REAL in the list calls the tool for real

The invoice stays approved, and the supplier goes on hold between the agent reading it and paying:

    with assay.faults(get_supplier={"returns": [assay.REAL, {"status": "on_hold"}]}):
        my_agent("Pay invoice 17", run=assay_case)
    assert_not_called(assay_case, "pay")   # it read the supplier again before paying, and stopped

Recorded steps say which fault they were (fault="error"; "return" for a value put in place of the
tool's), so evaluation knows it was injected.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Tuple


class ToolFault(Exception):
    """An error injected by assay.faults()."""


class _Real:
    """In {"returns": [...]}: call the tool for real."""

    def __repr__(self):
        return "assay.REAL"


REAL = _Real()


_active: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("assay_faults", default=None)


@contextmanager
def faults(**spec):
    for name, v in spec.items():
        ok = isinstance(v, str) and (v in ("empty", "error", "timeout", "lost") or (v.startswith("error:") and v[6:].isdigit())) \
            or (isinstance(v, dict) and ("return" in v or (isinstance(v.get("returns"), list) and v["returns"])))
        if not ok:
            raise ValueError(f"faults({name}=...): empty, error, timeout, lost, error:N, {{'return': value}}, "
                             "or {'returns': [value, ...]}")
    state = {"spec": dict(spec), "calls": {}}
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)


def call(name: str, fn: Callable, args: tuple, kwargs: dict) -> Tuple[Any, Optional[str]]:
    """(result, the fault applied or None). Raises what the fault raises."""
    state = _active.get()
    spec = state["spec"].get(name) if state else None
    if spec is None:
        return fn(*args, **kwargs), None
    n = state["calls"][name] = state["calls"].get(name, 0) + 1
    if isinstance(spec, dict) and "returns" in spec:
        out = spec["returns"][min(n, len(spec["returns"])) - 1]
        return (fn(*args, **kwargs), None) if out is REAL else (out, "return")
    if isinstance(spec, dict):
        return spec["return"], "return"
    if spec == "empty":
        return [], "empty"
    if spec == "lost":
        fn(*args, **kwargs)
        raise _tag(TimeoutError(f"{name} timed out after it ran (injected by assay.faults)"), "lost")
    if spec == "timeout":
        raise _tag(TimeoutError(f"{name} timed out (injected by assay.faults)"), "timeout")
    if spec == "error" or (spec.startswith("error:") and n <= int(spec[6:])):
        raise _tag(ToolFault(f"{name} failed (injected by assay.faults)"), "error")
    return fn(*args, **kwargs), None


def _tag(exc: BaseException, kind: str) -> BaseException:
    exc.assay_fault = kind
    return exc


def of(exc: Optional[BaseException]) -> Optional[str]:
    return getattr(exc, "assay_fault", None) if exc is not None else None
