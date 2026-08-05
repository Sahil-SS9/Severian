"""SEC-03: sensitive projection files must be owner-only (0600).

The canonical store hardens its DB + WAL/SHM to 0600; the FTS and vector
projection files must do the same — memory content is sensitive and the
default umask (0644) would make it world-readable.
"""

from __future__ import annotations

import os
import stat

from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


def _assert_private(path: str) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & 0o077 == 0, (
        f"{path} has group/world permissions {oct(mode)} — expected 0600"
    )


def test_fts_db_is_owner_only(tmp_path) -> None:
    db = tmp_path / "fts.db"
    index = FTS5Index(db)
    index.close()
    _assert_private(str(db))


def test_vector_db_is_owner_only(tmp_path) -> None:
    db = tmp_path / "vectors.db"
    index = LocalVectorIndex(db, HashEmbedding())
    index.close()
    _assert_private(str(db))
