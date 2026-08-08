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
    assert all(len(s.split(":")) == 5 for s in scopes), "five-dimensional scope ids"
    assert 100 <= len(out.query_pairs) <= 200, "100-200 query/evidence pairs"
    assert len(out.observations) >= 500, "500-5000 observations expected"
    assert out.corpus_sha256, "corpus hash required"
    assert out.raw_documents_logged == 0, "raw documents must never be logged"


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
