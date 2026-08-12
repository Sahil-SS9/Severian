"""Qwen 0.6B production embedding contract.

The selected provider must preserve the bake-off's exact semantics:
384-dimensional Matryoshka vectors, L2 normalisation, and the retrieval
instruction on queries only.
"""
from __future__ import annotations

import logging
import math
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.infrastructure.embedding_registry import resolve_embedding_metadata  # noqa: E402
from severian.infrastructure.qwen_embedding import (  # noqa: E402
    QWEN_DIMENSION,
    QWEN_MODEL,
    QWEN_RETRIEVAL_PROMPT,
    QWEN_REVISION,
    QwenEmbedding,
)
from severian.infrastructure.vector import HashEmbedding  # noqa: E402


class _RecordingModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def encode(self, text: str, **kwargs: object) -> list[float]:
        self.calls.append((text, kwargs))
        return [3.0] * QWEN_DIMENSION


def test_qwen_load_logs_status_without_writing_stdout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    class _FakeSentenceTransformer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    provider = QwenEmbedding()

    with caplog.at_level(logging.INFO, logger="severian.infrastructure.qwen_embedding"):
        assert provider.load()

    assert capsys.readouterr().out == ""
    assert "Qwen embeddings loaded" in caplog.text


def test_qwen_explicit_device_is_forwarded_without_changing_default_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeSentenceTransformer:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            calls.append(kwargs)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    assert QwenEmbedding().load()
    assert "device" not in calls[-1]
    assert QwenEmbedding(device="cpu").load()
    assert calls[-1]["device"] == "cpu"


def test_qwen_uses_query_only_instruction_and_384_projection() -> None:
    provider = QwenEmbedding()
    model = _RecordingModel()
    provider._model = model

    document = provider.encode("The Kestrel thruster reaches 40 km/s.")
    query = provider.encode_query("How fast is the Kestrel thruster?")

    document_call, query_call = model.calls
    assert document_call[1] == {
        "normalize_embeddings": True,
        "show_progress_bar": False,
        "truncate_dim": QWEN_DIMENSION,
    }
    assert query_call[1] == {
        "normalize_embeddings": True,
        "show_progress_bar": False,
        "truncate_dim": QWEN_DIMENSION,
        "prompt": QWEN_RETRIEVAL_PROMPT,
    }
    assert len(document) == QWEN_DIMENSION
    assert len(query) == QWEN_DIMENSION
    assert math.isclose(sum(value * value for value in document), 1.0, abs_tol=1e-6)
    assert math.isclose(sum(value * value for value in query), 1.0, abs_tol=1e-6)


def test_qwen_metadata_is_pinned_and_has_prompt_contract() -> None:
    metadata = resolve_embedding_metadata(QwenEmbedding())

    assert metadata.model_id == QWEN_MODEL
    assert metadata.revision == QWEN_REVISION
    assert metadata.dimension == QWEN_DIMENSION
    assert metadata.prompt_contract == QWEN_RETRIEVAL_PROMPT
    assert metadata.fingerprint


def test_qwen_rejects_ignored_truncation() -> None:
    provider = QwenEmbedding()
    provider._model = _RecordingModel()
    provider._model.encode = lambda _text, **_kwargs: [1.0] * 1024  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="expected 384"):
        provider.encode("document")


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), -float("inf")])
def test_qwen_rejects_nonfinite_values(invalid_value: float) -> None:
    provider = QwenEmbedding()
    provider._model = _RecordingModel()
    provider._model.encode = lambda _text, **_kwargs: [invalid_value] * QWEN_DIMENSION  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="non-finite"):
        provider.encode("document")


def test_qwen_rejects_finite_norm_overflow() -> None:
    provider = QwenEmbedding()
    provider._model = _RecordingModel()
    provider._model.encode = lambda _text, **_kwargs: [1e154] * QWEN_DIMENSION  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="norm overflowed"):
        provider.encode("document")


def test_qwen_hash_fallback_preserves_384_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = QwenEmbedding()
    monkeypatch.setattr(provider, "load", lambda: False)

    fallback = provider.or_hash()

    assert isinstance(fallback, HashEmbedding)
    assert fallback.dimension == QWEN_DIMENSION
