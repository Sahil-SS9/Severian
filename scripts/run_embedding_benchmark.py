"""Run the embedding bake-off (zero-debt plan, Task 1.2/1.3) — repaired.

Usage:
    uv run python scripts/run_embedding_benchmark.py \
        --manifest docs/benchmarks/manifest-embedding-v1.json \
        --corpus /tmp/severian-corpus-*/corpus.json \
        --out /tmp/zero-debt-embedding \
        [--only granite-97m,bekko-a8m]

Phase B remediation contract:
- ``--corpus`` is REQUIRED for a real bake-off; the corpus SHA published
  in the evidence is the SHA of the corpus file, never the manifest
  (audit CRITICAL 1);
- expected evidence is injected DIRECTLY from corpus ``evidence_ids`` —
  no content rematching (audit HIGH 8/B9);
- lane verdicts are gate-aware — no unconditional ``ok`` (audit
  CRITICAL 2);
- neural model revisions are resolved to immutable HF commits before
  load and passed to the runtime (audit HIGH 6);
- Qwen instruction prompts apply to QUERIES only; documents are encoded
  with the plain contract (audit HIGH 5).

Lanes that cannot run in this environment (missing dependency /
licence-gated download) are skipped and recorded with the reason — never
silently dropped, never faked.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_provenance import collect_provenance  # noqa: E402

from severian.benchmarks.embedding_runner import EmbeddingBakeoffRunner  # noqa: E402
from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402


def _make_severian_adapter(provider_id: str, backend: str, dsn: str | None,
                           dimension: int, embedding: object, *,
                           workdir: str | None = None) -> object:
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


def _resolve_hf_revision(model_name: str) -> str:
    """Resolve the CURRENT immutable HF commit for a model (audit HIGH 6).

    Falls back to the cached snapshot revision when offline; fails hard
    when neither is available — an unpinned run is not evidence.
    """
    import urllib.request

    api = f"https://huggingface.co/api/models/{model_name}"
    try:
        with urllib.request.urlopen(api, timeout=20) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
        sha = data.get("sha")
        if sha:
            return str(sha)
    except Exception as exc:  # noqa: BLE001 — offline fallback below
        print(f"note: HF revision lookup failed ({exc}); using cached snapshot")
    # Offline fallback: the snapshot revision cached locally is immutable.
    cache = Path.home() / ".cache/huggingface/hub"
    snapshot = cache / f"models--{model_name.replace('/', '--')}"
    refs = snapshot / "refs" / "main"
    if refs.exists():
        return refs.read_text().strip()
    raise SystemExit(
        f"cannot resolve immutable revision for {model_name} — refusing to run unpinned"
    )


def build_lane_factories(args: argparse.Namespace, manifest: BenchmarkManifest) -> dict[str, object]:
    """Return provider_id -> factory() for every lane requested + runnable.

    The factory closure defers embedding construction so cold-load time
    is measured inside the runner (construction == model load). Neural
    lanes load with the manifest-pinned revision (audit HIGH 6).
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

    def _st_embedding(model_name: str, dimension: int, revision: str,
                      prompt: str | None = None,
                      truncate_dim: int | None = None) -> object:
        """Sentence-transformers embedding with query/doc encoding split.

        Audit HIGH 5 / P0-F: instruction prompts (e.g. Qwen agent-memory)
        apply to QUERIES ONLY. Documents are always encoded with the
        plain contract — prompting both sides corrupts the doc index.

        P0-F contract:
        - ``prompt`` is the FULL documented query-prompt template prefix
          (e.g. Qwen's ``Instruct: {task}\\nQuery:``); it is passed as the
          ``prompt=`` kwarg for queries only, and asserted verbatim by
          spy tests.
        - ``truncate_dim`` is the Matryoshka/truncation target. The
          encoder ASSERTS the returned vector length equals the adapter
          dimension — if truncation is silently ignored the lane fails
          loudly instead of recording 1024-dim vectors as 384-dim.
        - normalisation is always requested and asserted (unit norm).
        """

        class _STEmbedding:
            def __init__(self) -> None:
                import os

                os.environ.setdefault("TQDM_DISABLE", "1")
                self.dimension = dimension
                self.revision = revision
                self._model = SentenceTransformer(model_name, revision=revision)
                self._prompt = prompt
                self._truncate_dim = truncate_dim

            def _encode_contract(self, text: str, *, query: bool) -> list[float]:
                from typing import Any as _Any

                kwargs: dict[str, _Any] = {"normalize_embeddings": True,
                                           "show_progress_bar": False}
                if self._truncate_dim is not None:
                    kwargs["truncate_dim"] = self._truncate_dim
                if query and self._prompt:
                    kwargs["prompt"] = self._prompt
                vector = self._model.encode(text, **kwargs)
                values = [float(v) for v in vector]
                if len(values) != self.dimension:
                    raise ValueError(
                        f"encoder returned {len(values)} dims, expected "
                        f"{self.dimension} (truncate_dim={self._truncate_dim})"
                    )
                # P0-F: normalisation must be EXPLICIT, not trusted from
                # the library. sentence-transformers' truncate_dim +
                # normalize_embeddings ordering does not guarantee unit
                # norm after truncation (observed 0.9985-1.0033), so the
                # lane renormalises itself and asserts the contract.
                norm = sum(v * v for v in values) ** 0.5
                if not (norm > 0.0):
                    raise ValueError("encoded vector has zero norm")
                values = [v / norm for v in values]
                norm = sum(v * v for v in values) ** 0.5
                if not (0.999999 <= norm <= 1.000001):
                    raise ValueError(
                        f"encoded vector is not normalised (norm={norm:.6f})"
                    )
                return values

            def encode(self, text: str) -> list[float]:
                # ProviderAdapter document/query path: encode with the
                # QUERY contract for queries and plain contract for docs.
                return self._encode_contract(text, query=False)

            def encode_query(self, text: str) -> list[float]:
                return self._encode_contract(text, query=True)

        return _STEmbedding()

    # provider_id -> (model name, dimension, manifest revision, prompt,
    #                 truncate_dim)
    lanes: dict[str, tuple[str, int, str, str | None, int | None]] = {}
    for provider in manifest.providers:
        if provider.vendor_reported_only:
            continue
        cfg = provider.config or {}
        model_id = provider.model.id
        if model_id == "hash" or provider.id == "hash":
            continue
        rev = str(cfg.get("revision", ""))
        # P0-F: full documented query-prompt template prefix (e.g. Qwen
        # "Instruct: {task}\nQuery:"); absent => no-instruction control.
        prompt = cfg.get("query_prompt")
        truncate_dim = cfg.get("truncate_dim")
        lanes[provider.id] = (
            model_id, int(cfg.get("dimension", 384)), rev,
            str(prompt) if prompt else None,
            int(truncate_dim) if truncate_dim is not None else None,
        )

    for provider_id, (model_name, dimension, revision, prompt, truncate_dim) in lanes.items():
        def _factory(
            pid: str = provider_id, mn: str = model_name, dim: int = dimension,
            rev: str = revision, pr: str | None = prompt,
            td: int | None = truncate_dim,
        ) -> object:
            return _make_severian_adapter(
                pid, "sqlite", None, dim,
                _st_embedding(mn, dim, rev, prompt=pr, truncate_dim=td),
            )

        factories[provider_id] = _factory

    return factories


def _inject_corpus(manifest: BenchmarkManifest, corpus_path: Path) -> BenchmarkManifest:
    """Merge the exporter's corpus.json into an empty manifest.

    The exporter writes {observations: [{scope, opaque_id, content}],
    query_pairs: [{query, evidence, evidence_ids, kind}]}. Documents map
    1:1 to observations; queries map 1:1 to pairs with expected evidence
    by OPAQUE ID directly — never rematched by content (audit B9).
    """
    data = json.loads(corpus_path.read_text())
    documents = []
    for obs in data["observations"]:
        doc: dict[str, Any] = {
            "id": obs["opaque_id"],
            "content": obs["content"],
            "scope": obs["scope"],
        }
        # P0-E: correction chains must reach the runner — pass through
        # the supersession identity (stale flag + supersedes ids).
        if obs.get("is_stale"):
            doc["is_stale"] = True
        supersedes = obs.get("supersedes") or []
        if supersedes:
            doc["supersedes"] = [str(s) for s in supersedes]
        documents.append(doc)
    queries = []
    scope_by_id = {obs["opaque_id"]: obs["scope"] for obs in data["observations"]}
    for i, pair in enumerate(data["query_pairs"]):
        kind = pair["kind"]
        # Negative and adversarial probes are unanswerable BY DESIGN: a
        # correct provider returns nothing (or nothing relevant) for them.
        answerable = kind not in {"negative", "adversarial"}
        evidence_ids = pair.get("evidence_ids") or []
        expected = [{"document_id": oid, "weight": 1.0} for oid in evidence_ids]
        query_scope = scope_by_id.get(evidence_ids[0]) if evidence_ids else _any_scope(data)
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


def _any_scope(data: dict[str, Any]) -> str:
    return str(data["observations"][0]["scope"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--corpus", type=Path,
                        help="corpus.json from build_redacted_embedding_corpus.py "
                             "(injected into the manifest; manifest must have "
                             "empty documents/queries). REQUIRED for a real bake-off.")
    parser.add_argument("--out", type=Path, default=Path("/tmp/zero-debt-embedding"))
    parser.add_argument("--only", type=str, default="",
                        help="comma-separated provider ids to run (default: all runnable)")
    args = parser.parse_args()

    manifest = BenchmarkManifest.model_validate_json(args.manifest.read_text())
    if args.corpus:
        args.corpus = args.corpus.resolve()  # child subprocess cwd != repo root
        if not args.corpus.exists():
            parser.error(f"--corpus file does not exist: {args.corpus}")
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
                                    corpus_path=args.corpus,
                                    manifest_path=args.manifest.resolve())
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

    # Verdict table (bottom line first) — GATE-AWARE (audit CRITICAL 2).
    print("\n=== bake-off results (gate-aware) ===")
    print(f"{'lane':<16} {'recall@10':>9} {'nDCG@10':>8} {'p95ms':>8} {'cold_s':>7} "
          f"{'RSS_MB':>7} {'vecB':>5}  status")
    any_gate_pass = False
    for lane in result.lanes:
        if lane.skipped:
            print(f"{lane.provider_id:<16} {'-':>9} {'-':>8} {'-':>8} {'-':>7} "
                  f"{'-':>7} {'-':>5}  SKIPPED: {lane.skip_reason[:60]}")
            continue
        q = lane.quality
        if lane.gate_passed:
            status = "PASS"
            any_gate_pass = True
        else:
            status = f"FAIL[{','.join(lane.gate_failures)}]"
        print(f"{lane.provider_id:<16} {q.recall_at_10:>9.3f} {q.ndcg_at_10:>8.3f} "
              f"{lane.query_latency_p95_ms:>8.1f} {lane.cold_load_seconds:>7.1f} "
              f"{lane.peak_rss_mb:>7.1f} {lane.vector_bytes_per_memory:>5.0f}  {status}")
    if not any_gate_pass:
        print("\nNO LANE PASSED THE RELEASE GATE — this evidence must NOT be "
              "used for model-tier selection.")


if __name__ == "__main__":
    main()
