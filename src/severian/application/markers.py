"""Maintenance markers: records a pass wrote about itself, not memories of the user.

The dreaming pass stores three kinds of memory whose content is a statement about the pass, never a
statement about the person using the store:

- ``retention_archive`` (``RetentionPolicyService.apply_archive``) — "N memories archived by policy";
- ``curation_review`` (``CurationService.curate``) — "N memories scored, M low-value candidates";
- ``consolidated`` (``ConsolidationService.consolidate``) — the merge summary, which names the
  memory ids it unifies and nothing else. ``ConsolidationService`` is its only writer.

They exist so the audit trail can name them (the curation report rides on its marker's row), and
they have always been ordinary active memories: indexed, ranked and rendered like a fact, at
confidence 1.0. A question about the conversation could therefore be answered with "Curation
review: 412 memories scored, 3 low-value candidates" — a maintenance record presented as the
user's own memory.

This module is the one place that says which kinds those are. ``search`` and ``assemble_context``
use it to keep them out. The rule is not behind a switch: these memories should never have been
searchable, and hiding them is a fix, not a feature.
"""

from __future__ import annotations

from collections.abc import Iterable

from severian.domain.models import Memory

#: Kinds whose content is a record of a pass, never a fact about the user.
MARKER_KINDS = frozenset({"retention_archive", "curation_review", "consolidated"})


def is_marker_kind(kind: str) -> bool:
    """``True`` for a maintenance marker kind (see :data:`MARKER_KINDS`)."""
    return kind in MARKER_KINDS


def is_marker(memory: Memory) -> bool:
    """``True`` when ``memory`` is a maintenance marker rather than a memory of the user."""
    return is_marker_kind(memory.kind)


def without_markers(memories: Iterable[Memory]) -> list[Memory]:
    """``memories`` minus the maintenance markers, in the order given."""
    return [memory for memory in memories if not is_marker(memory)]


__all__ = ["MARKER_KINDS", "is_marker", "is_marker_kind", "without_markers"]
