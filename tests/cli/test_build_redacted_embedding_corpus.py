"""RED: safe redacted embedding corpus exporter contract (zero-debt plan 1.1).

The exporter must:
- read an EXPLICITLY supplied read-only session DB (never auto-discovered);
- write output ONLY under a process-created owned temp root (never under
  .hermes, the repo, or a user-supplied existing directory);
- reject symlink roots and path traversal;
- map observations into five-dimensional synthetic benchmark scopes with
  opaque deterministic IDs;
- redact emails, phones, tokens, API keys, home paths, private URLs and
  named secrets BEFORE writing;
- run high-confidence secret/PII detection after redaction; any hit FAILS
  CLOSED and writes no corpus;
- never print raw documents (counts and hashes only);
- create 100-200 reviewable query/evidence pairs;
- leave the source DB untouched;
- clean up ONLY inside its owned root.

These tests are the contract; the implementation (scripts/
build_redacted_embedding_corpus.py) must make them pass.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_redacted_embedding_corpus as exporter  # noqa: E402


def _make_source_db(path: Path, rows: list[tuple[str, str, str]], min_rows: int = 600) -> None:
    """Create a minimal session-shaped SQLite DB (messages table).

    Pads to min_rows by cycling the sample rows so contract tests that
    require 500-5000 observations have enough source material.
    """
    if len(rows) < min_rows:
        base = rows
        rows = [base[i % len(base)] for i in range(min_rows)]
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT)"
    )
    for i, (session, role, content) in enumerate(rows):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content) VALUES (?,?,?,?)",
            (i, session, role, content),
        )
    conn.commit()
    conn.close()


def _sample_rows() -> list[tuple[str, str, str]]:
    return [
        ("sess-1", "user", "what is the status of project chrysalis?"),
        ("sess-1", "assistant", "the launch date is 2026-09-15 and the api key is sk-live-abc123xyz"),
        ("sess-2", "user", "email john.doe@example.com or call +44 7700 900123"),
        ("sess-2", "assistant", "the home path is /home/kensei/.hermes/config.yaml"),
        ("sess-3", "user", "secret token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"),
        ("sess-3", "assistant", "check the private url https://my-vpn.example.com/status"),
        ("sess-4", "user", "the deployment moved to https://kensei.example.com"),
        ("sess-4", "assistant", "remember the wifi password is hunter2!"),
        ("sess-5", "user", "can you check the sev-2 incident report for the api outage?"),
        ("sess-5", "assistant", "the outage started at 14:32 utc and affected the webhook service"),
    ]


# --- RED contract tests -------------------------------------------------


def test_unowned_output_root_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows())
    # A pre-existing user directory is NOT an owned root — must fail.
    preexisting = tmp_path / "preexisting"
    preexisting.mkdir()
    with _expect_exit():
        exporter.build(
            source_db=str(src),
            out_root=str(preexisting),
            query_pairs=20,
            seed=1,
        )
    assert not any(preexisting.iterdir()), "nothing may be written into an unowned root"


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows())
    target = tmp_path / "real-target"
    target.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(target, target_is_directory=True)
    with _expect_exit():
        exporter.build(source_db=str(src), out_root=str(link), query_pairs=20, seed=1)
    assert not any(target.iterdir()), "no writes through a symlink root"


def test_injected_secret_fails_closed(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    rows = _sample_rows()
    # A JWT is a high-confidence secret the redaction patterns do NOT
    # claim to cover — the post-redaction detector must catch it and
    # the builder must fail closed with NO corpus written.
    rows.append(("sess-5", "assistant",
                 "the session jwt is "
                 "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                 "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
                 "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"))
    _make_source_db(src, rows)
    with _expect_exit():
        exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-1"), query_pairs=20, seed=1)
    # Even on failure, an owned root may exist but must be empty or absent.
    owned = tmp_path / "owned-1"
    assert not owned.exists() or not any(owned.iterdir())


def test_email_and_phone_are_redacted(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows())
    out = exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-2"), query_pairs=20, seed=1)
    corpus_text = " ".join(_all_corpus_text(out))
    assert "john.doe@example.com" not in corpus_text
    assert "+44 7700 900123" not in corpus_text
    assert "sk-live-abc123xyz" not in corpus_text
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz" not in corpus_text
    assert "/home/kensei" not in corpus_text
    assert "my-vpn.example.com" not in corpus_text
    assert "hunter2" not in corpus_text


def test_uppercase_credential_labels_are_redacted(tmp_path: Path) -> None:
    """UPPERCASE credential labels must be redacted (regression).

    Found in the first real corpus build (2026-08-09): OLLAMA_API_KEY=...,
    GOOGLE_AI_API_KEY: ... leaked because every credential pattern was
    case-sensitive — API_KEY=/ApiKey: bypassed api[_-]?key=. All
    credential patterns are now case-insensitive; the fail-closed
    detector additionally catches any surviving label+value form.
    """
    rows = _sample_rows() + [
        ("sess-9", "assistant", "export OLLAMA_API_KEY=11f311abc9hbi"),
        ("sess-9", "assistant", "GOOGLE_AI_API_KEY: AIzaSyDnSAMPLEKEY0123456789"),
        ("sess-9", "assistant", "ApiKey=abcdefghijklmnop12345678"),
    ]
    src = tmp_path / "src.db"
    _make_source_db(src, rows)
    out = exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-6"),
                         query_pairs=20, seed=1)
    corpus_text = " ".join(o.content for o in out.observations)
    assert "11f311" not in corpus_text
    assert "AIzaSy" not in corpus_text
    assert "abcdefghijklmnop" not in corpus_text
    assert "[REDACTED-API-KEY]" in corpus_text


def test_empty_content_rows_are_skipped_and_ids_unique(tmp_path: Path) -> None:
    """Empty tool stubs are dropped; remaining IDs are unique (regression).

    Two fixes: (1) content-addressed IDs made empty-content rows hash to
    the same digest (918 of 5000 in the real corpus) — IDs now mix in
    session + row index; (2) empty rows are skipped outright — they carry
    no embedding signal and violate the manifest's non-empty content.
    """
    rows = _sample_rows() * 60 + [("sess-9", "tool", "")] * 200
    src = tmp_path / "src.db"
    _make_source_db(src, rows, min_rows=600)
    out = exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-7"),
                         query_pairs=20, seed=1)
    ids = [o.opaque_id for o in out.observations]
    assert len(ids) == len(set(ids)), "opaque IDs must be unique"
    assert all(o.content.strip() for o in out.observations), "no empty content"
    # 60 cycles × 10 sample rows = 600 non-empty survive the 200 empty stubs
    assert len(ids) == 600


def test_table_priority_prefers_messages_over_sessions(tmp_path: Path) -> None:
    """A DB listing `sessions` BEFORE `messages` must still read messages.

    Regression: _read_source iterated the DB's own table order, so a
    store whose sqlite_master lists sessions first (e.g. hermes state.db)
    picked `sessions` (metadata, no session_id column) and crashed.
    """
    import build_redacted_embedding_corpus as exp

    db = tmp_path / "tables.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('s1','user','hello world')")
    conn.commit()
    conn.close()

    rows = exp._read_source(str(db))  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0][0] == "s1"
    assert rows[0][2] == "hello world"


def test_source_db_is_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows())
    before = src.read_bytes()
    exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-3"), query_pairs=20, seed=1)
    assert src.read_bytes() == before, "source DB must never be modified"


def test_cleanup_only_inside_owned_root(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows())
    foreign = tmp_path / "foreign-file.txt"
    foreign.write_text("do not touch")
    exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-4"), query_pairs=20, seed=1)
    assert foreign.read_text() == "do not touch", "foreign files must survive"


def test_corpus_has_required_shape(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src, _sample_rows() * 60)  # 480 messages
    out = exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-5"), query_pairs=120, seed=7)
    scopes = out.scopes
    assert len(scopes) == 5, "exactly five synthetic benchmark scopes"
    assert all(len(s.split("/")) == 5 for s in scopes), "five-dimensional scope ids"
    assert 100 <= len(out.query_pairs) <= 200, "100-200 query/evidence pairs"
    assert len(out.observations) >= 500, "500-5000 observations expected"
    assert out.corpus_sha256, "corpus hash required"
    assert out.raw_documents_logged == 0, "raw documents must never be logged"


def test_answerable_queries_are_not_json_prefix_stubs(tmp_path: Path) -> None:
    """Query templates must never be built from degenerate tokens.

    Regression: `words[0]` from a whitespace split of real JSON-heavy
    content is often `{"output":` or `{"total_count":` — a JSON opening
    that carries zero retrieval signal. Every answerable query must
    contain at least one content-bearing token (letters), and no query
    may consist solely of JSON punctuation/redaction stubs.
    """
    import re

    src = tmp_path / "src.db"
    # JSON-heavy rows: the exact degenerate pattern from the real corpus
    rows = [
        ("s1", "tool", '{"output": "commit 79acdcc5bb73e8f6c72f7d38a01b", "exit_code": 0}'),
        ("s1", "tool", '{"total_count": 0, "results": []}'),
        ("s2", "tool", '{"output": "[REDACTED-PHONE] Ssl temporal-server", "ok": true}'),
    ] * 200
    _make_source_db(src, rows)
    out = exporter.build(source_db=str(src), out_root=str(tmp_path / "owned-json"), query_pairs=60, seed=1)
    assert len(out.query_pairs) >= 40
    for pair in out.query_pairs:
        if pair.kind in ("negative", "adversarial"):
            continue  # deliberately unanswerable
        # A query that is a raw JSON fragment is a degenerate probe: it
        # carries no retrieval signal beyond the braces all docs share.
        assert not any(ch in pair.query for ch in ('{', '}', '"', ':')), (
            f"query for kind={pair.kind} is a raw JSON fragment: {pair.query!r}"
        )
        # Template frames alone must not satisfy the check: the query has
        # to share a content-bearing token with its evidence.
        query_toks = set(re.findall(r"[A-Za-z]{4,}", pair.query))
        ev_text = " ".join(getattr(ev, "content", str(ev)) for ev in pair.evidence)
        ev_toks = set(re.findall(r"[A-Za-z]{4,}", ev_text))
        shared = query_toks & ev_toks
        assert shared, (
            f"query for kind={pair.kind} shares no content-bearing token "
            f"with its evidence (JSON/redaction stub?): {pair.query!r} vs {ev_text[:80]!r}"
        )


def _all_corpus_text(out: object) -> list[str]:
    """Extract every text field from the built corpus for redaction checks."""
    texts: list[str] = []
    for obs in getattr(out, "observations", []):
        texts.append(getattr(obs, "content", str(obs)))
    for pair in getattr(out, "query_pairs", []):
        texts.append(getattr(pair, "query", str(pair)))
        for ev in getattr(pair, "evidence", []):
            texts.append(getattr(ev, "content", str(ev)))
    return texts


@contextlib.contextmanager
def _expect_exit() -> Iterator[None]:
    try:
        yield
    except SystemExit:
        return
    raise AssertionError("expected SystemExit (fail-closed) but builder returned")
