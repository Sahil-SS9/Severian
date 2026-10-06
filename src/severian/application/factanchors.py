"""Zero-model-call check that a fact belongs to the message it is attached to.

A fact's anchors are its numbers and its capitalised words after the first word of a sentence (names,
places, products, days and months). If any anchor is missing from the message the fact was taken from,
the fact most likely came from a neighbouring message in the same batch or from the prompt itself, so it
is dropped. Facts with no anchors ("The user is allergic to penicillin.") pass unchanged: this check can
only catch misattached specifics, not invented generalities.
"""

from __future__ import annotations

import re

_DATED = re.compile(r"^\s*\[[^\]]*\]\s*")
_WORD = re.compile(r"[A-Za-z][A-Za-z.'’-]*")
_DIGITS = re.compile(r"\d+")
_SENTENCE_START = re.compile(r"(^|[.!?:;]\s+|[\"“(]\s*)$")
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100", "a dozen": "12", "dozen": "12",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "once": "1", "twice": "2",
    "half": "2", "couple": "2",
}  # fmt: skip
_NOT_ANCHORS = {"i", "user", "the", "assistant", "ai"}


def _norm(word: str) -> str:
    word = word.casefold().replace("’", "'")
    word = re.sub(r"'s$|'$", "", word)
    return word.replace(".", "").strip("'-")


def anchors(fact: str) -> set[str]:
    """Normalised numbers and mid-sentence capitalised words in ``fact`` (its date prefix ignored)."""
    text = _DATED.sub("", fact)
    found = set(_DIGITS.findall(text))
    for match in _WORD.finditer(text):
        word = match.group()
        if not word[0].isupper() or _SENTENCE_START.search(text[: match.start()]):
            continue
        normal = _norm(word)
        if normal and normal not in _NOT_ANCHORS:
            found.add(normal)
    return found


def _message_terms(message: str) -> set[str]:
    lowered = message.casefold()
    terms = {_norm(w) for w in _WORD.findall(lowered)} | set(_DIGITS.findall(lowered))
    terms |= {digit for word, digit in _NUMBER_WORDS.items() if re.search(rf"\b{word}\b", lowered)}
    return terms


_LABELLED_LINE = re.compile(r"^\s*(q|a|[a-z][\w ]{0,20}|slide \d+|#+.*)\s*[:.]\s|^\s*#{2,}", re.I | re.M)


def pasted(message: str) -> bool:
    """True when ``message`` looks like text the user pasted in rather than wrote about themselves.

    Pasted transcripts, outlines, essays and documents speak in someone else's voice, so facts taken from
    them get attached to the user wrongly. Two signs, no model call: three or more labelled lines
    ("Q.", "A.", "Name:", "Slide 2:", "###"), or six or more non-empty lines.
    """
    lines = [line for line in message.splitlines() if line.strip()]
    return len(_LABELLED_LINE.findall(message)) >= 3 or len(lines) >= 6


def grounded(fact: str, message: str) -> bool:
    """True when ``message`` is not pasted text and every anchor of ``fact`` appears in it (case,
    possessive and dots ignored)."""
    if pasted(message):
        return False
    wanted = anchors(fact)
    if not wanted:
        return True
    have = _message_terms(message)
    return all(a in have or (a.endswith("s") and a[:-1] in have) or f"{a}s" in have for a in wanted)
