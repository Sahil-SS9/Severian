# Benchmarks

| Document | What it covers |
|---|---|
| [omnimemeval-2026-10.md](omnimemeval-2026-10.md) | **Start here.** Severian on the OmniMemEval leaderboard: every system's published records (LoCoMo, LongMemEval, BEAM, PersonaMem v2, HaluMem) with Severian's row added, the LCM-X validation re-run, robustness checks, settings and improvements |
| [RESULTS.md](RESULTS.md) | Canonical results index: headline numbers, configurations, the 0.01 reconciliation, the NDCG audit and the reproduction route |
| [METHODOLOGY.md](METHODOLOGY.md) | Benchmark layers, fairness rules, configurations, how to reproduce and how labels are controlled |
| [competitor-round-2026-10.md](competitor-round-2026-10.md) | Earlier internal round: Severian against Mem0, Mnemosyne, Hindsight and Honcho on LoCoMo, BEAM and LongMemEval, with method, intervals and footnotes |
| [provider-benchmark.md](provider-benchmark.md) | The in-repo benchmark runner and manifest format |
| [strategy.md](strategy.md) | The August 2026 benchmark strategy (internal instruments, before the public-benchmark round) |
| `omnimemeval/` | The OmniMemEval adapter, `results-2026-10.json`, the per-question archive manifest and the offline recompute script |
| `manifest-*.json`, `corpus/` | Frozen benchmark inputs (checksummed) |
| `evidence/` | Raw outputs behind the internal results, one folder per run |
| [history/](history/) | August-era instruments retained as provenance: the old results framework, comparison matrix, scale lanes and dated working notes |

The August-era files are historical. They are not current results and should not be quoted without their dates.
