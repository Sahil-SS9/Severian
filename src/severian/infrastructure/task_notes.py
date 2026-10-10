"""Task-note storage shared by the SQLite and PostgreSQL repositories.

Each version of a note is one ``task_note`` record. The current version holds status ``current`` and
an identity key on the task, so the scoped unique index allows only one current note per task; a
replaced or cleared version drops its key and keeps its row as history. Every change is one
transaction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, select, update
from sqlalchemy.exc import IntegrityError

from severian.domain.errors import ConflictError
from severian.domain.models import Scope, TaskNote, now

KIND = "task_note"
CURRENT, REPLACED, CLEARED = "current", "replaced", "cleared"


class TaskNoteStore:
    """Mixin: needs ``engine``, ``_record_values``, ``_identity``, ``_scope_predicates``, ``_require_scope``."""

    _records: Table
    engine: Any

    def _note_key(self, task: str) -> str:
        return str(self._identity(KIND, task))  # type: ignore[attr-defined]

    def _current_note_row(self, connection: Any, scope: Scope, task: str) -> Any:
        table = self._records
        return connection.execute(
            select(table.c.id, table.c.payload).where(
                table.c.record_type == KIND,
                *self._scope_predicates(scope),  # type: ignore[attr-defined]
                table.c.identity_key == self._note_key(task),
            )
        ).one_or_none()

    def _retire(self, connection: Any, row: Any, status: str) -> None:
        payload = dict(row.payload)
        payload["replaced_at"] = now().isoformat()
        connection.execute(
            update(self._records).where(self._records.c.id == row.id).values(
                status=status, identity_key=None, payload=payload
            )
        )

    def set_task_note(self, scope: Scope, task: str, state: str, metadata: dict[str, Any]) -> TaskNote:
        self._require_scope(scope)  # type: ignore[attr-defined]
        note = TaskNote(scope=scope, task=task, state=state, metadata=dict(metadata))
        try:
            with self.engine.begin() as connection:
                current = self._current_note_row(connection, scope, task)
                if current is not None:
                    self._retire(connection, current, REPLACED)
                connection.execute(
                    self._records.insert().values(
                        **self._record_values(KIND, note, identity_key=self._note_key(task), status=CURRENT)  # type: ignore[attr-defined]
                    )
                )
        except IntegrityError as exc:  # another writer set the same task at the same moment
            raise ConflictError("task note changed concurrently; try again") from exc
        return note

    def get_task_note(self, scope: Scope, task: str) -> tuple[TaskNote | None, list[TaskNote]]:
        """The current note (or None) and its earlier versions, oldest first."""
        self._require_scope(scope)  # type: ignore[attr-defined]
        table = self._records
        with self.engine.connect() as connection:
            current = self._current_note_row(connection, scope, task)
            rows = connection.execute(
                select(table.c.payload).where(
                    table.c.record_type == KIND,
                    *self._scope_predicates(scope),  # type: ignore[attr-defined]
                    table.c.status == REPLACED,
                )
            ).scalars().all()
        history = sorted(
            (n for n in (TaskNote.model_validate(r) for r in rows) if n.task == task), key=lambda n: n.created_at
        )
        return (TaskNote.model_validate(current.payload) if current is not None else None), history

    def list_task_notes(self, scope: Scope) -> list[TaskNote]:
        """Current notes in scope, most recently set first."""
        self._require_scope(scope)  # type: ignore[attr-defined]
        table = self._records
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(table.c.payload).where(
                    table.c.record_type == KIND,
                    *self._scope_predicates(scope),  # type: ignore[attr-defined]
                    table.c.status == CURRENT,
                )
            ).scalars().all()
        return sorted((TaskNote.model_validate(r) for r in rows), key=lambda n: n.created_at, reverse=True)

    def clear_task_note(self, scope: Scope, task: str) -> bool:
        self._require_scope(scope)  # type: ignore[attr-defined]
        with self.engine.begin() as connection:
            current = self._current_note_row(connection, scope, task)
            if current is None:
                return False
            self._retire(connection, current, CLEARED)
        return True
