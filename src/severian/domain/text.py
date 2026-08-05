"""Pure text algorithms shared across the application layer (ARCH-03).

The 5-D boundary: application code must not reach into
``severian.infrastructure`` for algorithm constants or stemmers. These
are domain-level text primitives — no I/O, no storage, no adapter — so
they live here and infrastructure re-exports them for backward
compatibility.
"""

from __future__ import annotations

# Shared English stopword set used by FTS/vector tokenization and the
# MinHash-LSH signature builder. Kept in the domain so retrieval,
# dreaming and derivation agree on one tokenization vocabulary without
# importing infrastructure.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have",
        "how", "in", "into", "is", "it", "its", "no", "not", "of", "on",
        "or", "should", "that", "the", "their", "these", "they", "this",
        "those", "to", "was", "we", "were", "what", "when", "where",
        "which", "who", "whose", "will", "with", "would", "you",
    }
)


def porter_stem(word: str) -> str:
    """Porter stemmer (pure stdlib, 113/116 FTS5-porter agreement).

    Re-exported from ``severian.infrastructure.stemming`` to keep the
    canonical implementation in one place while giving application code
    a domain-level import that does not cross the infrastructure
    boundary.
    """
    from severian.infrastructure.stemming import porter_stem as _impl

    return _impl(word)
