#!/usr/bin/env python3
"""Build the deployable Hermes plugin directory for Severian.

    python3 scripts/build_hermes_plugin.py OUTPUT_DIR [--commit REV]

The wheel is built from a clean ``git archive`` of REV (default ``HEAD``),
never from the working tree, with ``SOURCE_DATE_EPOCH`` set to the commit
time so the same commit always yields the same wheel bytes. Writes::

    OUTPUT_DIR/__init__.py, plugin.yaml, pyproject.toml   (as committed at REV)
    OUTPUT_DIR/wheels/severian-<version>-py3-none-any.whl
    OUTPUT_DIR/BUILD-INFO.json                            (commit, version, sha256)

OUTPUT_DIR must not exist: nothing is overwritten or deleted. Deploying is a
separate, approved step: replace ``<hermes root>/plugins/severian`` with
OUTPUT_DIR, then let Hermes PM re-sync its managed environment.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

PLUGIN = "plugins/hermes/severian"
SHIPPED = ("__init__.py", "plugin.yaml", "pyproject.toml")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def _pinned_wheel(wrapper: dict, version: str) -> str:
    """The wheel file name the wrapper pins; refuses any drift from VERSION."""
    wheel = f"severian-{version}-py3-none-any.whl"
    if wrapper["project"].get("dependencies") != [f"severian=={version}"]:
        raise SystemExit(f"wrapper does not pin severian=={version}")
    if wrapper.get("tool", {}).get("uv", {}).get("sources", {}).get("severian") != {"path": f"wheels/{wheel}"}:
        raise SystemExit(f"wrapper source is not wheels/{wheel}")
    return wheel


def build(repo: Path, output: Path, rev: str) -> dict[str, str]:
    if output.exists():
        raise SystemExit(f"refusing to write into existing {output}")
    commit = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    version = tomllib.loads(_git(repo, "show", f"{commit}:pyproject.toml").decode())["project"]["version"]
    wrapper = tomllib.loads(_git(repo, "show", f"{commit}:{PLUGIN}/pyproject.toml").decode())
    wheel = _pinned_wheel(wrapper, version)

    work = Path(tempfile.mkdtemp(prefix="severian-plugin-build-"))
    source = work / "src"
    with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", commit))) as archive:
        archive.extractall(source, filter="data")
    env = {**os.environ, "SOURCE_DATE_EPOCH": _git(repo, "log", "-1", "--format=%ct", commit).decode().strip()}
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(work / "dist"), str(source)], check=True, env=env)
    built = sorted(path.name for path in (work / "dist").glob("*.whl"))
    if built != [wheel]:
        raise SystemExit(f"expected exactly {wheel}, built {built}")

    (output / "wheels").mkdir(parents=True)
    for name in SHIPPED:
        (output / name).write_bytes(_git(repo, "show", f"{commit}:{PLUGIN}/{name}"))
    shutil.copy2(work / "dist" / wheel, output / "wheels" / wheel)
    info = {
        "commit": commit,
        "version": version,
        "wheel": f"wheels/{wheel}",
        "sha256": hashlib.sha256((output / "wheels" / wheel).read_bytes()).hexdigest(),
    }
    (output / "BUILD-INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path, help="new directory to write the plugin into")
    parser.add_argument("--commit", default="HEAD", help="commit to build (default: HEAD)")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    info = build(repo, args.output.resolve(), args.commit)
    json.dump(info, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
