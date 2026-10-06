"""Zero-model-call filter: a fact whose names, places, products or numbers are not in its message is dropped."""

import pytest

from severian.application.factanchors import anchors, grounded, pasted


@pytest.mark.parametrize(
    ("fact", "said"),
    [
        # Wrongly attached in idea 1's 50-fact checks: the name or place came from a neighbouring message.
        ("The user no longer lives in Glasgow.", "I'm living in Cardiff now."),
        ("The user's manager is Rosa Haddad.", "Which board game should we play tonight?"),
        ("The user has a Trek Emonda.", "Explain how compound interest works in simple terms."),
        ("The user paid $65 for the course.", "The course was quite expensive."),
    ],
)
def test_fact_with_a_name_place_product_or_number_missing_from_its_message_is_dropped(fact, said):
    assert grounded(fact, said) is False


@pytest.mark.parametrize(
    ("fact", "said"),
    [
        ("The user started a new job at Starling.", "I just started a new job at Starling."),
        ("The user's sister Ben lives in Bristol.", "My sister Ben lives in Bristol."),
        ("The user has been doing yoga for 3 months.", "I've been doing yoga for three months now."),
        ("The user paid $65.", "It cost 65 dollars."),
        ("The user is allergic to penicillin.", "I'm allergic to penicillin."),  # no anchors: kept
        ("The user's dog is called Biscuit.", "Our dog Biscuit's walk was long."),  # possessive in message
        ("The user drives a Kia Niro.", "My new car is a KIA niro."),  # case differs
        ("The user had a dentist appointment on Tuesday.", "Dentist on tuesday, ugh."),
        ("The user was at the Glasgow 10k.", "Ran the Glasgow 10K today!"),
        ("The user owns 2 bikes.", "I have two bikes."),
        ("The user visited the U.K.", "Back from the UK."),
        ("The user wants the AI to answer in British English.", "From now on, answer everything in British English OK"),
    ],
)
def test_fact_whose_anchors_all_appear_in_its_message_is_kept(fact, said):
    assert grounded(fact, said) is True


def test_dated_prefix_and_sentence_start_words_are_not_anchors():
    assert anchors("[2026/01/19 (Mon) 17:00] The user left Starling.") == {"starling"}
    assert anchors("In June the user moved.") == {"june"}  # mid-sentence capital counts; "In" does not


def test_numbers_are_anchors():
    assert anchors("The user ran 10 miles in 1:05.") == {"10", "1", "05"}


TRANSCRIPT = (
    "Transcript 4: Dolly Vijay\n\nQ. Hello ma'am. Can you start with introducing yourself?\n\n"
    "A. My name's Dolly and I'm from New Delhi.\n\nQ. Which channel do you prefer?\n\nA. I prefer to shop online."
)
OUTLINE = "I have this outline. Add to it.\n###\n" + "\n".join(
    f"Slide {i}: Section {i}\n\nDetail line {i}" for i in range(1, 6)
)
ESSAY = "Proofread this:\n" + "\n".join(["A line of someone's essay text."] * 8)


@pytest.mark.parametrize("message", [TRANSCRIPT, OUTLINE, ESSAY])
def test_pasted_text_is_recognised(message):
    assert pasted(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "I'm looking for some new music recommendations. I've been into indie rock lately.",
        "My sister Nadia lives in Aberdeen.",
        "Thanks!\n\nAlso, I moved to Leeds last month and I start at the hospital on Monday.",
        " ".join(["I've been training a lot and my knee hurts after long runs."] * 12),  # long but one paragraph
    ],
)
def test_ordinary_messages_are_not_pasted(message):
    assert pasted(message) is False
