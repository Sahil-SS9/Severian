"""Fix (b), ``SEVERIAN_CONFLICT_NOTES``: a short note when two returned memories disagree.

Round 3 (BEAM contradiction resolution, 10 questions): Severian returned both sides every time ("The user has not
completed any coin toss problems before." and "The user has already completed 5 coin toss problems"), but the
answer mentioned the conflict in only 2. Retrieval was fine; nothing said the two memories disagree.

A disagreement is a denial ("never ...", "has not ... any/ever/before/yet") and a plain statement by the same
person about the same thing: they share at least 2 topic words, and at least 60% of the denial's topic words.
Only the user's own words count: a saved turn's reply is not the user's claim, a question is not a claim, and in a
saved turn only sentences in the first person count. A denial is compared clause by clause ("I've never fixed
those bugs, but ..." compares only the first clause). Both statements must come from different memories. The note
names at most two disagreements. Off by default.
"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from severian.domain.text import STOPWORDS, porter_stem

_LABEL = re.compile(r"^\[[^\]\n]{1,80}\][ \t]*")
_SPEAKER = re.compile(r"^(?P<speaker>user|[A-Z][\w'’.-]{0,30}(?: [A-Z][\w'’.-]{0,30})?): (?P<body>.*)$", re.DOTALL)
_REPLY = "\nassistant: "
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r",?\s+(?:but|although|though|however|whereas)\s+|;\s*", re.IGNORECASE)
_DENIAL = re.compile(
    r"\bnever\b|\b(?:have|has|had|did|do|does)(?:\s+not|n['’]t)\b.*?\b(?:any|ever|before|yet)\b", re.IGNORECASE
)
_NEGATION = re.compile(r"\b(?:never|not|no|none|nothing|nobody|neither|nor|cannot|without)\b|n['’]t\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine|myself|we|our|us)\b", re.IGNORECASE)
# An aim, not a fact ("I'm trying to ...", "The user wants to ...", "Can you ..."): quoted only when no plain
# claim matches as well.
_GOAL = re.compile(
    r"^(?:the user|i|we)(?:['’]m| am| is| are| was| were|['’]ve been| have been| has been)?\s*"
    r"(?:trying|planning|hoping|looking|wanting|going|wants?|needs?|would like|['’]d like|plans?|hopes?|wish(?:es)?"
    r"|intends?)\s+to\b"
    r"|^(?:can|could|would|will) you\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[\w'’]+")
# Words that say how or whether something was done rather than what it was about: a denial and the statement it
# contradicts often use different ones ("never attempted" / "completed").
_FILLER = frozenset(
    porter_stem(word)
    for word in """
    user users i me my mine myself we our us you your yours he she it its they them their his her him
    this that these those there here also just really already still currently recently ever before yet any some all
    every each about into over under up down out off again more most very much many such what which who whom whose
    when where why how am is are was were be been being have has had do does did done doing will would can could
    should may might must shall get got gotten make made want wants wanted try tried trying need needed like liked
    use used using work worked working attempt attempted complete completed finish finished practice practiced
    practised study studied test tested write wrote written implement implemented integrate integrated fix fixed
    obtain obtained create created start started handle handled solve solved learn learned learnt see seen go gone
    went take taken took project projects thing things something anything related regarding sure one two three
    so then than because while and or but if never none nor not no
    """.split()
)
_MAX_QUOTE = 150
# A long denial is quoted from a little before its "never"/"has not" so the denied thing stays in the quote.
_ANCHOR_ROOM = 60
_ANCHOR_LEAD = 30
_MAX_PAIRS = 2
_MIN_SHARED = 2
_MIN_SHARE = 0.6


def conflict_notes_enabled() -> bool:
    """``SEVERIAN_CONFLICT_NOTES`` switches fix (b) on. Off: no note is ever added."""
    return os.environ.get("SEVERIAN_CONFLICT_NOTES", "").strip().lower() in {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class _Statement:
    item: int
    speaker: str
    text: str
    topics: frozenset[str]
    denial: bool
    negated: bool
    goal: bool = False
    # Where the denial starts in ``text`` (quotes of long denials are cut around it).
    anchor: int = 0


def _topics(text: str) -> frozenset[str]:
    words: set[str] = set()
    for token in _WORD.findall(text.casefold()):
        if "n't" in token or "n’t" in token:
            continue
        head = re.split(r"['’]", token, maxsplit=1)[0]
        if len(head) <= 2 or head.isdigit() or head in STOPWORDS:
            continue
        stem = porter_stem(head)
        if stem not in _FILLER:
            words.add(stem)
    return frozenset(words)


def _statements(item: int, content: str) -> list[_Statement]:
    text = _LABEL.sub("", content, count=1).strip()
    match = _SPEAKER.match(text)
    if match is not None:
        # A saved message: only the speaker's own words, only sentences about themselves, no questions.
        speaker = match.group("speaker")
        body = match.group("body")
        cut = body.find(_REPLY)
        body = body[:cut] if cut >= 0 else body
        sentences = [part.strip() for part in _SENTENCE_END.split(body) if part.strip()]
        sentences = [part for part in sentences if not part.endswith("?") and _FIRST_PERSON.search(part)]
    elif text.startswith("The user"):
        speaker, sentences = "user", [text]
    else:
        return []
    found: list[_Statement] = []
    for sentence in sentences:
        denying = next((clause for clause in _CLAUSE.split(sentence) if _DENIAL.search(clause)), None)
        if denying is not None:
            clause = denying.strip()
            where = _DENIAL.search(clause)
            anchor = where.start() if where else 0
            # What is denied follows the "never" / "has not": "The user is having trouble with X, since they have
            # never done Y" denies Y, so only the words from the denial on are its topic.
            found.append(_Statement(item, speaker, clause, _topics(clause[anchor:]), True, True, anchor=anchor))
        else:
            found.append(
                _Statement(
                    item,
                    speaker,
                    sentence,
                    _topics(sentence),
                    False,
                    bool(_NEGATION.search(sentence)),
                    goal=_GOAL.match(sentence) is not None,
                )
            )
    return found


def _quote(text: str, anchor: int = 0) -> str:
    """``text`` cut to about ``_MAX_QUOTE`` characters at word breaks, keeping the part from ``anchor`` on."""
    text = text.strip().rstrip(".!")
    if len(text) <= _MAX_QUOTE:
        return text
    lead = ""
    if anchor + _ANCHOR_ROOM > _MAX_QUOTE:
        begin = text.rfind(" ", 0, max(anchor - _ANCHOR_LEAD, 0)) + 1
        text, lead = text[begin:], "…" if begin > 0 else ""
    room = _MAX_QUOTE - len(lead)
    if len(text) <= room:
        return lead + text
    cut = text.rfind(" ", 0, room - 1)
    return lead + text[: cut if cut > 0 else room - 1].rstrip(" ,;:") + "…"


def conflict_note(contents: Sequence[str], *, query: str | None = None) -> str | None:
    """A one-line note naming up to two disagreements among ``contents`` (in result order), else None.

    With ``query``, a disagreement counts only when it is about the question: the denial shares 2 of the question's
    topic words (1 if the question has only one). A question with no topic words gets no note.
    """
    asked = _topics(query) if query is not None else None
    if asked is not None and not asked:
        return None
    needed = min(_MIN_SHARED, len(asked)) if asked is not None else 0
    statements = [statement for item, content in enumerate(contents) for statement in _statements(item, content)]
    pairs: list[tuple[_Statement, _Statement]] = []
    # One mention per disagreement: a denial said twice (a turn and the note taken from it) or a counterpart
    # already quoted is not named again.
    named_topics: list[frozenset[str]] = []
    quoted: set[str] = set()
    for denial in (statement for statement in statements if statement.denial):
        if not denial.topics or denial.topics in named_topics:
            continue
        if asked is not None and len(asked & denial.topics) < needed:
            continue
        best: tuple[tuple[bool, int, int], _Statement] | None = None
        for other in statements:
            if other.item == denial.item or other.negated or other.speaker != denial.speaker:
                continue
            shared = len(denial.topics & other.topics)
            # A stated goal ("I'm trying to ...") counts only if it covers everything the denial is about.
            enough = shared == len(denial.topics) if other.goal else shared >= _MIN_SHARE * len(denial.topics)
            if shared >= _MIN_SHARED and enough:
                # A plain claim beats a stated goal; then more shared topic words; then the higher-ranked memory.
                rank = (not other.goal, shared, -other.item)
                if best is None or rank > best[0]:
                    best = (rank, other)
        if best is not None and best[1].text not in quoted:
            named_topics.append(denial.topics)
            quoted.add(best[1].text)
            pairs.append((denial, best[1]))
            if len(pairs) == _MAX_PAIRS:
                break
    if not pairs:
        return None
    said = "; ".join(
        f'"{_quote(denial.text, denial.anchor)}" but also "{_quote(other.text)}"' for denial, other in pairs
    )
    return f"Memories that disagree: {said}. If it matters, mention both."
