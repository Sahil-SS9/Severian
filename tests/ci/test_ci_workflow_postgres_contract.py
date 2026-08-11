"""Keep the GitHub PostgreSQL service credentials and DSNs coherent."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_postgres_test_dsns_match_declared_service_password() -> None:
    """Each CI job must use the disposable service password, not a redaction literal."""
    text = WORKFLOW.read_text()
    service_passwords = re.findall(r"^\s*POSTGRES_PASSWORD:\s*(\S+)\s*$", text, re.MULTILINE)
    dsns = re.findall(r"^\s*SEVERIAN_PG_DSN:\s*postgresql://([^:\s]+):([^@\s]+)@([^\s]+)\s*$", text, re.MULTILINE)

    assert service_passwords == ["severian", "severian"]
    assert len(dsns) == 2
    for username, password, address in dsns:
        assert username == "severian"
        assert password != "***", "CI must not use a redaction placeholder as a password"
        assert password == "severian"
        assert address == "127.0.0.1:54329/severian"
