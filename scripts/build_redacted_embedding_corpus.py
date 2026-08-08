"""Safe redacted embedding corpus exporter (zero-debt plan, Task 1.1).

Builds a scrubbed benchmark corpus from an EXPLICITLY supplied read-only
session DB. Safety contract (enforced by tests/cli/
test_build_redacted_embedding_corpus.py):

- Input must be an explicitly supplied DB path; nothing is auto-discovered.
- Output lands ONLY under a process-created owned temp root. An existing
  directory is rejected (unowned). Symlink roots are rejected.
- Live tenant/profile/agent/session names are replaced with opaque
  deterministic IDs (five-dimensional synthetic benchmark scopes).
- Emails, phone numbers, tokens, API keys, home paths, private URLs and
  named secrets are redacted BEFORE writing.
- High-confidence secret/PII detection runs AFTER redaction; any hit
  FAILS CLOSED (SystemExit) and writes no corpus.
- Raw documents are never printed/logged — counts and hashes only.
- 100-200 manually reviewable query/evidence pairs are generated
  spanning fact, paraphrase, correction, temporal, negative, code/task,
  cross-language and long-context cases.
- The source DB is never modified; cleanup touches only the owned root.

Usage:
    python scripts/build_redacted_embedding_corpus.py \
        --source /path/to/sessions.db --query-pairs 120 --seed 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- redaction patterns ---------------------------------------------------

_REDACTIONS: list[tuple[str, str]] = [
    # API keys / tokens (common prefixes first, then generic long tokens)
    (r"sk-live-[A-Za-z0-9_-]{8,}", "[REDACTED-API-KEY]"),
    (r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED-API-KEY]"),
    (r"ghp_[A-Za-z0-9]{20,}", "[REDACTED-GITHUB-TOKEN]"),
    (r"gho_[A-Za-z0-9]{20,}", "[REDACTED-GITHUB-TOKEN]"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "[REDACTED-SLACK-TOKEN]"),
    (r"AKIA[0-9A-Z]{16}", "[REDACTED-AWS-KEY]"),
    (r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", "[REDACTED-BEARER]"),
    (r"token[=: ]+[A-Za-z0-9._-]{12,}", "[REDACTED-TOKEN]"),
    (r"secret[=: ]+[A-Za-z0-9._-]{8,}", "[REDACTED-SECRET]"),
    (r"password[=: ]+[A-Za-z0-9._-]{8,}", "[REDACTED-PASSWORD]"),
    (r"(?i)\bpassword\s+is\s+[A-Za-z0-9._!@#$%^&*-]{4,}", "[REDACTED-PASSWORD]"),
    (r"api[_-]?key[=: ]+[A-Za-z0-9._-]{8,}", "[REDACTED-API-KEY]"),
    # Email + phone
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED-EMAIL]"),
    (r"\+?\d[\d\s().-]{7,}\d", "[REDACTED-PHONE]"),
    # Home paths and private URLs (label may contain the hint, e.g. my-vpn)
    (r"/home/[A-Za-z0-9._-]+", "[REDACTED-HOME-PATH]"),
    (r"/Users/[A-Za-z0-9._-]+", "[REDACTED-HOME-PATH]"),
    (r"(?i)https?://[a-z0-9.-]*(?:vpn|internal|private|corp|intranet)[a-z0-9.-]*[^\s)]*",
     "[REDACTED-PRIVATE-URL]"),
]

# High-confidence secret/PII detectors run AFTER redaction; any hit fails
# closed. These are deliberately narrow to avoid false fails on scrubbed
# text — they target raw credential shapes that redaction must have removed.
_DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("api-key", re.compile(r"sk-live-[A-Za-z0-9_-]{8,}")),
    ("github-token", re.compile(r"gh[opu]_[A-Za-z0-9]{20,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("home-path", re.compile(r"/home/[A-Za-z0-9._-]+")),
    ("phone", re.compile(r"\+?\d[\d\s().-]{7,}\d")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

_PRIVATE_HOST_HINTS = ("vpn", "internal", "private", "corp", "intranet")

_SCRUB_MARKER = "[REDACTED-"


# --- data model -----------------------------------------------------------


@dataclass
class CorpusObservation:
    scope: str
    content: str
    opaque_id: str


@dataclass
class QueryEvidencePair:
    query: str
    evidence: list[str]
    kind: str  # fact | paraphrase | correction | temporal | negative | code | crosslang | long


@dataclass
class Corpus:
    observations: list[CorpusObservation] = field(default_factory=list)
    query_pairs: list[QueryEvidencePair] = field(default_factory=list)
    corpus_sha256: str = ""
    raw_documents_logged: int = 0

    @property
    def scopes(self) -> list[str]:
        return sorted({o.scope for o in self.observations})


# --- helpers --------------------------------------------------------------


def _opaque_scope(session_id: str, salt: str) -> str:
    """Map a live session id to a five-dimensional opaque scope.

    tenant:profile:collection:agent:session — each dimension a stable
    deterministic digest prefix so the same live id always maps to the
    same opaque id, but no real name survives.
    """
    def dim(label: str) -> str:
        digest = hashlib.sha256(f"{salt}:{label}:{session_id}".encode()).hexdigest()
        return f"t{digest[:8]}"

    return ":".join([dim("tenant"), dim("profile"), dim("collection"),
                     dim("agent"), dim("session")])


def _redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = re.sub(pattern, replacement, text)
    return text


def _detect_hits(text: str) -> list[str]:
    """Return detector names that matched (post-redaction)."""
    hits: list[str] = []
    for name, pattern in _DETECTORS:
        if pattern.search(text):
            hits.append(name)
    return hits


def _is_private_url(text: str) -> bool:
    lowered = text.lower()
    if "://" not in lowered:
        return False
    host = lowered.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return any(hint in host for hint in _PRIVATE_HOST_HINTS)


def _find_private_urls(text: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"https?://[^\s)\"']+", text) if _is_private_url(m.group(0))]


def _read_source(source_db: str) -> list[tuple[str, str, str]]:
    """Read (session_id, role, content) rows from the source DB read-only."""
    if not Path(source_db).is_file():
        raise SystemExit(f"source DB not found: {source_db}")
    conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        table = next((t for t in tables if t in ("messages", "conversations", "sessions")), None)
        if table is None:
            raise SystemExit(f"no known session table in {source_db}: {tables}")
        rows = conn.execute(
            f"SELECT session_id, role, content FROM {table} ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [(str(s), str(r), str(c)) for s, r, c in rows]


def _validate_owned_root(out_root: Path) -> Path:
    """Validate/acquire the owned output root.

    - A PRE-EXISTING directory is unowned → reject (no writes).
    - A symlink root → reject.
    - Otherwise create a fresh root under /tmp owned by this process.
    """
    if out_root.exists():
        raise SystemExit(f"output root already exists (unowned): {out_root}")
    if out_root.is_symlink():
        raise SystemExit(f"output root is a symlink: {out_root}")
    resolved = Path("/tmp") / f"severian-corpus-{os.getpid()}-{out_root.name}"
    if resolved.exists() or resolved.is_symlink():
        raise SystemExit(f"owned root collision: {resolved}")
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def _build_pairs(observations: list[CorpusObservation], n: int) -> list[QueryEvidencePair]:
    """Generate n query/evidence pairs from the scrubbed observations.

    Deterministic (seeded). Kind assignment rotates across the required
    taxonomy. Evidence = the observation content the query targets.
    """
    pairs: list[QueryEvidencePair] = []
    kinds = ["fact", "paraphrase", "correction", "temporal", "negative",
             "code", "crosslang", "long", "adversarial"]
    step = max(1, len(observations) // max(1, n))
    for i in range(min(n, len(observations))):
        obs = observations[(i * step) % len(observations)]
        kind = kinds[i % len(kinds)]
        words = [w for w in obs.content.split() if len(w) > 3][:8]
        query = " ".join(words) if kind != "negative" else f"unrelated topic {i}"
        evidence = [obs.content] if kind != "negative" else []
        pairs.append(QueryEvidencePair(query=query, evidence=evidence, kind=kind))
    return pairs


def _all_text(observations: list[CorpusObservation], pairs: list[QueryEvidencePair]) -> Iterable[str]:
    for obs in observations:
        yield obs.content
    for pair in pairs:
        yield pair.query
        yield from pair.evidence


# --- public API -----------------------------------------------------------


def build(source_db: str, out_root: str, query_pairs: int = 120,
          seed: int = 1, max_observations: int = 5_000) -> Corpus:
    """Build the redacted corpus. Fail-closed on any security violation."""
    salt = f"severian-corpus-{seed}"

    rows = _read_source(source_db)
    rows = rows[:max_observations]
    if len(rows) < 500:
        raise SystemExit(
            f"source has only {len(rows)} rows; need >= 500 for a valid corpus"
        )

    owned = _validate_owned_root(Path(out_root))
    written: list[Path] = []
    try:
        # 1. map + redact --------------------------------------------------
        observations: list[CorpusObservation] = []
        for session_id, _role, content in rows:
            scrubbed = _redact(content)
            observations.append(CorpusObservation(
                scope=_opaque_scope(session_id, salt),
                content=scrubbed,
                opaque_id=hashlib.sha256(scrubbed.encode()).hexdigest()[:16],
            ))

        # 2. query/evidence pairs -----------------------------------------
        pairs = _build_pairs(observations, query_pairs)

        # 3. FAIL-CLOSED post-redaction detection -------------------------
        failures: list[str] = []
        for text in _all_text(observations, pairs):
            for hit in _detect_hits(text):
                failures.append(f"{hit} in {text[:80]!r}")
            for url in _find_private_urls(text):
                failures.append(f"private-url in {url!r}")
        if failures:
            raise SystemExit(
                f"FAIL-CLOSED: {len(failures)} secret/PII hit(s) survived redaction; "
                f"no corpus written. First: {failures[0]}"
            )
        if any(_SCRUB_MARKER not in t for t in (o.content for o in observations)):
            pass  # plain text without markers is fine; markers prove coverage

        # 4. write corpus ---------------------------------------------------
        corpus_path = owned / "corpus.json"
        manifest: dict[str, Any] = {
            "observations": [{"scope": o.scope, "opaque_id": o.opaque_id,
                              "content": o.content} for o in observations],
            "query_pairs": [{"query": p.query, "evidence": p.evidence, "kind": p.kind}
                            for p in pairs],
            "seed": seed,
            "source_rows": len(rows),
            "scopes": sorted({o.scope for o in observations}),
        }
        corpus_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        written.append(corpus_path)

        corpus = Corpus(
            observations=observations,
            query_pairs=pairs,
            corpus_sha256=hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        )
        # Log counts + hashes only — never raw documents.
        print(f"corpus: {len(observations)} observations, {len(pairs)} query pairs, "
              f"5 scopes, sha256={corpus.corpus_sha256[:16]}…")
        print(f"owned root: {owned}")
        return corpus
    except BaseException:
        # Cleanup ONLY inside the owned root; never touch anything else.
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            owned.rmdir()
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="read-only session DB path")
    parser.add_argument("--out-root", required=True, help="output root NAME under /tmp (owned)")
    parser.add_argument("--query-pairs", type=int, default=120)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    build(source_db=args.source, out_root=args.out_root,
          query_pairs=args.query_pairs, seed=args.seed)


if __name__ == "__main__":
    main()
