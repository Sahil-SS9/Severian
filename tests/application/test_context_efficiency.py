"""The three context-efficiency switches through the service and the Hermes prefetch path.

Each switch is off by default and, with every switch off, the assembled context is byte-for-byte what 0.3.11
returned: ``[<memory id>] <text>\\nsource: <observation ids>`` blocks joined by a blank line, no note. With a switch
on, only that switch's behaviour changes, and every id the block cites still resolves through the agent tools.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.fillbudget import pool_limit
from severian.application.sufficiency import NOTE
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope
from severian.integrations.hermes import SeverianMemoryProvider

A = Scope(tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a", session_id="s1")
B = Scope(tenant_id="t", profile_id="bob", collection_id="profile", agent_id="a", session_id="s1")
SWITCHES = ("SEVERIAN_FILL_BUDGET", "SEVERIAN_COMPACT_CITES", "SEVERIAN_SUFFICIENCY")
MEMORY = re.compile(r"^\[([0-9a-f-]{36})\] ")


@dataclass
class PassThrough:
    """Saves each turn as one memory, word for word, like the default local derivation."""

    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content, "confidence": 0.9}]


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    built: list[ServiceBundle] = []

    def factory(*on: str) -> ServiceBundle:
        for name in SWITCHES:
            if name in on:
                monkeypatch.setenv(name, "1")
            else:
                monkeypatch.delenv(name, raising=False)
        where = tmp_path / f"store{len(built)}"
        where.mkdir()
        bundle = build_bundle(database=where / "s.db", fts=where / "s.fts", vectors=where / "s.vec")
        built.append(bundle)
        return bundle

    yield factory
    for bundle in built:
        bundle.close()


def save(bundle: ServiceBundle, scope: Scope, *contents: str, session: str = "s0001") -> None:
    for turn, content in enumerate(contents, 1):
        bundle.service.ingest_observation(
            scope,
            content=content,
            source={"session_id": session, "turn": turn, "timestamp": "2024-03-15T09:00:00+00:00"},
        )
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0


def context(bundle: ServiceBundle, scope: Scope, query: str, *, limit: int = 10, max_chars: int | None = 8_000) -> str:
    hits = bundle.service.search(scope, query=query, limit=limit)
    return bundle.service.assemble_context(scope, hits, max_chars=max_chars, query=query)


def cited_ids(block: str) -> list[str]:
    return re.findall(r"\[([0-9a-f-]{36}(?:,[0-9a-f-]{36})*)\]", block)


TURNS = (
    "user: The sprint 1 deadline is April 1, 2024.\nassistant: Noted, April 1 it is.",
    "user: I moved the sprint 1 deadline to April 5, 2024.\nassistant: The deadline is now April 5.",
    "user: I set up the Trello board for sprint 1.\nassistant: Good, keep the cards moving.",
)


def test_off_is_the_legacy_block_byte_for_byte(make) -> None:
    bundle = make()
    save(bundle, A, *TURNS)
    block = context(bundle, A, "When is the sprint 1 deadline?")
    assert "source: " in block, "off: the legacy trailing source line is still there"
    assert NOTE not in block
    for line in block.split("\n\n"):
        head, _, tail = line.partition("] ")
        assert MEMORY.match(line), f"off: every block starts with the memory id in brackets: {head!r}"
        assert tail.startswith("user:"), "off: the text follows the bracket unchanged"
        assert "\nsource: " in tail, "off: the source line is the last line of the block"


def test_compact_cites_spend_less_and_still_resolve(make) -> None:
    off_bundle = make()
    save(off_bundle, A, *TURNS)
    off = context(off_bundle, A, "When is the sprint 1 deadline?")

    on_bundle = make("SEVERIAN_COMPACT_CITES")
    save(on_bundle, A, *TURNS)
    on = context(on_bundle, A, "When is the sprint 1 deadline?")

    assert "source: " not in on, "compact: the trailing source line is gone"
    assert len(on) < len(off), "compact: the same items take less room"
    ids = [part for cited in cited_ids(on) for part in cited.split(",")]
    assert ids, "compact: the block still cites ids"
    for cited in ids:
        resolved = on_bundle.service.get_observation(A, cited)  # provenance: every citation resolves
        assert resolved.id == cited
    memories = {hit.memory_id for hit in on_bundle.service.search(A, query="sprint 1 deadline", limit=10)}
    memory_ids = [part for cited in cited_ids(on) for part in cited.split(",")]
    assert not (set(memory_ids) & memories), "compact: the ids in the brackets are observation ids, not memory ids"


def test_fill_budget_adds_items_only_where_there_is_room(make) -> None:
    probe = "ledger item reviewed"
    assert pool_limit(10) == 10
    provider_off = _provider(make(), max_context_chars=20_000)
    for i in range(20):
        provider_off.sync_turn(f"note {i}: the ledger item number {i} was reviewed.", "ok")
    off = provider_off.prefetch(probe, derive=True, limit=10)
    assert len([line for line in off.split("\n\n") if "source: " in line]) <= 10, "off: exactly the top limit"

    provider_on = _provider(make("SEVERIAN_FILL_BUDGET"), max_context_chars=20_000)
    for i in range(20):
        provider_on.sync_turn(f"note {i}: the ledger item number {i} was reviewed.", "ok")
    on = provider_on.prefetch(probe, derive=True, limit=10)
    assert len([line for line in on.split("\n\n") if "source: " in line]) > 10, (
        "fill: with room to spare the block keeps adding ranked items past limit"
    )


def test_fill_budget_leaves_a_tight_budget_alone(make) -> None:
    tight = _provider(make(), max_context_chars=300)
    for i in range(20):
        tight.sync_turn(f"note {i}: the ledger item number {i} was reviewed.", "ok")
    off = tight.prefetch("ledger item reviewed", derive=True, limit=10)

    tight_on = _provider(make("SEVERIAN_FILL_BUDGET"), max_context_chars=300)
    for i in range(20):
        tight_on.sync_turn(f"note {i}: the ledger item number {i} was reviewed.", "ok")
    on = tight_on.prefetch("ledger item reviewed", derive=True, limit=10)
    # The two stores mint their own ids, so compare the visible text, not the uuids.
    assert _without_ids(on) == _without_ids(off), (
        "fill: a tight budget runs out inside the first limit items, exactly as today"
    )


def _without_ids(block: str) -> str:
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "ID", block)


def test_sufficiency_note_goes_last_and_costs_only_its_own_line(make) -> None:
    bundle = make("SEVERIAN_SUFFICIENCY")
    save(bundle, A, *TURNS)
    block = context(bundle, A, "What specific criteria did I use to prioritize tasks on the Trello board?")
    assert block.endswith(NOTE), "the note is one line after the results"
    assert "user: I set up the Trello board for sprint 1." in block, "the results are still there"
    plain = context(bundle, A, "When is the sprint 1 deadline?")
    assert NOTE not in plain, "never fire on a question the returned items answer"


def test_all_three_together_stay_inside_the_budget_and_keep_provenance(make) -> None:
    bundle = make(*SWITCHES)
    save(bundle, A, *TURNS)
    block = context(bundle, A, "What specific criteria did I use for sprint 1?", max_chars=700)
    assert len(block) <= 700
    assert block.endswith(NOTE)
    assert "source: " not in block
    for cited in (part for ids in cited_ids(block) for part in ids.split(",")):
        assert bundle.service.get_observation(A, cited).id == cited


def test_no_read_path_returns_another_profiles_records(make) -> None:
    bundle = make(*SWITCHES)
    save(bundle, A, "user: Alice's sprint 1 deadline is April 1, 2024.\nassistant: April 1.")
    save(bundle, B, "user: Bob's sprint 1 deadline is June 9, 2024.\nassistant: June 9.", session="s0002")
    block = context(bundle, A, "What specific criteria did I use for the sprint 1 deadline?", max_chars=8_000)
    assert "Alice" in block
    assert "Bob" not in block, "profile isolation: another profile's memory never appears"
    ids = {part for cited in cited_ids(block) for part in cited.split(",")}
    hits = bundle.service.search(A, query="sprint 1 deadline", limit=10)
    assert ids <= {obs for hit in hits for obs in hit.source_observation_ids} | set(), (
        "every cited id belongs to the caller's own scope"
    )


def legacy_assemble(results, limit: int) -> str:
    """0.3.11's assemble, written out here: the frozen reference the off path must still equal byte for byte."""
    blocks: list[str] = []
    used = 0
    for result in results:
        prefix = f"[{result.memory.id}] "
        suffix = f"\nsource: {', '.join(result.source_observation_ids)}"
        available = limit - used - len(prefix) - len(suffix) - 2
        if available <= 0:
            break
        block = f"{prefix}{result.memory.content[:available]}{suffix}"
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


@pytest.mark.parametrize("budget", [300, 900, 2_000, 8_000, 200_000])
def test_off_equals_the_frozen_0_3_11_assembler_byte_for_byte(
    make, budget: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from severian.application.retrieval import ContextAssembler, RetrievalResult

    monkeypatch.delenv("SEVERIAN_CONFLICT_NOTES", raising=False)  # the conflict note is on by default; keep it out
    monkeypatch.setenv("SEVERIAN_CONFLICT_NOTES", "0")
    bundle = make()
    save(bundle, A, *TURNS)
    hits = bundle.service.search(A, query="When is the sprint 1 deadline?", limit=10)
    results = [
        RetrievalResult(
            memory=memory,
            source_observation_ids=memory.source_observation_ids,
            lexical_score=hit.lexical_score,
            vector_score=hit.vector_score,
            fused_score=hit.fused_score,
        )
        for hit in hits
        for memory in [bundle.repository.get_memory(A, hit.memory_id)]
    ]
    # Explicitly off, so the comparison cannot depend on the environment the suite runs in.
    assert ContextAssembler(max_chars=budget).assemble(results, compact_cites=False, sufficiency=False) == (
        legacy_assemble(results, budget)
    )
    assert bundle.service.assemble_context(A, hits, max_chars=budget, query="When is the sprint 1 deadline?") == (
        legacy_assemble(results, budget)
    )


def _provider(bundle: ServiceBundle, *, max_context_chars: int = 900) -> SeverianMemoryProvider:
    provider = SeverianMemoryProvider(
        service=bundle.service, gateway=PassThrough(), max_context_chars=max_context_chars
    )
    provider.initialize(session_id="s1", tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a")
    return provider
