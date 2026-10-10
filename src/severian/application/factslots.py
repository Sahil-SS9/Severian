"""Reading a fact: who it is about, which slot it fills, and what it says has ended.

Handoff 1 (truth handling). A rule-first reader for the facts people actually state. It is
deliberately narrow: a missed update leaves one stale line, but a false one hides a true fact, so
anything doubtful (questions, wishes, plans, "might", past tense, someone else's life) reads as
nothing at all. Names and places must be capitalised for the same reason.

Slots come in two kinds:

* single-valued (``SINGLE_SLOTS``): home, employer, partner, phone, diet. A new value replaces the
  old one unless the sentence says "also" / "as well".
* additive: likes, pets, languages, cars, memberships, opening days and so on. Both stay true;
  only an explicit ending ("sold the MG4", "cancelled Third Space", "left Starling") closes one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

SINGLE_SLOTS = frozenset({"home", "employer", "partner", "phone", "diet"})

_ROLE_LINE = re.compile(r"^(?:\[[^\]]*\]\s*)?(user|assistant)\s*:\s*(.*)$", re.IGNORECASE)
_LABEL = re.compile(r"^\s*\[[^\]]*\]\s*")


@dataclass(frozen=True)
class Claim:
    """One thing a message says is true: ``subject`` has ``value`` in ``slot``."""

    subject: str
    slot: str
    value: str
    additive: bool


@dataclass(frozen=True)
class Ending:
    """A value the message says has ended, and the kinds of fact it may close."""

    value: str
    slots: frozenset[str]


@dataclass(frozen=True)
class Reading:
    """What a message claims and ends, plus how much of it the reader could NOT account for.

    ``unread`` counts the user's clauses (or leftovers of clauses) that say something the rules do
    not understand; ``assistant_words`` is how much the assistant said in the same saved turn. A fact
    with either is never closed as a whole: it holds more than we can see.
    """

    claims: tuple[Claim, ...] = ()
    endings: tuple[Ending, ...] = field(default_factory=tuple)
    unread: int = 0
    assistant_words: int = 0

    @property
    def ended_values(self) -> set[str]:
        return {ending.value for ending in self.endings}

    def ends(self, value: str, *, slot: str) -> bool:
        """True if this message says ``value`` ended and its verb allows closing a ``slot`` fact."""
        wanted = _norm(value)
        return any(ending.value == wanted and slot in ending.slots for ending in self.endings)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _split_turn(raw: str) -> tuple[list[str], int]:
    """The user's lines of a saved turn, and how many words the assistant said."""
    lines: list[str] = []
    assistant_words = 0
    role = "user"  # a line with no role marker is a plain fact
    for line in raw.splitlines():
        match = _ROLE_LINE.match(line.strip())
        if match:
            role = match.group(1).lower()
            text = match.group(2).strip()
        else:
            text = _LABEL.sub("", line).strip() if not lines and role == "user" else line.strip()
        if role == "user" and text:
            lines.append(text)
        elif role == "assistant":
            assistant_words += len(text.split())
    return lines, assistant_words


def user_words(raw: str) -> str:
    """The user's side of a saved turn; a plain fact (with or without a ``[date]`` label) as is."""
    return "\n".join(_split_turn(raw)[0])


# Words that carry no fact of their own: grammar, filler and small talk. Shared with the
# related-facts check in ``truthresolution``.
STOPWORDS = frozenset(
    "the a an is are was were be been am i me my we our you your he she his her they their it its this that "
    "these those to of in on at and or for with by from as has have had do does did not no now just also very "
    "really more most user users lives live living but if when while about would could should can will so well "
    "too then than there here what which who how why where finally recently actually quite new later today "
    "yesterday tomorrow tonight week month year ago ever still thanks thank great ok okay cool nice yeah yes "
    "please sorry hello hi hey oh got get gets getting going gone".split()
)


def _content_tokens(text: str) -> int:
    return sum(1 for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1 and token not in STOPWORDS)


# -- reading --------------------------------------------------------------------------------

_NAME_FIRST = r"[A-Z][A-Za-z0-9\-]*"
_NAME_NEXT = r"(?!I\b)[A-Z][A-Za-z0-9\-]*"
NAME = rf"{_NAME_FIRST}(?:\s+{_NAME_NEXT}){{0,3}}"

_DIETS = (
    "vegetarian|vegan|pescatarian|pescetarian|keto|gluten-free|dairy-free|paleo|flexitarian|halal|kosher"
)
_RELATIVES = (
    "sister|brother|mum|mom|mother|dad|father|aunt|uncle|cousin|daughter|son|grandma|grandmother|"
    "grandfather|grandpa|nan|niece|nephew|friend|colleague|manager|boss"
)
_PETS = "cat|dog|rabbit|hamster|guinea pig|parrot|horse|tortoise|goldfish|fish|bird|ferret|snake|lizard"
_DAYS = (
    "mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|weekdays|weekends?"
)

_NOT_A_CLAIM = re.compile(
    r"\b(?:if|unless|whether|when|whenever|would|could|might|may|maybe|perhaps|probably|possibly|"
    r"thinking (?:of|about)|going to|gonna|want(?:ed)? to|wanna|like to|love to|hope|hoping|"
    r"plan(?:ning|s)? to|considering|wish|should|someday|one day|next (?:year|month|week)|"
    r"about to|will|shall|i'll|i'd|we'll|we'd|not sure)\b",
    re.IGNORECASE,
)
_ADDITIVE = re.compile(r"\b(?:also|as well|too|in addition|additionally)\b", re.IGNORECASE)
_FILLER = re.compile(
    r"^(?:(?:yeah|yes|oh|well|so|actually|btw|anyway|also|and|but|okay|ok|just|hey|hi|right|sure|"
    r"thanks|thank you|honestly|basically|finally)[,\s]+)+",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"[.!?;\n]+|\s[—–-]+\s|[—–]")
_AND_CLAUSE = re.compile(
    r"\s+and\s+(?=(?:also\s+|now\s+|currently\s+)?(?:work|works|live|lives|speak|enjoy|joined|drive|am|have)\b)"
)
_LIKE_STOP = {
    "and", "but", "or", "because", "so", "when", "while", "as", "for", "at", "in", "on", "with", "since", "if",
    "than", "to", "that",
}
_LIKE_BAD_FIRST = {
    "to", "it", "that", "when", "how", "the", "a", "an", "my", "your", "this", "these", "those", "what", "if",
    "being", "you", "going", "having", "her", "his", "their", "our", "us", "them", "him", "me",
}


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_I = r"(?:I|We)"
_HAVE = r"(?:'ve| have)?"
_ADV = r"(?:(?:just|recently|finally|now|currently|also|still|really)\s+)*"

_HOME_MOVED_FROM = _rx(rf"^{_I}{_HAVE}\s+{_ADV}(?:moved|relocated)\s+from\s+({NAME})\s+to\s+({NAME})")
_HOME_MOVED = _rx(rf"^{_I}{_HAVE}\s+{_ADV}(?:moved|relocated)(?:\s+back)?\s+to\s+({NAME})")
_HOME_LIVE = _rx(rf"^{_I}\s+{_ADV}(?:live|lives)\s+in\s+({NAME})")
_HOME_LIVING = _rx(rf"^(?:I'm|I am|We're|We are)\s+{_ADV}(?:living|based)\s+in\s+({NAME})")
_HOME_NO_LONGER = _rx(
    rf"^{_I}\s+(?:no longer\s+live|don't\s+live|do not\s+live)\s+in\s+({NAME})"
)
_HOME_USED_TO = _rx(rf"^{_I}\s+used\s+to\s+live\s+in\s+({NAME})")

_JOB_STARTED = _rx(
    rf"^{_I}{_HAVE}\s+{_ADV}started\s+(?:a\s+|my\s+)?(?:new\s+)?(?:(?:job|role|position|post)\s+)?(?:at|with)\s+({NAME})"
)
_JOB_WORK = _rx(rf"^{_I}(?:'m| am)?\s+{_ADV}(?:work|works|working)\s+(?:at|for)\s+({NAME})")
_JOB_NO_LONGER = _rx(rf"^{_I}\s+(?:no longer\s+work|don't\s+work|do not\s+work)\s+(?:at|for)\s+({NAME})")
_JOB_USED_TO = _rx(rf"^{_I}\s+used\s+to\s+work\s+(?:at|for)\s+({NAME})")

_DIET_IS = _rx(
    rf"^(?:I'm|I am|We're|We are|I've gone|I have gone|I went|I've become|I became|I've switched to|"
    rf"I switched to|I have switched to)\s+{_ADV}({_DIETS})\b"
)
_DIET_NO_LONGER = _rx(rf"^(?:I'm|I am)\s+no longer\s+({_DIETS})\b")
_DIET_USED_TO = _rx(rf"^{_I}\s+used\s+to\s+be\s+({_DIETS})\b")

_LEFT_THEN_JOINED = _rx(
    rf"^{_I}{_HAVE}\s+{_ADV}(?:left|quit|resigned\s+from|cancelled|canceled|dropped|ended|stopped)\s+"
    rf"(?:my\s+(?:job|membership|subscription)\s+(?:at|with|of)\s+)?({NAME})\s*(?:,\s*)?and\s+"
    rf"(?:joined|signed\s+up\s+(?:at|with|to|for)|started\s+(?:at|with)|switched\s+to)\s+({NAME})"
)
_JOINED = _rx(rf"^{_I}{_HAVE}\s+{_ADV}joined\s+({NAME})")
_SOLD = _rx(
    rf"^(?:{_I}{_HAVE}\s+)?(?i:sold|got rid of|traded in|scrapped|replaced)\s+(?i:my\s+|the\s+|our\s+)?({NAME})"
)
_CAR_IS = _rx(r"\b(?i:my|our)(?i:\s+(?:new|current|old))?\s+car\s+is\s+(?i:a\s+|an\s+|the\s+)?(" + NAME + r")")
_CAR_DRIVE = _rx(rf"^{_I}\s+{_ADV}drive\s+(?:a\s+|an\s+|the\s+)?({NAME})")

_PARTNER = _rx(
    rf"\b(?i:my\s+(?:partner|wife|husband|girlfriend|boyfriend|fianc[ée]e?|spouse)),?\s+({NAME})"
)
_MARRIED = _rx(rf"^(?:I'm|I am)\s+married\s+to\s+({NAME})")
_PHONE = _rx(r"\b(?i:my\s+(?:phone|mobile|cell)(?:\s+number)?\s*(?:is|:))\s*(\+?\d[\d\s\-]{6,}\d)")

_LANGUAGE = _rx(rf"^{_I}\s+{_ADV}(?:can\s+)?speak\s+({NAME}(?:(?:,|\s+and|,\s+and)\s+{NAME})*)")
_PET = _rx(
    rf"\b(?i:my|our)\s+({_PETS})(?:\s+is)?,?\s+(?:(?:is\s+)?(?:called|named)\s+)?([A-Z][A-Za-z\-]+)"
)
_MEMBER = _rx(rf"^(?:I'm|I am)\s+{_ADV}a\s+member\s+(?:at|of)\s+({NAME})")
_LIKES = _rx(
    rf"^{_I}\s+(?:really\s+|also\s+|absolutely\s+)*(?:enjoy|like|love|am\s+into|'m\s+into)\s+"
    r"([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,3})"
)
_OPENING = _rx(
    rf"^((?i:the)\s+[a-z]+(?:\s+[a-z]+)?)\s+(?:is|are)\s+open\s+(?:on\s+|every\s+)?((?i:{_DAYS}))\b"
)
_RELATIVE_HOME = _rx(
    rf"^[Mm]y\s+({_RELATIVES})\s+({_NAME_FIRST})\s+(?:lives|is\s+living|is\s+based|moved|has\s+moved)\s+(?:in|to)\s+({NAME})"
)


def _clean(value: str) -> str:
    return _norm(value.strip(" ,.;:'\""))


def _clauses(text: str) -> list[str]:
    out: list[str] = []
    for sentence in _SPLIT.split(text):
        sentence = sentence.strip().replace("’", "'")
        if not sentence or sentence.endswith("?"):
            continue
        sentence = re.sub(r"^(?:the\s+)?user\b", "I", sentence, flags=re.IGNORECASE)
        parts = [sentence] if _LEFT_THEN_JOINED.match(_FILLER.sub("", sentence)) else _AND_CLAUSE.split(sentence)
        first = parts[0]
        out.append(first)
        subject = "I" if re.match(r"^(?:I|We)\b", first) else ""
        for part in parts[1:]:
            out.append(f"{subject} {part}".strip() if subject else part)
    return out


@lru_cache(maxsize=16384)
def read_fact(raw: str) -> Reading:
    """What the user's words claim, and what they say has ended."""
    claims: list[Claim] = []
    endings: list[Ending] = []
    unread = 0
    spans: list[tuple[int, int]] = []

    def take(match: re.Match[str] | None) -> re.Match[str] | None:
        if match is not None:
            spans.append(match.span())
        return match

    def claim(subject: str, slot: str, value: str, additive: bool) -> None:
        item = Claim(subject, slot, _clean(value), additive)
        if item.value and item not in claims:
            claims.append(item)

    def end(value: str, *slots: str) -> None:
        item = Ending(_clean(value), frozenset(slots))
        if item.value and item not in endings:
            endings.append(item)

    lines, assistant_words = _split_turn(raw)
    for original in _clauses("\n".join(lines)):
        spans.clear()
        skipped = bool(_NOT_A_CLAIM.search(original)) and not re.search(r"\bused\s+to\b", original, re.IGNORECASE)
        clause = _FILLER.sub("", original).strip()
        if not skipped:
            additive = bool(_ADDITIVE.search(original))
            _read_clause(clause, additive, take, claim, end)
        # What the patterns did not use up: if it still says something, this fact holds more than we read.
        leftover = clause
        for begin, stop in sorted(spans, reverse=True):
            leftover = leftover[:begin] + " " + leftover[stop:]
        if _content_tokens(leftover) >= 2:
            unread += 1

    return Reading(tuple(claims), tuple(endings), unread, assistant_words)


def _read_clause(clause, additive, take, claim, end) -> None:  # type: ignore[no-untyped-def]
    if m := take(_HOME_MOVED_FROM.match(clause)):
        end(m.group(1), "home")
        claim("user", "home", m.group(2), additive)
    elif m := take(_HOME_MOVED.match(clause)):
        claim("user", "home", m.group(1), additive)
    elif m := take(_HOME_LIVE.match(clause)) or take(_HOME_LIVING.match(clause)):
        claim("user", "home", m.group(1), additive)
    elif m := take(_HOME_NO_LONGER.match(clause)) or take(_HOME_USED_TO.match(clause)):
        end(m.group(1), "home")

    if m := take(_JOB_STARTED.match(clause)) or take(_JOB_WORK.match(clause)):
        claim("user", "employer", m.group(1), additive)
    elif m := take(_JOB_NO_LONGER.match(clause)) or take(_JOB_USED_TO.match(clause)):
        end(m.group(1), "employer")

    if m := take(_DIET_NO_LONGER.match(clause)) or take(_DIET_USED_TO.match(clause)):
        end(m.group(1), "diet")
    elif m := take(_DIET_IS.match(clause)):
        claim("user", "diet", m.group(1), additive)

    if m := take(_LEFT_THEN_JOINED.match(clause)):
        end(m.group(1), "employer", "joined", "member")
        claim("user", "joined", m.group(2), True)
    elif m := take(_JOINED.match(clause)):
        claim("user", "joined", m.group(1), True)

    if m := take(_SOLD.match(clause)):
        end(m.group(1), "car")
    if m := take(_CAR_IS.search(clause)) or take(_CAR_DRIVE.match(clause)):
        claim("user", "car", m.group(1), True)

    if m := take(_PARTNER.search(clause)) or take(_MARRIED.match(clause)):
        claim("user", "partner", m.group(1), additive)
    if m := take(_PHONE.search(clause)):
        claim("user", "phone", m.group(1), additive)

    if m := take(_LANGUAGE.match(clause)):
        for language in re.split(r",\s*and\s+|\s+and\s+|,\s*", m.group(1)):
            claim("user", "language", language, True)
    if m := take(_PET.search(clause)):
        claim("user", "pet", m.group(2), True)
    if m := take(_MEMBER.match(clause)):
        claim("user", "member", m.group(1), True)
    if m := take(_LIKES.match(clause)):
        words = m.group(1).split()
        if words and words[0] not in _LIKE_BAD_FIRST:
            kept: list[str] = []
            for word in words:
                if word in _LIKE_STOP:
                    break
                kept.append(word)
            claim("user", "likes", " ".join(kept), True)
    if m := take(_OPENING.match(clause)):
        claim(_clean(m.group(1)), "opening", m.group(2), True)
    if m := take(_RELATIVE_HOME.match(clause)):
        claim(f"{m.group(1).lower()} {m.group(2).lower()}", "home", m.group(3), additive)


# -- asking about the past ------------------------------------------------------------------

_PAST_QUESTION = re.compile(
    r"\b(?:before|previously|formerly|former|previous|originally|used\s+to|use\s+to|prior\s+to|"
    r"at\s+first|back\s+then|in\s+the\s+past)\b",
    re.IGNORECASE,
)


def asks_about_past(query: str) -> bool:
    """True when the question is about how things used to be, so history should be searched too."""
    return bool(_PAST_QUESTION.search(query))