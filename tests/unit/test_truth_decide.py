"""Replace-or-add decisions (handoff 1, ``SEVERIAN_TRUTH_RESOLUTION``).

Rules decide whenever they can. The model is asked only when the rules say nothing about a pair
of facts, its reply must be strict JSON, and any failure of any kind means "add": a missed
update leaves one stale line, a wrong delete would lose a true fact.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from severian.application.truthresolution import (
    MAX_MODEL_CANDIDATES,
    Candidate,
    ResolutionModel,
    Verdict,
    build_prompt,
    decide,
    parse_reply,
    truth_resolution_enabled,
)


class _Model:
    """Scripted stand-in for the resolution model; counts calls."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    @property
    def calls(self) -> int:
        return len(self.prompts)


def _one(new: str, old: str, model: _Model | ResolutionModel | None = None) -> Verdict:
    verdicts = decide(new, [Candidate("old-1", old)], model=model)
    assert len(verdicts) == 1, f"expected the candidate to be considered, got {verdicts}"
    return verdicts[0]


# -- the switch -----------------------------------------------------------------------------


def test_switch_is_off_by_default() -> None:
    assert truth_resolution_enabled({}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON", "True"])
def test_switch_on_values(value: str) -> None:
    assert truth_resolution_enabled({"SEVERIAN_TRUTH_RESOLUTION": value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_switch_off_values(value: str) -> None:
    assert truth_resolution_enabled({"SEVERIAN_TRUTH_RESOLUTION": value}) is False


# -- rules decide ---------------------------------------------------------------------------


def test_a_single_slot_value_replaces_the_old_one() -> None:
    model = _Model('{"decisions": [{"n": 1, "decision": "add"}]}')
    verdict = _one("I live in Leeds.", "I live in London.", model)
    assert verdict.replaces is True
    assert (verdict.method, verdict.slot) == ("rule", "home")
    assert model.calls == 0  # the rules decided; the model was never asked


def test_every_current_fact_in_the_slot_is_replaced() -> None:
    verdicts = decide(
        "I live in Leeds.",
        [Candidate("a", "I live in London."), Candidate("b", "I live in York."), Candidate("c", "I speak German.")],
        model=None,
    )
    assert {v.candidate_id: v.replaces for v in verdicts} == {"a": True, "b": True}  # c is not considered


def test_an_additive_slot_adds() -> None:
    verdict = _one("I speak Polish.", "I speak German.")
    assert verdict.replaces is False
    assert (verdict.method, verdict.slot) == ("rule", "language")


@pytest.mark.parametrize(
    ("new", "old"),
    [
        ("Our cat is called Miso.", "Our rabbit is called Clover."),
        ("The swimming pool is open on Wednesdays.", "The swimming pool is open on Saturdays."),
        ("I enjoy jazz.", "I enjoy rock."),
        ("My sister Nadia lives in Belfast.", "My sister Grace lives in Nottingham."),
        ("I'm a member at PureGym.", "I'm a member at Third Space."),  # a second membership is not a replacement
        ("My car is a Ford Puma.", "My car is a MG4."),  # no sale wording: could be two cars
    ],
)
def test_both_true_facts_are_added(new: str, old: str) -> None:
    assert _one(new, old).replaces is False


def test_the_same_relative_updated_is_a_replacement() -> None:
    verdict = _one("My sister Grace lives in Leeds.", "My sister Grace lives in Nottingham.")
    assert verdict.replaces is True


def test_restating_the_same_value_replaces_nothing() -> None:
    verdict = _one("I live in Leeds.", "I live in Leeds.")
    assert verdict.replaces is False
    assert verdict.reason == "same value"


def test_an_also_statement_adds() -> None:
    verdict = _one("I also work at Sage.", "I work at Arup.")
    assert verdict.replaces is False


@pytest.mark.parametrize(
    ("new", "old"),
    [
        ("I've left Starling and joined Rightmove this week.", "I just started a new job at Starling."),
        ("I've left Rightmove and joined Arup this week.", "I've left Starling and joined Rightmove this week."),
        ("I cancelled Third Space and signed up at Anytime Fitness instead.", "I'm a member at Third Space."),
        (
            "I cancelled PureGym and signed up at Anytime Fitness instead.",
            "I cancelled Third Space and signed up at PureGym instead.",
        ),
        ("Sold the MG4; my new car is a Ford Puma.", "My car is a MG4."),
        ("Sold the Ford Puma; my new car is a VW Golf.", "Sold the MG4; my new car is a Ford Puma."),
        ("I'm no longer vegetarian; I've gone keto.", "I'm vegetarian these days."),
        ("We finally moved — I'm living in Bath now.", "I live in Brighton."),
    ],
)
def test_the_benchmark_update_shapes_replace(new: str, old: str) -> None:
    verdict = _one(new, old)
    assert verdict.replaces is True, verdict


def test_a_saved_turn_and_a_plain_fact_compare_the_same_way() -> None:
    new = "[2026/01/28 (Wed) 04:00] user: We finally moved — I'm living in Bath now.\nassistant: Noted!"
    old = "[2026/01/24 (Sat) 20:00] user: I live in Brighton.\nassistant: Noted!"
    assert _one(new, old).replaces is True
    assert _one("The user lives in Bath.", old).replaces is True


def test_a_candidate_with_other_claims_is_not_replaced() -> None:
    # Replacing the whole line would also lose "lives in London".
    verdict = _one("I work at Sage.", "I live in London and work at Arup.")
    assert verdict.replaces is False
    assert verdict.reason == "candidate holds other claims"


def test_a_candidate_with_unread_content_is_not_replaced() -> None:
    old = "I live in Brighton. My dentist appointment is on the 14th of April at ten past nine."
    verdict = _one("We finally moved — I'm living in Bath now.", old)
    assert verdict.replaces is False
    assert verdict.reason == "candidate holds other content"


def test_a_candidate_with_a_long_assistant_reply_is_not_replaced() -> None:
    old = "[2026/01/24 (Sat) 20:00] user: I live in Brighton.\nassistant: " + " ".join(["word"] * 60)
    verdict = _one("We finally moved — I'm living in Bath now.", old)
    assert verdict.replaces is False
    assert verdict.reason == "candidate holds other content"


def test_a_candidate_that_may_not_go_to_the_model_is_skipped_when_the_rules_are_silent() -> None:
    model = _Model('{"decisions": [{"n": 1, "decision": "replace"}]}')
    quiet = Candidate("x", SEAT_OLD, askable=False)
    assert decide(SEAT_NEW, [quiet], model=model) == []
    assert model.calls == 0


def test_a_candidate_with_nothing_readable_is_never_replaced() -> None:
    assert decide("I live in Leeds.", [Candidate("x", "The deploy window is Friday morning.")], model=None) == []


def test_an_ending_does_not_reach_across_kinds_of_fact() -> None:
    # "Sold the MG4" says nothing about a home called MG4.
    assert decide("Sold the MG4; my new car is a Ford Puma.", [Candidate("x", "I live in Mg4.")], model=None) == []


# -- the model: only when the rules are silent ----------------------------------------------

SEAT_NEW = "The user prefers the window seat."
SEAT_OLD = "The user prefers the aisle seat."


def test_the_model_is_asked_only_when_rules_cannot_decide() -> None:
    model = _Model('{"decisions": [{"n": 1, "decision": "replace"}]}')
    verdict = _one(SEAT_NEW, SEAT_OLD, model)
    assert verdict.replaces is True
    assert verdict.method == "model"
    assert model.calls == 1
    assert SEAT_OLD in model.prompts[0] and SEAT_NEW in model.prompts[0]  # it saw both facts


def test_a_model_add_verdict_is_honoured() -> None:
    verdict = _one(SEAT_NEW, SEAT_OLD, _Model('{"decisions": [{"n": 1, "decision": "add"}]}'))
    assert verdict.replaces is False
    assert verdict.method == "model"


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "",
        "[]",
        '{"decisions": "replace"}',
        '{"decisions": [{"n": 1, "decision": "REPLACE!!"}]}',
        '{"decisions": [{"n": 1, "decision": "delete"}]}',
        '{"decisions": [{"n": 7, "decision": "replace"}]}',
        '{"decisions": [{"decision": "replace"}]}',
        "replace",
        RuntimeError("model fell over"),
        TimeoutError("slow"),
    ],
)
def test_any_model_failure_falls_back_to_add(reply: str | Exception) -> None:
    model = _Model(reply)
    verdict = _one(SEAT_NEW, SEAT_OLD, model)
    assert verdict.replaces is False  # hard fallback: add, never delete
    assert verdict.method == "fallback"
    assert model.calls == 1


def test_without_a_model_undecided_pairs_are_added() -> None:
    verdict = _one(SEAT_NEW, SEAT_OLD, None)
    assert verdict.replaces is False
    assert verdict.method == "fallback"


def test_the_model_is_not_asked_about_unrelated_facts() -> None:
    model = _Model('{"decisions": [{"n": 1, "decision": "replace"}]}')
    unrelated = [Candidate("x", "The deploy window is Friday morning.")]
    assert decide("The user prefers dark mode.", unrelated, model=model) == []
    assert model.calls == 0


def test_one_call_covers_at_most_a_few_candidates() -> None:
    model = _Model('{"decisions": []}')
    seats = ["aisle", "middle", "front", "back", "exit"]
    old = [Candidate(f"c{i}", f"The user prefers the {w} seat.") for i, w in enumerate(seats)]
    verdicts = decide(SEAT_NEW, old, model=model)
    assert model.calls == 1
    assert len(verdicts) == MAX_MODEL_CANDIDATES
    assert all(v.replaces is False for v in verdicts)


def test_the_model_never_overrides_a_rule_decision() -> None:
    model = _Model('{"decisions": [{"n": 1, "decision": "replace"}]}')
    verdict = _one("I speak Polish.", "I speak German.", model)
    assert verdict.replaces is False
    assert model.calls == 0


# -- reply parsing --------------------------------------------------------------------------


def test_parse_reply_accepts_strict_json_only() -> None:
    assert parse_reply('{"decisions": [{"n": 1, "decision": "replace"}, {"n": 2, "decision": "add"}]}', 2) == {
        1: True,
        2: False,
    }
    assert parse_reply('Sure!\n{"decisions": [{"n": 1, "decision": "replace"}]}', 1) == {1: True}  # prose around JSON
    assert parse_reply('{"decisions": [{"n": 3, "decision": "replace"}]}', 2) == {}  # unknown number ignored
    assert parse_reply("nope", 2) is None


def test_prompt_numbers_the_candidates() -> None:
    prompt = build_prompt(SEAT_NEW, [Candidate("a", SEAT_OLD), Candidate("b", "The user prefers the exit seat.")])
    assert "[1]" in prompt and "[2]" in prompt
    assert SEAT_NEW in prompt


# -- the model client -----------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat(content: str) -> _Response:
    return _Response(json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode())


def test_no_model_is_built_unless_a_url_is_set() -> None:
    assert ResolutionModel.from_env({}) is None
    assert ResolutionModel.from_env({"SEVERIAN_TRUTH_RESOLUTION_URL": ""}) is None


def test_the_client_posts_to_the_configured_endpoint() -> None:
    seen: dict[str, object] = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _chat('{"decisions": []}')

    model = ResolutionModel.from_env(
        {
            "SEVERIAN_TRUTH_RESOLUTION_URL": "http://127.0.0.1:9/v1/chat/completions",
            "SEVERIAN_TRUTH_RESOLUTION_MODEL": "tiny",
            "SEVERIAN_TRUTH_RESOLUTION_TIMEOUT": "7",
        },
        opener=opener,
    )
    assert model is not None
    assert model("hello") == '{"decisions": []}'
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert seen["timeout"] == 7.0
    body = seen["body"]
    assert body["model"] == "tiny" and body["temperature"] == 0
    assert model.calls == 1


def test_a_failing_endpoint_raises_so_the_decision_falls_back() -> None:
    def opener(request, timeout):
        raise urllib.error.URLError("refused")

    model = ResolutionModel("http://127.0.0.1:9/x", opener=opener)
    verdict = decide(SEAT_NEW, [Candidate("a", SEAT_OLD)], model=model)[0]
    assert verdict.replaces is False
    assert verdict.method == "fallback"
    assert model.failures == 1
