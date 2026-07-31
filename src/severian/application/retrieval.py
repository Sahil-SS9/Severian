from __future__ import annotations

from dataclasses import dataclass

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import LocalVectorIndex


@dataclass(frozen=True)
class RetrievalResult:
    memory: Memory
    source_observation_ids: list[str]
    lexical_score: float
    vector_score: float
    fused_score: float


class RetrievalService:
    def __init__(self, repository: CanonicalRepository, lexical: FTS5Index, vector: LocalVectorIndex):
        self.repository = repository
        self.lexical = lexical
        self.vector = vector

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[RetrievalResult]:
        if not query.strip() or not FTS5Index.is_searchable(query):
            return []
        lexical_hits = self.lexical.search(scope, query, limit=max(limit, 50))
        vector_hits = self.vector.search(scope, query, limit=max(limit, 50))
        lexical_by_id = {hit.memory_id: hit for hit in lexical_hits}
        vector_by_id = {hit.memory_id: hit for hit in vector_hits}
        ids = set(lexical_by_id) | set(vector_by_id)
        ranked: list[RetrievalResult] = []
        for memory_id in ids:
            memory = self.repository.get_memory(scope, memory_id)
            lexical_rank = next(
                (position for position, hit in enumerate(lexical_hits, start=1) if hit.memory_id == memory_id),
                None,
            )
            vector_rank = next(
                (position for position, hit in enumerate(vector_hits, start=1) if hit.memory_id == memory_id),
                None,
            )
            fused = (1.0 / (60 + lexical_rank) if lexical_rank else 0.0) + (
                1.0 / (60 + vector_rank) if vector_rank else 0.0
            )
            ranked.append(
                RetrievalResult(
                    memory=memory,
                    source_observation_ids=list(memory.source_observation_ids),
                    lexical_score=lexical_by_id[memory_id].score if memory_id in lexical_by_id else 0.0,
                    vector_score=vector_by_id[memory_id].score if memory_id in vector_by_id else 0.0,
                    fused_score=fused,
                )
            )
        ranked.sort(key=lambda item: (-item.fused_score, item.memory.id))
        return ranked[:limit]


class ContextAssembler:
    def __init__(self, max_chars: int = 8_000):
        if max_chars < 32:
            raise ValueError("max_chars must be at least 32")
        self.max_chars = max_chars

    def assemble(self, results: list[RetrievalResult]) -> str:
        blocks: list[str] = []
        used = 0
        for result in results:
            citation = ", ".join(result.source_observation_ids)
            prefix = f"[{result.memory.id}] "
            suffix = f"\nsource: {citation}"
            available = self.max_chars - used - len(prefix) - len(suffix) - 2
            if available <= 0:
                break
            content = result.memory.content[:available]
            block = f"{prefix}{content}{suffix}"
            blocks.append(block)
            used += len(block) + 2
        return "\n\n".join(blocks)
