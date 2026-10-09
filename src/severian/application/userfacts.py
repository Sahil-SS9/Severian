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
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
RETRY_PAUSE_S = 5.0  # a server or network fault gets this long before the one retry
# Added to the retry after an unreadable reply: at temperature 0 an identical request repeats the
# same bad reply (real model, 05/10), so the retry must differ to be worth its call.
JSON_ONLY = "Reply with only the JSON object described above: no list, no other text."
MAX_KEPT_REPLIES = 50  # bad replies kept per extractor, so failures can be read later
MAX_KEPT_REPLY_CHARS = 4000
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


@dataclass(frozen=True)
class Extraction:
    """Facts per message number (1-based) and, for messages given up on, why."""

    facts: dict[int, list[str]] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)


_DESCRIBED_REPLY_CHARS = 300


class FactCallError(Exception):
    """One model call that gave nothing usable.

    ``reply`` keeps what came back (the model's text, or the server's error body; may be empty).
    ``transient`` marks a server or network fault: a pause might help, a smaller batch would not.
    ``cut_off`` marks a reply that hit the token limit: more room might help.
    """

    def __init__(self, reason: str, *, reply: str = "", transient: bool = False, cut_off: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reply = reply
        self.transient = transient
        self.cut_off = cut_off

    def describe(self) -> str:
        """The reason and the start of the reply: what a given-up turn's job keeps as its error."""
        return f"{self.reason} (reply: {self.reply[:_DESCRIBED_REPLY_CHARS]!r})" if self.reply else self.reason


class FactExtractor:
    """Extracts user facts from a batch of messages with an OpenAI-compatible local chat endpoint.

    ``extract`` never raises: facts are an extra, so a broken model must never break saving.
    ``calls``, ``timings``, the token counters and the failure counters are the cost record the
    benchmark reports; ``bad_replies`` keeps the first replies that could not be used.
    """

    def __init__(
        self,
        url: str = DEFAULT_FACTS_URL,
        *,
        model: str = DEFAULT_FACTS_MODEL,
        timeout: float = 60.0,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.calls = 0
        self.failures = 0  # failed calls
        self.timings: list[float] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.batches = 0  # extract() calls with something to send
        self.first_failures = 0  # batches whose first call failed
        self.retries = 0
        self.splits = 0
        self.failed_batches = 0  # batches with at least one message given up
        self.given_up = 0  # messages given up
        self.bad_replies: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FactExtractor:
        values = os.environ if env is None else env
        return cls(
            values.get("SEVERIAN_FACTS_URL", DEFAULT_FACTS_URL),
            model=values.get("SEVERIAN_FACTS_MODEL", DEFAULT_FACTS_MODEL),
            timeout=float(values.get("SEVERIAN_FACTS_TIMEOUT", "60")),
        )

    def extract(self, messages: list[str]) -> Extraction:
        """Facts per message number (1-based, in the order given), and the messages given up on.

        A failed call keeps its reply and the batch is retried once: after a pause when the server
        or network failed, with twice the room when the reply was cut off. If the retry fails too,
        a reply problem splits the batch in half and each half is tried once, splitting again while
        it fails, so one unreadable message costs only its own facts. A server or network fault is
        never split (a smaller batch would not help): the batch is given up.
        """
        if not messages:
            return Extraction()
        self.batches += 1
        facts: dict[int, list[str]] = {}
        failed: dict[int, str] = {}
        self._attempt(messages, 0, facts, failed, top=True)
        if failed:
            self.failed_batches += 1
            self.given_up += len(failed)
        return Extraction(facts=dict(sorted(facts.items())), failed=dict(sorted(failed.items())))

    def _attempt(
        self, messages: list[str], offset: int, facts: dict[int, list[str]], failed: dict[int, str], *, top: bool
    ) -> None:
        try:
            found = self._call(messages)
        except FactCallError as first:
            if top:
                self.first_failures += 1
            if not (top or first.transient):  # inside a split: only a server fault earns a retry
                self._split_or_give_up(messages, offset, facts, failed, first)
                return
            self.retries += 1
            if first.transient:
                self._sleep(RETRY_PAUSE_S)
            try:
                found = self._call(
                    messages, room=2 if first.cut_off else 1, nudge=not (first.transient or first.cut_off)
                )
            except FactCallError as second:
                self._split_or_give_up(messages, offset, facts, failed, second)
                return
        for number, items in found.items():
            facts[offset + number] = items

    def _split_or_give_up(
        self,
        messages: list[str],
        offset: int,
        facts: dict[int, list[str]],
        failed: dict[int, str],
        error: FactCallError,
    ) -> None:
        if error.transient or len(messages) == 1:
            failed.update({offset + number: error.describe() for number in range(1, len(messages) + 1)})
            return
        self.splits += 1
        half = (len(messages) + 1) // 2
        self._attempt(messages[:half], offset, facts, failed, top=False)
        self._attempt(messages[half:], offset + half, facts, failed, top=False)

    def _call(self, messages: list[str], *, room: int = 1, nudge: bool = False) -> dict[int, list[str]]:
        """One model call; any problem becomes a FactCallError, counted and kept."""
        self.calls += 1
        started = time.perf_counter()
        try:
            return self._request(messages, room=room, nudge=nudge)
        except Exception as exc:  # facts are optional; never let a model fault escape
            error = exc if isinstance(exc, FactCallError) else FactCallError(f"{type(exc).__name__}: {exc}")
            self.failures += 1
            _log.warning("fact extraction failed (call %d, failures %d, model %r at %s): %s",
                         self.calls, self.failures, self.model, self.url, error.describe())
            if len(self.bad_replies) < MAX_KEPT_REPLIES:
                self.bad_replies.append(
                    {"reason": error.reason, "reply": error.reply[:MAX_KEPT_REPLY_CHARS], "messages": len(messages)}
                )
            raise error from exc
        finally:
            self.timings.append(time.perf_counter() - started)

    def _request(self, messages: list[str], *, room: int, nudge: bool = False) -> dict[int, list[str]]:
        numbered = "\n".join(f"[{number}] {text[:MAX_MESSAGE_CHARS]}" for number, text in enumerate(messages, 1))
        if nudge:
            numbered += f"\n\n{JSON_ONLY}"
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": min(4096, room * min(2048, 256 + 192 * len(messages))),
                "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": numbered}],
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - operator-configured local URL
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:  # the server answered with an error status
            raise FactCallError(str(exc), reply=_error_body(exc), transient=exc.code >= 500 or exc.code == 429) from exc
        except (OSError, http.client.HTTPException) as exc:  # refused, reset or timed out
            raise FactCallError(str(exc) or type(exc).__name__, transient=True) from exc
        usage = payload.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        choice = payload["choices"][0]
        content = str(choice["message"].get("content") or "")
        # An empty reply keeps the whole message (e.g. a reasoning field), so the failure can be read.
        reply = content or json.dumps(choice["message"])
        if choice.get("finish_reason") == "length":  # cut off: a long answer or a repetition loop
            raise FactCallError("reply cut off at max_tokens", reply=reply, cut_off=True)
        found = _first_json(content)
        if found is None:
            raise FactCallError("no JSON object or list in the reply", reply=reply)
        return clean_facts(found, count=len(messages))


def _error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", "replace")[:MAX_KEPT_REPLY_CHARS]
    except Exception:  # the body is a nicety; the status line already says what happened
        return ""
