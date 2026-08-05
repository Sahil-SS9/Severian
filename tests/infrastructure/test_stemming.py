"""Tests for the deterministic Porter stemmer (infrastructure/stemming.py).

The stemmer must be:
- deterministic (same input -> same output, no state)
- consistent with SQLite FTS5's porter tokenizer on the corpus-critical
  tokens (authentication/authenticated/requests/request/...)
- correct on the classic Porter vocabulary (subset that matters here)
"""

from __future__ import annotations

from severian.infrastructure.stemming import porter_stem


def test_stemmer_is_deterministic() -> None:
    samples = ["authentication", "authenticated", "requests", "incoming",
               "deployed", "credentials", "headcount", "engineers"]
    for word in samples:
        first = porter_stem(word)
        for _ in range(3):
            assert porter_stem(word) == first


def test_stemmer_inflections() -> None:
    # The corpus-critical inflections (aq3/aq11 zone): these MUST agree
    # with SQLite FTS5's porter tokenizer so the hash vector and the FTS
    # projection stem identically. Verified against local sqlite3
    # (Python 3.12.3) with the porter unicode61 tokenizer.
    assert porter_stem("authentication") == porter_stem("authenticated")
    assert porter_stem("authenticated") == porter_stem("authenticate")
    assert porter_stem("requests") == porter_stem("request")
    assert porter_stem("deployed") == porter_stem("deploy")
    assert porter_stem("credentials") == porter_stem("credential")
    # NOTE: 'storage' does NOT stem to the same root as 'stored' in
    # SQLite's porter ('storag' vs 'store') — a known SQLite quirk. We
    # match SQLite exactly, so the assertion below documents the
    # divergence rather than asserting an idealized equality.
    assert porter_stem("stored") == "store"
    assert porter_stem("storage") == "storag"


def test_stemmer_classic_porter_cases() -> None:
    cases = {
        "caresses": "caress", "ponies": "poni", "ties": "ti",
        "cats": "cat", "feed": "feed", "agreed": "agree",
        "plastered": "plaster", "bled": "bled", "motoring": "motor",
        "sing": "sing", "conflated": "conflat", "troubled": "trouble",
        "sized": "size", "hopping": "hop", "tanned": "tan",
        "falling": "fall", "hissing": "hiss", "fizzed": "fizz",
        "failing": "fail", "filing": "file", "happy": "happi",
        "sky": "sky", "relational": "relat", "conditional": "condit",
        "rational": "ration", "valenci": "valenc", "hesitanci": "hesit",
        "digitizer": "digit", "conformabli": "conform", "radicalli": "radic",
        "differentli": "differ", "vileli": "vile", "analogousli": "analog",
        "vietnamization": "vietnam", "predication": "predic",
        "operator": "oper", "feudalism": "feudal", "hopefulness": "hope",
        "goodness": "good", "revival": "reviv", "allowance": "allow",
        "inference": "infer", "airliner": "airlin", "gyroscopic": "gyroscop",
        "adjustable": "adjust", "irritant": "irrit", "replacement": "replac",
        "adjustment": "adjust", "dependent": "depend", "adoption": "adopt",
        "communism": "commun", "activate": "activ", "angulariti": "angular",
        "effective": "effect", "bowdlerize": "bowdler", "probate": "probat",
        "rate": "rate", "cease": "cease", "controll": "control", "roll": "roll",
        "sensitiviti": "sensit", "formaliti": "formal", "triplicate": "triplic",
        "formative": "form", "formalize": "formal", "electriciti": "electr",
    }
    for word, expected in cases.items():
        assert porter_stem(word) == expected, f"{word} -> {porter_stem(word)!r}, expected {expected!r}"


def test_stemmer_handles_empty_and_short() -> None:
    assert porter_stem("") == ""
    assert porter_stem("a") == "a"
    # 'is' -> 'i' (step 1a strips terminal s); 'it' unchanged (no rule fires)
    assert porter_stem("is") == "i"
    assert porter_stem("it") == "it"
