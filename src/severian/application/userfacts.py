"""Round 2, idea 1: facts the user stated, extracted by the local model in the background.

Switch: ``SEVERIAN_USER_FACTS`` (off by default). When on, every saved turn also queues an
``extract_facts`` job. The normal derive pass never takes those jobs, so saving and the raw turn's
searchability are unchanged; ``DerivationWorker.extract_facts`` sends batches of consecutive user
messages to the local model and stores each fact as a ``user_fact`` memory linked to its turn.

Borrowed:
- LongMemEval (Wu et al. 2024), src/index_expansion/batch_expansion_session_userfact.py: the
  user-fact prompt (personal information, life events, experiences, preferences; one simple
  standalone sentence each; pronouns replaced) and sending a session's user messages together.
- Mem0, mem0/configs/prompts.py: facts come solely from the user's messages, never the assistant's.
- Hindsight, hindsight_api/engine/retain/fact_extraction.py: self-contained facts that keep the time
  of the message they came from (here: the turn's leading time label, and Step 3 event time when
  that switch is on). Relative dates are kept as said; resolving them is a separate idea (5).
Own addition (v2): every fact in a message, and a change stated as the new situation in its own fact.
On one dev conversation plus a LongMemEval sample, v1 dropped the new state ("no longer pescatarian;
gone keto" -> only "no longer pescatarian") and most facts inside long LongMemEval requests.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

_log = logging.getLogger(__name__)

FACTS_JOB = "extract_facts"  # stored in job rows: renaming it strands queued work
BACKGROUND_JOB_KINDS = (FACTS_JOB,)  # never taken by the normal derive pass
FACT_KIND = "user_fact"
PROMPT_VERSION = "user-facts-v2"
DEFAULT_FACTS_MODEL = "active:aux"  # a Turbofit alias, like the cue model
DEFAULT_FACTS_URL = "http://127.0.0.1:8091/v1/chat/completions"
MAX_FACTS_PER_MESSAGE = 8
MAX_FACT_CHARS = 300
MAX_MESSAGE_CHARS = 2000  # long pastes rarely hold personal facts; cap the prompt
_TRUE = {"1", "true", "yes", "on"}


def user_facts_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return values.get("SEVERIAN_USER_FACTS", "").strip().lower() in _TRUE


# -- what the user said ---------------------------------------------------------------------

_LABEL = re.compile(r"^\[[^\]\n]{1,80}\][ \t]*")


def time_label(content: str) -> str:
    """The leading ``[...]`` label of a saved turn (e.g. its date), or ``""``."""
    match = _LABEL.match(content)
    return match.group(0).strip() if match else ""


def user_text(content: str, source: Mapping[str, Any]) -> str | None:
    """What the user said in a saved turn; ``None`` when it holds nothing from the user.

    Turns are saved the way Hermes ``sync_turn`` writes them: ``user: X\\nassistant: Y``, or just
    ``X`` when there is no reply yet (``source`` then carries the turn number). A caller that saves
    a bare assistant message marks it with ``source["speaker"] = "assistant"``. Anything else
    (delegated tasks, notes) is not a conversation turn and is skipped.
    """
    body = _LABEL.sub("", content, count=1).strip()
    if body.startswith("user:"):
        said = body[len("user:") :]
        cut = said.find("\nassistant:")
        return (said if cut < 0 else said[:cut]).strip() or None
    if body.startswith("assistant:") or source.get("turn") is None or source.get("speaker") == "assistant":
        return None
    return body or None


# -- the model call -------------------------------------------------------------------------

_PROMPT = (
    "You will be given numbered messages that a user sent to an AI assistant. Extract every fact the user "
    "states about themselves, their life and the people and things in it: personal information, life events, "
    "experiences, plans, preferences, possessions, relationships, routines, and anything they ask to be "
    "remembered or reminded about. Use only what the user said. Write each fact as one short standalone "
    "sentence, calling the user \"the user\": replace pronouns and vague references with what they refer to, "
    "and keep names, numbers, places, dates and time expressions exactly as the user gave them. A message can "
    "hold several facts: write each one. When the user says something changed, state the new situation as its "
    "own fact (for example, \"I swapped my bike for a car\" gives \"The user no longer has a bike.\" and \"The "
    "user now has a car.\"). Skip questions, requests and general knowledge that say nothing about the user. "
    "Answer with a JSON object that maps message numbers to lists of facts, for example {\"1\": [\"The user "
    "adopted a dog named Max.\"], \"3\": [\"The user prefers aisle seats.\"]}. Leave out messages with no facts. "
    "If there are none at all, answer {}."
)


def clean_facts(raw: object, *, count: int) -> dict[int, list[str]]:
    """Facts per message number (1..count) from a parsed reply.

    Accepts ``{"1": ["fact", ...]}`` (asked for) or ``[{"msg": 1, "fact": "..."}]`` (a common
    deviation). Whitespace is collapsed, duplicates dropped, and each message capped.
    """
    pairs: list[tuple[object, object]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            pairs += [(key, fact) for fact in (value if isinstance(value, list) else [value])]
    elif isinstance(raw, list):
        pairs += [(item.get("msg", item.get("message")), item.get("fact")) for item in raw if isinstance(item, dict)]
    out: dict[int, list[str]] = {}
    for key, fact in pairs:
        try:
            number = int(str(key).strip())
        except ValueError:
            continue
        text = " ".join(fact.split())[:MAX_FACT_CHARS].strip() if isinstance(fact, str) else ""
        if not text or not 1 <= number <= count:
            continue
        kept = out.setdefault(number, [])
        if len(kept) < MAX_FACTS_PER_MESSAGE and text.casefold() not in {k.casefold() for k in kept}:
            kept.append(text)
    return {number: out[number] for number in sorted(out)}


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _literal(text: str, start: int) -> object:
    """The bracketed Python literal starting at ``start`` (single-quoted replies), or None."""
    depth, quote = 0, ""
    for end in range(start, len(text)):
        char = text[end]
        if quote:
            quote = "" if char == quote and text[end - 1] != "\\" else quote
        elif char in "'\"":
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                try:
                    return ast.literal_eval(text[start : end + 1])
                except (ValueError, SyntaxError):
                    return None
    return None


def _first_json(text: str) -> object:
    """First JSON object or list of objects in a model reply, tolerating reasoning blocks, code
    fences, prose around it and Python-style quotes. Nothing usable -> None."""
    text = _THINK.sub("", text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            value = _literal(text, index)
        if isinstance(value, dict) or (isinstance(value, list) and all(isinstance(v, dict) for v in value)):
            return value
    return None


class FactExtractor:
    """Extracts user facts from a batch of messages with an OpenAI-compatible local chat endpoint.

    ``extract`` returns ``{}`` on any failure (down, slow, unusable reply) and counts it: facts are
    an extra, so a broken model must never break saving. ``calls``, ``timings`` and the token
    counters are the cost record the benchmark reports.
    """

    def __init__(
        self,
        url: str = DEFAULT_FACTS_URL,
        *,
        model: str = DEFAULT_FACTS_MODEL,
        timeout: float = 60.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.calls = 0
        self.failures = 0
        self.timings: list[float] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FactExtractor:
        values = os.environ if env is None else env
        return cls(
            values.get("SEVERIAN_FACTS_URL", DEFAULT_FACTS_URL),
            model=values.get("SEVERIAN_FACTS_MODEL", DEFAULT_FACTS_MODEL),
            timeout=float(values.get("SEVERIAN_FACTS_TIMEOUT", "60")),
        )

    def extract(self, messages: list[str]) -> dict[int, list[str]]:
        """Facts per message number (1-based, in the order given)."""
        if not messages:
            return {}
        self.calls += 1
        started = time.perf_counter()
        try:
            return self._extract(messages)
        except Exception as exc:  # facts are optional; never let a model fault escape
            self.failures += 1
            if self.failures == 1:  # say so once, so a misconfigured model is visible without log spam
                _log.warning("fact extraction failed (model %r at %s): %s", self.model, self.url, exc)
            return {}
        finally:
            self.timings.append(time.perf_counter() - started)

    def _extract(self, messages: list[str]) -> dict[int, list[str]]:
        numbered = "\n".join(f"[{number}] {text[:MAX_MESSAGE_CHARS]}" for number, text in enumerate(messages, 1))
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": min(2048, 256 + 192 * len(messages)),
                "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": numbered}],
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - operator-configured local URL
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with self._open(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        usage = payload.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":  # cut off: usually a repetition loop
            raise ValueError("reply cut off at max_tokens")
        found = _first_json(str(choice["message"]["content"]))
        if found is None:
            raise ValueError("no JSON object or list in the reply")
        return clean_facts(found, count=len(messages))
