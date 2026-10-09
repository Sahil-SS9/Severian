"""Reading a fact: who it is about, which slot it fills, what it says has ended.

Handoff 1 (truth handling). The rules are deliberately narrow: a missed update costs one stale
line, a false one hides a true fact. So every "not a claim" case below matters as much as the
claims.
"""

from __future__ import annotations

import pytest

from severian.application.factslots import (
    SINGLE_SLOTS,
    Claim,
    asks_about_past,
    read_fact,
    user_words,
)

# -- what the user said ---------------------------------------------------------------------


def test_user_words_keeps_only_the_users_side_of_a_saved_turn() -> None:
    raw = "[2026/01/28 (Wed) 04:00] user: We finally moved — I'm living in Bath now.\nassistant: Noted!"
    assert user_words(raw) == "We finally moved — I'm living in Bath now."


def test_user_words_joins_several_user_lines() -> None:
    raw = "[2026/02/01 (Sun) 09:00] user: I live in Bath.\nassistant: Nice.\nuser: And I work at Sage.\nassistant: Ok."
    assert user_words(raw) == "I live in Bath.\nAnd I work at Sage."


def test_user_words_strips_a_leading_label_from_a_plain_fact() -> None:
    assert user_words("[02/10/2026] The user lives in London.") == "The user lives in London."
    assert user_words("The user lives in London.") == "The user lives in London."


def test_user_words_is_empty_for_an_assistant_only_turn() -> None:
    assert user_words("[2026/01/28 (Wed) 04:00] assistant: You live in Paris.") == ""


# -- single-valued slots --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("The user lives in London.", "london"),
        ("I live in Brighton.", "brighton"),
        ("We finally moved — I'm living in Bath now.", "bath"),
        ("I've moved to Leeds.", "leeds"),
        ("The user moved to Milton Keynes.", "milton keynes"),
        ("I'm based in Manchester.", "manchester"),
        ("[2026/01/24 (Sat) 20:00] user: I live in Brighton.\nassistant: Noted!", "brighton"),
        ("[02/10/2026] The user lives in London.", "london"),
    ],
)
def test_home(text: str, value: str) -> None:
    assert read_fact(text).claims == (Claim("user", "home", value, False),)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("I just started a new job at Starling.", "starling"),
        ("I work at Arup as an engineer.", "arup"),
        ("I work for Sage.", "sage"),
        ("The user works at Arup.", "arup"),
        ("I'm working at Ocado these days.", "ocado"),
    ],
)
def test_employer(text: str, value: str) -> None:
    assert read_fact(text).claims == (Claim("user", "employer", value, False),)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("I'm vegetarian these days.", "vegetarian"),
        ("I'm pescatarian these days.", "pescatarian"),
        ("I've gone keto.", "keto"),
        ("I am now gluten-free.", "gluten-free"),
    ],
)
def test_diet(text: str, value: str) -> None:
    assert read_fact(text).claims == (Claim("user", "diet", value, False),)


def test_partner_and_phone() -> None:
    assert read_fact("My partner Chloe Iqbal is a structural engineer.").claims == (
        Claim("user", "partner", "chloe iqbal", False),
    )
    assert read_fact("I'm married to Sam.").claims == (Claim("user", "partner", "sam", False),)
    assert read_fact("My phone number is 07700 900123.").claims == (
        Claim("user", "phone", "07700 900123", False),
    )


def test_a_relatives_home_is_its_own_subject_not_the_users() -> None:
    grace = read_fact("My sister Grace lives in Nottingham.")
    nadia = read_fact("My sister Nadia lives in Belfast.")
    assert grace.claims == (Claim("sister grace", "home", "nottingham", False),)
    assert nadia.claims == (Claim("sister nadia", "home", "belfast", False),)
    assert all(claim.subject != "user" for claim in grace.claims)


def test_single_slots_are_the_ones_the_handoff_names() -> None:
    assert SINGLE_SLOTS == {"home", "employer", "partner", "phone", "diet"}


# -- additive slots -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("I speak German.", Claim("user", "language", "german", True)),
        ("Our cat is called Miso.", Claim("user", "pet", "miso", True)),
        ("Our rabbit is called Clover.", Claim("user", "pet", "clover", True)),
        ("The swimming pool is open on Saturdays.", Claim("the swimming pool", "opening", "saturdays", True)),
        ("My car is a MG4.", Claim("user", "car", "mg4", True)),
        ("My car is a Mazda CX-5.", Claim("user", "car", "mazda cx-5", True)),
        ("I'm a member at Third Space.", Claim("user", "member", "third space", True)),
        ("I enjoy bouldering.", Claim("user", "likes", "bouldering", True)),
    ],
)
def test_additive_slots(text: str, claim: Claim) -> None:
    assert read_fact(text).claims == (claim,)


def test_also_makes_a_single_slot_claim_additive() -> None:
    assert read_fact("I also work at Sage.").claims == (Claim("user", "employer", "sage", True),)
    assert read_fact("I live in Leeds as well.").claims == (Claim("user", "home", "leeds", True),)


# -- things that end ------------------------------------------------------------------------


def test_leaving_one_job_for_another() -> None:
    reading = read_fact("I've left Starling and joined Rightmove this week.")
    assert reading.ended_values == {"starling"}
    assert reading.claims == (Claim("user", "joined", "rightmove", True),)


def test_cancelling_one_membership_for_another() -> None:
    reading = read_fact("I cancelled Third Space and signed up at Anytime Fitness instead.")
    assert reading.ended_values == {"third space"}
    assert reading.claims == (Claim("user", "joined", "anytime fitness", True),)


def test_selling_a_car_and_getting_another() -> None:
    reading = read_fact("Sold the MG4; my new car is a Ford Puma.")
    assert reading.ended_values == {"mg4"}
    assert reading.claims == (Claim("user", "car", "ford puma", True),)


def test_dropping_a_diet_for_another() -> None:
    reading = read_fact("I'm no longer vegetarian; I've gone keto.")
    assert reading.ended_values == {"vegetarian"}
    assert reading.claims == (Claim("user", "diet", "keto", False),)


def test_moving_from_one_place_to_another() -> None:
    reading = read_fact("I moved from Leeds to York.")
    assert reading.ended_values == {"leeds"}
    assert reading.claims == (Claim("user", "home", "york", False),)


def test_an_ending_only_applies_to_the_kinds_of_fact_its_verb_allows() -> None:
    sold = read_fact("Sold the MG4; my new car is a Ford Puma.")
    assert sold.ends("mg4", slot="car")
    assert not sold.ends("mg4", slot="home")
    left = read_fact("I've left Starling and joined Rightmove this week.")
    assert left.ends("starling", slot="employer")
    assert left.ends("starling", slot="joined")
    assert not left.ends("starling", slot="home")  # leaving a job says nothing about a home


# -- things that are NOT claims -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I'm going to Belfast for a work trip in August.",
        "Where do I live?",
        "If I live in Leeds I would save money.",
        "I live in a flat.",  # not a place
        "I'd like to live in Paris.",
        "I might move to Leeds.",
        "I'm thinking of moving to York.",
        "I'm not vegetarian.",
        "Next year I will work at Arup.",
        "assistant: You live in Paris.",
        "I left Paris for the weekend.",  # leaving a place is not leaving a home
        "I left my keys at the office.",
        "How do I remove a red wine stain from a carpet?",
        "The meeting is on Thursday afternoon.",
    ],
)
def test_not_a_claim(text: str) -> None:
    reading = read_fact(text)
    assert reading.claims == ()
    assert not reading.ended_values


def test_a_past_tense_remark_ends_the_old_value_without_claiming_a_new_one() -> None:
    reading = read_fact("I used to live in Paris.")
    assert reading.claims == ()
    assert reading.ended_values == {"paris"}
    assert reading.ends("paris", slot="home")


def test_assistant_words_never_count() -> None:
    raw = "[2026/03/01 (Sun) 10:00] user: Thanks.\nassistant: Noted, you live in Paris and work at Arup."
    assert read_fact(raw).claims == ()


# -- one saved turn can hold several claims -------------------------------------------------


def test_one_turn_can_hold_several_claims() -> None:
    reading = read_fact("I live in London and work at Arup.")
    assert reading.claims == (
        Claim("user", "home", "london", False),
        Claim("user", "employer", "arup", False),
    )


# -- what the reader could not account for -------------------------------------------------


def test_nothing_is_left_over_when_every_sentence_was_read() -> None:
    assert read_fact("I live in Brighton.").unread == 0
    assert read_fact("We finally moved — I'm living in Bath now.").unread == 0  # a few filler words are fine
    assert read_fact("I live in London and work at Arup.").unread == 0


def test_a_sentence_the_reader_does_not_know_is_counted() -> None:
    reading = read_fact("I live in Brighton. My dentist appointment is on the 14th of April at ten past nine.")
    assert reading.claims == (Claim("user", "home", "brighton", False),)
    assert reading.unread == 1


def test_questions_do_not_count_as_left_over_but_wishes_and_plans_do() -> None:
    assert read_fact("I live in Brighton. Where should I eat tonight?").unread == 0
    # A plan is still information: closing the message that holds it would hide it.
    assert read_fact("I live in Brighton. I might move to Leeds one day.").unread == 1


def test_assistant_words_are_counted_apart_from_the_users() -> None:
    raw = "[2026/01/24 (Sat) 20:00] user: I live in Brighton.\nassistant: " + " ".join(["word"] * 55)
    reading = read_fact(raw)
    assert reading.assistant_words == 55
    assert reading.unread == 0
    assert read_fact("The user lives in Bath.").assistant_words == 0


# -- asking about the past ------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Which city did I live in before Bath?",
        "What car did I have before the Ford Puma?",
        "Where did I work previously?",
        "What diet did I follow before going keto?",
        "Which gym was I at before Anytime Fitness?",
        "Where did I use to live?",
        "What was my former employer?",
        "Where did I live originally?",
    ],
)
def test_asks_about_the_past(query: str) -> None:
    assert asks_about_past(query)


@pytest.mark.parametrize(
    "query",
    [
        "Where do I work now?",
        "Which city do I live in?",
        "What is my current diet?",
        "Which gym am I a member of?",
        "What car do I drive now?",
        "Tell me about Bath",
    ],
)
def test_does_not_ask_about_the_past(query: str) -> None:
    assert not asks_about_past(query)
