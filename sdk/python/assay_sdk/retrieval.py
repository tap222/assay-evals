"""What retrieval put into the prompt: each fragment, its tokens, and whether it was used.

    run.retrieve("refund policy for damaged items", docs, used=4)    # the top 4 after re-ranking
    run.retrieve(query, [{"id": "kb-12", "text": "...", "score": 0.82, "tokens": 310}], used=["kb-12"])

A fragment is text, a dict (text or content or page_content, id, tokens, score, source), a
LangChain Document, a LlamaIndex NodeWithScore, or a (document, score) pair. used says which
went into the prompt: all of them (None), the first n (an int), or ids or positions (a list).
A fragment without `tokens` is estimated at one token per four characters, and marked so.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, List, Optional, Union

MAX_TEXT = 2000  # characters of a fragment kept (its tokens are counted in full)
MAX_FRAGMENTS = 1000


def estimate_tokens(text: Optional[str]) -> int:
    return math.ceil(len(text) / 4) if text else 0


def _get(x: Any, *names: str):
    for n in names:
        v = x.get(n) if isinstance(x, dict) else getattr(x, n, None)
        if v is not None:
            return v
    return None


def _one(x: Any, i: int, max_text: Optional[int] = MAX_TEXT) -> dict:
    score = None
    if isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], (int, float)):  # (document, score)
        x, score = x
    if isinstance(x, str):
        text, fid, tokens, source = x, None, None, None
    else:
        node = _get(x, "node")  # LlamaIndex NodeWithScore
        if node is not None and not isinstance(x, dict):
            score = score if score is not None else _get(x, "score")
            get = getattr(node, "get_content", None)
            text = get() if callable(get) else _get(node, "text")
            fid, meta = _get(node, "node_id", "id_"), _get(node, "metadata") or {}
            tokens = None
        else:
            text = _get(x, "text", "content", "page_content")
            meta = _get(x, "metadata") or {}
            fid = _get(x, "id")
            tokens = _get(x, "tokens")
            score = score if score is not None else _get(x, "score")
        if not isinstance(meta, dict):
            meta = {}
        fid = fid or meta.get("id") or meta.get("doc_id")
        score = score if score is not None else meta.get("score") or meta.get("relevance_score")
        source = _get(x, "source") if isinstance(x, dict) else None
        source = source or meta.get("source") or meta.get("file_name") or meta.get("url")
        page = _get(x, "page") or meta.get("page") or meta.get("page_number")
        section = _get(x, "section") or meta.get("section") or meta.get("sec")
    text = text if isinstance(text, str) else (None if text is None else str(text))
    out = {"id": str(fid) if fid is not None else str(i), "position": i}
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        out["tokens"] = tokens
    else:
        out["tokens"], out["estimated"] = estimate_tokens(text), True
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        out["score"] = float(score)
    if source:
        out["source"] = str(source)[:512]
    if page is not None:
        out["page"] = int(page) if str(page).isdigit() else str(page)
    if section is not None:
        out["section"] = str(section)[:128]
    if text:
        out["text"] = text if max_text is None else text[:max_text]
    return out


def fragments(items: Optional[Iterable[Any]], used: Union[None, int, List[Any]] = None,
              max_text: Optional[int] = MAX_TEXT) -> List[dict]:
    """The fragments, each {"id", "position", "tokens", "used", "score"?, "source"?, "text"?}. Each
    text is kept to max_text characters, as recorded; None keeps it whole (a judge reading it)."""
    out = [_one(x, i, max_text) for i, x in enumerate(list(items or [])[:MAX_FRAGMENTS])]
    if used is None:
        chosen = None
    elif isinstance(used, bool):
        chosen = set(range(len(out))) if used else set()
    elif isinstance(used, int):
        chosen = set(range(min(used, len(out))))
    else:
        want = {str(u) for u in used if not isinstance(u, int)}
        positions = {u for u in used if isinstance(u, int) and not isinstance(u, bool)}
        chosen = {f["position"] for f in out if f["id"] in want or f["position"] in positions}
    for f in out:
        f["used"] = chosen is None or f["position"] in chosen
    return out


def summary(frags: Optional[List[dict]]) -> dict:
    """{"retrieved", "used", "tokens_retrieved", "tokens_used"} for a retrieval's fragments."""
    frags = [f for f in frags or [] if isinstance(f, dict)]
    used = [f for f in frags if f.get("used", True)]
    tok = lambda fs: sum(f.get("tokens") or 0 for f in fs)
    return {"retrieved": len(frags), "used": len(used), "tokens_retrieved": tok(frags), "tokens_used": tok(used)}
