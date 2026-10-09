"""Replace or add: what a new fact does to the facts already saved (handoff 1, truth handling).

``decide`` looks at a new fact and the current facts that might be about the same thing and says,
for each, whether the new fact REPLACES it (the old one stops being true) or is ADDED beside it.

Rules decide whenever they can (``factslots``). A model is asked only about pairs the rules say
nothing about, and only when they look related; its reply must be strict JSON, and every kind of
failure (no model, timeout, bad JSON, an answer we do not recognise) means "add". Nothing here
deletes: a replaced fact is closed with ``valid_until`` by the caller and stays as history.

Switch: ``SEVERIAN_TRUTH_RESOLUTION`` (off by default). Optional model:
``SEVERIAN_TRUTH_RESOLUTION_URL`` (an OpenAI-style chat completions endpoint),
``..._MODEL``, ``..._TIMEOUT`` (seconds, default 20) and ``..._API_KEY``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from severian.application.eventtime import as_utc, message_time
from severian.application.factslots import (
    SINGLE_SLOTS,
    STOPWORDS,
    Claim,
    Reading,
    read_fact,
    user_words,
)
from severian.application.userfacts import FACT_KIND
from severian.domain.errors import NotFoundError
from severian.domain.models import Memory, Scope

logger = logging.getLogger(__name__)

SWITCH = "SEVERIAN_TRUTH_RESOLUTION"
MAX_MODEL_CANDIDATES = 4
# A saved turn whose assistant reply is longer than this holds more than the user's one fact.
MAX_ASSISTANT_WORDS = 40

_TRUE = {"1", "true", "yes", "on"}


def truth_resolution_enabled(env: Mapping[str, str] | None = None) -> bool:
    import os

    source = os.environ if env is None else env
    return source.get(SWITCH, "").strip().lower() in _TRUE


@dataclass(frozen=True)
class Candidate:
    """A current fact that might be about the same thing as the new one."""

    candidate_id: str
    text: str
    # May the model be asked about this pair when the rules are silent? (Short, single-fact memories only.)
    askable: bool = True


@dataclass(frozen=True)
class Verdict:
    """What to do with one candidate: replace it (``replaces``) or leave it and add."""

    candidate_id: str
    replaces: bool
    method: str  # "rule", "model" or "fallback"
    slot: str = ""
    reason: str = ""


# -- rules ----------------------------------------------------------------------------------


def _same_value(a: str, b: str) -> bool:
    return re.sub(r"[\s\-]", "", a) == re.sub(r"[\s\-]", "", b)


def _rule_verdict(new: Reading, old: Reading, candidate: Candidate) -> Verdict | None:
    if not old.claims:
        return None
    covered = 0  # old claims that the new fact either closes or restates
    replaced_slot = ""
    related: Verdict | None = None
    for old_claim in old.claims:
        status = _claim_status(new, old_claim)
        if status.kind == "replace":
            covered += 1
            replaced_slot = replaced_slot or old_claim.slot
        elif status.kind == "same":
            covered += 1
            related = related or Verdict(candidate.candidate_id, False, "rule", old_claim.slot, "same value")
        elif status.kind == "add":
            related = related or Verdict(candidate.candidate_id, False, "rule", old_claim.slot, status.reason)
    if replaced_slot:
        if covered != len(old.claims):
            return Verdict(candidate.candidate_id, False, "rule", replaced_slot, "candidate holds other claims")
        if old.unread or old.assistant_words > MAX_ASSISTANT_WORDS:
            # It says something else we cannot read (or the assistant said a lot): closing the whole
            # message would hide that too.
            return Verdict(candidate.candidate_id, False, "rule", replaced_slot, "candidate holds other content")
        return Verdict(candidate.candidate_id, True, "rule", replaced_slot, f"replaced: {replaced_slot}")
    return related


@dataclass(frozen=True)
class _Status:
    kind: str  # "replace", "same", "add" or "none"
    reason: str = ""


def _claim_status(new: Reading, old: Claim) -> _Status:
    if new.ends(old.value, slot=old.slot):
        return _Status("replace", "ended by the new fact")
    for claim in new.claims:
        if claim.slot != old.slot:
            continue
        if claim.subject != old.subject:
            return _Status("add", "different subject")
        if _same_value(claim.value, old.value):
            return _Status("same")
        if old.slot in SINGLE_SLOTS and not claim.additive and not old.additive:
            return _Status("replace", "single-valued slot")
        return _Status("add", "additive slot")
    return _Status("none")


# -- is it worth asking the model? ----------------------------------------------------------

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", user_words(text).lower() or text.lower())
    return {t for t in words if len(t) > 1 and t not in STOPWORDS}


def _related(new_text: str, old_text: str) -> bool:
    a, b = _tokens(new_text), _tokens(old_text)
    shared = a & b
    if len(shared) >= 2:
        return True
    return len(shared) == 1 and len(shared) / len(a | b) >= 0.5


# -- the model ------------------------------------------------------------------------------


def build_prompt(new_text: str, candidates: Sequence[Candidate]) -> str:
    lines = [
        "You compare a NEW fact with saved facts about the same person.",
        "For each saved fact decide:",
        '- "replace": the new fact means the saved fact is no longer true (same attribute, only one value at a time).',
        '- "add": both can be true at once (different things, extra detail, a second item).',
        "When unsure, answer add.",
        'Reply with JSON only: {"decisions": [{"n": 1, "decision": "replace"}]}',
        "",
        f"NEW: {user_words(new_text) or new_text}",
        "SAVED:",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(f"[{index}] {user_words(candidate.text) or candidate.text}")
    return "\n".join(lines)


def parse_reply(reply: str, count: int) -> dict[int, bool] | None:
    """Strict JSON in, ``{n: replaces}`` out. ``None`` if the reply is not the JSON we asked for."""
    start, stop = reply.find("{"), reply.rfind("}")
    if start < 0 or stop < start:
        return None
    try:
        data = json.loads(reply[start : stop + 1])
    except ValueError:
        return None
    decisions = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list):
        return None
    out: dict[int, bool] = {}
    for item in decisions:
        if not isinstance(item, dict):
            continue
        n, choice = item.get("n"), item.get("decision")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count:
            continue
        if isinstance(choice, str) and choice.strip().lower() in {"replace", "add"}:
            out[n] = choice.strip().lower() == "replace"
    return out


class ResolutionModel:
    """A small OpenAI-style chat client. Raises on any failure; ``decide`` turns that into "add"."""

    def __init__(
        self,
        url: str,
        model: str = "",
        *,
        timeout: float = 20.0,
        api_key: str = "",
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._model = model
        self._timeout = timeout
        self._api_key = api_key
        self._opener = opener if opener is not None else urllib.request.urlopen
        self.calls = 0
        self.failures = 0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, opener: Callable[..., Any] | None = None
    ) -> ResolutionModel | None:
        import os

        source = os.environ if env is None else env
        url = source.get(f"{SWITCH}_URL", "").strip()
        if not url:
            return None
        try:
            timeout = float(source.get(f"{SWITCH}_TIMEOUT", "20"))
        except ValueError:
            timeout = 20.0
        return cls(
            url,
            source.get(f"{SWITCH}_MODEL", "").strip(),
            timeout=timeout,
            api_key=source.get(f"{SWITCH}_API_KEY", "").strip(),
            opener=opener,
        )

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }
        if self._model:
            body["model"] = self._model
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(self._url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("model reply has no text")
            return content
        except Exception:
            self.failures += 1
            raise


# -- the decision ---------------------------------------------------------------------------


def decide(
    new_text: str,
    candidates: Sequence[Candidate],
    *,
    model: Callable[[str], str] | None = None,
) -> list[Verdict]:
    """One verdict per candidate worth considering, in the order given. Never raises."""
    new = read_fact(new_text)
    decided: dict[str, Verdict] = {}
    asking: list[Candidate] = []
    for candidate in candidates:
        verdict = _rule_verdict(new, read_fact(candidate.text), candidate)
        if verdict is not None:
            decided[candidate.candidate_id] = verdict
        elif candidate.askable and _related(new_text, candidate.text):
            asking.append(candidate)
    asking = asking[:MAX_MODEL_CANDIDATES]
    if asking:
        for verdict in _ask_model(new_text, asking, model):
            decided[verdict.candidate_id] = verdict
    return [decided[c.candidate_id] for c in candidates if c.candidate_id in decided]


def _ask_model(new_text: str, asking: Sequence[Candidate], model: Callable[[str], str] | None) -> list[Verdict]:
    def fallback(reason: str) -> list[Verdict]:
        return [Verdict(c.candidate_id, False, "fallback", reason=reason) for c in asking]

    if model is None:
        return fallback("no model")
    try:
        reply = model(build_prompt(new_text, asking))
    except Exception as error:  # any failure at all means "add"
        return fallback(f"model failed: {type(error).__name__}")
    parsed = parse_reply(reply, len(asking)) if isinstance(reply, str) else None
    if parsed is None:
        return fallback("unreadable model reply")
    verdicts: list[Verdict] = []
    for index, candidate in enumerate(asking, start=1):
        if index in parsed:
            reason = "model: replace" if parsed[index] else "model: add"
            verdicts.append(Verdict(candidate.candidate_id, parsed[index], "model", reason=reason))
        else:
            verdicts.append(Verdict(candidate.candidate_id, False, "fallback", reason="no usable answer"))
    return verdicts


# -- the resolver ---------------------------------------------------------------------------

LOG_ENV = f"{SWITCH}_LOG"
_CACHE_LIMIT = 50_000
_MESSAGE_CHARS = 400


@dataclass(frozen=True)
class Closure:
    """One memory that stopped being current because a newer one replaced it."""

    old_id: str
    new_id: str
    valid_until: datetime
    method: str
    slot: str
    reason: str


class TruthResolver:
    """Closes the memories a newer fact replaces (``valid_until`` + ``replaced_by_id``), deleting nothing.

    Called after new memories are saved. Rules decide when they can; the model (optional) is asked only
    about short ``user_fact`` pairs the rules say nothing about, and any failure means \"keep both\".
    Time always comes from when the message was WRITTEN, never when it was saved, and a memory is only
    ever closed by one written strictly later. The check runs both ways, so turns saved out of order
    still end up right. Any error leaves every memory open: this never breaks saving.
    """

    def __init__(
        self,
        repository: Any,
        *,
        model: Callable[[str], str] | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self.repository = repository
        self.model = model
        self._log_path = Path(log_path) if log_path else None
        self.closed = 0
        self._seen: dict[tuple[str, str], tuple[datetime | None, str]] = {}

    @classmethod
    def from_env(cls, repository: Any, env: Mapping[str, str] | None = None) -> TruthResolver:
        source = os.environ if env is None else env
        return cls(
            repository,
            model=ResolutionModel.from_env(source),
            log_path=source.get(LOG_ENV, "").strip() or None,
        )

    # -- when a message was written ---------------------------------------------------------

    def _source(self, scope: Scope, observation_id: str) -> tuple[datetime | None, str]:
        key = (scope.model_dump_json(), observation_id)
        if key not in self._seen:
            if len(self._seen) >= _CACHE_LIMIT:
                self._seen.clear()
            found: tuple[datetime | None, str]
            try:
                observation = self.repository.get_observation(scope, observation_id)
                found = (
                    message_time(observation.source, observation.created_at),
                    observation.content[:_MESSAGE_CHARS],
                )
            except NotFoundError:
                found = (None, "")
            self._seen[key] = found
        return self._seen[key]

    def _time(self, scope: Scope, memory: Memory) -> datetime:
        moments = [t for t, _ in (self._source(scope, i) for i in memory.source_observation_ids) if t is not None]
        return min(moments) if moments else as_utc(memory.created_at)

    def _message(self, scope: Scope, memory: Memory) -> str:
        for observation_id in memory.source_observation_ids:
            text = self._source(scope, observation_id)[1]
            if text:
                return text
        return ""

    # -- the work ---------------------------------------------------------------------------

    def resolve(self, scope: Scope, new_memories: Sequence[Memory]) -> list[Closure]:
        """Close what ``new_memories`` replace (and what already replaces them). Never raises."""
        done: list[Closure] = []
        try:
            wanted = [m for m in new_memories if m.valid_until is None and self._worth_checking(m)]
            if not wanted:
                return done
            pool = list(self.repository.list_memories(scope))
            gone: set[str] = set()
            for memory in wanted:
                if memory.id in gone:
                    continue
                done += self._forward(scope, memory, pool, gone)
                done += self._backward(scope, memory, pool, gone)
        except Exception:  # truth resolution is optional: whatever goes wrong, nothing is closed by it
            logger.warning("truth resolution failed; leaving facts open", exc_info=True)
        return done

    def _worth_checking(self, memory: Memory) -> bool:
        reading = read_fact(memory.content)
        return bool(reading.claims or reading.endings) or (self.model is not None and memory.kind == FACT_KIND)

    def _plausible(self, new: Memory, other: Memory) -> bool:
        """A cheap superset of the pairs ``decide`` can act on: lets us skip looking up times for the rest."""
        a, b = read_fact(new.content), read_fact(other.content)
        a_slots = {c.slot for c in a.claims} | {s for e in a.endings for s in e.slots}
        b_slots = {c.slot for c in b.claims} | {s for e in b.endings for s in e.slots}
        if a_slots & b_slots:
            return True
        return self.model is not None and new.kind == other.kind == FACT_KIND and _related(new.content, other.content)

    def _forward(self, scope: Scope, new: Memory, pool: Sequence[Memory], gone: set[str]) -> list[Closure]:
        new_time = self._time(scope, new)
        older = [
            m
            for m in pool
            if m.id != new.id and m.id not in gone and m.valid_until is None and self._plausible(new, m)
            and self._time(scope, m) < new_time
        ]
        if not older:
            return []
        by_id = {m.id: m for m in older}
        askable = new.kind == FACT_KIND
        candidates = [Candidate(m.id, m.content, askable=askable and m.kind == FACT_KIND) for m in older]
        closures: list[Closure] = []
        for verdict in decide(new.content, candidates, model=self.model):
            old = by_id[verdict.candidate_id]
            closed = verdict.replaces and self.repository.close_memory(
                scope, old.id, valid_until=new_time, replaced_by_id=new.id
            )
            if closed:
                gone.add(old.id)
                self.closed += 1
                closures.append(Closure(old.id, new.id, new_time, verdict.method, verdict.slot, verdict.reason))
            self._record(scope, new, old, verdict, new_time if closed else None)
        return closures

    def _backward(self, scope: Scope, old: Memory, pool: Sequence[Memory], gone: set[str]) -> list[Closure]:
        """Is the new memory already replaced by a newer one that was saved first? Rules only; the earliest wins."""
        if old.id in gone:
            return []
        old_time = self._time(scope, old)
        newer = sorted(
            (
                (self._time(scope, m), m)
                for m in pool
                if m.id != old.id and m.id not in gone and m.valid_until is None and self._plausible(m, old)
            ),
            key=lambda pair: (pair[0], 0 if pair[1].kind == old.kind else 1, pair[1].id),
        )
        for when, later in newer:
            if when <= old_time:
                continue
            verdicts = decide(later.content, [Candidate(old.id, old.content, askable=False)], model=None)
            for verdict in verdicts:
                closed = verdict.replaces and self.repository.close_memory(
                    scope, old.id, valid_until=when, replaced_by_id=later.id
                )
                self._record(scope, later, old, verdict, when if closed else None)
                if closed:
                    gone.add(old.id)
                    self.closed += 1
                    return [Closure(old.id, later.id, when, verdict.method, verdict.slot, verdict.reason)]
        return []

    # -- the 50-item sheet ------------------------------------------------------------------

    def _record(self, scope: Scope, new: Memory, old: Memory, verdict: Verdict, valid_until: datetime | None) -> None:
        if self._log_path is None:
            return
        row = {
            "scope": scope.model_dump(),
            "new_id": new.id,
            "old_id": old.id,
            "new_text": new.content,
            "old_text": old.content,
            "new_message": self._message(scope, new),
            "old_message": self._message(scope, old),
            "new_sources": list(new.source_observation_ids),
            "old_sources": list(old.source_observation_ids),
            "replaces": valid_until is not None,
            "method": verdict.method,
            "slot": verdict.slot,
            "reason": verdict.reason,
            "valid_until": valid_until.isoformat() if valid_until is not None else None,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            logger.warning("could not write the truth-resolution sheet", exc_info=True)
