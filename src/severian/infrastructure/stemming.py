"""Compact, deterministic Porter stemmer (pure stdlib).

Implements the classic Porter stemming algorithm (Porter, 1980) so the
hash-embedding tokenizer can stem identically to SQLite FTS5's ``porter``
tokenizer. This keeps the vector and lexical projections consistent:
"authentication", "authenticated", and "authenticate" all hash to the
same token, so a query term matches a document's inflected variant in
BOTH projections (previously only FTS matched, crippling the fused rank).

Deterministic by construction: same input -> same output, no state, no
randomness. The implementation is a faithful port of the reference
algorithm (the same one FTS5's porter tokenizer implements), tested
against the classic Porter test vocabulary in tests/.

No new dependency: pure ``re`` + string ops.
"""
from __future__ import annotations

import re

_CONSONANT = re.compile(r"[^aeiou]")
_VOWEL = re.compile(r"[aeiou]")
_Y_AS_CONSONANT = re.compile(r"^y|[^aeiou]y")


def _is_consonant(word: str, i: int) -> bool:
    """True if word[i] is a consonant per Porter's definition."""
    ch = word[i]
    if ch in "aeiou":
        return False
    if ch == "y":
        if i == 0:
            return True
        return not _is_consonant(word, i - 1)
    return True


def _measure(word: str) -> int:
    """Porter's ``m``: the count of VC sequences in the word."""
    n = 0
    i = 0
    length = len(word)
    while i < length:
        if not _is_consonant(word, i):
            # vowel
            i += 1
            while i < length and not _is_consonant(word, i):
                i += 1
            # now at consonant (or end) -> one VC sequence
            if i < length:
                n += 1
                while i < length and _is_consonant(word, i):
                    i += 1
        else:
            i += 1
    return n


def _contains_vowel(word: str) -> bool:
    return any(not _is_consonant(word, i) for i in range(len(word)))


def _ends_double_consonant(word: str) -> bool:
    if len(word) < 2:
        return False
    return word[-1] == word[-2] and _is_consonant(word, len(word) - 1)


def _ends_cvc(word: str) -> bool:
    if len(word) < 3:
        return False
    return (
        _is_consonant(word, len(word) - 3)
        and not _is_consonant(word, len(word) - 2)
        and _is_consonant(word, len(word) - 1)
        and word[-1] not in "wxy"
    )


_STEP1A = [
    ("sses", "ss"),
    ("ies", "i"),
    ("ss", "ss"),
    ("s", ""),
]

_STEP1B = [
    ("eed", "ee"),
    ("ed", ""),
    ("ing", ""),
]

_STEP1B2 = [
    ("at", "ate"),
    ("bl", "ble"),
    ("iz", "ize"),
]


def _stem_once(word: str) -> str:
    """One full Porter pass (steps 1a-5b) — same output as FTS5 porter."""
    # Step 1a
    for suffix, repl in _STEP1A:
        if word.endswith(suffix):
            word = word[: -len(suffix)] + repl
            break

    # Step 1b
    for suffix, _repl in _STEP1B:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if suffix == "eed":
                if _measure(stem) > 0:
                    word = stem + "ee"
            else:
                if _contains_vowel(stem):
                    word = stem
                    for s2, r2 in _STEP1B2:
                        if word.endswith(s2):
                            word = word[: -len(s2)] + r2
                            break
                    else:
                        if _ends_double_consonant(word) and word[-1] not in "lsz":
                            word = word[:-1]
                        elif _measure(word) == 1 and _ends_cvc(word):
                            word += "e"
            break

    # Step 1c
    if word.endswith("y") and _contains_vowel(word[:-1]):
        word = word[:-1] + "i"

    # Step 2
    _STEP2 = [
        ("ational", "ate"), ("tional", "tion"), ("enci", "ence"),
        ("anci", "ance"), ("izer", "ize"), ("abli", "able"),
        ("alli", "al"), ("entli", "ent"), ("eli", "e"),
        ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"),
        ("ator", "ate"), ("alism", "al"), ("iveness", "ive"),
        ("fulness", "ful"), ("ousness", "ous"), ("aliti", "al"),
        ("iviti", "ive"), ("biliti", "ble"),
    ]
    for suffix, repl in _STEP2:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 0:
                word = stem + repl
            break

    # Step 3
    _STEP3 = [
        ("icate", "ic"), ("ative", ""), ("alize", "al"),
        ("iciti", "ic"), ("ical", "ic"), ("ful", ""),
        ("ness", ""),
    ]
    for suffix, repl in _STEP3:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 0:
                word = stem + repl
            break

    # Step 4
    _STEP4 = [
        ("al", ""), ("ance", ""), ("ence", ""), ("er", ""),
        ("ic", ""), ("able", ""), ("ible", ""), ("ant", ""),
        ("ement", ""), ("ment", ""), ("ent", ""),
        ("ou", ""), ("ism", ""), ("ate", ""), ("iti", ""),
        ("ous", ""), ("ive", ""), ("ize", ""),
    ]
    for suffix, _repl in _STEP4:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 1:
                word = stem
            break
    else:
        # "ion": only stripped when the remaining stem ends in s or t
        # (the reference algorithm's special condition for Step 4).
        if word.endswith("ion"):
            stem = word[:-3]
            if _measure(stem) > 1 and stem and stem[-1] in "st":
                word = stem

    # Step 5a
    # NOTE: SQLite FTS5's porter (the oracle we must match for vector/FTS
    # consistency) only strips the final 'e' when m > 1. The classic
    # Porter's "m == 1 and not cvc" branch is NOT present in SQLite's
    # implementation (verified empirically: 'agree' -> 'agree', 'cease'
    # -> 'cease' in SQLite, not 'agr'/'ceas').
    if word.endswith("e"):
        stem = word[:-1]
        if _measure(stem) > 1:
            word = stem

    # Step 5b
    if _measure(word) > 1 and _ends_double_consonant(word) and word[-1] == "l":
        word = word[:-1]

    return word


def porter_stem(word: str) -> str:
    """Stem a single lowercase word (Porter)."""
    if not word:
        return word
    return _stem_once(word)
