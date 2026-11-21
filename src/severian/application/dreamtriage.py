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
- ``compatible`` (0.6.2) — a value-bearing difference that is NOT one value changing inside one shared
  statement, so both memories can be true: the values sit in different sentences, the subject or the
  slot differs, one side only adds a detail, the difference is a provenance label, a date-time or an
  identifier, or the sentence frame carries a running figure. See :func:`value_edits`. Like
  ``contradict`` it is never a merge: precision must not buy merges.

The OPS contradiction rule ("hide only when certain") is why ``contradict`` is report-only: a
pass may say two memories disagree, it may not choose between them.

This module is pure: it takes overlaps and token sets as input so it can be tested on its own, and
the pair enumeration (the MinHash-LSH candidate scan) stays in :mod:`severian.application.dreaming`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from severian.domain.text import STOPWORDS

DUPLICATE = "duplicate"
RESTATE = "restate"
CONTRADICT = "contradict"
COMPATIBLE = "compatible"

#: Every class a pair can be given, strongest first.
PAIR_KINDS = (DUPLICATE, CONTRADICT, RESTATE, COMPATIBLE)

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


# -- 0.6.2: a contradiction is one value changing inside ONE shared statement -----------------------
# Measured on a copy of the live store (29k memories, R062 evidence): the overlap rule above called
# 120k pairs contradictions, ~4 per memory. Almost every one was a value-bearing token somewhere in
# two long, similar texts: a provenance label, a date-time, an id, or a number in a different
# sentence. A contradiction is narrower: the two memories share a sentence frame ("the office address
# is _ downing street") and only the slot's value differs. The rules below find exactly that shape.

#: One or more ``[...]`` labels at the start of a line: save stamps (``[2026/10/23 (Fri) 17:43]``),
#: import provenance (``[Mnemosyne working 2026-09-01]``) and category/source tags. Never a fact.
_LEADING_LABELS = re.compile(r"^(?:\s*\[[^\]\n]{0,200}\])+\s*")
#: Calendar dates, ISO date-times, clock times and bracketed weekdays inside the text: WHEN something
#: was said or seen. Two memories that differ only there are two sightings, not two answers.
_DATETIME = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\)"
)
_SENTENCE_BREAK = re.compile(r"[.!?;]+(?:\s+|$)|\s[—–-]+\s")
_WORD = re.compile(r"\w+", flags=re.UNICODE)
#: A letter+digit token of 6+ characters (commit hashes, uuid parts, ticket ids): it names WHICH
#: record, so a different id is a different event, not a different value. Short ones ("q3", "v2")
#: stay values.
_IDENTIFIER = re.compile(r"^(?=\w*\d)(?=\w*[^\W\d_])\w{6,}$")

#: Statements shorter than this have no frame to share.
_MIN_STATEMENT_WORDS = 3
#: Words both statements must share BEFORE the value: the subject and its predicate. A difference in
#: the first word is a different subject ("Alice works at" / "Bob works at").
_MIN_FRAME_PREFIX = 2
#: Longest value on either side, in words ("Northwind", "64", "not", "New York City").
MAX_VALUE_SPAN = 3
#: A pair with more changed statements than this is two versions of a report, not one disagreement.
MAX_EDITS_PER_PAIR = 3
#: A frame seen with more distinct values than this across one pass is a running figure or a log
#: line ("disk usage reached _ percent"), not a single fact that changed. Measured on the copy: see
#: the R062 evidence for the sweep that chose it.
MAX_FRAME_VALUES = 5


@dataclass(frozen=True)
class Statement:
    """One sentence of a memory, labels and date-times removed: casefolded words plus its names."""

    words: tuple[str, ...]
    #: Casefolded words capitalised AFTER the first word of this sentence (a sentence-initial capital
    #: says nothing about whether a word is a name).
    proper: frozenset[str]


@dataclass(frozen=True)
class ValueEdit:
    """One value that changes inside one shared statement frame."""

    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    value_a: tuple[str, ...]
    value_b: tuple[str, ...]

    @property
    def frame(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (self.prefix, self.suffix)


def statements(text: str) -> tuple[Statement, ...]:
    """The sentences of ``text`` that can carry a fact, with provenance labels and date-times removed."""
    out: list[Statement] = []
    for line in text.splitlines():
        line = _DATETIME.sub(" ", _LEADING_LABELS.sub("", line))
        for part in _SENTENCE_BREAK.split(line):
            raw = _WORD.findall(part)
            if len(raw) < _MIN_STATEMENT_WORDS:
                continue
            out.append(
                Statement(
                    words=tuple(word.casefold() for word in raw),
                    proper=frozenset(word.casefold() for word in raw[1:] if word[:1].isupper()),
                )
            )
    return tuple(out)


def _is_value(token: str, proper: frozenset[str]) -> bool:
    if _DIGIT.search(token):
        return not _IDENTIFIER.match(token)
    return token in NEGATIONS or token in proper


def statement_edit(a: Statement, b: Statement) -> ValueEdit | None:
    """The single value that changes between two statements of the same frame, or ``None``.

    Both statements must share an opening of at least two words with a content word in it (the
    subject and predicate), then differ in ONE span of at most :data:`MAX_VALUE_SPAN` words per side,
    then end alike. The span must hold a negation, or a value (number, name) on BOTH sides: a value on
    one side only is an added detail, and two spans with a shared word between them are two edits
    (a different slot, each with its own figure).
    """
    words_a, words_b = a.words, b.words
    if words_a == words_b:
        return None
    shortest = min(len(words_a), len(words_b))
    prefix = 0
    while prefix < shortest and words_a[prefix] == words_b[prefix]:
        prefix += 1
    if prefix < _MIN_FRAME_PREFIX or not any(word.isalpha() and word not in STOPWORDS for word in words_a[:prefix]):
        return None
    suffix = 0
    while suffix < shortest - prefix and words_a[-1 - suffix] == words_b[-1 - suffix]:
        suffix += 1
    value_a = words_a[prefix : len(words_a) - suffix]
    value_b = words_b[prefix : len(words_b) - suffix]
    if len(value_a) > MAX_VALUE_SPAN or len(value_b) > MAX_VALUE_SPAN:
        return None
    if set(value_a) & set(value_b):
        return None
    if not any(word in NEGATIONS for word in value_a + value_b):
        proper = a.proper | b.proper
        if not (any(_is_value(word, proper) for word in value_a) and any(_is_value(word, proper) for word in value_b)):
            return None
    return ValueEdit(
        prefix=words_a[:prefix],
        suffix=words_a[len(words_a) - suffix :],
        value_a=value_a,
        value_b=value_b,
    )


def value_edits(a: Sequence[Statement], b: Sequence[Statement]) -> list[ValueEdit]:
    """Every distinct value edit between the statements of two memories, in statement order.

    Only statements opening with the same two words can share a frame, so ``b`` is indexed by its
    opening and each statement of ``a`` is compared with its few same-opening partners only.
    """
    by_opening: dict[tuple[str, ...], list[Statement]] = {}
    for statement in b:
        by_opening.setdefault(statement.words[:_MIN_FRAME_PREFIX], []).append(statement)
    edits: list[ValueEdit] = []
    seen: set[ValueEdit] = set()
    for statement in a:
        for partner in by_opening.get(statement.words[:_MIN_FRAME_PREFIX], ()):
            edit = statement_edit(statement, partner)
            if edit is not None and edit not in seen:
                seen.add(edit)
                edits.append(edit)
    return edits


__all__ = [
    "COMPATIBLE",
    "CONTRADICT",
    "DUPLICATE",
    "MAX_EDITS_PER_PAIR",
    "MAX_FRAME_VALUES",
    "MAX_VALUE_SPAN",
    "NEGATIONS",
    "PAIR_KINDS",
    "RESTATE",
    "PairVerdict",
    "Statement",
    "Triage",
    "ValueEdit",
    "classify_pair",
    "proper_tokens",
    "statement_edit",
    "statements",
    "value_bearing",
    "value_edits",
]
