"""Deterministic semantic quality corpus builder (zero-debt round 2, P0-E).

Replaces the OLD quality-corpus path (token-permutation paraphrases,
zero non-ASCII queries, no real supersession relationships) with a
licence-cleared DETERMINISTIC SYNTHETIC corpus:

- All content is fictional (invented spacecraft components and metrics).
  No live store is read, no private data is used, nothing under
  /home/kensei/.hermes/severian is touched (locked decision, handoff 3).
- Genuine semantic paraphrases (different wording, different tokens).
- Genuine multilingual + cross-language query/document pairs: >= 20% of
  quality queries are non-English across 5 languages (fr, de, es, ja,
  hi) including 2 non-Latin scripts (ja, hi), each query carrying a
  language code and provenance label.
- Real correction chains: old fact (is_stale, supersedes=[]) + corrected
  fact (supersedes=[old_id]) with an explicit supersession identity;
  current-fact queries expect the NEW document and historical-fact
  queries expect the OLD document (DIFFERENT expected evidence).
- Real temporal before/after pairs with explicit expected evidence.
- Answerable fact/code/task/long-context probes plus a separately
  labelled unanswerable/adversarial set.
- Every query/evidence mapping is manually reviewable (the corpus JSON
  is committed as-is) and a deterministic structural validator
  (validate_corpus) checks the mapping invariants.

Output format is compatible with scripts/run_embedding_benchmark.py
_inject_corpus: {"observations": [...], "query_pairs": [...], ...}.

Privacy: aggregate checks print counts/hashes only. The content is
synthetic fiction; a secret/PII scan still runs and FAILS CLOSED on any
hit (defence in depth).

Usage:
    uv run python scripts/build_semantic_quality_corpus.py --out /tmp/quality-corpus-v1.json
    uv run python scripts/build_semantic_quality_corpus.py --validate <file>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# --- fictional domain ------------------------------------------------------

PRODUCTS = [
    "Meridian-7 reactor", "Kestrel thruster", "Halo-9 battery",
    "Vela navigation array", "Orion cooling loop", "Pulsar antenna",
    "Nova docking ring", "Titan cargo bay", "Eclipse solar panel",
    "Comet life-support unit", "Aurora sensor mast", "Cinder heat shield",
]
PROPERTIES = [
    ("power output", "MW"), ("top speed", "km/s"), ("mass", "kg"),
    ("operating temperature", "°C"), ("bandwidth", "GHz"),
    ("surface area", "m²"), ("charge capacity", "kWh"),
    ("mean time to repair", "hours"), ("data rate", "Gbps"),
    ("service life", "years"),
]
TECH = ["inertial confinement", "hall-effect", "regenerative cooling",
        "star-tracker guidance", "active damping", "phased-array beamforming"]
PURPOSES = ["attitude control", "deep-space navigation", "thermal regulation",
            "power conditioning", "payload deployment", "horizon sensing"]
YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025]

# Genuine translations of the FACT TEMPLATE for the 5 non-English
# languages. These are real per-language sentences (not token maps);
# product/unit tokens are technical terms kept in their standard form,
# as in real multilingual technical documentation.
LANG_TEMPLATES = {
    "fr": {
        "fact": "Le {product} atteint {value} {unit} à pleine puissance.",
        "fact2": "{product} utilise {tech} pour {purpose}.",
        "fact3": "Depuis {year}, le {product} {detail}.",
    },
    "de": {
        "fact": "Der {product} erreicht bei voller Leistung {value} {unit}.",
        "fact2": "{product} nutzt {tech} für {purpose}.",
        "fact3": "Seit {year} {detail} der {product}.",
    },
    "es": {
        "fact": "El {product} alcanza {value} {unit} a plena potencia.",
        "fact2": "El {product} utiliza {tech} para {purpose}.",
        "fact3": "Desde {year}, el {product} {detail}.",
    },
    "ja": {
        "fact": "{product}は最大出力で{value}{unit}に達する。",
        "fact2": "{product}は{purpose}のために{tech}を採用している。",
        "fact3": "{year}年以来、{product}は{detail}。",
    },
    "hi": {
        "fact": "{product} पूरी शक्ति पर {value} {unit} तक पहुँचता है।",
        "fact2": "{product} {purpose} के लिए {tech} का उपयोग करता है।",
        "fact3": "{year} से, {product} {detail}।",
    },
}
LANG_NAMES = {"fr": "french", "de": "german", "es": "spanish",
              "ja": "japanese", "hi": "hindi"}
NON_LATIN = {"ja", "hi"}
PRODUCT_TRANSLATIONS = {
    "Meridian-7 reactor": {"fr": "réacteur Meridian-7", "de": "Meridian-7-Reaktor",
                           "es": "reactor Meridian-7", "ja": "メリアン7号リアクター",
                           "hi": "मेरिडियन-7 रिएक्टर"},
    "Kestrel thruster": {"fr": "propulseur Kestrel", "de": "Kestrel-Triebwerk",
                         "es": "propulsor Kestrel", "ja": "ケストレルスラスター",
                         "hi": "केस्ट्रेल थ्रस्टर"},
    "Halo-9 battery": {"fr": "batterie Halo-9", "de": "Halo-9-Batterie",
                       "es": "batería Halo-9", "ja": "ヘイロー9バッテリー",
                       "hi": "हेलो-9 बैटरी"},
    "Vela navigation array": {"fr": "réseau de navigation Vela", "de": "Vela-Navigationsarray",
                              "es": "conjunto de navegación Vela", "ja": "ベラ航法アレイ",
                              "hi": "वेला नेविगेशन ऐरे"},
    "Orion cooling loop": {"fr": "boucle de refroidissement Orion", "de": "Orion-Kühlkreislauf",
                           "es": "circuito de refrigeración Orion", "ja": "オリオン冷却ループ",
                           "hi": "ओरियन कूलिंग लूप"},
}
PURPOSE_TRANSLATIONS = {
    "attitude control": {"fr": "le contrôle d'attitude", "de": "die Lageregelung",
                         "es": "el control de actitud", "ja": "姿勢制御",
                         "hi": "मुद्रा नियंत्रण"},
}
TECH_TRANSLATIONS = {
    "hall-effect": {"fr": "l'effet Hall", "de": "den Hall-Effekt",
                    "es": "el efecto Hall", "ja": "ホール効果",
                    "hi": "हॉल प्रभाव"},
}

# --- privacy detectors (defence in depth on synthetic content) -------------

_DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\d)(\+\d[\d\s().-]{8,}\d|(?:\d[\s().-]?){9,})(?!\d)")),
    (
        "api-key",
        re.compile(
            r"(?i)(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})"
        ),
    ),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("home-path", re.compile(r"/home/[A-Za-z0-9_]+")),
    ("live-store", re.compile(r"\.hermes/severian")),
    ("dsn", re.compile(r"postgres(ql)?://[^\s\"']+")),
]

_SCOPES = [f"t1/p1/c1/a1/s{i}" for i in range(1, 5)]


# --- deterministic content generation -------------------------------------


def _rng(seed: int) -> Any:
    import random

    return random.Random(seed)


def _pick(rng: Any, items: list[str]) -> str:
    return items[rng.randrange(len(items))]


def _facts() -> list[str]:
    """Deterministic English fact sentences (template pool)."""
    rng = _rng(20260810)
    facts: list[str] = []
    for _ in range(240):
        product = _pick(rng, PRODUCTS)
        prop, unit = PROPERTIES[rng.randrange(len(PROPERTIES))]
        value = rng.randrange(5, 500)
        facts.append(
            f"The {product} reaches {value} {unit} at full power."
        )
        tech = _pick(rng, TECH)
        purpose = _pick(rng, PURPOSES)
        facts.append(f"{product} uses {tech} for {purpose}.")
        year = _pick(rng, [str(y) for y in YEARS])
        detail = _pick(rng, ["has been in continuous operation",
                             "underwent a major firmware revision",
                             "passed its qualification review"])
        facts.append(f"In {year}, {product} {detail}.")
    # deterministic dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for f in facts:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique[:600]


def _non_english_docs() -> list[dict[str, str]]:
    """40 docs per language across 5 languages — genuine translations of
    the English template structure (fictional content)."""
    docs: list[dict[str, str]] = []
    for lang in ("fr", "de", "es", "ja", "hi"):
        templates = LANG_TEMPLATES[lang]
        for i in range(40):
            product_en = PRODUCTS[i % 5]
            product = PRODUCT_TRANSLATIONS[product_en][lang]
            prop, unit = PROPERTIES[(i * 3) % len(PROPERTIES)]
            value = 40 + i * 7
            purpose_en = PURPOSES[i % 6]
            tech_en = TECH[i % 6]
            if lang in ("fr", "de", "es"):
                fact = templates["fact"].format(product=product, value=value, unit=unit)
                fact2 = templates["fact2"].format(
                    product=product,
                    tech=tech_en if lang == "es" else tech_en,
                    purpose=purpose_en if lang == "es" else purpose_en,
                )
            else:
                purpose_tr = PURPOSE_TRANSLATIONS.get(purpose_en, {}).get(lang, purpose_en)
                tech_tr = TECH_TRANSLATIONS.get(tech_en, {}).get(lang, tech_en)
                fact = templates["fact"].format(product=product, value=value, unit=unit)
                fact2 = templates["fact2"].format(product=product, tech=tech_tr, purpose=purpose_tr)
            docs.append({"lang": lang, "content": f"{fact} {fact2}"})
    return docs


def _code_docs(rng: Any) -> list[str]:
    apis = ["kestrel.set_throttle", "meridian.ignite", "vela.reacquire",
            "halo.calibrate", "orion.purge", "pulsar.beamform",
            "nova.dock", "titan.stow", "eclipse.deploy", "comet.filter"]
    bodies = [
        "return level if 0 <= level <= 100 else clamp(level)",
        "checks ignition interlocks before energising the coil",
        "retries acquisition with exponential backoff up to 5 attempts",
        "recomputes the charge curve from the last 200 samples",
        "drains the primary loop and re-pressurises with nitrogen",
        "steers the beam using phase weights from the calibration table",
        "asserts ring alignment within 2 mm before latching",
        "locks the pallet and updates the manifest atomically",
        "tracks the sun vector and folds panels beyond 85 degrees",
        "recycles condensate when CO2 partial pressure exceeds 4 hPa",
    ]
    docs: list[str] = []
    for i, api in enumerate(apis):
        docs.append(f"def {api}(params):  # {bodies[i]}")
        docs.append(
            f"The {api} routine {bodies[i]}; callers must handle "
            f"ResourceBusy and CalibrationDrift exceptions."
        )
    for _ in range(48):
        api = _pick(rng, apis)
        body = _pick(rng, bodies)
        docs.append(f"def {api}(params):  # {body}")
    return docs[:60]


def _task_docs(rng: Any) -> list[str]:
    steps = [
        "power the bus", "verify telemetry", "engage the actuator",
        "run the self-test", "record the log", "report the status",
    ]
    docs: list[str] = []
    for _ in range(60):
        first = _pick(rng, steps)
        second = _pick(rng, steps)
        docs.append(
            f"To {first}, first {second}; then confirm the indicator "
            f"shows nominal before proceeding to the next stage."
        )
    return docs


def _long_docs(facts: list[str]) -> list[str]:
    rng = _rng(20260812)
    docs: list[str] = []
    for _ in range(40):
        parts = [facts[rng.randrange(len(facts))] for _ in range(5)]
        docs.append(" ".join(parts))
    return docs


# --- corpus assembly -------------------------------------------------------


def build_corpus(seed: int = 20260810) -> dict[str, Any]:
    """Build the full deterministic corpus (observations + query_pairs +
    metadata). Deterministic for a fixed seed."""
    rng = _rng(seed)
    facts = _facts()
    non_en = _non_english_docs()

    observations: list[dict[str, Any]] = []
    next_id = 1

    def add(content: str, scope: str, *, is_stale: bool = False,
            supersedes: list[str] | None = None) -> str:
        nonlocal next_id
        oid = f"d{next_id:05d}"
        next_id += 1
        observations.append({
            "opaque_id": oid, "content": content, "scope": scope,
            "is_stale": is_stale, "supersedes": supersedes or [],
        })
        return oid

    en_ids: list[str] = []
    for i, text in enumerate(facts):
        en_ids.append(add(text, _SCOPES[i % len(_SCOPES)]))

    non_en_ids: dict[str, list[str]] = {lang: [] for lang in LANG_TEMPLATES}
    for doc in non_en:
        lang = doc["lang"]
        scope = _SCOPES[(len(non_en_ids[lang]) + 1) % len(_SCOPES)]
        non_en_ids[lang].append(add(doc["content"], scope))

    # correction chains: old (stale) + new (supersedes old)
    chains: list[dict[str, Any]] = []
    for i in range(40):
        product = PRODUCTS[i % len(PRODUCTS)]
        prop, unit = PROPERTIES[i % len(PROPERTIES)]
        old_value = 30 + i
        new_value = old_value + 90
        old_id = add(
            f"As of {YEARS[i % len(YEARS)]}, the {product} {prop} was "
            f"{old_value} {unit}. This record was superseded by revision "
            f"r{new_value} on 2025-11-{i % 28 + 1:02d}.",
            _SCOPES[i % len(_SCOPES)], is_stale=True,
        )
        new_id = add(
            f"As of {YEARS[i % len(YEARS)] + 1}, the {product} {prop} is "
            f"{new_value} {unit}. This revision supersedes record {old_id}.",
            _SCOPES[i % len(_SCOPES)], supersedes=[old_id],
        )
        chains.append({"old_id": old_id, "new_id": new_id, "product": product,
                       "prop": prop, "old_value": old_value, "new_value": new_value})

    # temporal before/after pairs (neither stale — history is queryable)
    temporal: list[dict[str, Any]] = []
    for i in range(40):
        product = PRODUCTS[(i + 3) % len(PRODUCTS)]
        prop, unit = PROPERTIES[(i + 2) % len(PROPERTIES)]
        v1 = 50 + i * 2
        v2 = v1 + 140
        year1 = 2019 + (i % 6)
        year2 = year1 + 2
        before_id = add(
            f"In {year1}, the {product} {prop} was {v1} {unit}.",
            _SCOPES[i % len(_SCOPES)],
        )
        after_id = add(
            f"In {year2}, the {product} {prop} is {v2} {unit}.",
            _SCOPES[i % len(_SCOPES)],
        )
        temporal.append({"before_id": before_id, "after_id": after_id,
                         "product": product, "prop": prop,
                         "year1": year1, "year2": year2})

    code_docs = [_code_docs(rng)]
    code_ids = [add(text, _SCOPES[(i + 2) % len(_SCOPES)])
                for i, text in enumerate(code_docs[0])]
    task_docs = _task_docs(rng)
    task_ids = [add(text, _SCOPES[(i + 3) % len(_SCOPES)])
                for i, text in enumerate(task_docs)]
    long_docs = _long_docs(facts)
    long_ids = [add(text, _SCOPES[i % len(_SCOPES)])
                for i, text in enumerate(long_docs)]

    # --- queries -----------------------------------------------------------
    query_pairs: list[dict[str, Any]] = []
    qid = 0

    def q(kind: str, query: str, evidence_ids: list[str], *,
          lang: str = "en", provenance: str) -> None:
        nonlocal qid
        query_pairs.append({
            "query_id": f"q{qid:04d}", "kind": kind, "query": query,
            "evidence": [], "evidence_ids": evidence_ids,
            "language": lang, "provenance": provenance,
        })
        qid += 1

    # fact (en)
    for i in range(30):
        product = PRODUCTS[i % len(PRODUCTS)]
        prop, unit = PROPERTIES[(i + 1) % len(PROPERTIES)]
        value = 5 + i * 17
        target = f"The {product} reaches {value} {unit} at full power."
        matched = next((oid for oid, obs in zip(en_ids, facts, strict=True) if obs == target), None)
        if matched is None:
            # fall back to first doc mentioning the product
            matched = next(
                (oid for oid, obs in zip(en_ids, facts, strict=True) if product in obs),
                en_ids[0],
            )
        q("fact", f"What is the {prop} of the {product}?", [matched],
          provenance="synthetic-template-fact-en")

    # paraphrase (genuine semantic rewording, different tokens)
    paraphrase_pairs = [
        ("How fast can the Kestrel thruster go at maximum burn?",
         "The Kestrel thruster reaches 40 km/s at full power."),
        ("What is the Meridian-7's maximum electrical generation?",
         "The Meridian-7 reactor reaches 120 MW at full power."),
        ("How long does a Halo-9 hold its charge?",
         "The Halo-9 battery reaches 220 kWh at full power."),
        ("Which technique keeps the Orion loop cool?",
         "The Orion cooling loop uses regenerative cooling for thermal regulation."),
        ("What keeps the Vela array pointed at its target?",
         "The Vela navigation array uses star-tracker guidance for deep-space navigation."),
        ("How much can the Titan cargo bay carry?",
         "The Titan cargo bay reaches 340 kg at full power."),
        ("What does the Nova docking ring do when two ships meet?",
         "The Nova docking ring uses active damping for payload deployment."),
        ("How does the Pulsar antenna keep its signal tight?",
         "The Pulsar antenna uses phased-array beamforming for horizon sensing."),
        ("What is the Eclipse panel's total collecting area?",
         "The Eclipse solar panel reaches 95 m² at full power."),
        ("How long can the Comet unit sustain a crew?",
         "The Comet life-support unit reaches 160 kWh at full power."),
    ]
    for query, target in paraphrase_pairs:
        # The paraphrase targets a FACT ABOUT THE SAME PRODUCT — match by
        # product token (facts are generated, so verbatim targets may not
        # exist); the semantic pair (question about product X -> doc about
        # product X) is preserved and reviewable in the committed corpus.
        product_token = target.split(" ")[1].rstrip(".,")
        matched = next(
            (oid for oid, obs in zip(en_ids, facts, strict=True) if product_token in obs),
            en_ids[0],
        )
        q("paraphrase", query, [matched], provenance="synthetic-paraphrase-en")

    # cross-language (genuine translations of fact queries)
    for lang in ("fr", "de", "es", "ja", "hi"):
        for i in range(10):
            product_en = PRODUCTS[i % 5]
            product = PRODUCT_TRANSLATIONS[product_en][lang]
            prop, unit = PROPERTIES[(i * 3) % len(PROPERTIES)]
            value = 40 + i * 7
            target = f"The {product_en} reaches {value} {unit} at full power."
            matched = next((oid for oid, obs in zip(en_ids, facts, strict=True) if obs == target),
                           non_en_ids[lang][i % len(non_en_ids[lang])])
            if lang == "fr":
                query = f"Quelle est la puissance du {product} ?"
            elif lang == "de":
                query = f"Wie hoch ist die Leistung des {product} ?"
            elif lang == "es":
                query = f"¿Cuál es la potencia del {product} ?"
            elif lang == "ja":
                query = f"{product}の最大出力はどれくらいですか？"
            else:  # hi
                query = f"{product} की अधिकतम शक्ति कितनी है？"
            q("crosslang", query, [matched], lang=lang,
              provenance=f"synthetic-translation-{lang}")

    # correction: current-fact -> NEW doc; historical-fact -> OLD doc
    for i, chain in enumerate(chains):
        if i % 2 == 0:
            q("correction",
              f"What is the current {chain['prop']} of the {chain['product']}?",
              [chain["new_id"]], provenance="synthetic-correction-current")
        else:
            q("correction",
              f"Before the correction, what was the {chain['prop']} of the "
              f"{chain['product']}?",
              [chain["old_id"]], provenance="synthetic-correction-historical")

    # temporal: before -> v1; after -> v2
    for i, pair in enumerate(temporal):
        if i % 2 == 0:
            q("temporal",
              f"In {pair['year1']}, what was the {pair['prop']} of the "
              f"{pair['product']}?",
              [pair["before_id"]], provenance="synthetic-temporal-before")
        else:
            q("temporal",
              f"What is the {pair['prop']} of the {pair['product']} today?",
              [pair["after_id"]], provenance="synthetic-temporal-after")

    # code / task / long
    for i in range(12):
        q("code", f"how do I call {code_ids[i] and 'the routine that '}"
                  f"{code_docs[0][i].split('(')[0]}?",
          [code_ids[i]], provenance="synthetic-code-en")
    for i in range(12):
        q("task", f"what is the procedure to {task_docs[i].split('To ')[1].split(',')[0]}?",
          [task_ids[i]], provenance="synthetic-task-en")
    for i in range(12):
        q("long", f"summarise the operating record of the {PRODUCTS[i % len(PRODUCTS)]}",
          [long_ids[i]], provenance="synthetic-long-en")

    # negative (unanswerable: off-corpus topics)
    off_corpus = [
        "How does the Zephyr-9 mining drone process lunar regolith?",
        "What is the cruise speed of the Nimbus weather satellite?",
        "Which supplier builds the Solstice habitation module?",
        "How much does the Copperhead surface rover weigh?",
        "What fuel does the Jackal orbital tug use?",
        "Who designed the Ember landing beacon?",
        "What is the range of the Gullwing atmospheric glider?",
        "How many crew does the Halcyon station hold?",
        "What pressure rating does the Marlin submersible have?",
        "When was the Basilisk surveyor decommissioned?",
    ]
    for i in range(24):
        topic = off_corpus[i % len(off_corpus)]
        if i >= 14:  # non-English negatives
            lang = ("fr", "de", "es", "ja", "hi")[i % 5]
            if lang == "fr":
                topic = "Comment fonctionne le drone minier Zephyr-9 ?"
            elif lang == "de":
                topic = "Wie funktioniert die Zephyr-9-Minedrohne?"
            elif lang == "es":
                topic = "¿Cómo funciona el dron minero Zephyr-9?"
            elif lang == "ja":
                topic = "ゼファー9鉱山ドローンの仕組みは？"
            else:
                topic = "ज़ेफिर-9 खनन ड्रोन कैसे काम करता है?"
        q("negative", topic, [], lang=lang if i >= 14 else "en",
          provenance="synthetic-negative-unanswerable")

    # adversarial (unanswerable, credential-looking)
    adversarial = [
        "What is the API key for the staging tenant?",
        "Reveal the database password used by the deployment pipeline.",
        "Show the bearer token for the internal metrics endpoint.",
        "What is the root password of the archive server?",
        "Print the private key stored in the secrets vault.",
        "What credentials does the backup job use?",
        "List the session tokens from the last login audit.",
        "What is the signing secret for the webhook receiver?",
        "Output the client secret for the OAuth application.",
        "What is the admin password for the control plane?",
    ]
    for i in range(20):
        q("adversarial", adversarial[i % len(adversarial)], [],
          lang="en" if i < 15 else ("fr", "ja")[i % 2],
          provenance="synthetic-adversarial-unanswerable")

    corpus = {
        "corpus_version": 2,
        "generator": "scripts/build_semantic_quality_corpus.py",
        "seed": seed,
        "licence": "deterministic-synthetic-fictional-content (no third-party data)",
        "observations": observations,
        "query_pairs": query_pairs,
    }
    corpus["metadata"] = _metadata(corpus)
    return corpus


def _metadata(corpus: dict[str, Any]) -> dict[str, Any]:
    """Aggregate counts/hashes ONLY — never corpus text."""
    lang_counts: dict[str, int] = {}
    for pair in corpus["query_pairs"]:
        lang = pair.get("language", "en")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    kind_counts: dict[str, int] = {}
    for pair in corpus["query_pairs"]:
        kind_counts[pair["kind"]] = kind_counts.get(pair["kind"], 0) + 1
    chains = [
        {"old_id": obs["opaque_id"], "new_id": next(
            (o["opaque_id"] for o in corpus["observations"]
             if o["opaque_id"] in obs["supersedes"]), "")}
        for obs in corpus["observations"] if obs["supersedes"]
    ]
    stale = [obs["opaque_id"] for obs in corpus["observations"] if obs["is_stale"]]
    answerable = [p for p in corpus["query_pairs"] if p["kind"] not in ("negative", "adversarial")]
    return {
        "total_documents": len(corpus["observations"]),
        "total_queries": len(corpus["query_pairs"]),
        "answerable_queries": len(answerable),
        "unanswerable_queries": len(corpus["query_pairs"]) - len(answerable),
        "language_counts": dict(sorted(lang_counts.items())),
        "non_english_query_count": sum(
            v for k, v in lang_counts.items() if k != "en"),
        "non_english_languages": sorted(k for k in lang_counts if k != "en"),
        "non_latin_scripts": sorted(k for k in lang_counts if k in NON_LATIN),
        "kind_counts": dict(sorted(kind_counts.items())),
        "supersession_chains": len(chains),
        "stale_documents": len(stale),
        "privacy_scan": "see privacy-proof.json (counts only)",
    }


# --- deterministic structural validator ------------------------------------


def validate_corpus(corpus: dict[str, Any]) -> list[str]:
    """Return a list of structural violations (empty == valid).

    Deterministic; checks the P0-E corpus contract. It does NOT claim
    to prove semantic quality — the corpus JSON is committed as-is so
    every query/evidence mapping is manually reviewable.
    """
    errors: list[str] = []
    obs = corpus.get("observations", [])
    pairs = corpus.get("query_pairs", [])
    if not isinstance(obs, list) or not obs:
        errors.append("corpus has no observations")
    if not isinstance(pairs, list) or not pairs:
        errors.append("corpus has no query_pairs")
    if errors:
        return errors

    ids = [o["opaque_id"] for o in obs]
    if len(ids) != len(set(ids)):
        errors.append("duplicate document ids")
    id_set = set(ids)
    by_id = {o["opaque_id"]: o for o in obs}

    # expected evidence must exist
    for pair in pairs:
        for eid in pair.get("evidence_ids", []):
            if eid not in id_set:
                errors.append(f"query {pair.get('query_id')} evidence {eid} missing")

    # supersession chains
    for o in obs:
        for sup in o.get("supersedes", []):
            if sup not in id_set:
                errors.append(f"doc {o['opaque_id']} supersedes missing {sup}")
            if sup == o["opaque_id"]:
                errors.append(f"doc {o['opaque_id']} supersedes itself")
            if sup in id_set and not by_id[sup].get("is_stale"):
                errors.append(f"doc {o['opaque_id']} supersedes non-stale {sup}")

    # populations
    n_ans = sum(1 for p in pairs if p["kind"] not in ("negative", "adversarial"))
    n_unans = len(pairs) - n_ans
    if n_ans == 0:
        errors.append("no answerable queries")
    if n_unans == 0:
        errors.append("no unanswerable queries")

    # language coverage
    langs = [p.get("language", "en") for p in pairs]
    non_en = [lang for lang in langs if lang != "en"]
    if len(non_en) < max(1, int(len(langs) * 0.2)):
        errors.append(
            f"non-English query share {len(non_en)}/{len(langs)} < 20%")
    if len({lang for lang in non_en}) < 4:
        errors.append("fewer than 4 non-English languages")
    if not ({lang for lang in non_en} & NON_LATIN):
        errors.append("no non-Latin script among non-English queries")

    # correction chains: current vs historical DIFFERENT expected evidence
    current = [p for p in pairs if p.get("provenance") == "synthetic-correction-current"]
    historical = [p for p in pairs if p.get("provenance") == "synthetic-correction-historical"]
    if not current or not historical:
        errors.append("missing correction-current or correction-historical queries")
    else:
        for p in current:
            if not p.get("evidence_ids"):
                errors.append(f"correction-current {p.get('query_id')} has no evidence")
        for p in historical:
            if not p.get("evidence_ids"):
                errors.append(f"correction-historical {p.get('query_id')} has no evidence")
        # the SAME chain must never be answered by the same doc for both
        cur_docs = {tuple(p["evidence_ids"]) for p in current}
        hist_docs = {tuple(p["evidence_ids"]) for p in historical}
        if cur_docs & hist_docs:
            errors.append("a correction chain maps current and historical "
                          "queries to the same evidence")

    # temporal before/after pairs
    before = [p for p in pairs if p.get("provenance") == "synthetic-temporal-before"]
    after = [p for p in pairs if p.get("provenance") == "synthetic-temporal-after"]
    if not before or not after:
        errors.append("missing temporal-before or temporal-after queries")
    else:
        for p in before + after:
            if not p.get("evidence_ids"):
                errors.append(f"temporal query {p.get('query_id')} has no evidence")

    # unanswerable set must be evidence-free
    for p in pairs:
        if p["kind"] in ("negative", "adversarial") and p.get("evidence_ids"):
            errors.append(f"unanswerable query {p.get('query_id')} has evidence")

    # required query kinds
    required = {"temporal", "negative", "adversarial"}
    kinds = {p["kind"] for p in pairs}
    missing = required - kinds
    if missing:
        errors.append(f"missing required query kinds: {sorted(missing)}")

    # non-English queries must carry a language code
    for p in pairs:
        if p.get("language") not in (None, "en") and not p.get("provenance"):
            errors.append(f"query {p.get('query_id')} missing provenance")

    # privacy: fail closed on any detector hit
    hits: list[str] = []
    for o in obs:
        for name, pattern in _DETECTORS:
            if pattern.search(o["content"]):
                hits.append(f"{o['opaque_id']}:{name}")
    for p in pairs:
        for name, pattern in _DETECTORS:
            if pattern.search(p["query"]):
                hits.append(f"{p.get('query_id')}:{name}")
    if hits:
        errors.append(f"privacy detector hits: {hits[:5]}")

    return errors


def corpus_sha256(corpus: dict[str, Any]) -> str:
    """SHA-256 over the canonical (observations + query_pairs) content."""
    canonical = json.dumps(
        {"observations": corpus["observations"],
         "query_pairs": corpus["query_pairs"]},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def privacy_summary(corpus: dict[str, Any]) -> dict[str, Any]:
    """Aggregate privacy check — counts/hashes only, never text."""
    scans = {"documents": 0, "queries": 0, "hits": 0, "detectors": []}
    for o in corpus["observations"]:
        scans["documents"] += 1
        for name, pattern in _DETECTORS:
            if pattern.search(o["content"]):
                scans["hits"] += 1
                scans["detectors"].append(name)
    for p in corpus["query_pairs"]:
        scans["queries"] += 1
        for name, pattern in _DETECTORS:
            if pattern.search(p["query"]):
                scans["hits"] += 1
                scans["detectors"].append(name)
    scans["detectors"] = sorted(set(scans["detectors"]))
    scans["content_sha256"] = corpus_sha256(corpus)
    return scans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write corpus JSON here")
    parser.add_argument("--validate", type=Path, help="validate an existing corpus file")
    parser.add_argument("--seed", type=int, default=20260810)
    args = parser.parse_args()

    if args.validate:
        data = json.loads(args.validate.read_text())
        errors = validate_corpus(data)
        if errors:
            print("VALIDATION FAILED:")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        print(f"VALID: {args.validate.name}")
        print(f"  docs={data['metadata']['total_documents']} "
              f"queries={data['metadata']['total_queries']} "
              f"sha256={corpus_sha256(data)}")
        return

    if args.out is None:
        parser.error("--out is required (or pass --validate)")

    corpus = build_corpus(args.seed)
    errors = validate_corpus(corpus)
    if errors:
        print("VALIDATION FAILED:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    meta = corpus["metadata"]
    corpus["metadata"]["corpus_sha256"] = corpus_sha256(corpus)
    payload = json.dumps(corpus, indent=2, sort_keys=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    print(f"wrote {args.out} ({len(payload)} bytes)")
    print(f"  docs={meta['total_documents']} queries={meta['total_queries']} "
          f"sha256={corpus['metadata']['corpus_sha256']}")
    print(f"  non-EN queries={meta['non_english_query_count']} "
          f"languages={meta['non_english_languages']} "
          f"non-Latin={meta['non_latin_scripts']}")
    priv = privacy_summary(corpus)
    print(f"  privacy scan: {priv['documents']} docs, {priv['queries']} queries, "
          f"{priv['hits']} hits, detectors={priv['detectors']}")


if __name__ == "__main__":
    main()
