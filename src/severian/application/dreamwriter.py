"""The only model call a dreaming pass may make: writing a merge summary.

The pass never asks a model what to do. :mod:`severian.application.dreamtriage` decides what is a
duplicate, a restatement or a contradiction before this module is reached; here the model is given
an already-decided group and asked for one sentence saying what the group holds. Its output is
text, nothing else: it cannot add a fact (the prompt forbids it and the text is only ever the
summary of a group whose ids are written by code), and a failed call costs the pass nothing but a
line in its counters, because the deterministic summary text is used instead.

Mechanics follow the 0.3.7 busy-model rules already in :mod:`severian.application.userfacts`
(a local model that times out, restarts or refuses an identical request is the normal failure, not
an exception): one retry after a pause when the fault is transient, twice the room when the reply
was cut off, a nudge when the reply was unreadable, and no retry storm — a batch that fails twice is
counted and given up.

The endpoint must be loopback. Pointing a pass at a paid endpoint is a costed decision, so it needs
``SEVERIAN_DREAM_ALLOW_REMOTE=1`` as well as the URL: the default path cannot spend money.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

DEFAULT_DREAM_URL = "http://127.0.0.1:8091/v1/chat/completions"
DEFAULT_DREAM_MODEL = "active:aux"
#: Groups per call. Small on purpose: a busy local model fails a big batch whole.
DREAM_BATCH = 8
RETRY_PAUSE_S = 5.0
MAX_SUMMARY_CHARS = 300
MAX_INPUT_CHARS = 400
MAX_KEPT_REPLIES = 20
MAX_KEPT_REPLY_CHARS = 2000
#: The share of a pass's batches allowed to fail before it stops calling the model at all.
FAILED_BATCH_CAP = 0.10
_TRUE = {"1", "true", "yes", "on"}

_PROMPT = (
    "You are given short numbered items, each one a merge summary: a set of memories in a personal "
    "memory store that say the same thing. Rewrite each item as ONE short sentence that says what "
    "the group holds, in the third person, calling the person \"the user\". Keep every name, number, "
    "place and date exactly as written. Do not add anything the item does not say, do not merge two "
    "items, and do not answer questions or add commentary. Reply with a JSON object that maps each "
    "number to its sentence, for example {\"1\": \"The user keeps a blue Ridgeback tourer.\"}. "
    "Reply with only the JSON object."
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def is_loopback(url: str) -> bool:
    """``True`` when ``url`` points at this machine (no network egress, no cost)."""
    host = urllib.parse.urlparse(url).hostname
    return (host or "") in _LOOPBACK_HOSTS


class DreamCallError(Exception):
    """One model call that gave nothing usable. ``transient`` marks a server or network fault."""

    def __init__(self, reason: str, *, reply: str = "", transient: bool = False, cut_off: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reply = reply
        self.transient = transient
        self.cut_off = cut_off

    def describe(self) -> str:
        return f"{self.reason} (reply: {self.reply[:200]!r})" if self.reply else self.reason


@dataclass
class DreamWriterStats:
    """The cost record a pass reports next to its gain: calls, latency and failed batches."""

    batches: int = 0
    calls: int = 0
    failures: int = 0
    retries: int = 0
    failed_batches: int = 0
    summaries: int = 0
    deduped: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    stopped_for_failures: bool = False
    bad_replies: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "model_batches": self.batches,
            "model_failures": self.failures,
            "model_retries": self.retries,
            "failed_batches": self.failed_batches,
            "failed_batch_share": round(self.failed_batches / self.batches, 4) if self.batches else 0.0,
            "summaries": self.summaries,
            "deduped_summaries": self.deduped,
            "model_seconds": round(self.seconds, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "stopped_for_failures": self.stopped_for_failures,
        }


class DreamWriter:
    """Writes merge summaries with an OpenAI-compatible chat endpoint (loopback by default)."""

    def __init__(
        self,
        url: str = DEFAULT_DREAM_URL,
        *,
        model: str = DEFAULT_DREAM_MODEL,
        timeout: float = 60.0,
        allow_remote: bool = False,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not allow_remote and not is_loopback(url):
            raise ValueError(
                "the dreaming writer refuses a non-loopback endpoint without SEVERIAN_DREAM_ALLOW_REMOTE=1: "
                "a remote model is a costed run, not a default"
            )
        self.url = url
        self.model = model
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.stats = DreamWriterStats()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DreamWriter:
        values = os.environ if env is None else env
        allow_remote = values.get("SEVERIAN_DREAM_ALLOW_REMOTE", "").strip().lower() in _TRUE
        return cls(
            values.get("SEVERIAN_DREAM_URL", DEFAULT_DREAM_URL),
            model=values.get("SEVERIAN_DREAM_MODEL", DEFAULT_DREAM_MODEL),
            timeout=float(values.get("SEVERIAN_DREAM_TIMEOUT", "60")),
            allow_remote=allow_remote,
        )

    def write(self, texts: list[str]) -> list[str | None]:
        """One normalised sentence per input text, or ``None`` where the batch was given up.

        Ids are never in ``texts``: the caller passes the group's own words and keeps the ids. A
        batch of one gives up on its own, so one unreadable group cannot cost the others theirs.
        """
        out: list[str | None] = [None] * len(texts)
        started = time.perf_counter()
        try:
            for start in range(0, len(texts), DREAM_BATCH):
                batch = texts[start : start + DREAM_BATCH]
                if self.stats.batches and self.stats.failed_batches / self.stats.batches > FAILED_BATCH_CAP:
                    # The per-pass cap: a model that keeps failing must stop costing the pass time.
                    self.stats.stopped_for_failures = True
                    break
                self.stats.batches += 1
                found = self._attempt(batch)
                if found is None:
                    self.stats.failed_batches += 1
                    continue
                seen: set[str] = set()
                for offset, text in enumerate(found):
                    if text is None:
                        continue
                    folded = " ".join(text.split())
                    if folded.casefold() in seen:
                        # Dedupe within a batch: a model that repeats itself must not put one
                        # sentence in the store under several groups.
                        self.stats.deduped += 1
                        continue
                    seen.add(folded.casefold())
                    out[start + offset] = folded[:MAX_SUMMARY_CHARS]
                    self.stats.summaries += 1
        finally:
            self.stats.seconds += time.perf_counter() - started
        return out

    def _attempt(self, batch: list[str]) -> list[str | None] | None:
        """One call, one retry per the 0.3.7 rules; ``None`` when the batch is given up."""
        try:
            return self._call(batch)
        except DreamCallError as first:
            if first.transient:
                self.stats.retries += 1
                self._sleep(RETRY_PAUSE_S)
            else:
                self.stats.retries += 1
            try:
                return self._call(batch, room=2 if first.cut_off else 1, nudge=not (first.transient or first.cut_off))
            except DreamCallError:
                return None

    def _call(self, batch: list[str], *, room: int = 1, nudge: bool = False) -> list[str | None]:
        self.stats.calls += 1
        numbered = "\n".join(f"[{index}] {text[:MAX_INPUT_CHARS]}" for index, text in enumerate(batch, 1))
        if nudge:
            numbered += "\n\nReply with only the JSON object described above: no prose, no list."
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": min(2048, room * (128 + 96 * len(batch))),
                "messages": [
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": numbered},
                ],
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - operator-configured endpoint
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            transient = exc.code >= 500 or exc.code in (409, 429)
            error = DreamCallError(str(exc), reply=_error_body(exc), transient=transient)
            self._record_failure(error)
            raise error from exc
        except (OSError, http.client.HTTPException) as exc:
            error = DreamCallError(str(exc) or type(exc).__name__, transient=True)
            self._record_failure(error)
            raise error from exc
        usage = payload.get("usage") or {}
        self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
        choice = payload["choices"][0]
        content = str(choice["message"].get("content") or "")
        reply = content or json.dumps(choice["message"])
        if choice.get("finish_reason") == "length":
            error = DreamCallError("reply cut off at max_tokens", reply=reply, cut_off=True)
            self._record_failure(error)
            raise error
        found = _summaries(content, count=len(batch))
        if found is None:
            error = DreamCallError("no usable JSON object of summaries in the reply", reply=reply)
            self._record_failure(error)
            raise error
        return found

    def _record_failure(self, error: DreamCallError) -> None:
        self.stats.failures += 1
        _log.warning("dream writer call failed (model %r at %s): %s", self.model, self.url, error.describe())
        if len(self.stats.bad_replies) < MAX_KEPT_REPLIES:
            self.stats.bad_replies.append(
                {"reason": error.reason, "reply": error.reply[:MAX_KEPT_REPLY_CHARS]}
            )


def _error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", "replace")[:MAX_KEPT_REPLY_CHARS]
    except Exception:  # the body is a nicety; the status line already says what happened
        return ""


def _summaries(content: str, *, count: int) -> list[str | None] | None:
    """One entry per numbered item from a ``{"1": "..."}`` reply; ``None`` when nothing usable came back."""
    from severian.application.userfacts import _first_json  # one tolerant JSON reader, not two

    found = _first_json(content)
    if isinstance(found, dict):
        pairs = [(key, value) for key, value in found.items()]
    elif isinstance(found, list):
        pairs = [(item.get("n", item.get("id", item.get("item"))), item.get("summary", item.get("text")))
                 for item in found if isinstance(item, dict)]
    else:
        return None
    out: list[str | None] = [None] * count
    for key, value in pairs:
        try:
            number = int(str(key).strip())
        except ValueError:
            continue
        if not 1 <= number <= count or not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if text:
            out[number - 1] = text
    return out if any(item is not None for item in out) else None


__all__ = [
    "DEFAULT_DREAM_MODEL",
    "DEFAULT_DREAM_URL",
    "DREAM_BATCH",
    "DreamCallError",
    "DreamWriter",
    "DreamWriterStats",
    "is_loopback",
]
