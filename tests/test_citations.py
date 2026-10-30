"""Citations the answer gives, checked:
A citation (a document, a page, a section) should be checked against the source it names:
that source exists, was retrieved, and says what the sentence citing it says.
A right claim with the wrong page fails as a wrong citation, not as a wrong answer.
"""
from __future__ import annotations

import pytest

import assay_sdk as assay
from assay_sdk.citations import check_citations, extract_citations
from assay_sdk.testing import expect


def test_extract_citations():
    text = (
        "Refunds must be requested within 30 days [refund-policy.md, p. 12]. "
        "Standard shipping takes 3-5 business days [shipping-guide.pdf#page=4]. "
        "For enterprise plans, contact sales (enterprise.md, sec. 2.1). "
        "For support, see [help guide](help.md#p=9)."
    )
    cits = extract_citations(text)
    assert len(cits) == 4

    assert cits[0].source == "refund-policy.md"
    assert cits[0].page == 12
    assert "Refunds must be requested" in cits[0].sentence

    assert cits[1].source == "shipping-guide.pdf"
    assert cits[1].page == 4

    assert cits[2].source == "enterprise.md"
    assert cits[2].section == "2.1"

    assert cits[3].source == "help.md"
    assert cits[3].page == 9


def test_valid_citation_passes():
    docs = [
        {"id": "doc-1", "source": "refund-policy.md", "page": 12,
         "text": "All refund requests must be filed within 30 days of purchase for a full refund."}
    ]
    with assay.run("rag_run") as r:
        r.retrieve("What is the refund policy?", docs)
        r.answer("Refunds must be requested within 30 days [refund-policy.md, p. 12].")

    expect(r).citations_valid(require_citations=True).verify()


def test_wrong_page_fails_as_wrong_citation_not_wrong_answer():
    # The claim is true, but the citation names page 14 instead of page 4
    docs = [
        {"id": "doc-1", "source": "policy.pdf", "page": 4,
         "text": "Annual leave entitlement is 25 days per calendar year."}
    ]
    with assay.run("hr_rag") as r:
        r.retrieve("How many days of annual leave do I get?", docs)
        r.answer("You are entitled to 25 days of annual leave [policy.pdf, page 14].")

    failures = expect(r).citations_valid().failures()
    assert len(failures) == 1
    assert "Wrong citation" in failures[0]
    assert "cited page 14 of 'policy.pdf', but the evidence is from page 4" in failures[0]

    with pytest.raises(AssertionError, match="cited page 14 of 'policy.pdf', but the evidence is from page 4"):
        expect(r).citations_valid().verify()


def test_source_not_retrieved_fails():
    docs = [
        {"id": "doc-1", "source": "faq.md", "text": "Accounts can be created online."}
    ]
    with assay.run("rag_run") as r:
        r.retrieve("Can I delete my account?", docs)
        # Citations cites secret-policy.md which was never retrieved
        r.answer("Accounts cannot be deleted directly [secret-policy.md].")

    failures = expect(r).citations_valid().failures()
    assert len(failures) == 1
    assert "Cited source 'secret-policy.md' was never retrieved" in failures[0]


def test_unsupported_claim_fails():
    # The document exists and was retrieved, but says nothing about the cited claim
    docs = [
        {"id": "doc-1", "source": "warranty.md", "page": 2,
         "text": "The laptop hardware is covered against manufacturing defects for one year."}
    ]
    with assay.run("support_run") as r:
        r.retrieve("Are software bugs covered?", docs)
        # Fabricated claim attributed to warranty.md
        r.answer("Software bugs and user damage are 100% replaced with new models [warranty.md, p. 2].")

    failures = expect(r).citations_valid().failures()
    assert len(failures) == 1
    assert "does not mention or support" in failures[0] or "does not support" in failures[0]


def test_require_citations_flag():
    with assay.run("no_citations_run") as r:
        r.answer("The warranty lasts two years.")

    failures = expect(r).citations_valid(require_citations=True).failures()
    assert len(failures) == 1
    assert "The answer gave no citations." in failures[0]

    # Without require_citations=True, an answer with no citations simply passes
    assert expect(r).citations_valid(require_citations=False).failures() == []
