"""Deterministic triage: code decides what a dreaming pass does with a pair of memories.

Owner decision (S2 §3): the classifier is code, never a model. A model that is configured for the
pass may only *write* text — the merge summary — and even then only after every decision about
merging, superseding or reporting has already been made here.

Three classes, all decided on token overlap and on what the diffing tokens are:

- ``contradict`` — the tokens that differ are **value bearing**: a digit, a negation, or a word that
  was capitalised in the original text (a name or a place). Two statements about the same thing that
  disagree about the value. Checked FIRST, at any overlap: one word barely moves Jaccard, so
  "the user has 2 cats" against "the user has 3 cats" overlaps at 0.83 and the duplicate band would
  otherwise merge two disagreeing facts.
- ``duplicate`` — no value difference, and Jaccard overlap at or above ``duplicate_jaccard`` (0.8).
  The same thing said again, near enough: the older head is superseded by the newer one.
- ``restate`` — overlap in the contradiction band and every differing token is soft (a verb, a
  preposition, a filler word). The same fact in different words, so there is nothing to resolve and
  nothing to write.

The OPS contradiction rule ("hide only when certain") is why ``contradict`` is report-only: a
pass may say two memories disagree, it may not choose between them.

This module is pure: it takes overlaps and token sets as input so it can be tested on its own, and
the pair enumeration (the MinHash-LSH candidate scan) stays in :mod:`severian.application.dreaming`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DUPLICATE = "duplicate"
RESTATE = "restate"
CONTRADICT = "contradict"

#: Every class a pair can be given, strongest first.
PAIR_KINDS = (DUPLICATE, CONTRADICT, RESTATE)

_DIGIT = re.compile(r"\d")
#: Words that flip the meaning of a statement. Tokenised form (no apostrophes: ``isn't`` -> ``isn``/``t``).
NEGATIONS = frozenset({"not", "no", "never", "none", "nobody", "nothing", "without", "cannot", "neither", "nor", "n't"})


@dataclass(frozen=True)
class PairVerdict:
    """What one pair is, and the evidence the classifier used."""

    memory_a_id: str
    memory_b_id: str
    kind: str
    overlap: float
    #: The tokens that differ, after stopwords: what the verdict was decided on.
    differing: tuple[str, ...] = ()


@dataclass
class Triage:
    """Every classified pair of one pass, in candidate order."""

    verdicts: list[PairVerdict] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[PairVerdict]:
        return [verdict for verdict in self.verdicts if verdict.kind == kind]

    @property
    def duplicates(self) -> list[PairVerdict]:
        return self.of_kind(DUPLICATE)

    @property
    def contradictions(self) -> list[PairVerdict]:
        return self.of_kind(CONTRADICT)

    @property
    def restatements(self) -> list[PairVerdict]:
        return self.of_kind(RESTATE)

    def counts(self) -> dict[str, int]:
        return {kind: len(self.of_kind(kind)) for kind in PAIR_KINDS}


def proper_tokens(text: str) -> set[str]:
    """Casefolded tokens that appear capitalised in ``text`` (names, places, products).

    A capitalised word is the cheapest deterministic signal that a difference is about *which*
    thing is meant rather than how it is phrased: "the flat is in Leeds" against "the flat is in
    Bradford" must never be read as a restatement.
    """
    return {match.casefold() for match in re.findall(r"[^\W\d_]\w*", text) if match[:1].isupper()}


def value_bearing(token: str, *, proper: frozenset[str] | set[str]) -> bool:
    """``True`` when ``token`` carries a value a restatement cannot change: a number, a negation, a name."""
    return bool(_DIGIT.search(token)) or token in NEGATIONS or token in proper


def classify_pair(
    *,
    overlap: float,
    differing: set[str] | frozenset[str],
    proper: set[str] | frozenset[str],
    duplicate_jaccard: float,
    contradiction_jaccard: float,
) -> str | None:
    """The class of one pair, or ``None`` when the pair is not about the same thing at all.

    ``differing`` is the symmetric difference of the two token sets after stopwords; ``proper`` is
    the union of the two texts' capitalised tokens (see :func:`proper_tokens`).
    """
    if overlap < contradiction_jaccard:
        return None
    # A value-bearing difference outranks the overlap band. One word is enough to change a fact and
    # barely moves Jaccard: "the user has 2 cats" against "the user has 3 cats" overlaps at 0.83,
    # which the duplicate band would have MERGED — silently retiring one of two disagreeing facts.
    # The OPS rule is to hide only when certain, so a value difference is never a merge.
    if any(value_bearing(token, proper=proper) for token in differing):
        return CONTRADICT
    if overlap >= duplicate_jaccard:
        return DUPLICATE
    return RESTATE


__all__ = [
    "CONTRADICT",
    "DUPLICATE",
    "NEGATIONS",
    "PAIR_KINDS",
    "RESTATE",
    "PairVerdict",
    "Triage",
    "classify_pair",
    "proper_tokens",
    "value_bearing",
]
