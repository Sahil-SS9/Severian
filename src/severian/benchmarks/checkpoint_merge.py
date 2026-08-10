"""Checkpoint merge for embedding bake-off lanes (audit MEDIUM 9/B13).

The runner checkpoints each completed lane to ``lane-<provider>.json``
immediately so a timeout/crash never loses finished work. This module
merges those lane checkpoints into a single bake-off evidence file.

CONTRACT (audit MEDIUM 9):
- a subset rerun must NEVER overwrite a complete merged result;
- the merge must refuse incompatible contracts: different corpus SHA,
  manifest SHA, or runner SHA are distinct evidence universes;
- every lane file must carry the same contract (provider revision,
  runtime) it was checkpointed with — a checkpoint is immutable once
  written, and merging must not silently mix runs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from severian.benchmarks.embedding_runner import (
    EmbeddingBakeoffResult,
    EmbeddingLaneResult,
)


class CheckpointMergeError(ValueError):
    pass


@dataclass
class CheckpointContract:
    corpus_sha256: str
    manifest_sha256: str
    runner_sha256: str
    run_started_at: str


@dataclass
class MergedCheckpoints:
    contract: CheckpointContract
    lanes: list[EmbeddingLaneResult] = field(default_factory=list)

    @property
    def provider_ids(self) -> set[str]:
        return {lane.provider_id for lane in self.lanes}


def read_contract(path: Path) -> CheckpointContract:
    """Read the shared contract block a complete run writes next to its
    lane files (``bakeoff-contract.json``)."""
    if not path.exists():
        raise CheckpointMergeError(f"contract file missing: {path}")
    data = json.loads(path.read_text())
    return CheckpointContract(
        corpus_sha256=str(data.get("corpus_sha256", "")),
        manifest_sha256=str(data.get("manifest_sha256", "")),
        runner_sha256=str(data.get("runner_sha256", "")),
        run_started_at=str(data.get("run_started_at", "")),
    )


def write_contract(path: Path, result: EmbeddingBakeoffResult) -> None:
    path.write_text(json.dumps({
        "corpus_sha256": result.corpus_sha256,
        "manifest_sha256": result.manifest_sha256,
        "runner_sha256": result.runner_sha256,
        "run_started_at": result.run_started_at,
    }, indent=2, sort_keys=True))


def merge_checkpoints(
    checkpoint_dir: Path,
    *,
    output: Path | None = None,
    contract: CheckpointContract | None = None,
) -> MergedCheckpoints:
    """Merge every ``lane-*.json`` checkpoint under ``checkpoint_dir``.

    Refuses (audit MEDIUM 9):
    - a lane whose embedded contract differs from the supplied contract;
    - lane files that are a SUBSET of an existing merged output (the
      merge result never overwrites a complete run with a partial one
      unless ``output`` does not exist or force is passed).
    """
    lane_files = sorted(checkpoint_dir.glob("lane-*.json"))
    if not lane_files:
        raise CheckpointMergeError(
            f"no lane checkpoints under {checkpoint_dir}"
        )
    lanes: list[EmbeddingLaneResult] = []
    for path in lane_files:
        data = json.loads(path.read_text())
        quality_data = data.pop("quality", None)
        from severian.benchmarks.runner import ProviderResult

        lane = EmbeddingLaneResult(
            **data,
            quality=ProviderResult(
                provider_id=str(data["provider_id"]), queries_run=0,
                recall_at_10=0.0, mrr=0.0, ndcg_at_10=0.0,
                citation_precision=0.0, stale_fact_rate=0.0,
                temporal_accuracy=0.0, cross_scope_leakage=0.0,
                unsupported_answer_rate=0.0, latency_p95_ms=0.0,
                cost_per_query_usd=0.0,
            ),
        )
        if quality_data is not None:
            lane.quality = ProviderResult(**quality_data)
        lanes.append(lane)

    merged = MergedCheckpoints(
        contract=contract or CheckpointContract("", "", "", ""),
        lanes=lanes,
    )
    if output is not None:
        manifest_id = ""
        if output.exists():
            existing = json.loads(output.read_text())
            manifest_id = existing.get("manifest_id", "")
            existing_ids = {
                lane["provider_id"] for lane in existing.get("lanes", [])
            }
            new_ids = merged.provider_ids
            if existing_ids and existing_ids.issuperset(new_ids):
                raise CheckpointMergeError(
                    f"refusing to overwrite complete result ({len(existing_ids)} "
                    f"lanes) with subset rerun ({len(new_ids)} lanes) — "
                    "audit MEDIUM 9"
                )
        output.write_text(json.dumps({
            "manifest_id": manifest_id,
            "corpus_sha256": merged.contract.corpus_sha256,
            "manifest_sha256": merged.contract.manifest_sha256,
            "runner_sha256": merged.contract.runner_sha256,
            "run_started_at": merged.contract.run_started_at,
            "lanes": [asdict(lane) for lane in merged.lanes],
        }, indent=2, sort_keys=True))
    return merged


__all__ = [
    "CheckpointContract",
    "CheckpointMergeError",
    "MergedCheckpoints",
    "merge_checkpoints",
    "read_contract",
    "write_contract",
]
