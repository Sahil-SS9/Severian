"""Run the embedding bake-off (zero-debt plan, Task 1.2/1.3).

Usage:
    uv run python scripts/run_embedding_benchmark.py \
        --manifest docs/benchmarks/manifest-embedding-v1.json \
        --out /tmp/zero-debt-embedding \
        [--only granite-97m,bekko-a8m]

Lanes are built by the registry below; each maps a provider id to a
factory returning a SeverianAdapter with that embedding. Lanes that
cannot run in this environment (missing dependency / licence-gated
download) are skipped and recorded with the reason — never silently
dropped, never faked.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_provenance import collect_provenance  # noqa: E402

from severian.benchmarks.embedding_runner import EmbeddingBakeoffRunner  # noqa: E402
from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402


def _make_severian_adapter(provider_id: str, backend: str, dsn: str | None,
                           dimension: int, embedding: object, *, workdir: str | None = None):
    from severian.benchmarks.adapters.severian import SeverianAdapter

    return SeverianAdapter(
        provider_id=provider_id,
        backend=backend,
        dsn=dsn,
        dimension=dimension,
        embedding=embedding,
        reset_db=(backend == "postgresql"),
        workdir=workdir,
    )


def build_lane_factories(args: argparse.Namespace, manifest: BenchmarkManifest) -> dict[str, object]:
    """Return provider_id -> factory() for every lane requested + runnable.

    The factory closure defers embedding construction so cold-load time
    is measured inside the runner (construction == model load).
    """
    from severian.infrastructure.vector import HashEmbedding

    factories: dict[str, object] = {}

    def hash_lane() -> object:
        return _make_severian_adapter("hash", "sqlite", None, 256,
                                      HashEmbedding(dimension=256))

    factories["hash"] = hash_lane

    # Neural lanes: sentence-transformers based, lazy import so the
    # script degrades gracefully when the optional dependency is absent.
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # noqa: BLE001
        print(f"note: neural embedding deps unavailable ({exc}); "
              f"only the hash baseline lane will run")
        return factories

    def _st_embedding(model_name: str, dimension: int, prompt: str | None = None) -> object:
        class _STEmbedding:
            def __init__(self) -> None:
                import os

                os.environ.setdefault("TQDM_DISABLE", "1")
                self.dimension = dimension
                self._model = SentenceTransformer(model_name)
                self._prompt = prompt

            def encode(self, text: str) -> list[float]:
                kwargs = {"normalize_embeddings": True,
                          "show_progress_bar": False}
                if self._prompt:
                    kwargs["prompt"] = self._prompt  # type: ignore[assignment]
                vector = self._model.encode(text, **kwargs)
                return [float(v) for v in vector]

        return _STEmbedding()

    # Granite 107M (current control)
    factories["granite-107m"] = lambda: _make_severian_adapter(
        "granite-107m", "sqlite", None, 384,
        _st_embedding("ibm-granite/granite-embedding-107m-multilingual", 384))

    # Granite 97M R2 (balanced default candidate)
    factories["granite-97m"] = lambda: _make_severian_adapter(
        "granite-97m", "sqlite", None, 384,
        _st_embedding("ibm-granite/granite-embedding-97m-multilingual-r2", 384))

    # Granite 311M R2 (high tier, 768)
    factories["granite-311m"] = lambda: _make_severian_adapter(
        "granite-311m", "sqlite", None, 768,
        _st_embedding("ibm-granite/granite-embedding-311m-multilingual-r2", 768))

    # EmbeddingGemma 300M (balanced candidate; gated acquisition terms)
    factories["gemma-300m"] = lambda: _make_severian_adapter(
        "gemma-300m", "sqlite", None, 768,
        _st_embedding("google/embeddinggemma-300m", 768))

    # Qwen3 Embedding 0.6B (high tier; instruction for agent-memory)
    factories["qwen-0.6b"] = lambda: _make_severian_adapter(
        "qwen-0.6b", "sqlite", None, 1024,
        _st_embedding("Qwen/Qwen3-Embedding-0.6B", 1024,
                      prompt="Given a web search query, retrieve relevant passages that answer the query"))

    # Bekko a8m (lite multilingual)
    factories["bekko-a8m"] = lambda: _make_severian_adapter(
        "bekko-a8m", "sqlite", None, 384,
        _st_embedding("hotchpotch/bekko-embedding-v1-a8m", 384))

    # Granite small English R2 (English-only control — cannot win the
    # multilingual default, but measures the English-only tradeoff)
    factories["granite-small-en"] = lambda: _make_severian_adapter(
        "granite-small-en", "sqlite", None, 384,
        _st_embedding("ibm-granite/granite-embedding-small-english-r2", 384))

    return factories


def _inject_corpus(manifest: BenchmarkManifest, corpus_path: Path) -> BenchmarkManifest:
    """Merge the exporter's corpus.json into an empty manifest.

    The exporter writes {observations: [{scope, opaque_id, content}],
    query_pairs: [{query, evidence, kind}]}. Documents map 1:1 to
    observations; queries map 1:1 to pairs with expected evidence by
    opaque_id. Negative pairs (kind == 'negative') are answerable=False.
    """
    data = json.loads(corpus_path.read_text())
    docs_by_id: dict[str, str] = {}
    documents = []
    for obs in data["observations"]:
        doc_id = obs["opaque_id"]
        docs_by_id[doc_id] = obs["scope"]
        documents.append({
            "id": doc_id,
            "content": obs["content"],
            "scope": obs["scope"],
        })
    queries = []
    for i, pair in enumerate(data["query_pairs"]):
        kind = pair["kind"]
        # Negative and adversarial probes are unanswerable BY DESIGN: a
        # correct provider returns nothing (or nothing relevant) for them.
        answerable = kind not in {"negative", "adversarial"}
        expected = []
        query_scope = _any_scope(data)
        if answerable:
            # Match evidence by content → opaque_id (evidence is the
            # observation content it targets).
            for ev in pair["evidence"]:
                matched = next(
                    (oid for oid, content in _content_map(data).items()
                     if content == ev),
                    None,
                )
                if matched:
                    expected.append({"document_id": matched, "weight": 1.0})
                    query_scope = docs_by_id[matched]
        queries.append({
            "id": f"q{i:04d}",
            "kind": kind if kind in {"fact", "paraphrase", "multi_source",
                                     "correction", "temporal", "negative",
                                     "adversarial", "code", "crosslang", "long"}
            else "fact",
            "query": pair["query"],
            "scope": query_scope,
            "answerable": answerable,
            "expected_evidence": expected,
        })
    payload = json.loads(manifest.model_dump_json())
    payload["corpus"]["documents"] = documents
    payload["queries"] = queries
    return BenchmarkManifest.model_validate(payload)


def _content_map(data: dict) -> dict[str, str]:
    return {obs["opaque_id"]: obs["content"] for obs in data["observations"]}


def _any_scope(data: dict) -> str:
    return data["observations"][0]["scope"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--corpus", type=Path,
                        help="corpus.json from build_redacted_embedding_corpus.py "
                             "(injected into the manifest; manifest must have "
                             "empty documents/queries)")
    parser.add_argument("--out", type=Path, default=Path("/tmp/zero-debt-embedding"))
    parser.add_argument("--only", type=str, default="",
                        help="comma-separated provider ids to run (default: all runnable)")
    args = parser.parse_args()

    manifest = BenchmarkManifest.model_validate_json(args.manifest.read_text())
    if args.corpus:
        manifest = _inject_corpus(manifest, args.corpus)
    else:
        placeholders = [
            d.id for d in manifest.corpus.documents if d.id == "template-placeholder"
        ]
        if placeholders:
            parser.error(
                "manifest still contains template placeholders — pass --corpus "
                "from build_redacted_embedding_corpus.py (never benchmark the "
                "placeholder template)"
            )
    factories = build_lane_factories(args, manifest)
    if args.only:
        requested = {p.strip() for p in args.only.split(",") if p.strip()}
        factories = {k: v for k, v in factories.items() if k in requested}

    args.out.mkdir(parents=True, exist_ok=True)
    runner = EmbeddingBakeoffRunner(manifest=manifest, lane_factory=factories,
                                    corpus_path=args.manifest)
    print(f"bake-off manifest: {manifest.manifest_id} "
          f"({len(manifest.corpus.documents)} docs, {len(manifest.queries)} queries)")
    # zero-debt 3.3: checkpoint every completed lane immediately; a
    # timeout/crash then preserves finished lanes for the merge step.
    result = runner.run(checkpoint_dir=args.out)

    evidence = result.to_dict()
    evidence["repro"] = collect_provenance(
        REPO_ROOT, sys.argv, seed=manifest.corpus.seed,
        model_revision="per-lane revisions in lane results",
    )
    out_path = args.out / "embedding-bakeoff.json"
    out_path.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    print(f"\nwrote {out_path}")

    # Verdict table (bottom line first)
    print("\n=== bake-off results ===")
    print(f"{'lane':<16} {'recall@10':>9} {'nDCG@10':>8} {'p95ms':>8} {'cold_s':>7} "
          f"{'RSS_MB':>7} {'vecB':>5}  status")
    for lane in result.lanes:
        if lane.skipped:
            print(f"{lane.provider_id:<16} {'-':>9} {'-':>8} {'-':>8} {'-':>7} "
                  f"{'-':>7} {'-':>5}  SKIPPED: {lane.skip_reason[:60]}")
            continue
        q = lane.quality
        print(f"{lane.provider_id:<16} {q.recall_at_10:>9.3f} {q.ndcg_at_10:>8.3f} "
              f"{lane.query_latency_p95_ms:>8.1f} {lane.cold_load_seconds:>7.1f} "
              f"{lane.peak_rss_mb:>7.1f} {lane.vector_bytes_per_memory:>5.0f}  ok")


if __name__ == "__main__":
    main()
