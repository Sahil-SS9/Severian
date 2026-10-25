"""Render the OmniMemEval 2026-10 benchmark charts (plain SVG) and leaderboard tables (Markdown).

    python scripts/bench_charts.py            # reads docs/benchmarks/omnimemeval/results-2026-10.json
                                              # writes docs/benchmarks/img/*.svg and refreshes every
                                              # <!-- board:KEY --> ... <!-- /board:KEY --> block in the docs

Every number comes from the JSON file; the charts and tables carry no values of their own.
"""
from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "benchmarks" / "omnimemeval" / "results-2026-10.json"
OUT = ROOT / "docs" / "benchmarks" / "img"

SEV = "#4f46e5"  # Severian
OTHER = "#9ca3af"  # everyone else
LCMX = "#f59e0b"
LIGHT = "#c7d2fe"
FONT = "font-family='-apple-system,Segoe UI,Helvetica,Arial,sans-serif'"
BENCH = {"locomo": "LoCoMo", "lme": "LongMemEval", "beam": "BEAM 100K"}
SEVERIAN = "Severian 0.3.11"
DOCS = (ROOT / "README.md", ROOT / "docs" / "benchmarks" / "omnimemeval-2026-10.md")
_BLOCK = re.compile(r"(<!-- (board|breakdown):(\w+) -->).*?(<!-- /\2:\3 -->)", re.S)


def _svg(width: int, height: int, body: list[str], title: str) -> str:
    head = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}' "
        f"{FONT} font-size='12'>"
        f"<title>{escape(title)}</title><rect width='100%' height='100%' fill='white'/>"
        f"<text x='{width / 2}' y='22' text-anchor='middle' font-size='15' font-weight='600'>{escape(title)}</text>"
    )
    return head + "".join(body) + "</svg>\n"


def _text(x: float, y: float, s: str, **kw: str) -> str:
    attrs = " ".join(f"{k.replace('_', '-')}='{v}'" for k, v in kw.items())
    return f"<text x='{x:.1f}' y='{y:.1f}' {attrs}>{escape(s)}</text>"


def grouped(title: str, groups: list[tuple[str, list[tuple[str, float, str]], str]], *, ymax: float = 100.0,
            legend: list[tuple[str, str]] | None = None, width: int = 760, height: int = 360) -> str:
    """Vertical grouped bars. groups = [(label, [(series, value, colour)], footnote)]."""
    left, right, top, bottom = 50, 20, 60, 70
    plot_w, plot_h = width - left - right, height - top - bottom
    body: list[str] = []
    for tick in range(0, int(ymax) + 1, 20):
        y = top + plot_h * (1 - tick / ymax)
        body.append(f"<line x1='{left}' x2='{width - right}' y1='{y:.1f}' y2='{y:.1f}' stroke='#e5e7eb'/>")
        body.append(_text(left - 6, y + 4, str(tick), text_anchor="end", fill="#6b7280"))
    gw = plot_w / len(groups)
    for gi, (label, bars, foot) in enumerate(groups):
        bw = min(46.0, (gw - 30) / len(bars))
        x0 = left + gi * gw + (gw - bw * len(bars)) / 2
        for bi, (_, value, colour) in enumerate(bars):
            h = plot_h * value / ymax
            x, y = x0 + bi * bw, top + plot_h - h
            body.append(f"<rect x='{x + 2:.1f}' y='{y:.1f}' width='{bw - 4:.1f}' height='{h:.1f}' "
                        f"fill='{colour}' rx='2'/>")
            body.append(_text(x + bw / 2, y - 5, f"{value:.1f}", text_anchor="middle", font_size="11"))
        cx = left + gi * gw + gw / 2
        body.append(_text(cx, top + plot_h + 20, label, text_anchor="middle", font_weight="600"))
        if foot:
            body.append(_text(cx, top + plot_h + 38, foot, text_anchor="middle", fill="#374151", font_size="11"))
    if legend:
        lx = left
        for name, colour in legend:
            body.append(f"<rect x='{lx}' y='34' width='12' height='12' fill='{colour}' rx='2'/>")
            body.append(_text(lx + 16, 44, name))
            lx += 16 + 7 * len(name) + 24
    return _svg(width, height, body, title)


def leaderboard(
    title: str, rows: list[tuple[str, float, bool]], *, width: int = 360, bar_h: int = 18
) -> tuple[list[str], int]:
    """One horizontal-bar panel (sorted high to low); returns (svg parts, panel height). Highlighted rows are ours."""
    body = [_text(width / 2, 0, title, text_anchor="middle", font_weight="600")]
    label_w, value_w = 150, 46
    plot_w = width - label_w - value_w
    for i, (name, value, ours) in enumerate(sorted(rows, key=lambda r: -r[1])):
        y = 12 + i * (bar_h + 4)
        colour = SEV if ours else (LCMX if name == "LCM-X" else OTHER)
        weight = "600" if ours else "400"
        body.append(_text(label_w - 6, y + bar_h - 5, name, text_anchor="end", font_weight=weight))
        body.append(f"<rect x='{label_w}' y='{y}' width='{plot_w * value / 100:.1f}' height='{bar_h}' "
                    f"fill='{colour}' rx='2'/>")
        body.append(_text(label_w + plot_w * value / 100 + 4, y + bar_h - 5, f"{value:.2f}", font_weight=weight))
    return body, 12 + len(rows) * (bar_h + 4)


def board_chart(d: dict) -> str:
    rows = d["board_published"]["rows"]
    h2h = d["head_to_head"]
    panels, heights = [], []
    for i, key in enumerate(("locomo", "lme", "beam")):
        data = [(name, vals[i], False) for name, vals in rows.items() if vals[i] is not None]
        data.append(("Severian (self-run)", h2h[key]["severian"], True))
        parts, h = leaderboard(BENCH[key], data)
        panels.append(parts)
        heights.append(h)
    width, top = 3 * 370 + 20, 60
    body: list[str] = []
    for i, parts in enumerate(panels):
        body.append(f"<g transform='translate({10 + i * 370},{top})'>" + "".join(parts) + "</g>")
    note = ("Grey/amber: published OmniMemEval rows. Indigo: Severian 0.3.11, run by us through the same pipeline and "
            "models (not an official board entry).")
    height = top + max(heights) + 40
    body.append(_text(width / 2, height - 14, note, text_anchor="middle", fill="#6b7280", font_size="11"))
    return _svg(width, height, body, "OmniMemEval leaderboard context (LoCoMo / LongMemEval / BEAM 100K)")


def _ci(diff: float, lo: float, hi: float) -> str:
    return f"{diff:+.1f} (95% range {lo:+.1f} to {hi:+.1f})"


def h2h_chart(d: dict) -> str:
    groups = []
    for key in ("locomo", "lme", "beam"):
        r = d["head_to_head"][key]
        groups.append((BENCH[key], [("Severian", r["severian"], SEV), ("LCM-X", r["lcmx"], LCMX)],
                       _ci(r["diff"], *r["ci"])))
    return grouped("Severian vs LCM-X, same pipeline and models (n = 1,540 / 500 / 400)", groups,
                   legend=[("Severian 0.3.11", SEV), ("LCM-X v0.28.1", LCMX)])


def strict_chart(d: dict) -> str:
    s = d["strict_marker"]
    groups = []
    for key, label in (("locomo", "LoCoMo (200)"), ("lme_full", "LongMemEval (500)"), ("beam", "BEAM (200)")):
        b, t = s[key]["board"], s[key]["strict"]
        groups.append((label, [("S board", b[0], LIGHT), ("L board", b[1], "#fde68a"), ("S strict", t[0], SEV),
                               ("L strict", t[1], LCMX)], "strict: " + _ci(t[2], t[3], t[4])))
    return grouped("Board marker vs strict second marker (same answers)", groups,
                   legend=[("Severian, board", LIGHT), ("LCM-X, board", "#fde68a"), ("Severian, strict", SEV),
                           ("LCM-X, strict", LCMX)], width=820)


def settings_chart(d: dict) -> str:
    st, imp = d["settings"], d["improvements"]
    groups = []
    for key in ("locomo", "lme", "beam"):
        bars = [("board", st["board"][key][0], SEV), ("shipped", st["shipped"][key][0], OTHER)]
        fill = imp["fill_compact_shipped"].get(key)
        if fill:
            bars.append(("shipped+fill", fill[1], "#10b981"))
        cap = (imp.get("cap16k_shipped") or {}).get(key)
        if cap:
            bars.append(("16k+fill", cap[1], "#0ea5e9"))
        foot = f"context tokens: board {st['board'][key][1]:,} / default {st['shipped'][key][1]:,}"
        if imp.get("cap16k_shipped") and not cap:
            foot += " · 16,000 not run"
        groups.append((BENCH[key], bars, foot))
    legend = [("board depth (top-20, no cap)", SEV), ("Hermes default (top-10, 8,000 chars)", OTHER),
              ("default + fill-budget", "#10b981")]
    if imp.get("cap16k_shipped"):
        legend.append(("16,000 chars + fill", "#0ea5e9"))
    return grouped("Severian settings: what the context limit costs", groups, legend=legend, width=980)


def _score(cell: str) -> float | None:
    m = re.match(r"\d[\d,]*(\.\d+)?", cell)
    return float(m.group(0).replace(",", "")) if m else None


def board_table(d: dict, key: str) -> str:
    """A board table (verbatim OmniMemEval rows) plus Severian's row; the best score in each column is bold, and
    in the summary also marked 🥇. Context-token columns are not ranked (lower is cheaper, not better)."""
    table = d["board_tables"][key]
    cols, rows = table["columns"], [list(r) for r in table["rows"]]
    ours = d["severian_rows"].get(key)
    if ours:
        rows.append([f"**{SEVERIAN}** †", *ours])
    ranked = [i for i, c in enumerate(cols) if c not in ("Backend", "Deployment") and "Tokens" not in c]
    for i in ranked:
        best = max((v for r in rows if (v := _score(r[i])) is not None), default=None)
        for r in rows:
            if best is not None and _score(r[i]) == best:
                r[i] = f"**🥇 {r[i]}**" if key == "summary" else f"**{r[i]}**"
    align = ["---" if c in ("Backend", "Deployment") else "---:" for c in cols]
    return "\n".join("| " + " | ".join(r) + " |" for r in [cols, align, *rows])


def breakdown_table(d: dict, key: str) -> str:
    """A per-category breakdown table (computed by docs/benchmarks/omnimemeval/breakdowns.py), with its note."""
    t = d["breakdowns"][key]
    align = ["---" if c in ("Category", "Difficulty") else "---:" for c in t["columns"]]
    lines = ["| " + " | ".join(r) + " |" for r in [t["columns"], align, *t["rows"]]]
    return "\n".join([*lines, "", f"*{t['note']}*"])


def render_block(d: dict, kind: str, key: str) -> str:
    return board_table(d, key) if kind == "board" else breakdown_table(d, key)


def category_chart(d: dict, key: str) -> str:
    """Severian vs the best board system in each category (the name of that system under each pair)."""
    table = d["board_tables"][key]
    cols = table["columns"]
    ours = dict(zip(cols[1:], d["severian_rows"][key], strict=True))
    groups = []
    for c in cols[2:]:
        if c in ("Overall", "Context Tokens"):
            continue
        best = max(table["rows"], key=lambda r: _score(r[cols.index(c)]) or -1)
        groups.append((c, [("Severian", _score(ours[c]) or 0.0, SEV),
                           ("best other", _score(best[cols.index(c)]) or 0.0, OTHER)], best[0]))
    return grouped(f"{BENCH[key]} by category: Severian vs the best other system", groups,
                   legend=[("Severian 0.3.11 (our run)", SEV), ("best other board system (named below)", OTHER)],
                   width=150 * len(groups) + 70)


def refresh_docs(d: dict) -> list[Path]:
    """Rewrite every board block in the docs from the data; returns the files that changed."""
    changed = []
    for path in DOCS:
        text = path.read_text()
        new = _BLOCK.sub(lambda m: f"{m.group(1)}\n{render_block(d, m.group(2), m.group(3))}\n{m.group(4)}", text)
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


def main() -> None:
    d = json.loads(DATA.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    charts = {"h2h.svg": h2h_chart(d), "board.svg": board_chart(d), "strict.svg": strict_chart(d),
              "settings.svg": settings_chart(d), "categories-locomo.svg": category_chart(d, "locomo"),
              "categories-lme.svg": category_chart(d, "lme")}
    for name, svg in charts.items():
        (OUT / name).write_text(svg)
        print(f"wrote {OUT / name} ({len(svg):,} bytes)")
    for path in refresh_docs(d):
        print(f"refreshed tables in {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
