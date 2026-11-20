# Running Severian through OmniMemEval

Files used for [omnimemeval-2026-10.md](../omnimemeval-2026-10.md), against OmniMemEval
[`f7476c5`](https://github.com/MemTensor/OmniMemEval/tree/f7476c5502115542eca9971195bf8c5ca97b6256).

| File | Goes to | Purpose |
| --- | --- | --- |
| `severian_client.py` | `scripts/client_factory/` | The Severian adapter: one store per benchmark user; search follows the Hermes prefetch path (`service.search`, then `service.assemble_context`) |
| `test_severian_client.py` | `scripts/tests/unit/` | Adapter tests |
| `register-severian.patch` | repo root (`git apply`) | Registers `severian` and routes it through the same `generic_text_search` LCM-X uses |
| `results-2026-10.json` | - | Every number on the results page; `scripts/bench_charts.py` draws the charts from it |
| `recompute-2026-10.py` | - | Offline recompute of the headline scores, category rows and paired ranges from `results-per-question.tar.zst` (no network, no model calls) |
| `MANIFEST-results-per-question-2026-10.sha256` | - | SHA-256 of all 3,437 files inside the per-question archive; verified by the recompute script |
| `lcmx-embed-deadline.patch` | repo root (`git apply`) | Optional, LCM-X only: lets `LCMX_EMBED_TIMEOUT_S` raise the connector's fixed 300 s embedding deadline, which very long BEAM sessions missed on a busy CPU. Unset = the original call, same model, chunks and vectors |

Environment (board setup): `SEVERIAN_OMNI_DB_ROOT`, `SEVERIAN_OMNI_EMBED_PATH` (local Qwen3-Embedding-0.6B),
`SEVERIAN_OMNI_LIMIT=harness`, `SEVERIAN_OMNI_MAX_CHARS=0`, `SEVERIAN_USER_FACTS=1` with `SEVERIAN_FACTS_URL` /
`SEVERIAN_FACTS_MODEL=openai/gpt-4.1-mini`, `SEVERIAN_DATE_STAMPS=1`, `SEVERIAN_TRUTH_RESOLUTION=1`,
`SEVERIAN_OPT_NUMPY=1`, `SEVERIAN_RERANK=cross-encoder/ms-marco-MiniLM-L6-v2`, `SEVERIAN_CONFLICT_NOTES=1`, `TOPK=20`.
Hermes-default rows use `SEVERIAN_OMNI_LIMIT=10` and `SEVERIAN_OMNI_MAX_CHARS=8000`.

    ./scripts/run_locomo_eval.sh --lib severian --env .env.severian --version p1 --workers 4 --top-k 20
    ./scripts/run_lme_eval.sh    --lib severian --env .env.severian --version p1 --workers 8 --top-k 20
    ./scripts/run_beam_eval.sh   --lib severian --env .env.severian --version p1 --workers 4 --top-k 20 --scale 100k

## Archive and redistribution

The judged per-question archive is held at
`~/data/benchmarks/_records/omnimemeval-2026-10/results-per-question.tar.zst`
(sha256 `303e2486850257ad39a94315b3ceae480e5a36f92cc51803cfa29c1f928f34be`).
`sha256sum -c SHA256SUMS` passes for the directory (verified 20 November 2026).

The archive does not carry the LoCoMo, LongMemEval-S or BEAM dataset licence
texts. Redistribution permission for these derived judged outputs has not been
established, so this repository commits the manifest, hashes and recompute
script only. It does not redistribute the per-question answers or contexts.
