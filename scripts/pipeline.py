#!/usr/bin/env python3
"""Script 2: Validate questions and generate HTML cheat sheets."""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
BROAD_THRESHOLD = 0.25             # wide-net semantic threshold; lower to 0.20 if <5 matches
VALIDATION_MODEL = "gemini-3.1-flash-lite-preview"
VALIDATION_FALLBACK = "gemini-2.5-flash-lite"   # used when VALIDATION_MODEL returns 503
HTML_MODEL = "gemini-3.1-pro-preview"           # primary model for tab-content JSON generation
HTML_FALLBACK = "gemini-2.5-pro"                # used when HTML_MODEL returns 503

# Tabs whose content the LLM produces. The shell renders the static Progress
# tab itself. The Top Questions tab ("questions") is filled in afterwards by
# top_questions_images.postprocess_cheatsheet, NOT by the LLM — so it is not
# in this list. build_html_shell still writes an empty placeholder there.
DYNAMIC_TABS = ["overview", "mistakes", "distinctions",
                "keywords", "traps", "formula", "summary"]

# ---------------------------------------------------------------------------
# Tier-2 helper-doc selective injection (see optimized-jumping-glacier plan)
# ---------------------------------------------------------------------------
TIER2_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "water_heat_matter": ["water", "heat", "boil", "evapor", "condens", "matter", "state",
                          "gas", "liquid", "solid", "temperature", "melt", "freez", "steam",
                          "ice", "vapour", "kettle", "beaker", "cool", "warm"],
    "biology":           ["plant", "flower", "leaf", "root", "stem", "seed", "fruit", "pollin",
                          "animal", "organ", "heart", "lung", "stomach", "kidney", "muscle",
                          "skeleton", "reproduc", "grow", "life cycle", "cell", "habitat"],
    "electricity":       ["circuit", "battery", "bulb", "switch", "current", "wire",
                          "electric", "conduct", "insulator", "ammeter", "series", "parallel"],
}
TIER2_SECTION_HEADER = "=== TIER 2 SUB-SHAPE REFERENCE — domain-gated, selective injection ==="


def select_tier2_domains(topic: str, validated_questions: list[dict]) -> list[str]:
    """Return Tier-2 sub-shape domains relevant to the topic. Pure keyword match."""
    text = (topic + " " + " ".join(q.get("question_text", "") for q in validated_questions)).lower()
    return [d for d, kws in TIER2_DOMAIN_KEYWORDS.items() if any(k in text for k in kws)]


def build_tier2_docs(prompt_text: str, domains: list[str]) -> tuple[str, str]:
    """Extract Tier-2 helper docs for selected domains.

    Returns (docs_block, prompt_without_section). The bottom TIER 2 SUB-SHAPE
    REFERENCE section is stripped from the returned prompt; its filtered content
    is injected at the {TIER2_HELPER_DOCS} placeholder by the caller.
    """
    idx = prompt_text.find(TIER2_SECTION_HEADER)
    if idx < 0:
        return "", prompt_text
    section = prompt_text[idx:]
    rest = prompt_text[:idx].rstrip() + "\n"
    parts: list[str] = []
    for domain in domains:
        pattern = re.compile(
            rf"<!--HELPER:{re.escape(domain)}-->\s*([\s\S]*?)\s*<!--/HELPER:{re.escape(domain)}-->",
        )
        m = pattern.search(section)
        if m:
            parts.append(m.group(1).strip())
    return ("\n\n".join(parts), rest)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
BANKS_DIR = ROOT / "banks"
OUTPUT_HTML_DIR = ROOT / "output" / "html"
OUTPUT_VALIDATED_DIR = ROOT / "output" / "validated"
OUTPUT_CONCEPTVIZ_PROMPTS_DIR = ROOT / "output" / "conceptviz_prompts"
VALIDATION_PROMPT_FILE = ROOT / "prompts" / "validation_prompt.txt"
GENERATION_PROMPT_FILE = ROOT / "prompts" / "generation_prompt.txt"
SYLLABUS_TOPICS_FILE = ROOT / "syllabus_topics.json"
TEMPLATES_DIR = ROOT / "templates"
SHELL_FILE = TEMPLATES_DIR / "shell.html"
STYLES_FILE = TEMPLATES_DIR / "styles.css"
SCRIPT_FILE = TEMPLATES_DIR / "script.js"
AVAILABLE_CLASSES_FILE = TEMPLATES_DIR / "available_classes.txt"
CONCEPTVIZ_REQUIREMENTS_FILE = TEMPLATES_DIR / "conceptviz_requirements.txt"

# ---------------------------------------------------------------------------
# Sentence transformer (lazy, module-level cache)
# ---------------------------------------------------------------------------
_st_model: SentenceTransformer | None = None


def get_st_model() -> SentenceTransformer:
    global _st_model
    if _st_model is None:
        print("Loading sentence transformer model…")
        _st_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _st_model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def topic_to_slug(topic: str) -> str:
    return re.sub(r"[^\w]+", "_", topic.strip().lower()).strip("_")


def level_from_bank(bank: str) -> str:
    return bank.upper()  # p5 → P5, p4 → P4


# ---------------------------------------------------------------------------
# Step 1: Load
# ---------------------------------------------------------------------------

def load_questions(bank: str) -> list[dict]:
    path = BANKS_DIR / bank / "master_questions.jsonl"
    if not path.exists():
        return []
    questions = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        school = record["school"]
        for q in record["questions"]:
            q = dict(q)
            q.setdefault("school", school)
            questions.append(q)
    return questions


# ---------------------------------------------------------------------------
# Step 2: Broad Python filter (semantic + keyword union)
# ---------------------------------------------------------------------------

def load_syllabus_details(level: str) -> dict[str, dict]:
    """Return {topic_name: {keywords, description}} for the given level, or {} if unavailable."""
    if not SYLLABUS_TOPICS_FILE.exists():
        return {}
    data = json.loads(SYLLABUS_TOPICS_FILE.read_text())
    level_data = data.get(level.lower(), {})
    if not isinstance(level_data, dict):
        return {}   # old flat-list format has no detail
    return level_data


def _find_syllabus_entry(topic: str, details: dict[str, dict]) -> dict:
    """Return the syllabus entry whose name best matches the requested topic."""
    topic_lower = topic.lower()
    # Exact match first
    for name, info in details.items():
        if name.lower() == topic_lower:
            return info
    # Substring match (topic arg contained in syllabus name or vice versa)
    for name, info in details.items():
        if topic_lower in name.lower() or name.lower() in topic_lower:
            return info
    return {}


def broad_python_filter(
    questions: list[dict], topic: str, level: str, threshold: float
) -> list[dict]:
    """Cast a wide net: semantic similarity on an enriched query + keyword union.

    The LLM validation call downstream handles the final relevance refinement.
    """
    details = load_syllabus_details(level)
    entry = _find_syllabus_entry(topic, details)
    keywords: list[str] = entry.get("keywords", [])
    description: str = entry.get("description", "")

    # Build enriched query so the embedding captures the full topic scope
    query_parts = [topic]
    if description:
        query_parts.append(description)
    if keywords:
        query_parts.append(" ".join(keywords[:12]))
    query = " ".join(query_parts)

    st = get_st_model()
    topic_emb = st.encode(query, normalize_embeddings=True)
    corpora = [
        q.get("topic", "") + " " + q.get("question_text", "")[:150]
        for q in questions
    ]
    q_embs = st.encode(corpora, normalize_embeddings=True, batch_size=64, show_progress_bar=False)
    sims = q_embs @ topic_emb

    semantic_hits = {i for i, s in enumerate(sims) if float(s) >= threshold}

    # Keyword pass: any syllabus keyword present in the question's topic field or text
    keyword_hits: set[int] = set()
    if keywords:
        kw_lower = {kw.lower() for kw in keywords}
        for i, q in enumerate(questions):
            haystack = (q.get("topic", "") + " " + q.get("question_text", "")[:200]).lower()
            if any(kw in haystack for kw in kw_lower):
                keyword_hits.add(i)

    all_hits = semantic_hits | keyword_hits
    result = [questions[i] for i in sorted(all_hits)]

    if keywords:
        print(f"  Broad filter: {len(semantic_hits)} semantic + {len(keyword_hits)} keyword "
              f"= {len(result)} unique (threshold={threshold})")
    else:
        print(f"  Broad filter (no syllabus keywords): {len(result)} semantic hits (threshold={threshold})")

    return result


# ---------------------------------------------------------------------------
# Step 3: Classify question type
# ---------------------------------------------------------------------------

_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("EXPLAIN",     ["explain", "why does", "why is", "why did", "give a reason why", "give one reason why"]),
    ("COMPARE",     ["compare", "difference between", "how is", "how are", "similar to", "different from"]),
    ("DESCRIBE",    ["describe", "what happens", "how does", "what occurred"]),
    ("STATE",       ["state", "name one", "name two", "give one", "give two", "list", "what is", "identify"]),
    ("SUGGEST",     ["suggest", "recommend", "what could", "what would you"]),
    ("PREDICT",     ["predict", "what will happen", "what would happen"]),
    ("GIVE_REASON", ["give a reason", "account for"]),
    ("CALCULATE",   ["calculate", "find the", "how many", "how much", "what is the value"]),
]


def classify_question_type(question_text: str) -> str:
    text = question_text.lower()
    for qtype, keywords in _TYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return qtype
    return "OTHER_OPEN_ENDED"


# ---------------------------------------------------------------------------
# Step 4: Score difficulty
# ---------------------------------------------------------------------------

_DIAGRAM_WORDS = {"diagram", "figure", "table", "above", "shown", "refer"}


def score_difficulty(q: dict) -> int:
    if q.get("question_kind") == "mcq":
        return score_difficulty_mcq(q)

    qtype = q.get("question_type", "OTHER_OPEN_ENDED")
    text = q.get("question_text", "").lower()
    answer = q.get("model_answer", "")
    sub = q.get("sub_question_part", "none")

    score = int(q.get("marks", 1))
    if qtype in ("EXPLAIN", "COMPARE"):
        score += 2
    elif qtype in ("DESCRIBE", "SUGGEST", "PREDICT"):
        score += 1
    if any(w in text for w in _DIAGRAM_WORDS):
        score += 1
    if len(answer.split()) >= 8:
        score += 1
    if sub not in ("none", "single", "", None):
        score += 1
    return score


def score_difficulty_mcq(q: dict) -> int:
    raw = q.get("question_text", "")
    text = raw.lower()
    options = q.get("options", {}) or {}
    diagrams = q.get("diagrams_and_tables", []) or []

    score = int(q.get("marks", 1))

    wc = len(raw.split())
    if wc >= 30:
        score += 2
    elif wc >= 18:
        score += 1

    if diagrams:
        score += 1
    if any(w in text for w in _DIAGRAM_WORDS):
        score += 1

    if any(w in text for w in (" not ", " except ", "incorrect", "false")):
        score += 2
    if " NOT " in raw or " EXCEPT " in raw:
        score += 1

    opts_concat = " ".join(str(v) for v in options.values()).lower()
    if "all of the above" in opts_concat or "none of the above" in opts_concat:
        score += 1

    plausible = sum(1 for v in options.values() if len(str(v).split()) >= 4)
    if plausible >= 3:
        score += 2
    elif plausible >= 2:
        score += 1

    return min(score, 15)


def difficulty_label(score: int) -> str:
    if score <= 3:
        return "Easy"
    if score <= 6:
        return "Medium"
    return "Hard"


# ---------------------------------------------------------------------------
# Step 5–7: Group multi-part, sort, assign IDs
# ---------------------------------------------------------------------------

def group_and_sort(questions: list[dict], topic: str) -> list[dict]:
    # Classify and score first
    for q in questions:
        q["question_type"] = classify_question_type(q.get("question_text", ""))
        q["difficulty_score"] = score_difficulty(q)
        q["topic_sub_category"] = q.get("topic", "")

    # Group by (school, parent_question_id)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for q in questions:
        key = (q.get("school", ""), q.get("parent_question_id", ""))
        groups[key].append(q)

    def _lift_parent_locator(focus: dict, group_members: list[dict]) -> None:
        """Copy page_number + parent_bbox onto `focus` from whichever group
        member carries them (the parent entry under the new schema). No-op when
        focus already has them, or when no member does."""
        if focus.get("page_number") and focus.get("parent_bbox"):
            return
        for m in group_members:
            if m.get("page_number") and m.get("parent_bbox"):
                focus["page_number"] = m["page_number"]
                focus["parent_bbox"] = m["parent_bbox"]
                return

    result = []
    for group in groups.values():
        # Sub-part entries are the answerable ones; parent-only entries (empty
        # text + empty answer) are scaffolding for shared diagrams/bbox.
        answerable = [q for q in group
                     if (q.get("question_text") or "").strip()
                     or (q.get("model_answer") or "").strip()]
        scaffolding = [q for q in group if q not in answerable]
        # Fall back to the full group when nothing has text — should be rare.
        candidates = answerable or group

        if len(candidates) == 1:
            q = dict(candidates[0])
            q["is_multi_part"] = len(candidates) < len(group) or len(group) > 1
            q["sibling_parts"] = []
            _lift_parent_locator(q, group)
            result.append(q)
        else:
            # Hardest question is the focus; rest are siblings shown for context
            sorted_group = sorted(candidates, key=lambda x: x["difficulty_score"])
            hardest = dict(sorted_group[-1])
            siblings = sorted_group[:-1]  # easiest first
            hardest["is_multi_part"] = True
            hardest["sibling_parts"] = [
                {
                    "sub_question_part": s.get("sub_question_part", ""),
                    "question_text": s.get("question_text", ""),
                    "model_answer": s.get("model_answer", ""),
                    "marks": s.get("marks", 1),
                    "difficulty_score": s["difficulty_score"],
                    "question_type": s["question_type"],
                    "diagrams_and_tables": s.get("diagrams_and_tables", []),
                }
                for s in siblings
            ]
            _lift_parent_locator(hardest, group)
            result.append(hardest)

    # Sort: difficulty_score desc, marks desc
    result.sort(key=lambda q: (q["difficulty_score"], q.get("marks", 1)), reverse=True)

    # Assign IDs
    for i, q in enumerate(result, 1):
        q["id"] = f"Q{i:03d}"

    return result


# ---------------------------------------------------------------------------
# Step 8: Build processing summary
# ---------------------------------------------------------------------------

def build_processing_summary(
    topic: str, level: str, total_in_jsonl: int, questions: list[dict]
) -> dict:
    type_dist: dict[str, int] = defaultdict(int)
    marks_dist = {"1_mark": 0, "2_marks": 0, "3_plus_marks": 0}
    schools: set[str] = set()

    for q in questions:
        type_dist[q.get("question_type", "OTHER_OPEN_ENDED")] += 1
        m = int(q.get("marks", 1))
        if m == 1:
            marks_dist["1_mark"] += 1
        elif m == 2:
            marks_dist["2_marks"] += 1
        else:
            marks_dist["3_plus_marks"] += 1
        schools.add(q.get("school", ""))

    return {
        "topic": topic,
        "level": level,
        "total_in_jsonl": total_in_jsonl,
        "final_usable_count": len(questions),
        "schools_represented": sorted(schools),
        "question_type_distribution": dict(type_dist),
        "marks_distribution": marks_dist,
        "misaligned_excluded": 0,
        "garbled_excluded": 0,
        "corrections_applied": 0,
    }


# ---------------------------------------------------------------------------
# 503 helpers
# ---------------------------------------------------------------------------

def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s


def _try_validate(
    model: str,
    questions: list[dict],
    topic: str,
    level: str,
    client: "genai.Client",
) -> "tuple[list[dict], list, list, dict] | None":
    """Call validate_questions with model. Returns None if 503, re-raises otherwise."""
    try:
        return validate_questions(questions, topic, level, client, model)
    except Exception as e:
        if _is_503(e):
            return None
        raise


def _save_validated(
    path: Path,
    topic: str,
    level: str,
    summary: dict,
    corrections_log: list,
    exclusions_log: list,
    validated_questions: list[dict],
    model_used: str,
) -> None:
    OUTPUT_VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "topic": topic,
        "level": level,
        "validation_model": model_used,
        "processing_summary": summary,
        "corrections_log": corrections_log,
        "exclusions_log": exclusions_log,
        "validated_questions": validated_questions,
    }, indent=2, ensure_ascii=False))
    print(f"  Saved validated JSON → {path.relative_to(ROOT)}")


def save_top_questions_picks(
    topic: str,
    level: str,
    slug: str,
    validated_questions: list[dict],
    n: int = 10,
) -> Path:
    """Persist the top-N hardest validated picks in a structured JSON the
    post-processor consumes directly. Skips the HTML LLM round-trip for the
    Top Questions tab.

    `validated_questions` is already grouped (one focus entry per parent) and
    sorted hardest first by `pipeline.group_and_sort`."""
    OUTPUT_VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_VALIDATED_DIR / f"top_questions_{slug}_{level.lower()}.json"

    picks = []
    for q in validated_questions[:n]:
        focus_sub = (q.get("sub_question_part") or "").lower() or None
        parts = []
        # Focus part as one row.
        if (q.get("question_text") or "").strip() or (q.get("model_answer") or "").strip():
            parts.append({
                "sub": focus_sub if focus_sub != "none" else None,
                "question_text": q.get("question_text", ""),
                "model_answer": q.get("model_answer", ""),
                "is_focus": True,
            })
        # Sibling sub-parts (other letters of the same parent).
        for s in q.get("sibling_parts", []) or []:
            sub = (s.get("sub_question_part") or "").lower() or None
            parts.append({
                "sub": sub if sub != "none" else None,
                "question_text": s.get("question_text", ""),
                "model_answer": s.get("model_answer", ""),
                "is_focus": False,
            })
        # Sort a, b, c, …, ai, aii, ci, cii (None/single-part sorts last).
        parts.sort(key=lambda p: (p.get("sub") is None, p.get("sub") or ""))
        all_parts = ",".join(p["sub"] for p in parts if p.get("sub"))

        picks.append({
            "school": q.get("school", ""),
            "parent_question_id": str(q.get("parent_question_id", "") or ""),
            "is_multipart": bool(q.get("is_multi_part") or len(parts) > 1),
            "focus_part": focus_sub if focus_sub != "none" else None,
            "all_parts": all_parts,
            "page_number": q.get("page_number"),
            "parent_bbox": q.get("parent_bbox"),
            "difficulty": q.get("difficulty"),
            "difficulty_score": q.get("difficulty_score"),
            "parts": parts,
        })

    out_path.write_text(json.dumps({
        "topic": topic,
        "level": level,
        "picks": picks,
    }, indent=2, ensure_ascii=False))
    print(f"  Saved Top Questions picks → {out_path.relative_to(ROOT)} "
          f"({len(picks)} pick(s))")
    return out_path


# ---------------------------------------------------------------------------
# Gemini validation
# ---------------------------------------------------------------------------

def validate_questions(
    questions: list[dict],
    topic: str,
    level: str,
    client: genai.Client,
    model: str = VALIDATION_MODEL,
) -> tuple[list[dict], list, list, dict]:
    """Returns (validated_questions, corrections_log, exclusions_log, verdict_map)."""
    prompt_template = VALIDATION_PROMPT_FILE.read_text()

    compact = [
        {
            "id": q["id"],
            "question_type": q["question_type"],
            "marks": q.get("marks", 1),
            "topic": q.get("topic_sub_category", ""),
            "question_text": q.get("question_text", ""),
            "model_answer": q.get("model_answer", ""),
        }
        for q in questions
    ]

    prompt = (
        prompt_template
        .replace("{TOPIC}", topic)
        .replace("{LEVEL}", level)
        .replace("{QUESTIONS_JSON}", json.dumps(compact, ensure_ascii=False, indent=2))
    )

    print(f"  Calling {model} for validation…")
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    result = json.loads(response.text)
    verdicts = {v["id"]: v for v in result.get("verdicts", [])}
    corrections_log = result.get("corrections_log", [])
    exclusions_log = result.get("exclusions_log", [])

    validated = []
    misaligned = 0
    garbled = 0
    corrections = 0

    for q in questions:
        v = verdicts.get(q["id"], {})
        alignment = v.get("alignment", "ALIGNED")
        accuracy = v.get("accuracy", "PASS")

        if alignment == "MISALIGNED":
            misaligned += 1
            continue
        if accuracy == "EXCLUDE":
            garbled += 1
            continue

        q = dict(q)
        correction_applied = False
        correction_note = ""

        if accuracy == "CORRECT" and v.get("correction"):
            q["model_answer"] = v["correction"]
            correction_applied = True
            correction_note = v["correction"]
            corrections += 1

        q["alignment"] = alignment
        q["correction_applied"] = correction_applied
        q["correction_note"] = correction_note
        q["difficulty_label"] = difficulty_label(q["difficulty_score"])
        validated.append(q)

    return validated, corrections_log, exclusions_log, {
        "misaligned_excluded": misaligned,
        "garbled_excluded": garbled,
        "corrections_applied": corrections,
    }


# ---------------------------------------------------------------------------
# HTML shell construction (Python-built skeleton, LLM-filled tab content)
# ---------------------------------------------------------------------------

def build_html_shell(topic: str, level: str, summary: dict, content: dict[str, str]) -> str:
    """Combine static shell template with per-tab LLM content.

    Substitutes:
      - {{TOPIC}}, {{LEVEL}}, {{LEVEL_NUM}}
      - {{TOTAL_QUESTIONS}}, {{TOTAL_SCHOOLS}}
      - {{STYLES}}, {{SCRIPT}}    (inline css/js bodies)
      - {{OVERVIEW}}, {{QUESTIONS}}, {{MISTAKES}}, {{DISTINCTIONS}},
        {{KEYWORDS}}, {{TRAPS}}, {{FORMULA}}, {{SUMMARY}}    (LLM content)
    """
    shell = SHELL_FILE.read_text()
    css = STYLES_FILE.read_text()
    js = SCRIPT_FILE.read_text().replace("{LEVEL}", level)

    total_questions = summary.get("final_usable_count", 0)
    total_schools = len(summary.get("schools_represented", []))
    level_num = re.sub(r"[^0-9]", "", level) or level  # P5 -> 5

    out = (
        shell
        .replace("{{STYLES}}", css)
        .replace("{{SCRIPT}}", js)
        .replace("{{TOPIC}}", topic)
        .replace("{{LEVEL_NUM}}", level_num)
        .replace("{{LEVEL}}", level)
        .replace("{{TOTAL_QUESTIONS}}", str(total_questions))
        .replace("{{TOTAL_SCHOOLS}}", str(total_schools))
    )

    for tab in DYNAMIC_TABS:
        placeholder = "{{" + tab.upper() + "}}"
        out = out.replace(placeholder, content.get(tab, f"<p style='color:#999;font-style:italic'>(no {tab} content generated)</p>"))

    # Top Questions tab is populated by top_questions_images.postprocess_cheatsheet
    # — leave the placeholder empty here so the post-processor has a clean target.
    out = out.replace("{{QUESTIONS}}", "")
    return out


# ---------------------------------------------------------------------------
# ConceptViz prompt extraction (post-processes HTML after LLM generation)
# ---------------------------------------------------------------------------

def _save_conceptviz_prompt(html: str, slug: str, level: str) -> None:
    """Extract the ConceptViz prompt text from the conceptviz-placeholder div
    and save it to output/conceptviz_prompts/{slug}_{level}.txt so the user
    can copy it into an external image generator."""
    pattern = (
        r'<div\s[^>]*class="conceptviz-placeholder"[^>]*'
        r'data-diagram-id="master-big-picture"[^>]*>([\s\S]*?)</div>'
    )
    m = re.search(pattern, html)
    if not m:
        return
    prompt_text = m.group(1).strip()
    if not prompt_text:
        return
    OUTPUT_CONCEPTVIZ_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_CONCEPTVIZ_PROMPTS_DIR / f"{slug}_{level.lower()}.txt"
    out_path.write_text(prompt_text + "\n", encoding="utf-8")
    print(f"  Saved ConceptViz prompt → {out_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# Gemini HTML generation (LLM produces JSON of per-tab HTML strings)
# ---------------------------------------------------------------------------

def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def generate_html(
    topic: str,
    level: str,
    validated_questions: list[dict],
    summary: dict,
    client: genai.Client,
    model: str = HTML_MODEL,
    thinking_level: "types.ThinkingLevel | None" = None,
) -> str:
    """Call the LLM for per-tab HTML content, then assemble final HTML via the shell.

    thinking_level: optional types.ThinkingLevel (e.g. types.ThinkingLevel.HIGH) — passed
    via ThinkingConfig for models that support deliberate reasoning (e.g. gemini-3-flash-preview).
    """
    available_classes = (
        AVAILABLE_CLASSES_FILE.read_text() if AVAILABLE_CLASSES_FILE.exists() else ""
    )
    conceptviz_req = (
        CONCEPTVIZ_REQUIREMENTS_FILE.read_text() if CONCEPTVIZ_REQUIREMENTS_FILE.exists() else ""
    )

    prompt_template = GENERATION_PROMPT_FILE.read_text()
    tier2_domains = select_tier2_domains(topic, validated_questions)
    tier2_docs, prompt_template = build_tier2_docs(prompt_template, tier2_domains)
    print(f"  Tier-2 helper domains: {tier2_domains or '(none — raw SVG fallback only)'}")

    prompt = (
        prompt_template
        .replace("{TIER2_HELPER_DOCS}", tier2_docs)
        .replace("{TOPIC}", topic)
        .replace("{LEVEL}", level)
        .replace("{SUMMARY_JSON}", json.dumps(summary, indent=2, ensure_ascii=False))
        .replace("{VALIDATED_JSON}", json.dumps(validated_questions, indent=2, ensure_ascii=False))
        .replace("{AVAILABLE_CLASSES}", available_classes)
        .replace("{CONCEPTVIZ_REQUIREMENTS}", conceptviz_req)
    )

    config_kwargs: dict = {
        "response_mime_type": "application/json",
        "max_output_tokens": 131072,
    }
    if thinking_level is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)

    label = f"{model}" + (f" (thinking={thinking_level.value})" if thinking_level else "")
    print(f"  Calling {label} for tab-content JSON…")
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )

    # Check finish reason before parsing — truncated JSON is never valid
    finish = None
    if response.candidates:
        finish = str(response.candidates[0].finish_reason)
    if finish and "MAX_TOKEN" in finish.upper():
        raise RuntimeError(
            f"Response truncated (finish_reason={finish}). "
            "Try splitting into fewer tabs or switching to a model with higher output limits."
        )

    raw = _strip_json_fences(response.text)
    try:
        content = json.loads(raw)
    except json.JSONDecodeError as e:
        head = raw[:300].replace("\n", " ")
        tail = raw[-200:].replace("\n", " ") if len(raw) > 300 else ""
        raise RuntimeError(
            f"LLM did not return valid JSON ({e}).\n"
            f"  finish_reason: {finish}\n"
            f"  first 300 chars: {head}\n"
            f"  last  200 chars: {tail}"
        ) from e

    if not isinstance(content, dict):
        raise RuntimeError(f"LLM returned non-object JSON (type={type(content).__name__})")

    missing = [t for t in DYNAMIC_TABS if t not in content]
    if missing:
        print(f"  WARNING: LLM omitted tabs: {missing}")

    content.pop("diagram_prompts", None)  # no longer used; prompts are embedded in div text
    return build_html_shell(topic, level, summary, content)


# ---------------------------------------------------------------------------
# Core pipeline for one topic (or one sub-topic in wide-theme mode)
# ---------------------------------------------------------------------------

def run_pipeline(
    topic: str,
    bank: str,
    questions: list[dict],
    total_in_jsonl: int,
    args,
    client: genai.Client,
    output_slug: str | None = None,
) -> None:
    level = level_from_bank(bank)
    slug = output_slug or topic_to_slug(topic)
    validated_path = OUTPUT_VALIDATED_DIR / f"validated_{slug}_{level.lower()}.json"
    force = getattr(args, "force_validation", False)

    validated_questions = None
    summary = None

    if not force and validated_path.exists():
        saved_data = json.loads(validated_path.read_text())
        saved_model = saved_data.get("validation_model", VALIDATION_MODEL)

        if saved_model == VALIDATION_MODEL:
            # Primary model was used last time — trust the saved data as-is
            validated_questions = saved_data["validated_questions"]
            summary = saved_data["processing_summary"]
            print(f"  Using saved validated JSON → {len(validated_questions)} question(s)")
        else:
            # Fallback model was used last time — try to upgrade with primary
            print(f"  Saved JSON used {saved_model}; trying upgrade with {VALIDATION_MODEL}…")
            processed = group_and_sort(questions, topic)
            if not processed:
                print("  No questions to process — skipping.")
                return
            result = _try_validate(VALIDATION_MODEL, processed, topic, level, client)
            if result is None:
                print(f"  {VALIDATION_MODEL} still unavailable — keeping existing validated JSON")
                validated_questions = saved_data["validated_questions"]
                summary = saved_data["processing_summary"]
            else:
                vq, cl, el, vc = result
                summary = build_processing_summary(topic, level, total_in_jsonl, processed)
                summary.update(vc)
                summary["final_usable_count"] = len(vq)
                validated_questions = vq
                _save_validated(validated_path, topic, level, summary, cl, el, vq, VALIDATION_MODEL)
                print(f"  Upgraded to {VALIDATION_MODEL} → {len(vq)} question(s)")

    if validated_questions is None:
        # Fresh validation run
        processed = group_and_sort(questions, topic)
        summary = build_processing_summary(topic, level, total_in_jsonl, processed)
        print(f"  {len(processed)} question(s) after grouping (from {total_in_jsonl} in JSONL)")
        if not processed:
            print("  No questions to process — skipping.")
            return

        # Try primary model, fall back to VALIDATION_FALLBACK on 503
        result = _try_validate(VALIDATION_MODEL, processed, topic, level, client)
        if result is None:
            print(f"  {VALIDATION_MODEL} unavailable (503), trying {VALIDATION_FALLBACK}…")
            result = _try_validate(VALIDATION_FALLBACK, processed, topic, level, client)
            if result is None:
                print(f"  {VALIDATION_FALLBACK} also unavailable — please try again later.",
                      file=sys.stderr)
                return
            model_used = VALIDATION_FALLBACK
        else:
            model_used = VALIDATION_MODEL

        vq, cl, el, vc = result
        summary.update(vc)
        summary["final_usable_count"] = len(vq)
        validated_questions = vq
        print(f"  {len(vq)} question(s) after validation (model: {model_used}, "
              f"{vc['misaligned_excluded']} excluded, {vc['corrections_applied']} corrected)")
        _save_validated(validated_path, topic, level, summary, cl, el, vq, model_used)

    if not validated_questions:
        print("  No validated questions — skipping HTML generation.")
        return

    # Persist the top-10 hardest picks for the Top Questions tab. The
    # post-processor consumes this JSON directly so the HTML LLM no longer
    # needs to render the Top Questions cards (saves ~5K output tokens/topic).
    save_top_questions_picks(topic, level, slug, validated_questions, n=10)

    OUTPUT_HTML_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_HTML_DIR / f"cheatsheet_{slug}_{level.lower()}.html"
    force_html = getattr(args, "force_html", False)

    if out_path.exists() and not force_html:
        print(f"  HTML already exists → {out_path.relative_to(ROOT)}")
        print("  Use --force-html to regenerate.")
        return

    # Pass FULL validated set to the LLM (it derives sub-topics from all questions)
    # but the prompt instructs it to render only the top 10 hardest in the Top Questions tab.
    # HTML generation with fallback on 503.
    try:
        html = generate_html(topic, level, validated_questions, summary, client)
    except Exception as e:
        if _is_503(e):
            print(f"  {HTML_MODEL} unavailable (503), trying {HTML_FALLBACK}…")
            html = generate_html(topic, level, validated_questions, summary, client,
                                 model=HTML_FALLBACK)
        else:
            raise

    out_path.write_text(html, encoding="utf-8")
    print(f"  Saved HTML → {out_path.relative_to(ROOT)}")
    _save_conceptviz_prompt(html, slug, level)

    # Replace the LLM's text-rendered Top Questions cards with cropped PDF
    # images + per-sub-part flip buttons. Imported lazily so pipeline.py keeps
    # starting fast when the post-processor isn't needed (e.g. on errors).
    try:
        from top_questions_images import postprocess_cheatsheet
        print("  Post-processing Top Questions tab (image crops + flips)…")
        postprocess_cheatsheet(out_path, slug, level, bank, client)
    except Exception as e:
        print(f"  WARNING: Top Questions post-processing failed: {e}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Science cheat sheets")
    parser.add_argument("--topic", required=True, help="Topic to process, e.g. 'Plant Reproduction'")
    parser.add_argument("--bank", required=True, choices=["p4", "p5"])
    parser.add_argument("--wide-theme", action="store_true",
                        help="Split by sub-topic, produce one HTML per sub-topic")
    parser.add_argument("--threshold", type=float, default=BROAD_THRESHOLD,
                        help=f"Broad filter threshold (default {BROAD_THRESHOLD}); lower to 0.20 if <5 matches")
    parser.add_argument("--force-validation", action="store_true",
                        help="Re-run Gemini validation even if saved validated JSON already exists")
    parser.add_argument("--force-html", action="store_true",
                        help="Regenerate HTML even if output file already exists")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    hf_token = os.getenv("HF_TOKEN", "").strip()
    if hf_token:
        os.environ["HF_TOKEN"] = hf_token
    client = genai.Client(api_key=api_key)
    level = level_from_bank(args.bank)

    # --- Fast path: validated JSON already exists with primary model ----------------
    # If the validated JSON for this topic already exists and was produced by
    # VALIDATION_MODEL, skip loading the master JSONL, the broad filter, and the
    # sentence transformer entirely — run_pipeline reads the cached data from disk.
    if not args.wide_theme and not args.force_validation:
        slug = topic_to_slug(args.topic)
        validated_path = OUTPUT_VALIDATED_DIR / f"validated_{slug}_{level.lower()}.json"
        if validated_path.exists():
            try:
                saved = json.loads(validated_path.read_text())
            except json.JSONDecodeError:
                saved = {}
            if saved.get("validation_model") == VALIDATION_MODEL:
                cached_total = saved.get("processing_summary", {}).get("total_in_jsonl", 0)
                print(f"Validated JSON exists ({validated_path.name}) with {VALIDATION_MODEL} — "
                      f"skipping JSONL load, broad filter & transformer.")
                print(f"\n--- Topic: {args.topic} ---")
                run_pipeline(args.topic, args.bank, [], cached_total, args, client)
                print("\nDone.")
                return
    # --------------------------------------------------------------------------------

    # Fresh validation needed — now load and filter questions
    all_questions = load_questions(args.bank)
    total = len(all_questions)
    if not all_questions:
        print(f"No questions found in banks/{args.bank}/master_questions.jsonl")
        sys.exit(1)

    print(f"Loaded {total} question(s) from {args.bank} bank.")

    filtered = broad_python_filter(all_questions, args.topic, level, args.threshold)

    if not filtered:
        print("No matching questions — try lowering --threshold")
        sys.exit(1)

    if args.wide_theme:
        # Split by topic_sub_category (question's own topic field)
        sub_groups: dict[str, list[dict]] = defaultdict(list)
        for q in filtered:
            sub_groups[q.get("topic", args.topic)].append(q)

        print(f"Wide theme: {len(sub_groups)} sub-topic(s)")
        main_slug = topic_to_slug(args.topic)
        for sub_topic, sub_questions in sorted(sub_groups.items()):
            if len(sub_questions) < 2:
                print(f"  Skipping '{sub_topic}' — only {len(sub_questions)} question(s)")
                continue
            print(f"\n--- Sub-topic: {sub_topic} ({len(sub_questions)} questions) ---")
            slug = f"{main_slug}_{topic_to_slug(sub_topic)}"
            run_pipeline(sub_topic, args.bank, sub_questions, total, args, client, output_slug=slug)
    else:
        print(f"\n--- Topic: {args.topic} ---")
        run_pipeline(args.topic, args.bank, filtered, total, args, client)

    print("\nDone.")


if __name__ == "__main__":
    main()
