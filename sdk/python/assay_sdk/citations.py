"""Check citations the answer gives against retrieved evidence:
A citation (a document, a page, a section) should be checked against the source it names:
  1. The cited source exists and was retrieved.
  2. The cited metadata (page, section) matches the retrieved source.
  3. The cited source says what the sentence citing it says.

A right claim with the wrong page fails as a wrong citation, not as a wrong answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

STOP_WORDS = {
    "the", "and", "that", "this", "with", "from", "for", "are", "was", "were",
    "will", "have", "has", "had", "they", "our", "you", "your", "its", "not",
    "but", "about", "into", "over", "after", "been", "also", "their", "which"
}


@dataclass
class Citation:
    raw: str
    source: str
    page: Optional[Union[int, str]] = None
    section: Optional[str] = None
    sentence: str = ""


@dataclass
class CitationCheckResult:
    citation: Citation
    valid: bool
    kind: str  # "ok", "source_not_retrieved", "wrong_page", "wrong_section", "unsupported_claim"
    reason: Optional[str] = None


def _clean_source_name(s: str) -> str:
    s = s.strip()
    if s.lower().startswith("source:"):
        s = s[7:].strip()
    elif s.lower().startswith("source"):
        s = s[6:].strip()
    if "#" in s:
        s = s.split("#", 1)[0]
    return s.strip().rstrip(",")


def _parse_citation_interior(interior: str, raw: str, sentence: str) -> Optional[Citation]:
    text = interior.strip()
    if not text or len(text) > 200:
        return None

    # Check for page or section inside
    page = None
    section = None

    # Page patterns: p. 12, page 12, p 12, #page=12, #p=12
    p_match = re.search(r'(?:#|\b|\()(?:page|p)\.?\s*[=:]?\s*(\d+)', text, re.I)
    if p_match:
        page = int(p_match.group(1))

    # Section patterns: section 3.2, sec. 3.2, #section=3.2
    s_match = re.search(r'(?:#|\b|\()(?:section|sec)\.?\s*[=:]?\s*([a-zA-Z0-9._-]+)', text, re.I)
    if s_match:
        section = s_match.group(1)

    # The source name is typically before the comma or page/section reference
    base = text.split(",")[0]
    # Remove #page / #section from base
    base = _clean_source_name(base)
    if not base:
        return None

    return Citation(raw=raw, source=base, page=page, section=section, sentence=sentence)


def _surrounding_sentence(text: str, start_pos: int, end_pos: int) -> str:
    start = 0
    for i in range(start_pos - 1, -1, -1):
        if text[i] == "\n":
            start = i + 1
            break
        if text[i] in ".!?" and (i == 0 or not text[i - 1].isdigit()):
            word_before = text[max(0, i - 4):i].lower()
            if not any(word_before.endswith(abbrev) for abbrev in ("p", "sec", "vol", "no", "eg", "ie")):
                start = i + 1
                break
    end = len(text)
    for i in range(end_pos, len(text)):
        if text[i] == "\n":
            end = i
            break
        if text[i] in ".!?":
            end = i + 1
            break
    return text[start:end].strip()


def extract_citations(text: Optional[str]) -> List[Citation]:
    """Extract inline citations from answer text, in order of occurrence."""
    if not text:
        return []

    citations_with_pos: List[Tuple[int, Citation]] = []
    bracket_pat = re.compile(r'\[([^\]]+)\]')
    paren_pat = re.compile(r'\(((?:[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+|doc-\d+|kb-\d+)[^)]*)\)', re.I)

    # 1. Bracket citations: [refund-policy.md, p. 12] or [doc-1] or [markdown link](url)
    for m in bracket_pat.finditer(text):
        raw = m.group(0)
        interior = m.group(1).strip()
        # Markdown link check: [label](url)
        if text[m.end():m.end() + 1] == "(":
            link_match = re.match(r'^\(([^)]+)\)', text[m.end():])
            if link_match:
                url = link_match.group(1)
                sentence = _surrounding_sentence(text, m.start(), m.end() + len(link_match.group(0)))
                c = _parse_citation_interior(url, f"[{interior}]({url})", sentence)
                if c:
                    citations_with_pos.append((m.start(), c))
                continue
        sentence = _surrounding_sentence(text, m.start(), m.end())
        c = _parse_citation_interior(interior, raw, sentence)
        if c:
            citations_with_pos.append((m.start(), c))

    # 2. Parenthetical citations: (refund-policy.md, p. 12)
    for m in paren_pat.finditer(text):
        if m.start() > 0 and text[m.start() - 1] == "]":
            continue  # part of markdown link [label](url)
        raw = m.group(0)
        interior = m.group(1).strip()
        sentence = _surrounding_sentence(text, m.start(), m.end())
        c = _parse_citation_interior(interior, raw, sentence)
        if c and not any(existing.raw == raw and existing.sentence == sentence for _, existing in citations_with_pos):
            citations_with_pos.append((m.start(), c))

    citations_with_pos.sort(key=lambda x: x[0])
    return [c for _, c in citations_with_pos]


def _extract_distinctive_words(sentence: str, raw_citation: str) -> Set[str]:
    # Remove citation markup
    cleaned = sentence.replace(raw_citation, " ")
    # Extract words
    words = re.findall(r'\b[a-zA-Z0-9_$%-]+\b', cleaned.lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS and not w.isdigit()}


def _match_source_to_fragment(citation_source: str, frag: dict) -> bool:
    cs = citation_source.lower()
    fid = str(frag.get("id") or "").lower()
    fsrc = str(frag.get("source") or "").lower()

    # Exact match on id or source
    if cs == fid or cs == fsrc:
        return True

    # Numeric match (e.g. [1] matching position 0 or id "1")
    if cs.isdigit():
        if fid == cs or str(frag.get("position", -1) + 1) == cs or str(frag.get("position", -1)) == cs:
            return True

    # Substring / basename match
    if cs in fsrc or fsrc.endswith("/" + cs) or fsrc.endswith("\\" + cs):
        return True
    if fsrc and fsrc in cs:
        return True

    return False


def check_citations(answer: Optional[str], fragments: List[dict]) -> List[CitationCheckResult]:
    """Check every citation in the answer against retrieved fragments."""
    citations = extract_citations(answer)
    results: List[CitationCheckResult] = []

    for c in citations:
        matching = [f for f in fragments if _match_source_to_fragment(c.source, f)]
        if not matching:
            results.append(CitationCheckResult(
                citation=c,
                valid=False,
                kind="source_not_retrieved",
                reason=f"Cited source {c.source!r} was never retrieved."
            ))
            continue

        # Check page metadata if cited
        if c.page is not None:
            pages = [f.get("page") for f in matching if f.get("page") is not None]
            if pages:
                # Compare page
                norm_cited = int(c.page) if str(c.page).isdigit() else str(c.page)
                norm_pages = [int(p) if str(p).isdigit() else str(p) for p in pages]
                if norm_cited not in norm_pages:
                    results.append(CitationCheckResult(
                        citation=c,
                        valid=False,
                        kind="wrong_page",
                        reason=f"Wrong citation: cited page {c.page} of {c.source!r}, but the evidence is from page {pages[0]}."
                    ))
                    continue

        # Check section metadata if cited
        if c.section is not None:
            sections = [str(f.get("section")).lower() for f in matching if f.get("section")]
            if sections and str(c.section).lower() not in sections:
                results.append(CitationCheckResult(
                    citation=c,
                    valid=False,
                    kind="wrong_section",
                    reason=f"Wrong citation: cited section {c.section!r} of {c.source!r}, but the evidence is from section {sections[0]!r}."
                ))
                continue

        # Check claim support: does the cited source say what the sentence says?
        words = _extract_distinctive_words(c.sentence, c.raw)
        if words:
            all_text = " ".join(str(f.get("text") or "").lower() for f in matching)
            # Numbers or specific quantities in the sentence should be present
            numbers = re.findall(r'\b\d+(?:\.\d+)?%?\b', c.sentence.replace(c.raw, ""))
            missing_numbers = [num for num in numbers if num not in all_text]

            matched_words = {w for w in words if w in all_text}
            overlap = len(matched_words) / len(words) if words else 1.0

            if overlap < 0.25 and len(matched_words) < 2:
                results.append(CitationCheckResult(
                    citation=c,
                    valid=False,
                    kind="unsupported_claim",
                    reason=f"Wrong citation: {c.source!r} does not mention or support what the sentence says: {c.sentence[:120]!r}."
                ))
                continue

            if missing_numbers and overlap < 0.5:
                results.append(CitationCheckResult(
                    citation=c,
                    valid=False,
                    kind="unsupported_claim",
                    reason=f"Wrong citation: {c.source!r} does not support {missing_numbers[0]!r} in {c.sentence[:120]!r}."
                ))
                continue

        # Citation is valid
        results.append(CitationCheckResult(
            citation=c,
            valid=True,
            kind="ok"
        ))

    return results
