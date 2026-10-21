"""The README's runnable example keeps working."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "quickstart.py"


def test_quickstart_runs_and_keeps_users_apart() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SEVERIAN_")}
    cmd = [sys.executable, str(EXAMPLE)]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, check=True)
    assert "alice: I live in Manchester and I work at Acme." in out.stdout
    assert "bob can see alice's memories: False" in out.stdout
    assert "\nsource: " in out.stdout
