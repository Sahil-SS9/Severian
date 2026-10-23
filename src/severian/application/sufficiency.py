"""``SEVERIAN_SUFFICIENCY``: one line after the results when the asked-for detail is not in them.

When nothing returned holds the detail the question asks for, the answer model tends to build an answer out of
neighbouring text rather than say it does not know: BEAM abstention questions are answered with a guess 40% of the
time (``REPORT.md``, section B). One line telling the reader the detail may not be in memory gives it permission to
say so.

The line goes to the reader, so a wrong firing costs a right answer. The rule is therefore lexical and narrow, and
only fires on two shapes:

1. a detail slot from a short curated list (``criteria``, ``rationale``, ``agenda``, ``reaction``, ``rules``,
   ``terms``, ``feedback``, ...) appears in the question and no returned item mentions it; or
2. the question asks for something ``specific``/``exact`` and the first detail word after that marker is absent.

Both were measured against controls before being wired in: on the kept stores the pair fires on 6 of the 13
abstention questions the stores hold (10 BEAM, 3 LongMemEval ``_abs``) and on 0 of 72 answerable single-session /
single-hop questions whose evidence the re-read did find. The rest of the abstention set is attribute gaps whose
nouns *are* present ("rules", "bugs"), which no lexical rule can see; that needs a semantic check and is left out on
purpose rather than loosening the rule and firing on questions the items answer. Evidence: ``evidence/ctxeff/``.
Off by default.
"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence

#: The one line the reader gets. Paid for out of the same budget as the items.
NOTE = "Note: the specific detail this question asks for is not in the memories above; it may not be in memory."

#: Nouns that name a detail rather than a topic. Firing needs one of these absent from every returned item.
DETAIL_SLOTS = frozenset(
    {
        "agenda",
        "arguments",
        "atmosphere",
        "background",
        "budget",
        "bugs",
        "conditions",
        "considerations",
        "criteria",
        "criterion",
        "decisions",
        "details",
        "expertise",
        "feedback",
        "ingredients",
        "itinerary",
        "measurements",
        "outcome",
        "plan",
        "plans",
        "preferences",
        "process",
        "projects",
        "qualifications",
        "rationale",
        "reaction",
        "reason",
        "reasons",
        "requirements",
        "rule",
        "rules",
        "schedule",
        "sessions",
        "specifications",
        "step",
        "steps",
        "strategies",
        "strategy",
        "structure",
        "technique",
        "techniques",
    }
)

#: Phrases where a slot word is doing something else. "in terms of" is a comparison, not a request for conditions.
_IDIOMS = ("in terms of", "on good terms", "terms of service")
_SPECIFIC = re.compile(r"\b(?:specific|exact|exactly|precise|precisely|particular)\b", re.IGNORECASE)
_SUMMARY = re.compile(r"\b(?:summar(?:y|ise|ize)|overview|recap|everything you know)\b", re.IGNORECASE)
# Enough of English's small words to keep the content-word scan from firing on grammar.
_STOP = frozenset(
    """a an the of in on at to for and or but is was were be been being i my me you your it its this that these those
    with by from as did do does done have has had what which who whom whose when where why how many much about than
    then there their we our us he she they them his her if not no any some can could should would will shall may
    might must please tell share give list describe explain provide confirm remind say said wrote write mention
    mentioned use used using get got make made take taken over under between during before after again further once
    also more most other another such same own very each few both all only just into out up down""".split()
)
#: Words that stand in for a detail without naming one: "what specific things did we discuss" must not fire on
#: "things" alone.
_VAGUE = frozenset(
    {"anything", "bit", "data", "info", "information", "kind", "lot", "sort", "something", "stuff", "thing", "things",
     "type", "types", "way", "ways"}
)
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")

#: Irregular pairs where stripping a plural suffix gets the wrong stem.
_ALIASES = {"criteria": "criterion", "criterion": "criterion"}


def sufficiency_enabled() -> bool:
    """``SEVERIAN_SUFFICIENCY`` switches the line on. Off: the block is exactly as before."""
    return os.environ.get("SEVERIAN_SUFFICIENCY", "").strip().lower() in {"1", "true", "on", "yes"}


def _singular(word: str) -> str:
    """A canonical form, so a question and an item match across singular/plural (``rule``/``rules``)."""
    word = _ALIASES.get(word, word)
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")) and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


#: The slot list in canonical form, so a question and an item meet on the same key.
_SLOT_KEYS = frozenset(_singular(slot) for slot in DETAIL_SLOTS)


def _content_words(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOP and len(word) > 2]


def _item_keys(items: Sequence[str]) -> set[str]:
    """Every word the returned items hold, canonical, so singular/plural picks up either form."""
    keys: set[str] = set()
    for item in items:
        for word in _WORD.findall(item.lower()):
            keys.add(_singular(word))
    return keys


def sufficiency_note(query: str | None, items: Sequence[str]) -> str | None:
    """The one line to put after the results, or None when the returned items may well hold the asked-for detail.

    ``items`` are the texts actually shown to the reader (not the full candidate pool): a candidate that was cut for
    space cannot answer the question. Wording only — no model call, no store read, no dataset label.
    """
    if not query or not query.strip():
        return None
    if not items or not any(item.strip() for item in items):
        return None
    if _SUMMARY.search(query):  # a recap request is not a detail lookup
        return None
    asked = query
    for idiom in _IDIOMS:
        asked = re.sub(re.escape(idiom), " ", asked, flags=re.IGNORECASE)
    have = _item_keys(items)
    for word in _content_words(asked):
        key = _singular(word)
        if key in _VAGUE:
            continue
        if key in _SLOT_KEYS and key not in have:
            return NOTE
    marker = _SPECIFIC.search(asked)
    if marker is not None:
        for word in _content_words(asked[marker.end():])[:4]:
            key = _singular(word)
            if key in _VAGUE:
                continue
            if key not in have:
                return NOTE
            break  # the first thing named after the marker is there, so the detail may be there too
    return None


__all__ = ["DETAIL_SLOTS", "NOTE", "sufficiency_enabled", "sufficiency_note"]
