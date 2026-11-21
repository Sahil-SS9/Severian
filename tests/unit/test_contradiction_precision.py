"""Contradiction precision (0.6.2): only a value that changes inside ONE shared statement is a contradiction.

On a real store the triage called ~4 pairs per memory a contradiction. Almost all of them were noise
of a few kinds, each pinned here with synthetic text (never real memory text):

- provenance labels (``[2026/10/23 (Fri) 17:43]``, ``[Mnemosyne working 2026-09-01]``) and date/time
  stamps counted as a value difference;
- a value that differs in two DIFFERENT sentences (two facts that can both be true);
- a different subject (\"Alice works at\" / \"Bob works at\");
- two edits in one sentence (a different slot: Q3 against Q4, each with its own figure);
- an added detail (one side has a value the other simply does not mention);
- commit ids and other letter+digit identifiers (two events, not two answers);
- a running figure (the same sentence frame with many different values is a log, not one fact).

The true contradictions keep their label: a number, a name or a negation that changes inside the same
sentence about the same subject. Duplicate decisions are untouched: nothing here may turn a pair into
a merge.
"""

from __future__ import annotations

import pytest

from severian.application.dreaming import triage
from severian.application.dreamtriage import CONTRADICT, DUPLICATE, RESTATE
from severian.domain.models import Memory, Scope

SCOPE = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def _memories(*texts: str) -> list[Memory]:
    return [
        Memory(
            scope=SCOPE,
            kind="fact",
            content=text,
            source_observation_ids=[f"obs-{index}"],
            model_id="t",
            prompt_version="p",
            confidence=0.9,
        )
        for index, text in enumerate(texts, start=1)
    ]


def kind_of(a: str, b: str) -> str | None:
    verdicts = triage(_memories(a, b)).verdicts
    return verdicts[0].kind if verdicts else None


#: True contradictions: one value changes inside one shared statement about one subject.
TRUE_CONTRADICTIONS: list[tuple[str, str, str]] = [
    ("number", "the office address is 10 downing street", "the office address is 20 downing street"),
    ("count", "the user has 2 cats", "the user has 3 cats"),
    ("place", "the user's flat is in Leeds", "the user's flat is in Bradford"),
    ("negation", "the user has a garden", "the user has not a garden"),
    ("negation-2", "The nightly backup is not encrypted.", "The nightly backup is encrypted."),
    ("rate", "The API rate limit is 100 requests per minute.", "The API rate limit is 250 requests per minute."),
    (
        "employer",
        "Sam works at Northwind as a data engineer.",
        "Sam works at Contoso as a data engineer.",
    ),
    ("partner", "The user's partner is called Priya.", "The user's partner is called Meera."),
    (
        "under-date-label",
        "[2026/10/23 (Fri) 17:43] The team standup starts at 9 every weekday in the small room.",
        "[2026/10/30 (Fri) 08:10] The team standup starts at 10 every weekday in the small room.",
    ),
    (
        "under-import-label",
        "[Mnemosyne working 2026-09-01] The staging database listens on port 5432 behind the VPN.",
        "[Mnemosyne working 2026-09-14] The staging database listens on port 6543 behind the VPN.",
    ),
    (
        "one-sentence-of-several",
        "Project notes. The release train leaves every Tuesday. The build server has 64 GB of RAM. "
        "Reviews need two approvals.",
        "Project notes. The release train leaves every Tuesday. The build server has 32 GB of RAM. "
        "Reviews need two approvals.",
    ),
]

#: Compatible pairs: both can be true. Every one of these is flagged as a contradiction by 0.6.0.
COMPATIBLE: list[tuple[str, str, str]] = [
    (
        "import-label-date-only",
        "[Mnemosyne working 2026-09-01] The user prefers tea over coffee in the morning and green tea after lunch.",
        "[Mnemosyne working 2026-09-14] The user prefers tea over coffee in the morning and green tea after lunch.",
    ),
    (
        "stacked-labels-with-ids",
        "[Mnemosyne working 2026-09-01] [Preference] [Import batch — ab12cd34] The user likes long walks by the sea.",
        "[Mnemosyne working 2026-09-02] [Preference] [Import batch — ef56ab78] The user likes long walks by the sea.",
    ),
    (
        "datetime-in-body",
        "Checked the queue at 2026-10-23T17:43Z and all workers were healthy with an empty backlog.",
        "Checked the queue at 2026-11-02T09:15Z and all workers were healthy with an empty backlog.",
    ),
    (
        "different-subject",
        "Alice works at Northwind as a data engineer in the London office.",
        "Bob works at Northwind as a data engineer in the London office.",
    ),
    (
        "different-slot",
        "The revenue target for Q3 is 12 million dollars.",
        "The revenue target for Q4 is 15 million dollars.",
    ),
    (
        "added-detail",
        "The user's flat is in Leeds near the station.",
        "The user's flat is in Leeds near the station, number 4.",
    ),
    (
        "commit-ids",
        "Merged the fix on branch main at commit a1b2c3d4.",
        "Merged the fix on branch main at commit 9f8e7d6c.",
    ),
    (
        "different-sentences",
        "Weekly notes. The user started learning Spanish and booked a weekly class with a tutor. "
        "The user ran 5 km on Sunday. The dog is called Rex and still hates the vacuum cleaner.",
        "Weekly notes. The user started learning Spanish and booked a weekly class with a tutor. "
        "The user cooked 3 new recipes. The dog is called Rex and still hates the vacuum cleaner.",
    ),
]


@pytest.mark.parametrize(("name", "a", "b"), TRUE_CONTRADICTIONS, ids=[case[0] for case in TRUE_CONTRADICTIONS])
def test_a_value_changed_inside_one_statement_is_a_contradiction(name: str, a: str, b: str) -> None:
    assert kind_of(a, b) == CONTRADICT


@pytest.mark.parametrize(("name", "a", "b"), COMPATIBLE, ids=[case[0] for case in COMPATIBLE])
def test_facts_that_can_both_be_true_are_not_a_contradiction(name: str, a: str, b: str) -> None:
    kind = kind_of(a, b)
    assert kind != CONTRADICT
    # Precision must not buy merges: a pair the old triage did not merge is still not merged.
    assert kind != DUPLICATE


def test_restatements_are_unchanged() -> None:
    assert kind_of("the user's dog is called Max", "the user's dog is named Max") == RESTATE
    assert kind_of("the user keeps a steel kettle", "the user keeps a steel kettle") == DUPLICATE


def test_a_running_figure_is_a_log_not_a_contradiction() -> None:
    """Seven readings of one figure: the same frame with many values is a series, not one fact."""
    texts = [
        f"The disk usage on the build server reached {value} percent during the nightly run."
        for value in (41, 47, 52, 58, 63, 66, 71)
    ]
    classified = triage(_memories(*texts))
    assert classified.contradictions == []


def test_a_fact_that_changed_a_few_times_is_still_flagged() -> None:
    """Three addresses over time: few values in one frame stay contradictions (the cap is for logs)."""
    texts = [f"The user's office is at {number} Market Street in the city centre." for number in (12, 40, 77)]
    classified = triage(_memories(*texts))
    assert len(classified.contradictions) == 3
