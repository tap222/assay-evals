"""Capture, templatize, and analyze errors and warnings logged during test execution.

Turns log messages into templates with IDs, numbers, and PII removed, e.g.:
  "user 1234 not found" -> "user {id} not found"
  "user 5678 not found" -> "user {id} not found"
"""
from __future__ import annotations

import logging
import re
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple


# Regexes for parameterizing IDs, numbers, and PII into canonical templates
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"https?://[^\s/$.?#].[^\s]*")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Entity identifiers like "user 1234", "order O-17", "account_id: 99", "file /tmp/abc"
_ENTITY_ID_RE = re.compile(
    r"\b(user|customer|order|account|invoice|session|tenant|job|task|item|id|file)[-_:\s#]+([a-zA-Z0-9_-]+)\b",
    re.I
)

# Generic numeric tokens with optional units (30s, 100ms, 5MB)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|d|kb|mb|gb|%|px)?\b", re.I)
_WHITESPACE_RE = re.compile(r"\s+")


def template_log(message: str) -> str:
    """Turn a log or warning message into a canonical template by stripping IDs, numbers, and PII.

    >>> template_log("user 1234 not found")
    'user {id} not found'
    >>> template_log("user 5678 not found")
    'user {id} not found'
    """
    if not message:
        return ""
    text = str(message)
    # 1. Normalize high-entropy PII and technical tokens
    text = _UUID_RE.sub("{uuid}", text)
    text = _HEX_RE.sub("{hex}", text)
    text = _EMAIL_RE.sub("{email}", text)
    text = _URL_RE.sub("{url}", text)
    text = _IP_RE.sub("{ip}", text)
    text = _TIMESTAMP_RE.sub("{timestamp}", text)
    text = _DATE_RE.sub("{date}", text)

    # 2. Normalize named entities (user 1234 -> user {id})
    text = _ENTITY_ID_RE.sub(r"\1 {id}", text)

    # 3. Normalize remaining numbers and unit tokens
    def _num_repl(m):
        u = re.search(r"[a-zA-Z%]+$", m.group(0))
        return f"{{n}}{u.group(0)}" if u else "{n}"

    text = _NUMBER_RE.sub(_num_repl, text)

    # 4. Collapse whitespace
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


@dataclass
class LoggedError:
    level: str  # "WARNING", "ERROR", "CRITICAL"
    message: str
    template: str
    logger: str = ""
    file: Optional[str] = None
    line: Optional[int] = None
    function: Optional[str] = None
    timestamp: Optional[datetime] = None
    exception: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "template": self.template,
            "logger": self.logger,
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "exception": self.exception,
        }


class _LogCaptureHandler(logging.Handler):
    def __init__(self, target: List[LoggedError], min_level: int = logging.WARNING):
        super().__init__(level=min_level)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            tmpl = template_log(msg)
            exc = None
            if record.exc_text:
                exc = record.exc_text
            elif record.exc_info and record.exc_info[1]:
                exc = str(record.exc_info[1])
            entry = LoggedError(
                level=record.levelname,
                message=msg,
                template=tmpl,
                logger=record.name or "",
                file=record.pathname,
                line=record.lineno,
                function=record.funcName or "",
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc),
                exception=exc,
            )
            self.target.append(entry)
        except Exception:
            pass


@contextmanager
def capture_logs(run: Optional[Any] = None, min_level: int = logging.WARNING):
    """Context manager to intercept errors and warnings during test execution and record them."""
    captured: List[LoggedError] = []
    handler = _LogCaptureHandler(captured, min_level=min_level)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always")
        try:
            yield captured
        finally:
            try:
                root_logger.removeHandler(handler)
            except Exception:
                pass
            for w in recorded_warnings:
                msg = str(w.message)
                entry = LoggedError(
                    level="WARNING",
                    message=msg,
                    template=template_log(msg),
                    logger=w.category.__name__ if hasattr(w, "category") else "UserWarning",
                    file=w.filename if hasattr(w, "filename") else None,
                    line=w.lineno if hasattr(w, "lineno") else None,
                    function="",
                    timestamp=datetime.now(timezone.utc),
                )
                captured.append(entry)

            if run is not None:
                for item in captured:
                    if hasattr(run, "log"):
                        run.log(
                            level=item.level,
                            message=item.message,
                            logger=item.logger,
                            file=item.file,
                            line=item.line,
                            function=item.function,
                        )
                    elif hasattr(run, "logged_errors"):
                        run.logged_errors.append(item.to_dict())
