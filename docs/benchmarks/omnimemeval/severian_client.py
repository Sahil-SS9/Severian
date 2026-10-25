import hashlib
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from .base_client import env_int, env_str, require_env

_EMBEDDER = None
_EMBEDDER_LOCK = threading.Lock()
_USER_LOCKS = {}
_USER_LOCKS_GUARD = threading.Lock()
_FACTS_LOCK = threading.Lock()  # one background fact batch at a time (shared model endpoint)
_TRUE = {"1", "true", "on", "yes"}


def _validate_user_id(user_id):
    rule = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"
    if not isinstance(user_id, str) or re.fullmatch(rule, user_id) is None:
        raise ValueError(f"user_id must fullmatch {rule}")
    return user_id


def _user_key(user_id):
    """Distinct accepted IDs never share a store, even on case-insensitive filesystems."""
    user_id = _validate_user_id(user_id)
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return f"{user_id[:24].lower()}-{digest}"


def _user_lock(user_id):
    key = _user_key(user_id)
    with _USER_LOCKS_GUARD:
        return _USER_LOCKS.setdefault(key, threading.Lock())


class _LockedEmbedding:
    """Share one GPU embedding model across worker threads: every ``encode*`` call is serialised
    (encode, encode_query, ...), the same wrapper the Severian bench harness uses."""

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr) and name.startswith("encode"):
            def locked(*a, **kw):
                with self._lock:
                    return attr(*a, **kw)
            return locked
        return attr


def _label(iso):
    """'[2023/05/08 (Mon) 13:56]' - the date label Severian's Hermes path and our harness both use."""
    if not iso:
        return None, None
    text = str(iso).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y/%m/%d (%a) %H:%M"), dt.isoformat()


def _pairs(messages):
    """Pair each user message with the following assistant message (Hermes sync_turn shape)."""
    out, i = [], 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "user":
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            reply = nxt if nxt is not None and nxt.get("role") == "assistant" else None
            out.append((m, reply))
            i += 2 if reply is not None else 1
        else:
            out.append((None, m))
            i += 1
    return out


class SeverianClient:
    """Severian (local-first memory engine) in-process client for OmniMemEval.

    Ingest follows Severian's Hermes path: one observation per user turn plus the assistant reply that follows it,
    prefixed with its date label and carrying the message time, then the derivation drain and (when
    SEVERIAN_USER_FACTS is on) the background fact pass, so everything is searchable when ``add`` returns.
    Search follows Hermes prefetch: ``service.search`` then ``service.assemble_context`` (the product's own cited
    block: date labels, user facts with the user's words, conflict notes).

    One Severian store per ``user_id`` under ``$SEVERIAN_OMNI_DB_ROOT/<user_key>/``.

    Configurable env vars (product switches are the normal SEVERIAN_* variables, set by the env file):
        SEVERIAN_OMNI_DB_ROOT     - root for per-user stores (required)
        SEVERIAN_OMNI_EMBED_PATH  - local Qwen3-Embedding-0.6B snapshot path (required)
        SEVERIAN_OMNI_DEVICE      - embedding device (default cuda:0)
        SEVERIAN_OMNI_LIMIT       - "harness" (use the harness top_k) or an int (Hermes default 10)
        SEVERIAN_OMNI_MAX_CHARS   - assembled-context cap in characters (Hermes default 8000; 0 = no cap)
        SEVERIAN_OMNI_FACTS_SERIAL - "1" (default): one fact batch at a time across users (a local model serves one
                                     request at a time); "0": parallel across users (a cloud endpoint)
    """

    def __init__(self):
        from severian.composition import build_bundle  # noqa: F401  (fail fast if Severian is missing)

        self._root = Path(require_env("SEVERIAN_OMNI_DB_ROOT")).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._embed_path = require_env("SEVERIAN_OMNI_EMBED_PATH")
        self._device = env_str("SEVERIAN_OMNI_DEVICE", "cuda:0")
        limit = (env_str("SEVERIAN_OMNI_LIMIT", "10") or "10").strip().lower()
        self._limit = None if limit == "harness" else int(limit)
        self._max_chars = int(env_int("SEVERIAN_OMNI_MAX_CHARS", 8000, min_value=0) or 0)
        self._facts = os.environ.get("SEVERIAN_USER_FACTS", "").strip().lower() in _TRUE
        self._facts_serial = (env_str("SEVERIAN_OMNI_FACTS_SERIAL", "1") or "1").strip().lower() in _TRUE
        global _EMBEDDER
        with _EMBEDDER_LOCK:
            if _EMBEDDER is None:
                from severian.infrastructure.qwen_embedding import QwenEmbedding

                model = QwenEmbedding(model_name=self._embed_path, device=self._device)
                if not model.load() or not model.loaded:
                    raise RuntimeError("Severian's Qwen embedding failed to load; refusing a keyword-only run")
                model.encode("embedding probe")
                _EMBEDDER = _LockedEmbedding(model)

    # -- store helpers --------------------------------------------------------

    def _dir(self, user_id):
        return self._root / _user_key(user_id)

    def _open(self, user_id, create):
        from severian.composition import build_bundle
        from severian.domain.models import Collection, Scope

        d = self._dir(user_id)
        fresh = not (d / "s.db").exists()
        if fresh and not create:
            return None, None
        d.mkdir(parents=True, exist_ok=True)
        bundle = build_bundle(database=d / "s.db", fts=d / "s.fts", vectors=d / "s.vec", embedding=_EMBEDDER)
        key = _user_key(user_id)
        scope = Scope(tenant_id="omni", profile_id=key, collection_id="omni", agent_id=key, session_id="shared")
        if fresh:
            bundle.repository.create_collection(Collection(id="omni", tenant_id="omni", name="omni"))
        return bundle, scope

    def _sessions(self, user_id):
        p = self._dir(user_id) / "sessions.txt"
        return int(p.read_text()) if p.exists() else 0

    # -- add ------------------------------------------------------------------

    def add(self, messages, user_id, *args, **kwargs):
        user_id = _validate_user_id(user_id)
        if not messages:
            return
        default_time = kwargs.get("timestamp")
        if isinstance(default_time, (int, float)) and not isinstance(default_time, bool):
            default_time = datetime.fromtimestamp(default_time, tz=timezone.utc).isoformat()
        with _user_lock(user_id):
            bundle, scope = self._open(user_id, create=True)
            assert bundle is not None and scope is not None  # create=True always opens a store
            try:
                n = self._sessions(user_id) + 1
                for turn, (user, reply) in enumerate(_pairs(messages), 1):
                    first = user or reply
                    label, iso = _label(first.get("chat_time") or default_time)
                    u = str(user.get("content", "")) if user else ""
                    a = str(reply.get("content", "")) if reply else ""
                    content = f"user: {u}\nassistant: {a}" if u and a else (u or a)
                    if not content.strip():
                        continue
                    source = {"omni": "1", "session_id": f"s{n:04d}", "turn": turn}
                    if iso:
                        source["timestamp"] = iso
                    if a and not u:
                        source["speaker"] = "assistant"
                    text = f"[{label}] {content}" if label else content
                    bundle.service.ingest_observation(scope, content=text, source=source)
                self._drain(bundle)
                (self._dir(user_id) / "sessions.txt").write_text(str(n))
            finally:
                bundle.close()

    def _drain(self, bundle):
        from severian.application.worker import DerivationWorker

        worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
        while True:
            summary = worker.drain(worker_id="omni", gateway=bundle.gateway, max_jobs=None)
            if summary.failed or summary.projection_failed:
                raise RuntimeError(f"severian drain failures: {summary}")
            if not summary.processed:
                break
        extract = getattr(worker, "extract_facts", None)
        if self._facts and callable(extract):
            while True:
                if self._facts_serial:
                    with _FACTS_LOCK:
                        summary = extract(worker_id="omni-facts", max_batches=1)
                else:
                    summary = extract(worker_id="omni-facts", max_batches=1)
                if summary.failed or summary.projection_failed:
                    raise RuntimeError(f"severian fact pass failures: {summary}")
                if not summary.processed + getattr(summary, "gave_up", 0):
                    break

    # -- search ---------------------------------------------------------------

    def search(self, query, user_id, top_k):
        user_id = _validate_user_id(user_id)
        bundle, scope = self._open(user_id, create=False)
        if bundle is None or scope is None:
            return ""
        try:
            limit = int(top_k) if self._limit is None else self._limit
            hits = bundle.service.search(scope, query=query, limit=limit)
            if not hits:
                return ""
            cap = self._max_chars if self._max_chars > 0 else 10 ** 9
            return bundle.service.assemble_context(scope, hits, max_chars=cap, query=query)
        finally:
            bundle.close()

    # -- delete ---------------------------------------------------------------

    def delete_all(self, user_id):
        user_id = _validate_user_id(user_id)
        with _user_lock(user_id):
            shutil.rmtree(self._dir(user_id), ignore_errors=True)

    def delete(self, user_id):
        self.delete_all(user_id)
