"""Handoff 3: make the answerer actually use the memory it is given (SEVERIAN_CONTEXT_FORMAT=v2).

Benchmarks showed Severian often FINDS the right memory but the answering model ignores it. The v2
context block is the fix: most relevant note first, each note marked current/historical and dated,
a one-line "most directly relevant" lead, a compact evidence quote per user fact, and an explicit
"say you don't know" rule. Off (unset, or any other value): the v1 format, byte for byte, and no
read path may ever cross the five-dimension scope.

Ported onto 0.3.11 from `d956996` (feat/answer-memory-2026-10-09) for Handoff 2. Two changes since
that commit: the service is composed through `build_bundle`, and 0.3.11's `assemble_context` takes
the question as `query` (v2 renders its own lead, so it does not need it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from severian.application.context_v2 import ContextItem, ContextV2Assembler, context_v2_enabled
from severian.application.retrieval import ContextAssembler, RetrievalResult
from severian.application.service import SearchHitView
from severian.composition import build_bundle
from severian.domain.errors import NotFoundError
from severian.domain.models import Collection, Memory, Scope


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _scope(tenant: str = "t1", profile: str = "p1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id=profile,
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited SEVERIAN_* settings must never decide what this module asserts."""
    for name in [key for key in os.environ if key.startswith("SEVERIAN_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.repository.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    built.gateway = FakeGateway()
    yield built
    built.close()


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", "v2")


def _hit(
    scope: Scope,
    memory_id: str,
    content: str,
    *,
    fused: float = 1.0,
    truth: str = "current",
    obs: tuple[str, ...] = ("o1",),
) -> SearchHitView:
    return SearchHitView(
        memory_id=memory_id,
        content=content,
        source_observation_ids=list(obs),
        lexical_score=0.1,
        vector_score=0.1,
        fused_score=fused,
        scope=scope,
        truth_state=truth,
    )


def _memory(scope: Scope, content: str, obs_id: str, *, kind: str = "fact",
            created: datetime | None = None) -> Memory:
    memory = Memory(
        scope=scope,
        kind=kind,
        content=content,
        source_observation_ids=[obs_id],
        model_id="test/model",
        prompt_version="v2",
        confidence=0.8,
    )
    if created is not None:
        memory.created_at = created
    return memory


def _store(bundle, scope: Scope, content: str, *, kind: str = "fact",
           created: datetime | None = None) -> tuple[str, Memory]:
    """Insert one observation + memory directly (a user_fact is normally made by the background pass)."""
    observation = bundle.service.ingest_observation(scope, content=content, source={"uri": f"n://{content[:20]}"})
    memory = _memory(scope, content, observation.observation_id, kind=kind, created=created)
    bundle.repository.add_memory(memory)
    return observation.observation_id, memory


# -- the switch -----------------------------------------------------------------------------

def test_switch_is_off_unless_exactly_v2(monkeypatch):
    monkeypatch.delenv("SEVERIAN_CONTEXT_FORMAT", raising=False)
    assert not context_v2_enabled()
    for value in ("", "v1", "off", "V3", "v2x", "2"):
        monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", value)
        assert not context_v2_enabled(), value
    monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", "v2")
    assert context_v2_enabled()
    monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", " V2 ")
    assert context_v2_enabled()


def test_off_equals_the_v1_assembler_byte_for_byte(bundle, monkeypatch):
    """Switches off: assemble_context must equal the old context, exactly, for every non-v2 value."""
    scope = _scope()
    bundle.service.ingest_observation(scope, content="Ada founded a company", source={"uri": "a://1"})
    bundle.service.ingest_observation(scope, content="Ada loves maths", source={"uri": "a://2"})
    hits = bundle.service.search(scope, query="Ada", limit=5, derive=True, gateway=bundle.gateway)
    assert hits
    reference_hits = [
        RetrievalResult(
            memory=bundle.repository.get_memory(scope, hit.memory_id),
            source_observation_ids=hit.source_observation_ids,
            lexical_score=hit.lexical_score,
            vector_score=hit.vector_score,
            fused_score=hit.fused_score,
        )
        for hit in hits
    ]
    reference = ContextAssembler(max_chars=2000).assemble(reference_hits)

    monkeypatch.delenv("SEVERIAN_CONTEXT_FORMAT", raising=False)
    assert bundle.service.assemble_context(scope, hits, max_chars=2000) == reference
    for value in ("v1", "off", "banana"):
        monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", value)
        assert bundle.service.assemble_context(scope, hits, max_chars=2000) == reference
    # And the v2-only surface says "not active" while off, so the benchmark falls back to v1 items.
    assert bundle.service.context_block(scope, hits) is None


# -- the v2 block ---------------------------------------------------------------------------

def test_v2_has_a_most_directly_relevant_first_lead_and_a_dont_know_rule(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    bundle.service.ingest_observation(scope, content="Ada founded a company", source={"uri": "a://1"})
    hits = bundle.service.search(scope, query="Ada", limit=5, derive=True, gateway=bundle.gateway)
    block = bundle.service.assemble_context(scope, hits, max_chars=4000)

    first_line = block.splitlines()[0]
    assert "most directly relevant" in first_line
    assert "shown first" in first_line
    assert "say you don't know" in block


def test_v2_keeps_the_search_order_note_most_relevant_first(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    _, first = _store(bundle, scope, "FIRST-AND-MOST-RELEVANT", created=datetime(2026, 9, 12, tzinfo=UTC))
    _, second = _store(bundle, scope, "SECOND-LESS-RELEVANT", created=datetime(2026, 9, 13, tzinfo=UTC))
    hits = [_hit(scope, first.id, first.content, fused=9.0), _hit(scope, second.id, second.content, fused=1.0)]
    block = bundle.service.context_block(scope, hits)
    assert block is not None
    assert block.index("1. [current") < block.index("2. [current")
    assert block.index("1. [current") < block.index("FIRST-AND-MOST-RELEVANT")
    assert block.index("FIRST-AND-MOST-RELEVANT") < block.index("SECOND-LESS-RELEVANT")


def test_v2_marks_current_change_and_earlier_notes(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    hits = []
    for truth in ("current", "transition", "historical", "concurrent"):
        _, memory = _store(bundle, scope, f"NOTE-{truth}", created=datetime(2026, 9, 12, tzinfo=UTC))
        hits.append(_hit(scope, memory.id, memory.content, truth=truth))
    block = bundle.service.context_block(scope, hits)
    assert block is not None
    assert "[current, said 2026/09/12]" in block
    assert "[change, replaces an earlier note, said 2026/09/12]" in block
    assert "[earlier, replaced, said 2026/09/12]" in block
    assert "[also true together, said 2026/09/12]" in block


def test_v2_dates_come_from_the_content_label_first_and_created_at_otherwise(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    _, labelled = _store(
        bundle, scope, "[2026/01/28 (Wed) 04:00] user: We moved to Bath", created=datetime(2026, 9, 1, tzinfo=UTC)
    )
    _, undated = _store(bundle, scope, "No label here", created=datetime(2026, 3, 1, tzinfo=UTC))
    block = bundle.service.context_block(
        scope, [_hit(scope, labelled.id, labelled.content), _hit(scope, undated.id, undated.content)]
    )
    assert block is not None
    assert "said 2026/01/28" in block  # from the label, not created_at
    assert "said 2026/03/01" in block  # from created_at when there is no label
    assert "said 2026/09/01" not in block


def test_v2_facts_date_from_the_source_turn_when_the_fact_has_no_label(bundle, monkeypatch):
    """A user fact is a clean sentence; its honest date is the turn it came from, not save time."""
    _on(monkeypatch)
    scope = _scope()
    turn = bundle.service.ingest_observation(
        scope, content="[2023/05/25 (Thu) 19:20] user: I moved to York\nassistant: nice", source={"turn": 1}
    )
    fact = _memory(scope, "The user moved to York.", turn.observation_id, kind="user_fact",
                   created=datetime(2026, 10, 9, tzinfo=UTC))
    bundle.repository.add_memory(fact)
    block = bundle.service.context_block(scope, [_hit(scope, fact.id, fact.content, obs=(turn.observation_id,))])
    assert block is not None
    assert "said 2023/05/25" in block
    assert "said 2026/10/09" not in block


def test_v2_quotes_a_user_fact_with_the_users_own_words(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    turn = bundle.service.ingest_observation(
        scope, content="user: I don't eat shellfish\nassistant: noted", source={"turn": 1}
    )
    fact = _memory(scope, "The user does not eat shellfish.", turn.observation_id, kind="user_fact")
    bundle.repository.add_memory(fact)
    other = bundle.service.ingest_observation(
        scope, content="user: I love pasta\nassistant: yum", source={"turn": 2}
    )
    plain = _memory(scope, "Fact: user loves pasta.", other.observation_id, kind="fact")
    bundle.repository.add_memory(plain)

    block = bundle.service.context_block(
        scope,
        [_hit(scope, fact.id, fact.content, obs=(turn.observation_id,)),
         _hit(scope, plain.id, plain.content, obs=(other.observation_id,))],
    )
    assert block is not None
    assert 'the user said: "I don\'t eat shellfish"' in block
    assert '"I love pasta"' not in block  # only user facts carry the quote marker


def test_v2_quote_is_cut_to_a_compact_length(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    long_turn = "user: " + ("I love baking cakes and decorating them " * 40) + "\nassistant: lovely"
    observation = bundle.service.ingest_observation(
        scope, content=f"[2026/09/13 (Sun) 10:00] {long_turn}", source={"turn": 1}
    )
    fact = _memory(scope, "The user loves baking.", observation.observation_id, kind="user_fact")
    bundle.repository.add_memory(fact)
    block = bundle.service.context_block(
        scope, [_hit(scope, fact.id, fact.content, obs=(observation.observation_id,))]
    )
    assert block is not None
    quote_line = next(line for line in block.splitlines() if "the user said:" in line)
    assert quote_line.endswith('…"')
    assert len(quote_line) < 260  # compact quote, not the whole turn


def test_v2_never_exceeds_max_chars(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    hits = []
    for i in range(3):
        _, memory = _store(bundle, scope, f"MEMORY-{i}-" + "x" * 3000, created=datetime(2026, 9, 12, tzinfo=UTC))
        hits.append(_hit(scope, memory.id, memory.content))
    block = bundle.service.context_block(scope, hits, max_chars=1000)
    assert block is not None
    assert len(block) <= 1000
    assert "say you don't know" in block  # the rule is reserved, never trimmed away
    assert "most directly relevant" in block
    tiny = bundle.service.context_block(scope, hits, max_chars=64)
    assert tiny is not None and len(tiny) <= 64


def test_v2_block_is_the_same_block_the_context_and_the_hermes_path_get(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    bundle.service.ingest_observation(scope, content="Ada founded a company", source={"uri": "a://1"})
    hits = bundle.service.search(scope, query="Ada", limit=5, derive=True, gateway=bundle.gateway)
    assert bundle.service.context_block(scope, hits) == bundle.service.assemble_context(scope, hits)


def test_v2_rejects_cross_scope_hits_and_forged_memory_ids(bundle, monkeypatch):
    _on(monkeypatch)
    p1, p2 = _scope("t1", "p1"), _scope("t1", "p2")
    bundle.service.ingest_observation(p1, content="P1-EVIDENCE", source={"uri": "p1://1"})
    _, secret = _store(bundle, p2, "P2-SECRET", created=datetime(2026, 9, 12, tzinfo=UTC))
    p2_hits = [_hit(p2, secret.id, secret.content)]

    with pytest.raises(NotFoundError):
        bundle.service.context_block(p1, p2_hits)
    with pytest.raises(NotFoundError):
        bundle.service.assemble_context(p1, p2_hits)

    forged = _hit(p1, secret.id, "STALE-VIEW-CONTENT")  # scope says p1, the memory belongs to p2
    with pytest.raises(NotFoundError):
        bundle.service.context_block(p1, [forged])


def test_v2_renders_from_the_canonical_memory_not_the_view(bundle, monkeypatch):
    _on(monkeypatch)
    scope = _scope()
    _, memory = _store(bundle, scope, "CANONICAL-CONTENT", created=datetime(2026, 9, 12, tzinfo=UTC))
    forged = _hit(scope, memory.id, "STALE-VIEW-CONTENT")
    block = bundle.service.context_block(scope, [forged])
    assert block is not None
    assert "CANONICAL-CONTENT" in block
    assert "STALE-VIEW-CONTENT" not in block


def test_v2_carries_the_disagreement_in_its_note_labels_not_a_trailing_note(bundle, monkeypatch):
    """SEVERIAN_CONFLICT_NOTES is on by default; v2 states each note's status instead of appending a note.

    Handoff 2 asks for the order of the blocks to be decided and documented: the conflict information
    lives on the notes themselves (``earlier, replaced`` / ``change, replaces an earlier note``), so the
    trailing note is not repeated after the don't-know rule. Nothing is lost — the control below proves
    the pair really does disagree and that v1 does append the note.
    """
    from severian.application.conflicts import conflict_note

    scope = _scope()
    query = "How many coin toss problems have I completed?"
    _, denial = _store(
        bundle,
        scope,
        "The user has not completed any coin toss problems before.",
        created=datetime(2026, 9, 12, tzinfo=UTC),
    )
    _, claim = _store(
        bundle,
        scope,
        "The user has already completed 5 coin toss problems.",
        created=datetime(2026, 9, 13, tzinfo=UTC),
    )
    hits = [
        _hit(scope, claim.id, claim.content, truth="current"),
        _hit(scope, denial.id, denial.content, truth="historical"),
    ]
    note = conflict_note([claim.content, denial.content], query=query)
    assert note, "control: this pair does fire the conflict note"

    v1 = bundle.service.assemble_context(scope, hits, max_chars=4000, query=query)
    assert note in v1, "control: the v1 block appends it"

    monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", "v2")
    block = bundle.service.assemble_context(scope, hits, max_chars=4000, query=query)
    assert note not in block, "v2 says it on the notes themselves, so the trailing note is not repeated"
    assert "[earlier, replaced" in block and "[current" in block


def test_v2_assembler_is_pure_and_orders_items_as_given():
    """The assembler renders exactly what it is given — no reordering, no hidden extras."""
    item_a = ContextItem(content="A", state="current", said="2026/09/12", quote=None, sources="s1")
    item_b = ContextItem(content="B", state="also true together", said=None, quote="the words", sources="s2")
    block = ContextV2Assembler().assemble([item_a, item_b])
    assert block.index("A") < block.index("B")
    assert "[current, said 2026/09/12]" in block
    assert "[also true together]" in block
    assert 'the user said: "the words"' in block
    assert ContextV2Assembler().assemble([]) == ""
