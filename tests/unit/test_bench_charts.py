"""The benchmark charts are well-formed SVG and show exactly the numbers in the data file."""
from __future__ import annotations

import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bench_charts", ROOT / "scripts" / "bench_charts.py")
assert spec and spec.loader
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)
DATA = json.loads((ROOT / "docs" / "benchmarks" / "omnimemeval" / "results-2026-10.json").read_text())


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


def test_doc_tables_match_the_data_and_mark_each_columns_winner():
    for path in bc.DOCS:
        text = path.read_text()
        blocks = re.findall(r"<!-- board:(\w+) -->\n(.*?)\n<!-- /board:\1 -->", text, re.S)
        assert blocks, path
        for key, body in blocks:
            assert body == bc.board_table(DATA, key), (path.name, key)
    summary = bc.board_table(DATA, "summary")
    assert summary.count("🥇") >= 6  # one per benchmark column (ties may add more)
    sev = next(line for line in summary.splitlines() if "Severian" in line)
    assert "🥇 90.60" in sev and "🥇" not in sev.split("|")[2]  # LongMemEval win; LoCoMo is MemOS's


def test_board_rows_are_verbatim():
    # Published records are copied, never edited: every board row appears unchanged once highlighting is removed.
    for key in ("summary", "locomo", "lme", "beam", "pmv2", "halumem"):
        plain = bc.board_table(DATA, key).replace("**", "").replace("🥇 ", "")
        for row in DATA["board_tables"][key]["rows"]:
            assert "| " + " | ".join(row) + " |" in plain, (key, row[0])


def test_breakdown_blocks_match_the_data():
    text = (bc.ROOT / "docs/benchmarks/omnimemeval-2026-10.md").read_text()
    blocks = re.findall(r"<!-- breakdown:(\w+) -->\n(.*?)\n<!-- /breakdown:\1 -->", text, re.S)
    assert {k for k, _ in blocks} == {k for k in DATA["breakdowns"] if not k.startswith("_")}
    for key, body in blocks:
        assert body == bc.breakdown_table(DATA, key), key


def test_category_chart_names_the_best_system_per_category():
    svg = bc.category_chart(DATA, "locomo")
    ET.fromstring(svg)
    assert svg.count(">MemOS<") == 4 and "88.7" in svg
