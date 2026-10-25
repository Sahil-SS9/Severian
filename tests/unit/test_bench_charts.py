"""The benchmark charts are well-formed SVG and show exactly the numbers in the data file."""
from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bench_charts", ROOT / "scripts" / "bench_charts.py")
assert spec and spec.loader
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)
DATA = json.loads((ROOT / "docs" / "benchmarks" / "data" / "omnimemeval-2026-10.json").read_text())


def _texts(svg: str) -> list[str]:
    root = ET.fromstring(svg)  # raises on malformed XML
    return [el.text or "" for el in root.iter("{http://www.w3.org/2000/svg}text")]


def test_head_to_head_chart_labels_every_score_and_interval():
    texts = _texts(bc.h2h_chart(DATA))
    for key in ("locomo", "lme", "beam"):
        r = DATA["head_to_head"][key]
        assert f"{r['severian']:.1f}" in texts and f"{r['lcmx']:.1f}" in texts
        assert any(f"{r['diff']:+.1f}" in t and f"{r['ci'][0]:+.1f}" in t for t in texts)


def test_board_chart_marks_severian_as_self_run_and_keeps_every_published_row():
    texts = _texts(bc.board_chart(DATA))
    assert texts.count("Severian (self-run)") == 3
    for name, vals in DATA["board_published"]["rows"].items():
        shown = sum(v is not None for v in vals)
        assert texts.count(name) == shown, name


def test_other_charts_parse():
    for svg in (bc.strict_chart(DATA), bc.settings_chart(DATA)):
        assert _texts(svg)


def test_committed_svgs_match_the_generator():
    # The images in the repo must be regenerated whenever the data changes.
    for name, fn in (("h2h.svg", bc.h2h_chart), ("board.svg", bc.board_chart), ("strict.svg", bc.strict_chart),
                     ("settings.svg", bc.settings_chart)):
        assert (ROOT / "docs" / "benchmarks" / "img" / name).read_text() == fn(DATA), name
