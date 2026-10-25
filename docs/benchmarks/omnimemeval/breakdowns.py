"""Per-category breakdowns for the Severian results page, computed from the saved per-question run files.

    SEVERIAN_BENCH_RUN=/path/to/run python breakdowns.py OUT.json
                                       # writes {"breakdowns": {...}} ready to merge into results-2026-10.json

Each breakdown is {"title", "note", "columns", "rows"}; rows are display strings (scores to 1 dp, counts as ints).
Nothing here calls a model: it only reads judged files, side-track files and second-marker files already on disk.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# The run folder: OmniMemEval checkouts (omni*/results), side tracks (tracks*/) and second-marker files (marker2/).
P = Path(os.environ.get("SEVERIAN_BENCH_RUN", "."))
LOCOMO_CATS = {"1": "Multi-Hop", "2": "Temporal", "3": "Open-Domain", "4": "Single-Hop",
               # the second-marker files store names (marker2_prep.py), the judged files store numbers
               "multi-hop": "Multi-Hop", "temporal": "Temporal", "open-domain": "Open-Domain",
               "single-hop": "Single-Hop"}
LME_CATS = {"single-session-user": "SS-User", "single-session-assistant": "SS-Asst",
            "single-session-preference": "SS-Pref", "temporal-reasoning": "Temp. Reas", "multi-session": "Multi-S",
            "knowledge-update": "Know. Upd"}
BEAM_CATS = {"abstention": "Abstention", "contradiction_resolution": "Contradiction",
             "event_ordering": "Event ordering",
             "information_extraction": "Info extraction", "instruction_following": "Instruction following",
             "knowledge_update": "Knowledge update", "multi_session_reasoning": "Multi-session",
             "preference_following": "Preference following", "summarization": "Summarisation",
             "temporal_reasoning": "Temporal"}
CAT_NAMES = {"locomo": LOCOMO_CATS, "lme": LME_CATS, "beam": BEAM_CATS}
ORDER = {"locomo": ["Single-Hop", "Multi-Hop", "Temporal", "Open-Domain"],
         "lme": ["SS-User", "SS-Asst", "SS-Pref", "Temp. Reas", "Multi-S", "Know. Upd"],
         "beam": list(BEAM_CATS.values())}


def _lit(v):
    return ast.literal_eval(v) if isinstance(v, str) else v


def judged(root: Path, bench: str, system: str = "severian") -> dict:
    """{key: (category name, score 0..1, difficulty)} from an OmniMemEval judged file."""
    d = json.loads((root / f"results/{bench}/{system}-p1/{system}_{bench}_judged.json").read_text())
    out = {}
    for uid, rows in d.items():
        rows = [rows] if isinstance(rows, dict) else rows
        for i, r in enumerate(rows):
            if "llm_judgments" in r:
                vals = [float(v) for v in _lit(r["llm_judgments"]).values()]
                score = sum(vals) / len(vals)
                cat = r.get("category")
            else:
                score, cat = float(r["nugget_score"]), r.get("dimension")
            out[(uid, r.get("key", i))] = (CAT_NAMES[bench].get(str(cat), str(cat)), score, r.get("difficulty"))
    return out


def by_cat(rows: dict, keys=None) -> dict:
    acc = defaultdict(list)
    for k, (cat, score, _) in rows.items():
        if keys is None or k in keys:
            acc[cat].append(score)
    return {c: (100 * sum(v) / len(v), len(v)) for c, v in acc.items()}


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.1f}"


def table(title: str, note: str, bench: str, series: list[tuple[str, dict]], *, overall: list | None = None) -> dict:
    """Rows = categories (in the benchmark's order), columns = series; plus n and an Overall row."""
    cols = ["Category", "n", *[name for name, _ in series]]
    rows = []
    for cat in ORDER[bench]:
        ns = [d[cat][1] for _, d in series if cat in d]
        if not ns:
            continue
        rows.append([cat, str(max(ns)), *[_fmt(d[cat][0]) if cat in d else "-" for _, d in series]])
    if overall:
        rows.append(["**Overall**", str(overall[0]), *[f"**{_fmt(v)}**" for v in overall[1:]]])
    return {"title": title, "note": note, "columns": cols, "rows": rows}


def _overall(rows: dict, keys=None) -> float:
    vals = [s for k, (_, s, _) in rows.items() if keys is None or k in keys]
    return 100 * sum(vals) / len(vals)


def settings(bench: str) -> dict:
    roots = [("Board depth", P / "omni"), ("Hermes default", P / "omni-shipped"),
             ("Default + fill", P / "omni-h1-shipped")]
    if (P / f"omni-h1-16k/results/{bench}/severian-p1/exp_report.md").exists():  # only once the run has finished
        roots.append(("16,000 chars + fill", P / "omni-h1-16k"))
    data = [(name, judged(root, bench)) for name, root in roots]
    keys = set(data[0][1])
    for name, d in data:  # every arm must cover the same full question set; never shrink the comparison silently
        if set(d) != keys:
            raise ValueError(f"{bench} {name}: {len(d)} questions vs {len(keys)}")
    series = [(name, by_cat(d, keys)) for name, d in data]
    return table(f"{bench}: Severian settings by category",
                 "Severian 0.3.11 on the board models. Board depth = top-20, no cap; Hermes default = top-10, 8,000 "
                 "characters; fill = SEVERIAN_FILL_BUDGET + SEVERIAN_COMPACT_CITES (0.3.12 candidate).",
                 bench, series, overall=[len(keys), *[_overall(d, keys) for _, d in data]])


def strict(bench: str, sample: str) -> dict:
    """Board marker vs strict second marker, same answers, per category, both systems."""
    out = []
    for system in ("severian", "lcmx"):
        rows = [json.loads(line) for line in (P / f"marker2/{sample}/{system}/judged-opus.jsonl").open()]
        names = CAT_NAMES[bench]
        out.append({r["question_id"]: (names.get(str(r["category"]), r["category"]), float(r["opus_score"]), None)
                    for r in rows})
    board = []
    for system in ("severian", "lcmx"):
        full = judged(P / "omni", bench, system)
        if bench == "lme":
            # LongMemEval question ids map to users through the dataset index (marker2_prep.py does the same).
            ds = json.loads((P / "omni/data/longmemeval/longmemeval_s_cleaned.json").read_text())
            idx = {f"lme-{q['question_id']}": f"lme_exper_user_p1_{i}" for i, q in enumerate(ds)}
            board.append({qid: full[(idx[qid], 0)] for qid in out[0]})
        elif bench == "beam":
            board.append({qid: next(v for k, v in full.items() if k[1] == qid[len("beam-"):]) for qid in out[0]})
        else:
            board.append({qid: full[(qid[len("locomo-"):].rsplit("-", 1)[0], int(qid.rsplit("-", 1)[1]))]
                          for qid in out[0]})
    series = [("Severian, board", by_cat(board[0])), ("LCM-X, board", by_cat(board[1])),
              ("Severian, strict", by_cat(out[0])), ("LCM-X, strict", by_cat(out[1]))]
    n = len(out[0])
    return table(f"{bench}: board marker vs strict marker by category ({n} questions)",
                 "Same answers marked twice: the board's gpt-4o-mini judge, then Claude Opus with the official "
                 "LongMemEval templates / BEAM per-item rubric.", bench, series,
                 overall=[n, _overall(board[0]), _overall(board[1]), _overall(out[0]), _overall(out[1])])


def side(bench: str, base: str, arms: list[str]) -> dict:
    series, overall = [], []
    common = None
    loaded = {}
    for arm in arms:
        for system in ("severian", "lcmx"):
            rows = [json.loads(line) for line in (P / base / arm / system / f"{bench}.jsonl").open()]
            loaded[(arm, system)] = {r["qkey"]: (CAT_NAMES[bench].get(str(r["category"]), str(r["category"])),
                                                 float(r["score"]), None) for r in rows if not r.get("error")}
    for (arm, system), d in loaded.items():
        other = loaded[(arm, "lcmx" if system == "severian" else "severian")]
        keys = set(d) & set(other)
        series.append((f"{arm} {'Sev' if system == 'severian' else 'LCM-X'}", by_cat(d, keys)))
        overall.append(_overall(d, keys))
        common = len(keys) if common is None else min(common, len(keys))
    return table(f"{bench}: equal-text side track by category",
                 "One fixed open reader (Qwen3.8-27B) and marker (DeepSeek-V4-Pro); each system's text cut to the same "
                 "token budget (eq1000/eq4000/eq8000) or full (replicate). Failed marks excluded, pairs only.",
                 bench, series, overall=[common, *overall])


def difficulty() -> dict:
    sev, lcx = judged(P / "omni", "beam"), judged(P / "omni", "beam", "lcmx")
    acc = defaultdict(lambda: ([], []))
    for k, (_, s, diff) in sev.items():
        diff = diff or "unlabelled"
        acc[diff][0].append(s)
        acc[diff][1].append(lcx[k][1])
    order = ["clear", "easy", "medium", "hard", "unlabelled"]
    rows = [[d.capitalize(), str(len(a)), _fmt(100 * sum(a) / len(a)), _fmt(100 * sum(b) / len(b))]
            for d, (a, b) in sorted(acc.items(), key=lambda kv: order.index(kv[0]))]
    return {"title": "BEAM 100K by difficulty", "note": "Board models, board marker (nugget score).",
            "columns": ["Difficulty", "n", "Severian 0.3.11", "LCM-X v0.28.1"], "rows": rows}


def main() -> None:
    b = {
        "settings_locomo": settings("locomo"), "settings_lme": settings("lme"), "settings_beam": settings("beam"),
        "strict_locomo": strict("locomo", "locomo"), "strict_lme": strict("lme", "lme-full"),
        "strict_beam": strict("beam", "beam"),
        "side_locomo": side("locomo", "tracks", ["replicate", "eq1000", "eq4000", "eq8000"]),
        "side_beam": side("beam", "tracks-nous", ["replicate", "eq1000", "eq4000", "eq8000"]),
        "side_lme": side("lme", "tracks-nous", ["replicate"]),
        "beam_difficulty": difficulty(),
    }
    Path(sys.argv[1]).write_text(json.dumps({"breakdowns": b}, indent=1) + "\n")
    print("wrote", sys.argv[1], ", ".join(b))


if __name__ == "__main__":
    main()
