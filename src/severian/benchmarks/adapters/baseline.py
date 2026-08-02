"""Lexical baseline adapter.

Pure-python bag-of-words retrieval over the ingested corpus. No vector
store, no FTS index, no scope isolation — it is the "what does a naive
keyword matcher do" baseline that shows the value of the platform's
projection, scoping and supersession machinery. Latency and quality are
reported on equal footing with the other providers.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from severian.benchmarks.runner import ProviderQueryResponse

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


class LexicalBaselineAdapter:
    """Naive TF (term-frequency) retrieval, per scope."""

    def __init__(self, *, provider_id: str) -> None:
        self.provider_id = provider_id
        # scope -> {document_id: Counter}
        self._corpus: dict[str, dict[str, Counter[str]]] = {}

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        # A naive keyword matcher has no supersession machinery. The
        # stale-fact gate is exactly what this lane is expected to fail.
        self._corpus.setdefault(scope, {})[document_id] = _tokens(content)

    def query(self, query: str, *, limit: int, scope: str) -> ProviderQueryResponse:
        query_tokens = _tokens(query)
        docs = self._corpus.get(scope, {})
        scores: list[tuple[float, str]] = []
        for document_id, tokens in docs.items():
            # Unnormalised term-frequency dot product (no IDF, no length
            # normalisation) — deliberately naive.
            score = sum(
                count * tokens.get(token, 0)
                for token, count in query_tokens.items()
            )
            if score > 0:
                scores.append((score, document_id))
        scores.sort(key=lambda pair: (-pair[0], pair[1]))
        items = [
            {
                "document_id": document_id,
                "citation": document_id,
                "score": score,
            }
            for score, document_id in scores[:limit]
        ]
        return ProviderQueryResponse(items=items, cost_usd=0.0)

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend": "pure-python-bag-of-words",
            "model": "tf-dot-product",
            "prompt_version": "n/a",
            "notes": "baseline; no scope isolation beyond per-scope map",
        }
