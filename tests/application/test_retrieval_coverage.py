"""Tests for query-coverage abstention (retrieval.py).

The abstention floor is the principled mechanism that stops lexical
token-overlap from fabricating answers to unanswerable probes:
- a negative query ("salary of the new CFO" with no supporting doc)
  must return NO evidence, even when a decoy shares one generic token
  ("new").
- a genuine query must survive even when its best hit is weak-but-real
  (shares 2+ query tokens).
- a stale-fact leak (a stale doc sharing one rare token like
  "project") must be dropped from the results.
"""

from __future__ import annotations

from severian.application.retrieval import query_coverage, query_tokens


def test_query_tokens_filters_stopwords_and_stems() -> None:
    tokens = query_tokens("How is authentication enforced on inbound requests?")
    assert "authent" in tokens  # stemmed from authentication
    assert "request" in tokens  # stemmed from requests
    assert "how" not in tokens  # stopword
    assert "is" not in tokens  # stopword
    assert "on" not in tokens  # stopword


def test_query_coverage_genuine_high() -> None:
    # Genuine evidence shares 2+ query tokens.
    cov = query_coverage(
        "When is Project Chrysalis launching?",
        "Project Chrysalis launches on 2026-09-01.",
    )
    assert cov >= 0.4
    # The launch doc shares 'project' (stem) + 'chrysalis' + 'launch'
    assert cov >= 0.5


def test_query_coverage_decoy_low() -> None:
    # Lexical decoy: shares only 'new' with the query.
    cov = query_coverage(
        "What is the salary of the new CFO?",
        "The headcount target for Q3 is twelve new engineers.",
    )
    assert cov < 0.4


def test_query_coverage_single_rare_token_low() -> None:
    # Stale-fact false positive: shares only 'project' with the query.
    cov = query_coverage(
        "When is Project Chrysalis launching?",
        "The old role of the project lead was software engineer.",
    )
    assert cov < 0.4


def test_query_coverage_empty_query() -> None:
    assert query_coverage("", "anything") == 0.0
    assert query_coverage("   ", "anything") == 0.0
    # Stopword-only query has no meaningful tokens.
    assert query_coverage("the and of", "the and of or") == 0.0


def test_query_coverage_matches_sqlite_stemming() -> None:
    # The corpus-critical inflection: authentication ~ authenticated.
    # Both stem to 'authent', so a doc containing 'authenticated' covers
    # a query containing 'authentication'.
    cov = query_coverage(
        "How is authentication enforced?",
        "Every incoming request is authenticated before it reaches the handler.",
    )
    assert cov > 0.0
