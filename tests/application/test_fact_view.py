"""Improvement 1 helpers (no model calls): a fact's short source message, who it is about, grouping by person."""

import pytest

from severian.application.factview import about, group_by_person, short_message


def test_short_message_keeps_short_text_whole_and_collapses_whitespace():
    assert short_message("  I moved to\n Leeds  last month. ") == "I moved to Leeds last month."


def test_short_message_cuts_long_text_at_a_word_boundary_with_an_ellipsis():
    text = "I have been training for the Glasgow half marathon " * 6
    short = short_message(text)
    assert len(short) <= 160 and short.endswith("…")
    assert text.startswith(short[:-1])  # nothing invented, only cut
    assert short[-2] != " " and text[len(short) - 1] == " "  # cut on a word boundary


def test_short_message_hard_cuts_one_giant_word():
    assert short_message("x" * 400) == "x" * 159 + "…"


@pytest.mark.parametrize(
    ("fact", "who"),
    [
        ("The user's sister Nadia lives in Aberdeen.", "Nadia"),
        ("The user's manager is Rosa Haddad.", "Rosa"),
        ("The user's friend, Tom, plays chess.", "Tom"),
        ("The user's dog is called Biscuit.", "user"),  # a pet is not a relation word here
        ("The user's best friend named Priya moved to Leeds.", "Priya"),
        ("[2026/01/19 (Mon) 17:00] The user's mum Anne turned 60.", "Anne"),
        ("The user moved to Leeds.", "user"),
        ("The user's sister lives in Aberdeen.", "user"),  # relation without a name
        ("The user's colleagues Sam and Jo left.", "Sam"),
    ],
)
def test_about_is_the_name_after_a_relation_word_else_the_user(fact, who):
    assert about(fact) == who


def test_grouping_moves_a_persons_facts_next_to_their_highest_ranked_fact_order_only():
    ranked = [
        ("Nadia lives in Aberdeen", "Nadia"),
        ("raw turn about trains", None),  # not a fact: stays put
        ("user moved to Leeds", "user"),
        ("Tom plays chess", "Tom"),
        ("Nadia is a nurse", "Nadia"),
        ("Tom likes jazz", "Tom"),
        ("user has a dog", "user"),
    ]
    grouped = group_by_person(ranked, who=lambda item: item[1])
    assert [text for text, _ in grouped] == [
        "Nadia lives in Aberdeen",
        "Nadia is a nurse",
        "raw turn about trains",
        "user moved to Leeds",
        "Tom plays chess",
        "Tom likes jazz",
        "user has a dog",
    ]
    assert sorted(grouped) == sorted(ranked)  # same set


def test_grouping_matches_names_case_insensitively_and_leaves_user_facts_in_rank_order():
    ranked = [("a", "Rosa"), ("b", "user"), ("c", "user"), ("d", "rosa")]
    assert [t for t, _ in group_by_person(ranked, who=lambda item: item[1])] == ["a", "d", "b", "c"]
