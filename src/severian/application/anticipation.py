"""Anticipation (Step 4, next-improvements plan): cues and triggers.

Two small ideas, both off unless ``SEVERIAN_ANTICIPATION`` is switched on:

* Cues. When a memory is derived, a short local-model generation writes a few "when this will be
  needed" phrases (likely future questions, situations). They are indexed for search next to the
  memory and linked to it, never shown as facts, and erased with it. Generation happens in the
  background derive worker only, never on the chat path, and a failed generation never fails the
  derive (the memory simply has no cues).
* Triggers. A memory that is an instruction for later ("next time you touch auth.py, run the
  linter first") gets a trigger: tools, file paths, repositories and keywords. ``match_due`` returns
  the memories whose trigger fits the current situation, a few at a time, so the host can remind
  rather than nag.

Everything here is rules plus one optional HTTP call; no new dependency, no model on the read path.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from severian.domain.models import Memory, Trigger

__all__ = [
    "CueGenerator",
    "DueContext",
    "MAX_CUES",
    "MAX_REMINDERS",
    "anticipation_enabled",
    "clean_cues",
    "extract_trigger",
    "match_due",
]

MAX_CUES = 3
MAX_CUE_CHARS = 120
# TriggerBench: models drift to "always remind". Never more than this many reminders at once.
MAX_REMINDERS = 2

_TRUE = {"1", "true", "yes", "on"}


def anticipation_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True when ``SEVERIAN_ANTICIPATION`` is switched on (default off)."""
    values = os.environ if env is None else env
    return values.get("SEVERIAN_ANTICIPATION", "").strip().casefold() in _TRUE


# -- cues -----------------------------------------------------------------------------------


def clean_cues(raw: object) -> list[str]:
    """Keep up to ``MAX_CUES`` short, distinct, non-empty strings from a model answer."""
    if not isinstance(raw, list):
        return []
    cues: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cue = " ".join(item.split())[:MAX_CUE_CHARS].strip()
        key = cue.casefold()
        if cue and key not in seen:
            seen.add(key)
            cues.append(cue)
        if len(cues) == MAX_CUES:
            break
    return cues


_CUE_PROMPT = (
    "You index a personal memory. Given one remembered statement, write up to 3 short cues: the "
    "questions someone might ask later, or the situations they will be in, when this statement "
    "will be needed. Use different words from the statement where you can. Answer with a JSON "
    'list of strings only, for example ["what does Alice drink", "tea or coffee for Alice"].'
)


class CueGenerator:
    """Writes cues for a memory with an OpenAI-compatible local chat endpoint (Turbofit 8091).

    ``generate`` returns ``[]`` on any failure (down, slow, bad answer): cues are an extra, so a
    broken model must never break derivation. ``timings`` records seconds per call for the
    "worker latency per memory" measurement.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8091/v1/chat/completions",
        *,
        model: str = "local",
        timeout: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.timings: list[float] = []
        self.failures = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CueGenerator:
        values = os.environ if env is None else env
        return cls(
            values.get("SEVERIAN_CUE_URL", "http://127.0.0.1:8091/v1/chat/completions"),
            model=values.get("SEVERIAN_CUE_MODEL", "local"),
            timeout=float(values.get("SEVERIAN_CUE_TIMEOUT", "20")),
        )

    def generate(self, content: str) -> list[str]:
        started = time.perf_counter()
        try:
            return self._generate(content)
        except Exception:  # cues are optional; never let a model fault escape
            self.failures += 1
            return []
        finally:
            self.timings.append(time.perf_counter() - started)

    def _generate(self, content: str) -> list[str]:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 160,
                "messages": [
                    {"role": "system", "content": _CUE_PROMPT},
                    {"role": "user", "content": content},
                ],
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - operator-configured local URL
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with self._open(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        text = str(payload["choices"][0]["message"]["content"])
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        return clean_cues(json.loads(text[start : end + 1]))


# -- triggers -------------------------------------------------------------------------------

# "next time", "whenever", "from now on" ... : the phrases that make a statement an instruction for later.
_INSTRUCTION = re.compile(
    r"\b(next time|every time|each time|whenever|from now on|in future|in the future|"
    r"remind me|remember to|don'?t forget to|make sure (?:to|you)|always|before (?:you|i) )\b",
    re.IGNORECASE,
)
_PATH = re.compile(
    r"(?<![\w/])((?:~|\.{1,2})?/?(?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|js|ts|tsx|jsx|md|yaml|yml|json|toml|sql|sh|rs|go|c|h|cpp|css|html))\b"
)
_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_TOOL_VERB = re.compile(
    r"\b(?:using|use|uses|running|run|runs|calling|call|calls|invoking|invoke|opening|open|editing|edit|touch|touching)"
    r"\s+(?:the\s+)?`?([A-Za-z][\w.-]{1,30})`?",
    re.IGNORECASE,
)
_REPO = re.compile(
    r"\b(?:in|on|for|inside)\s+(?:the\s+)?([A-Za-z][\w.-]{1,40})\s+(?:repo|repository|project|codebase)\b",
    re.IGNORECASE,
)
_NOT_TOOLS = {
    "the",
    "a",
    "an",
    "it",
    "this",
    "that",
    "these",
    "those",
    "any",
    "my",
    "your",
    "our",
    "to",
    "in",
    "on",
    "of",
}
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "you",
    "i",
    "me",
    "my",
    "it",
    "is",
    "are",
    "be",
    "do",
    "does",
    "this",
    "that",
    "then",
    "when",
    "next",
    "time",
    "every",
    "each",
    "whenever",
    "always",
    "remember",
    "remind",
    "make",
    "sure",
    "before",
    "after",
    "from",
    "now",
    "future",
    "dont",
    "don",
    "t",
    "forget",
    "please",
    "should",
    "will",
    "at",
    "as",
    "we",
    "our",
    "your",
    "if",
    "so",
    "up",
}


def _stem(word: str) -> str:
    word = word.casefold()
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _words(text: str) -> list[str]:
    return [
        _stem(word) for word in re.findall(r"[A-Za-z0-9_]+", text) if word.casefold() not in _STOP and len(word) > 1
    ]


def extract_trigger(content: str) -> Trigger | None:
    """Rules-only: a trigger for an instruction-for-later, else ``None`` (plain facts stay untriggered)."""
    match = _INSTRUCTION.search(content)
    if match is None:
        return None
    paths: list[str] = []
    for found in _PATH.findall(content):
        if found not in paths and not re.fullmatch(r"\d+(?:\.\d+)+", found):
            paths.append(found)
    quoted = [item.strip() for item in _BACKTICK.findall(content)]
    tools: list[str] = []
    for name in [*_TOOL_VERB.findall(content), *(q for q in quoted if " " not in q and "/" not in q and "." not in q)]:
        if name.casefold() not in _NOT_TOOLS and name not in tools:
            tools.append(name)
    repos = [name for name in _REPO.findall(content) if name.casefold() not in _NOT_TOOLS]
    # Keywords: what the instruction is about, from the clause that follows the "when/next time" phrase.
    clause = content[match.end() :] if match.group(0).casefold() not in {"remind me", "remember to"} else content
    keywords: list[str] = []
    for word in _words(clause):
        if word not in keywords:
            keywords.append(word)
    trigger = Trigger(tools=tools[:4], paths=paths[:4], repos=repos[:2], keywords=keywords[:6])
    return None if trigger.is_empty else trigger


# -- due ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DueContext:
    """What the agent is doing right now: the tool it is about to use, the file, the repo, the message."""

    tool: str = ""
    path: str = ""
    repo: str = ""
    message: str = ""


def _path_matches(trigger_path: str, current: str) -> bool:
    if not current:
        return False
    a, b = trigger_path.strip().rstrip("/"), current.strip().rstrip("/")
    return a == b or b.endswith("/" + a) or a.endswith("/" + b) or ("/" not in a and b.rsplit("/", 1)[-1] == a)


def _keywords_match(keywords: list[str], message: str) -> bool:
    if not keywords or not message:
        return False
    present = set(_words(message))
    hits = sum(1 for word in keywords if word in present)
    # Mostly present, and never on a single shared word when the trigger has several: unrelated
    # messages that merely share a common word must not fire a reminder.
    needed = 1 if len(keywords) == 1 else max(2, -(-len(keywords) * 3 // 5))
    return hits >= needed


def trigger_matches(trigger: Trigger, context: DueContext) -> bool:
    if context.tool and any(context.tool.casefold() == tool.casefold() for tool in trigger.tools):
        return True
    if any(_path_matches(path, context.path) for path in trigger.paths):
        return True
    if context.repo and any(context.repo.casefold() == repo.casefold() for repo in trigger.repos):
        return True
    return _keywords_match(trigger.keywords, context.message)


def match_due(memories: list[Memory], context: DueContext, *, limit: int = MAX_REMINDERS) -> list[Memory]:
    """Memories whose trigger fits ``context``, oldest first, at most ``limit`` (never more than 2)."""
    cap = max(0, min(limit, MAX_REMINDERS))
    if cap == 0:
        return []
    due: list[Memory] = []
    for memory in memories:
        if memory.trigger is not None and trigger_matches(memory.trigger, context):
            due.append(memory)
            if len(due) == cap:
                break
    return due
