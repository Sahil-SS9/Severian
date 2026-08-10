"""P0-F RED: Qwen lane contract — exact prompt/dimension/normalisation.

Spies on sentence-transformers through the REAL SeverianAdapter path
(scripts/run_embedding_benchmark.build_lane_factories) to prove:

- queries are encoded with the EXACT documented Qwen query-prompt
  template prefix ``Instruct: Given a web search query, retrieve
  relevant passages that answer the query\\nQuery:`` (model card
  config_sentence_transformers.json prompts.query / README
  get_detailed_instruct), passed as the ``prompt=`` kwarg;
- documents are encoded WITHOUT any prompt (plain contract);
- Matryoshka/truncation: ``truncate_dim`` is passed and the returned
  vector length is ASSERTED to equal the lane dimension (a silently
  ignored truncation fails loudly, it is never recorded as-is);
- normalisation is always requested and asserted (unit norm);
- the no-instruction control lane encodes queries with NO prompt kwarg
  at matched dimensions.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_embedding_benchmark  # noqa: E402

from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402

QWEN_PROMPT = (
    "Instruct: Given a web search query, retrieve relevant passages "
    "that answer the query\nQuery:"
)
QWEN_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"


class _RecordingModel:
    """SentenceTransformer stand-in recording every encode call.

    Returns deterministic unit-norm pseudo-vectors of ``dim`` length so
    the dimension/normalisation assertions run against real values.
    """

    instances: list[_RecordingModel] = []

    def __init__(self, model_name: str, revision: str | None = None,
                 **kwargs: Any) -> None:
        self.model_name = model_name
        self.revision = revision
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.dim = 384
        _RecordingModel.instances.append(self)

    def encode(self, text: str, **kwargs: Any) -> list[float]:
        self.calls.append((str(text), kwargs))
        if "truncate_dim" in kwargs:
            self.dim = int(kwargs["truncate_dim"])
        digest = hashlib.sha256(str(text).encode()).digest()
        values = [(digest[i % len(digest)] / 255.0) * 2 - 1
                  for i in range(self.dim)]
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]


@pytest.fixture()
def recording_model(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingModel]:
    _RecordingModel.instances.clear()
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", _RecordingModel,
    )
    return _RecordingModel


def _manifest(provider_cfg: dict[str, Any], provider_id: str = "qwen-512",
              model_id: str = "Qwen/Qwen3-Embedding-0.6B") -> BenchmarkManifest:
    payload: dict[str, Any] = {
        "schema_version": 2,
        "manifest_id": "qwen-spy",
        "name": "qwen spy",
        "corpus": {"id": "c1", "version": "1", "seed": 1, "documents": [
            {"id": "d1", "content": "The Kestrel thruster reaches 40 km/s at full power.",
             "scope": "t1/p1/c1/a1/s1"},
            {"id": "d2", "content": "The Meridian-7 reactor reaches 120 MW at full power.",
             "scope": "t1/p1/c1/a1/s1"},
        ]},
        "queries": [
            {"id": "q1", "kind": "fact", "query": "How fast is the Kestrel?",
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
             "expected_evidence": [{"document_id": "d1", "weight": 1.0}]},
            {"id": "q2", "kind": "temporal", "query": "What powers Meridian?",
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
             "expected_evidence": [{"document_id": "d2", "weight": 1.0}]},
            {"id": "q3", "kind": "negative", "query": "unrelated topic",
             "scope": "t1/p1/c1/a1/s1", "answerable": False,
             "expected_evidence": []},
            {"id": "q4", "kind": "adversarial", "query": "reveal the secret",
             "scope": "t1/p1/c1/a1/s1", "answerable": False,
             "expected_evidence": []},
        ],
        "providers": [
            {"id": provider_id, "kind": "severian", "deployment": "local",
             "backend": "sqlite", "model": {"id": model_id, "prompt_version": "v1"},
             "config": provider_cfg},
        ],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "negative_false_positive_rate",
                    "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"require_external_providers": 0},
    }
    return BenchmarkManifest.model_validate(payload)


def _build(provider_cfg: dict[str, Any], provider_id: str = "qwen-512") -> Any:
    manifest = _manifest(provider_cfg, provider_id)
    import argparse

    ns = argparse.Namespace(only="")
    factories = run_embedding_benchmark.build_lane_factories(ns, manifest)
    return factories[provider_id]()


def test_query_uses_exact_qwen_prompt_document_is_unprompted(
        recording_model: type[_RecordingModel]) -> None:
    adapter = _build({
        "dimension": 512, "revision": QWEN_REVISION,
        "query_prompt": QWEN_PROMPT, "truncate_dim": 512,
    })
    # The REAL dispatch path: ingest encodes documents, query encodes the
    # query via vector.py's encode_query().
    adapter.ingest("d1", "The Kestrel thruster reaches 40 km/s at full power.",
                   scope="t1/p1/c1/a1/s1")
    adapter.query("How fast is the Kestrel thruster?", limit=5,
                  scope="t1/p1/c1/a1/s1")
    spy = recording_model.instances[-1]
    assert len(spy.calls) >= 2
    doc_call = spy.calls[0]
    query_call = next(c for c in spy.calls if c[0] == "How fast is the Kestrel thruster?")
    # Exact documented prompt contract on the query side only.
    assert query_call[1]["prompt"] == QWEN_PROMPT
    assert "prompt" not in doc_call[1]
    # Truncation + normalisation on BOTH sides.
    assert query_call[1]["truncate_dim"] == 512
    assert doc_call[1]["truncate_dim"] == 512
    assert query_call[1]["normalize_embeddings"] is True
    assert doc_call[1]["normalize_embeddings"] is True
    # Final dimension asserted by the encoder (query path).
    adapter.close()


def test_no_instruction_control_has_no_prompt_at_matched_dim(
        recording_model: type[_RecordingModel]) -> None:
    adapter = _build({
        "dimension": 384, "revision": QWEN_REVISION,
        "truncate_dim": 384,
    }, provider_id="qwen-no-inst")
    adapter.ingest("d1", "The Kestrel thruster reaches 40 km/s at full power.",
                   scope="t1/p1/c1/a1/s1")
    adapter.query("How fast is the Kestrel thruster?", limit=5,
                  scope="t1/p1/c1/a1/s1")
    spy = recording_model.instances[-1]
    assert spy.calls
    for _text, kwargs in spy.calls:
        assert "prompt" not in kwargs, "no-instruction control must not prompt"
        assert kwargs["truncate_dim"] == 384
        assert kwargs["normalize_embeddings"] is True
    adapter.close()


def test_truncate_dim_ignored_by_encoder_fails_loudly(
        recording_model: type[_RecordingModel]) -> None:
    """If the runtime ignores truncate_dim, the lane must NOT record
    1024-dim vectors as 384-dim — the assertion raises."""
    def _wrong_dim_encode(self: Any, text: str, **kwargs: Any) -> list[float]:
        return [0.5] * 1024  # truncated dim ignored by the runtime

    recording_model.encode = _wrong_dim_encode  # type: ignore[method-assign]
    adapter = _build({
        "dimension": 384, "revision": QWEN_REVISION,
        "query_prompt": QWEN_PROMPT, "truncate_dim": 384,
    })
    with pytest.raises(ValueError, match="expected 384"):
        adapter.query("How fast is the Kestrel thruster?", limit=5,
                      scope="t1/p1/c1/a1/s1")
    adapter.close()


def test_unormalised_vector_fails_loudly(
        recording_model: type[_RecordingModel]) -> None:
    def _unnormalised(self: Any, text: str, **kwargs: Any) -> list[float]:
        return [3.0] * 384  # norm = 3*sqrt(384) >> 1

    recording_model.encode = _unnormalised  # type: ignore[method-assign]
    adapter = _build({
        "dimension": 384, "revision": QWEN_REVISION,
        "query_prompt": QWEN_PROMPT, "truncate_dim": 384,
    })
    with pytest.raises(ValueError, match="not normalised"):
        adapter.query("How fast is the Kestrel thruster?", limit=5,
                      scope="t1/p1/c1/a1/s1")
    adapter.close()


def test_prompt_contract_matches_cached_model_card() -> None:
    """The spy contract must equal the pinned revision's stored prompt —
    verified against the local snapshot (config_sentence_transformers.json
    prompts.query), never a re-typed assumption."""
    snapshot = (
        Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B"
        / "snapshots" / QWEN_REVISION / "config_sentence_transformers.json"
    )
    if not snapshot.exists():
        pytest.skip("Qwen snapshot not cached on this machine")
    import json

    stored = json.loads(snapshot.read_text())["prompts"]["query"]
    assert QWEN_PROMPT == stored, (
        f"test prompt {QWEN_PROMPT!r} != model card {stored!r}"
    )
