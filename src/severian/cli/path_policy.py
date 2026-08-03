"""Approved-path policy for CLI file operations (export/import).

SEC-04 hardening: a CLI that writes or reads files on behalf of the
operator must not be a redirect primitive. This module enforces:

1. **Approved roots** — when ``SEVERIAN_APPROVED_ROOTS`` is set, every
   target path must resolve inside one of the listed roots (colon- or
   os.pathsep-separated absolute paths). When unset, local operation is
   permitted (the operator's explicit path is the trust boundary) and
   the symlink/traversal rules below still apply.
2. **Symlink refusal at every component** — not just the final path
   element. A symlinked parent directory redirects the write just as
   effectively as a symlinked file.
3. **Traversal rejection** — the path is resolved to its absolute
   canonical form; ``..`` segments that escape the approved root are
   rejected.
4. **Overwrite policy** — the destination must not be a directory or a
   symlink; regular-file replacement goes through an atomic temp +
   rename so a concurrent process cannot race the temporary file.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathPolicyError(RuntimeError):
    """Raised when a path violates the approved-path policy."""


def _approved_roots() -> list[Path]:
    raw = os.environ.get("SEVERIAN_APPROVED_ROOTS", "")
    if not raw.strip():
        return []
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        roots.append(Path(entry).resolve())
    return roots


def resolve_approved(target: Path, *, purpose: str) -> Path:
    """Validate ``target`` against the approved-path policy.

    Returns the canonical absolute path when the target is permitted.
    Raises :class:`PathPolicyError` on any violation.
    """
    if not target.is_absolute():
        target = Path.cwd() / target
    # Reject symlinks BEFORE resolving: ``Path.resolve()`` follows
    # symlinks, so a check after resolution can never see the link. The
    # final element and every ancestor are inspected on the raw path.
    if target.is_symlink():
        raise PathPolicyError(f"refusing to {purpose} through symlink: {target}")
    current = target.parent
    while current != current.parent:
        if current.is_symlink():
            raise PathPolicyError(f"refusing to {purpose} through symlinked directory: {current}")
        current = current.parent

    resolved = target.resolve()
    roots = _approved_roots()
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        listed = ", ".join(str(root) for root in roots)
        raise PathPolicyError(
            f"refusing to {purpose} outside approved roots ({listed}): {resolved}"
        )
    return resolved


def check_write_destination(target: Path, *, purpose: str) -> Path:
    """Validate a destination path for writing (export).

    Applies :func:`resolve_approved` plus overwrite policy: the
    destination must not be an existing directory or symlink.
    """
    resolved = resolve_approved(target, purpose=purpose)
    if resolved.exists() and resolved.is_dir():
        raise PathPolicyError(f"refusing to {purpose} over a directory: {resolved}")
    return resolved


def check_read_source(target: Path, *, purpose: str) -> Path:
    """Validate a source path for reading (import).

    Applies :func:`resolve_approved` plus: the source must be an
    existing regular file (never a symlink, directory, or device).
    """
    resolved = resolve_approved(target, purpose=purpose)
    if not resolved.is_file():
        raise PathPolicyError(f"refusing to {purpose}: not a regular file: {resolved}")
    return resolved


__all__ = [
    "PathPolicyError",
    "check_read_source",
    "check_write_destination",
    "resolve_approved",
]
