"""Deterministic entity/relation extraction for the derive path (P1c).

The derive pipeline is fully deterministic (no LLM in the default path).
Entity extraction must be too. This module provides a rule-based,
reproducible extractor that:

- recognises proper-noun phrases via capitalization + known suffix cues
  (Inc, Ltd, Corp, University, Project, Team, etc.);
- classifies known kinds (person/org/place/project/product/team/other)
  via deterministic heuristics (title suffixes, generic suffixes);
- links entities by (kind, name.casefold()) — the same identity the
  repository's add_entity uses, so repeated observations converge on
  the same Entity row (no duplicates);
- extracts typed relations from a small, explicit template table
  (``X works at Y``, ``X is the lead of Y``, ``X is in Y``...);
- is scope-bound and non-mutating on its own — callers (the derive
  service) perform the upserts through the repository.

No LLM, no heuristics that depend on download state, no randomness:
the same observation content always yields the same entity/relation
set. This is what keeps the graph deterministic under the platform's
P0 guarantees.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from severian.application.ports import CanonicalRepository
from severian.domain.models import Scope

# -- deterministic heuristics ------------------------------------------------

_ORG_SUFFIXES = ("inc", "ltd", "corp", "corporation", "llc", "gmbh", "ag", "plc", "co")
_PERSON_TITLES = ("dr", "prof", "mr", "mrs", "ms", "ceo", "cto", "cfo", "founder", "lead", "engineer")
_PLACE_CUES = ("office", "university", "headquarters", "campus", "region")
_PROJECT_CUES = ("project", "initiative", "program", "programme")
_TEAM_CUES = ("team", "group", "squad", "crew")

# relation templates: (pattern, predicate) — subject is the first
# capitalised entity, object is the entity after the trigger phrase.
_RELATION_TEMPLATES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bworks?\s+at\b", re.I), "works_at"),
    (re.compile(r"\bworks?\s+with\b", re.I), "works_with"),
    (re.compile(r"\bis\s+(?:the\s+)?(?:lead|head|director|founder|ceo|cto)\s+of\b", re.I), "leads"),
    (re.compile(r"\bis\s+(?:a\s+)?member\s+of\b", re.I), "member_of"),
    (re.compile(r"\bpart\s+of\b", re.I), "part_of"),
    (re.compile(r"\bstaffed\s+by\b", re.I), "staffed_by"),
    (re.compile(r"\blocated\s+in\b", re.I), "located_in"),
    (re.compile(r"\bbased\s+in\b", re.I), "based_in"),
    (re.compile(r"\buses\b", re.I), "uses"),
    (re.compile(r"\bdeployed\s+to\b", re.I), "deployed_to"),
]

# proper-noun phrase: capitalised words, optionally preceded by an
# adjective determiner (the/this/our) or possessive. We deliberately
# avoid single bare capitalised words to reduce noise ("The", "I").
_PROPER_NOUN = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b")

# common non-entity words that appear capitalised at sentence start.
_STOP = {
    "the", "this", "that", "these", "those", "our", "their", "its",
    "a", "an", "and", "or", "but", "in", "on", "at", "of", "for",
    "with", "by", "to", "from", "after", "before", "during", "every",
    "how", "what", "when", "where", "who", "why", "which", "while",
    "old", "new", "current", "final", "one", "two", "three",
    "four", "five", "six", "seven", "eight", "nine", "ten", "is", "are",
    "was", "were", "has", "have", "had", "does", "do", "done", "being",
}

# articles/determiners that can precede a proper noun and are NOT part
# of the entity name ("The New Project" -> "New Project"). General
# stopwords like "new"/"old" ARE kept when they form part of a name.
_LEADING_TRIMMERS = {"the", "this", "that", "these", "those", "our", "their", "its", "a", "an"}


@dataclass
class ExtractionResult:
    """Deterministic extraction output; entities/relations are upserted
    by the caller (derive service) through the repository."""

    entities: list[tuple[str, str]] = field(default_factory=list)  # (kind, name)
    relations: list[tuple[str, str, str]] = field(default_factory=list)  # (subject, predicate, object)
    source_observation_id: str = ""

    def summary(self) -> dict[str, int]:
        return {"entities": len(self.entities), "relations": len(self.relations)}


def _classify(name: str) -> str:
    """Deterministic kind classification from name cues."""
    lower = name.casefold()
    if any(lower.endswith(suffix) for suffix in _ORG_SUFFIXES):
        return "org"
    if any(cue in lower for cue in _PROJECT_CUES):
        return "project"
    if any(cue in lower for cue in _TEAM_CUES):
        return "team"
    if any(cue in lower for cue in _PLACE_CUES):
        return "place"
    if any(lower.startswith(title) or f" {title} " in f" {lower} " for title in _PERSON_TITLES):
        return "person"
    return "other"


def extract_entities(content: str) -> list[tuple[str, str]]:
    """Deterministic proper-noun entity candidates.

    Returns (kind, name) pairs, deduplicated by (kind, name.casefold())
    preserving first-seen order. Capitalised phrases are candidates;
    stopwords and sentence-initial articles are excluded. Names that
    start with a stopword (e.g. \"The Old Password\") are trimmed.
    """
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for match in _PROPER_NOUN.finditer(content):
        phrase = match.group(0).strip()
        # Trim a leading article/determiner (The/Our/This...) — not an
        # entity head, but keep the rest of the phrase. General
        # stopwords like "New"/"Old" are kept: they can be meaningful
        # name components ("New Project").
        words = phrase.split()
        while words and words[0].casefold() in _LEADING_TRIMMERS:
            words = words[1:]
        if not words:
            continue
        name = " ".join(words)
        # reject if the whole remaining phrase is a stopword/function word
        if name.casefold() in _STOP or len(name) < 2:
            continue
        # reject pure-sentence-start noise: single capitalised word that
        # is a common verb/adjective (Old, New, Current, Final, Every)
        if len(words) == 1 and words[0].casefold() in _STOP:
            continue
        kind = _classify(name)
        key = (kind, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append((kind, name))
    return result


def extract_relations(content: str, entities: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Deterministic typed-relation extraction.

    For each template trigger found in ``content``, the nearest entity
    BEFORE the trigger is the subject and the nearest entity AFTER is
    the object. Returns (subject_name, predicate, object_name) tuples
    with names exactly as extracted (callers resolve to ids).
    """
    results: list[tuple[str, str, str]] = []
    # Build positions of entity mentions (by name, first occurrence).
    positions: list[tuple[int, str]] = []
    lower_content = content.casefold()
    for _kind, name in entities:
        idx = lower_content.find(name.casefold())
        if idx >= 0:
            positions.append((idx, name))
    if not positions:
        return results
    positions.sort(key=lambda item: item[0])

    for pattern, predicate in _RELATION_TEMPLATES:
        for match in pattern.finditer(lower_content):
            trigger_mid = match.start() + (match.end() - match.start()) // 2
            before = [name for pos, name in positions if pos < trigger_mid]
            after = [name for pos, name in positions if pos > trigger_mid]
            if not before or not after:
                continue
            subject = before[-1]
            obj = after[0]
            # skip self-relations
            if subject.casefold() == obj.casefold():
                continue
            results.append((subject, predicate, obj))
    # dedupe preserving order
    seen: set[tuple[str, str, str]] = set()
    deduped: list[tuple[str, str, str]] = []
    for triple in results:
        key = (triple[0].casefold(), triple[1], triple[2].casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(triple)
    return deduped


class EntityExtractionService:
    """Deterministic graph extraction wired into the derive path."""

    def __init__(self, repository: CanonicalRepository) -> None:
        self._repository = repository

    def extract_and_upsert(
        self,
        scope: Scope,
        observation_id: str,
        content: str,
    ) -> ExtractionResult:
        """Extract entities + relations from content and upsert them.

        Uses the repository's identity-deduped add_entity/add_relation:
        repeated observations converge on the same entity rows and the
        same typed relations (no duplicates, deterministic).
        """
        entity_pairs = extract_entities(content)
        relation_triples = extract_relations(content, entity_pairs)

        entity_ids: dict[str, str] = {}
        for kind, name in entity_pairs:
            entity = self._repository.add_entity(scope, kind, name)
            entity_ids[name.casefold()] = entity.id

        for subject, predicate, obj in relation_triples:
            subject_id = entity_ids.get(subject.casefold())
            object_id = entity_ids.get(obj.casefold())
            if subject_id is None or object_id is None:
                continue
            self._repository.add_relation(scope, subject_id, predicate, object_id)

        return ExtractionResult(
            entities=entity_pairs,
            relations=relation_triples,
            source_observation_id=observation_id,
        )
