from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope


@dataclass(frozen=True)
class VectorHit:
    memory_id: str
    score: float


_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have",
        "how", "in", "into", "is", "it", "its", "no", "not", "of", "on",
        "or", "should", "that", "the", "their", "these", "they", "this",
        "those", "to", "was", "we", "were", "what", "when", "where",
        "which", "who", "whose", "will", "with", "would", "you",
    }
)


class HashEmbedding:
    def __init__(self, dimension: int = 256, *, skip_stopwords: bool = True):
        if dimension < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dimension = dimension
        self.skip_stopwords = skip_stopwords

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE)
        for token in tokens:
            if self.skip_stopwords and token in _STOPWORDS:
                continue
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class LocalVectorIndex:
    def __init__(self, path: str | Path, embedding: HashEmbedding):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding = embedding
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_vectors (
                memory_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                vector TEXT NOT NULL
            )"""
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def upsert(self, memory: Memory) -> None:
        vector = self.embedding.encode(memory.content)
        self.connection.execute(
            """INSERT INTO memory_vectors
            (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, vector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                tenant_id=excluded.tenant_id, profile_id=excluded.profile_id,
                collection_id=excluded.collection_id, agent_id=excluded.agent_id,
                session_id=excluded.session_id, content=excluded.content, vector=excluded.vector""",
            (
                memory.id,
                memory.scope.tenant_id,
                memory.scope.profile_id,
                memory.scope.collection_id,
                memory.scope.agent_id,
                memory.scope.session_id,
                memory.content,
                json.dumps(vector, separators=(",", ":")),
            ),
        )
        self.connection.commit()

    def delete(self, memory_id: str) -> None:
        self.connection.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        self.connection.commit()

    def count(self, scope: Scope) -> int:
        return int(
            self.connection.execute(
                """SELECT COUNT(*) FROM memory_vectors
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            ).fetchone()[0]
        )

    def clear(self, scope: Scope | None = None) -> None:
        if scope is None:
            self.connection.execute("DELETE FROM memory_vectors")
        else:
            self.connection.execute(
                """DELETE FROM memory_vectors
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            )
        self.connection.commit()

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]:
        query_vector = self.embedding.encode(query)
        rows = self.connection.execute(
            """SELECT memory_id, vector FROM memory_vectors
               WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        scored: list[VectorHit] = []
        for memory_id, encoded in rows:
            vector = json.loads(encoded)
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            scored.append(VectorHit(memory_id=memory_id, score=score))
        return sorted(scored, key=lambda item: (-item.score, item.memory_id))[:limit]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        for memory in repository.list_memories(scope):
            self.upsert(memory)

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope."""
        import hashlib

        rows = self.connection.execute(
            """SELECT memory_id, content FROM memory_vectors
               WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?
               ORDER BY memory_id""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        digest = hashlib.sha256()
        for memory_id, content in rows:
            digest.update(memory_id.encode())
            digest.update(b"\x00")
            digest.update(content.encode())
            digest.update(b"\x00")
        return digest.hexdigest()

    def health(self) -> str:
        """Adapter-neutral probe: returns ``ok`` or ``error``."""
        try:
            self.connection.execute("SELECT 1")
            return "ok"
        except Exception:  # pragma: no cover - defensive
            return "error"
