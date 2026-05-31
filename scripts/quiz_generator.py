#!/usr/bin/env python3
"""Generate two HTML files (questions + answer key) from MCQ banks.

Pipeline per topic:
  load master_mcq.jsonl → broad_python_filter (semantic + keyword)
                       → Gemini validation (cached)
                       → score_difficulty (MCQ branch)
                       → pick top N hardest
                       → sort ascending for display (1 = easiest of selected, N = hardest)
                       → crop tight PNG per question via PyMuPDF
                       → emit per-topic <h2> sections in quiz HTML and answer table HTML.

Outputs:
  output/quizzes/quiz_{slug}.html
  output/quizzes/answers_{slug}.html
  output/quizzes/screenshots/{bank}/{school}/q{orig#}.png
  output/validated_mcq/validated_{topic_slug}_{level}.json   (per-topic cache)
"""

import argparse
import html as _html
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import (             # noqa: E402
    BROAD_THRESHOLD,
    VALIDATION_MODEL,
    VALIDATION_FALLBACK,
    broad_python_filter,
    level_from_bank,
    score_difficulty,
    topic_to_slug,
)

ROOT = Path(__file__).resolve().parent.parent
BANKS_DIR = ROOT / "banks"
OUTPUT_DIR = ROOT / "output" / "quizzes"
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
VALIDATED_DIR = ROOT / "output" / "validated_mcq"
VALIDATION_PROMPT_FILE = ROOT / "prompts" / "mcq_validation_prompt.txt"
BBOX_CACHE_FILE = OUTPUT_DIR / "bbox_cache.json"

VISION_MODEL = "gemini-3-flash-preview"
VISION_FALLBACK = "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------

def load_mcqs(bank: str) -> list[dict]:
    path = BANKS_DIR / bank / "master_mcq.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        school = rec.get("school", "")
        pdf_filename = rec.get("pdf_filename", "")
        for q in rec.get("questions", []):
            q = dict(q)
            q.setdefault("school", school)
            q["_pdf_filename"] = pdf_filename
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# Validation (mirrors pipeline.py OE flow but for MCQ)
# ---------------------------------------------------------------------------

def _validate_mcq(
    questions: list[dict], topic: str, level: str, client: genai.Client, model: str
) -> tuple[list[dict], list, list, dict]:
    """Returns (kept, corrections_log, exclusions_log, counters). Raises on API error."""
    prompt_template = VALIDATION_PROMPT_FILE.read_text()
    compact = [
        {
            "id": q["id"],
            "topic": q.get("topic", ""),
            "question_text": q.get("question_text", ""),
            "options": q.get("options", {}),
            "correct_option": q.get("correct_option", ""),
        }
        for q in questions
    ]
    prompt = (
        prompt_template
        .replace("{TOPIC}", topic)
        .replace("{LEVEL}", level)
        .replace("{QUESTIONS_JSON}", json.dumps(compact, ensure_ascii=False, indent=2))
    )
    print(f"  Calling {model} for MCQ validation ({len(compact)} candidates)…")
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    result = json.loads(response.text)
    verdicts = {v["id"]: v for v in result.get("verdicts", [])}
    corrections_log = result.get("corrections_log", [])
    exclusions_log = result.get("exclusions_log", [])

    kept: list[dict] = []
    misaligned = 0
    excluded = 0
    corrected = 0
    for q in questions:
        v = verdicts.get(q["id"], {})
        alignment = v.get("alignment", "ALIGNED")
        ans = v.get("answer_check", "PASS")
        if alignment == "MISALIGNED":
            misaligned += 1
            continue
        if ans == "EXCLUDE":
            excluded += 1
            continue
        q = dict(q)
        if ans == "CORRECT" and v.get("correct_option_fix") in ("A", "B", "C", "D"):
            q["correct_option"] = v["correct_option_fix"]
            corrected += 1
        q["alignment"] = alignment
        kept.append(q)
    counters = {
        "misaligned_excluded": misaligned,
        "answer_excluded": excluded,
        "corrections_applied": corrected,
    }
    return kept, corrections_log, exclusions_log, counters


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s


def _try_validate(model, questions, topic, level, client):
    try:
        return _validate_mcq(questions, topic, level, client, model)
    except Exception as e:
        if _is_503(e):
            return None
        raise


def _save_validated_mcq(path: Path, topic: str, level: str, summary: dict,
                       corrections: list, exclusions: list,
                       kept: list[dict], model_used: str) -> None:
    VALIDATED_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "topic": topic,
        "level": level,
        "validation_model": model_used,
        "processing_summary": summary,
        "corrections_log": corrections,
        "exclusions_log": exclusions,
        "validated_questions": kept,
    }, indent=2, ensure_ascii=False))
    print(f"  Saved validated MCQ JSON → {path.relative_to(ROOT)}")


def _load_validated_mcq(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Bounding-box cache (per-question, persists across runs)
# ---------------------------------------------------------------------------

def _bbox_key(school: str, parent_qid: str) -> str:
    return f"{school}#{parent_qid}"


def load_bbox_cache() -> dict:
    if not BBOX_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(BBOX_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_bbox_cache(cache: dict) -> None:
    BBOX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BBOX_CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ---------------------------------------------------------------------------
# Vision-based bbox detection (used when PyMuPDF text-search misses on
# image-only PDFs — the common case for scanned Singapore exam papers).
# ---------------------------------------------------------------------------

VISION_PROMPT_TEMPLATE = """You are given an image of one page from a Singapore Primary School Science exam paper (Booklet A, multiple-choice).

Find the bounding box of the question whose stem and options match the text below. The bounding box must tightly enclose the ENTIRE question — the stem, any associated diagram or table, and ALL FOUR options.

QUESTION_NUMBER: {qid}
QUESTION_TEXT: {stem}

OPTIONS:
A: {opt_a}
B: {opt_b}
C: {opt_c}
D: {opt_d}

Return ONLY a JSON object — no markdown fences, no commentary:
{{
  "top_y_norm": <int 0..1000 — top edge of the question, 0 = top of page>,
  "bottom_y_norm": <int 0..1000 — bottom edge, 1000 = bottom of page>
}}

If the question is NOT on this page, return {{"top_y_norm": null, "bottom_y_norm": null}}.

Coordinates are normalized to the page height (independent of resolution).
Be tight: do not include the next question or page margins beyond a few percent of padding.
"""


def detect_question_bbox_vision(
    pdf_path: Path, page_number: int, q: dict, client: genai.Client,
) -> tuple[float, float] | None:
    """Render the page and ask Gemini for a normalized vertical bbox.

    Returns (top_norm_0_to_1, bottom_norm_0_to_1) or None on failure.
    Tries VISION_MODEL first, falls back to VISION_FALLBACK on 503 / empty response.
    """
    doc = fitz.open(pdf_path)
    if page_number < 1 or page_number > doc.page_count:
        doc.close()
        return None
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=120)
    png_bytes = pix.tobytes("png")
    doc.close()

    options = q.get("options", {}) or {}
    prompt = VISION_PROMPT_TEMPLATE.format(
        qid=q.get("parent_question_id", ""),
        stem=q.get("question_text", "")[:600],
        opt_a=options.get("A", "")[:200],
        opt_b=options.get("B", "")[:200],
        opt_c=options.get("C", "")[:200],
        opt_d=options.get("D", "")[:200],
    )
    img_part = types.Part.from_bytes(data=png_bytes, mime_type="image/png")

    def _call(model: str):
        return client.models.generate_content(
            model=model,
            contents=[img_part, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

    response = None
    fallback_reason = None
    try:
        response = _call(VISION_MODEL)
        if not (response.text and response.text.strip()):
            fallback_reason = "empty response"
    except Exception as e:
        if _is_503(e):
            fallback_reason = "503 unavailable"
        else:
            print(f"      vision call failed: {e}")
            return None

    if fallback_reason is not None:
        print(f"      {VISION_MODEL} {fallback_reason}; falling back to {VISION_FALLBACK}…")
        try:
            response = _call(VISION_FALLBACK)
        except Exception as e:
            print(f"      vision fallback failed: {e}")
            return None

    if response is None or not response.text:
        return None
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError:
        return None
    top = data.get("top_y_norm")
    bot = data.get("bottom_y_norm")
    if top is None or bot is None:
        return None
    try:
        top = max(0, min(1000, int(top))) / 1000.0
        bot = max(0, min(1000, int(bot))) / 1000.0
    except (TypeError, ValueError):
        return None
    if bot <= top:
        return None
    return top, bot


# ---------------------------------------------------------------------------
# PDF cropping
# ---------------------------------------------------------------------------

def _stem_probe(text: str, max_chars: int = 60) -> str:
    s = text.strip()
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > max_chars // 2 else cut


def _option_probe(opt: str, max_chars: int = 40) -> str:
    s = (opt or "").strip()
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > max_chars // 2 else cut


def _crop_with_pdf_text(doc, page_number: int, q: dict, out_png: Path,
                       dpi: int) -> tuple[bool, str] | None:
    """Try to crop via PDF text-layer search. Returns (ok, note) on success, None on miss."""
    n_pages = doc.page_count
    stem_probe = _stem_probe(q.get("question_text", ""))
    if not stem_probe:
        return None
    options = q.get("options", {}) or {}

    candidate_pages = [page_number, page_number - 1, page_number + 1]
    candidate_pages = [p for p in candidate_pages if 1 <= p <= n_pages]

    stem_top = None
    page = None
    page_idx = None
    for p1 in candidate_pages:
        page = doc[p1 - 1]
        rects = page.search_for(stem_probe)
        if rects:
            stem_top = rects[0].y0
            page_idx = p1
            break
    if stem_top is None or page is None or page_idx is None:
        return None

    bottom_y = None
    for letter in ("D", "C", "B"):
        probe = _option_probe(options.get(letter, ""))
        if not probe:
            continue
        rects = page.search_for(probe)
        below = [r for r in rects if r.y0 >= stem_top]
        if below:
            bottom_y = max(r.y1 for r in below)
            break

    next_q_top = None
    try:
        next_id = int(q.get("parent_question_id", "0")) + 1
        words = page.get_text("words")
        candidates = []
        for w in words:
            x0, y0, x1, y1, txt, *_ = w
            if y0 <= stem_top:
                continue
            if txt in (str(next_id), f"{next_id}.", f"{next_id})"):
                candidates.append(y0)
        if candidates:
            next_q_top = min(candidates)
    except (TypeError, ValueError):
        pass

    page_h = page.rect.height
    page_w = page.rect.width
    if bottom_y is None:
        bottom_y = stem_top + 260
    if next_q_top is not None:
        bottom_y = min(bottom_y, next_q_top - 4)
    bottom_y = min(bottom_y + 8, page_h - 16)
    top_y = max(stem_top - 8, 0)

    pix = page.get_pixmap(clip=fitz.Rect(0, top_y, page_w, bottom_y), dpi=dpi)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    return True, f"text-search p{page_idx}"


def _bbox_from_extraction(q: dict) -> tuple[float, float] | None:
    """Read top/bottom from the bbox baked in by the MCQ extraction prompt.
    Returns (top_norm_0_to_1, bottom_norm_0_to_1) or None when missing."""
    bb = q.get("bbox") or {}
    top = bb.get("top_y_norm")
    bot = bb.get("bottom_y_norm")
    if top is None or bot is None:
        return None
    try:
        top_n = max(0, min(1000, int(top))) / 1000.0
        bot_n = max(0, min(1000, int(bot))) / 1000.0
    except (TypeError, ValueError):
        return None
    if bot_n <= top_n:
        return None
    return top_n, bot_n


def crop_question(pdf_path: Path, page_number: int, q: dict, out_png: Path,
                 dpi: int, client: genai.Client | None,
                 bbox_cache: dict | None) -> tuple[bool, str]:
    """Crop a tight rect for the question and save as PNG.

    Strategy:
      1. Bbox baked in by the extraction prompt (no API call). Preferred.
      2. PyMuPDF text search (fast, works on PDFs with a text layer).
      3. Vision bbox detection (cached in bbox_cache, then live).
      4. Last resort: full-page render.
    """
    if not pdf_path.exists():
        return False, f"pdf not found: {pdf_path.name}"

    doc = fitz.open(pdf_path)
    n_pages = doc.page_count

    # 1) extraction-time bbox path
    extracted = _bbox_from_extraction(q)
    if extracted is not None:
        bbox_norm = extracted
        cache_note = "extraction"
        # Skip steps 2 and 3 — render directly.
        p1 = max(1, min(n_pages, page_number or 1))
        page = doc[p1 - 1]
        h, w = page.rect.height, page.rect.width
        top_y = max(0, bbox_norm[0] * h - 4)
        bot_y = min(h, bbox_norm[1] * h + 4)
        if bot_y - top_y >= 30:
            pix = page.get_pixmap(clip=fitz.Rect(0, top_y, w, bot_y), dpi=dpi)
            out_png.parent.mkdir(parents=True, exist_ok=True)
            pix.save(out_png)
            doc.close()
            return True, f"{cache_note} p{p1} y={top_y:.0f}-{bot_y:.0f}"
        # If the extracted bbox is implausibly tiny, fall through to text/vision.

    # 2) text-search path
    text_result = _crop_with_pdf_text(doc, page_number, q, out_png, dpi)
    if text_result is not None:
        ok, note = text_result
        doc.close()
        return ok, note

    # 3) vision path (cached)
    cache_key = _bbox_key(q.get("school", ""), q.get("parent_question_id", ""))
    bbox_norm = None
    if bbox_cache is not None and cache_key in bbox_cache:
        cached = bbox_cache[cache_key]
        bbox_norm = (cached["top"], cached["bottom"])
        cache_note = "vision-cache"
    elif client is not None:
        bbox_norm = detect_question_bbox_vision(pdf_path, page_number, q, client)
        if bbox_norm is not None and bbox_cache is not None:
            bbox_cache[cache_key] = {"top": bbox_norm[0], "bottom": bbox_norm[1]}
        cache_note = "vision-call"
    else:
        cache_note = "no-client"

    if bbox_norm is not None:
        p1 = max(1, min(n_pages, page_number or 1))
        page = doc[p1 - 1]
        h = page.rect.height
        w = page.rect.width
        top_y = max(0, bbox_norm[0] * h - 4)
        bot_y = min(h, bbox_norm[1] * h + 4)
        if bot_y - top_y < 30:
            # Implausibly tiny — bail to full page.
            pix = page.get_pixmap(dpi=dpi)
            out_png.parent.mkdir(parents=True, exist_ok=True)
            pix.save(out_png)
            doc.close()
            return True, f"vision-tiny, full-page p{p1}"
        pix = page.get_pixmap(clip=fitz.Rect(0, top_y, w, bot_y), dpi=dpi)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_png)
        doc.close()
        return True, f"{cache_note} p{p1} y={top_y:.0f}-{bot_y:.0f}"

    # 3) full-page fallback
    p1 = max(1, min(n_pages, page_number or 1))
    page = doc[p1 - 1]
    pix = page.get_pixmap(dpi=dpi)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    doc.close()
    return True, f"vision-failed, full-page p{p1}"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

QUIZ_STYLE = """
body { font-family: Georgia, "Times New Roman", serif; max-width: 880px;
       margin: 2em auto; padding: 0 1em; color: #222; line-height: 1.45; }
h1 { font-size: 1.5em; border-bottom: 2px solid #444; padding-bottom: 6px; }
h2 { font-size: 1.18em; margin-top: 2em; color: #234;
     padding: 8px 12px; background: #eef3fb; border-left: 4px solid #4D96FF;
     page-break-before: always; }
h2:first-of-type { page-break-before: auto; }
.summary { color: #666; font-size: 0.9em; margin-bottom: 0.5em; }
.q { margin: 1.2em 0 1.8em; page-break-inside: avoid; }
.q-num { font-weight: bold; font-size: 1.1em; color: #234; }
.q-meta { color: #888; font-size: 0.85em; margin-bottom: 6px; }
.q img { max-width: 100%; border: 1px solid #ddd;
         display: block; margin: 6px 0; border-radius: 4px; }
.footer { margin-top: 3em; color: #888; font-size: 0.85em;
          border-top: 1px solid #ccc; padding-top: 8px; }
@media print { body { max-width: none; } h2 { page-break-before: always; } }
"""

ANSWER_STYLE = """
body { font-family: Georgia, "Times New Roman", serif; max-width: 640px;
       margin: 2em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.4em; border-bottom: 2px solid #444; padding-bottom: 6px; }
h2 { font-size: 1.05em; margin-top: 1.6em; color: #234; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 1.5em; }
td, th { border: 1px solid #bbb; padding: 5px 10px; font-size: 0.95em; text-align: left; }
th { background: #eef3fb; }
.ans { font-weight: bold; }
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _safe_dirname(s: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", s).strip()


def render_quiz_html(topic_blocks: list[dict], slug: str, bank: str,
                    topics: list[str], count: int) -> str:
    out = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        f"<title>{_esc(bank.upper())} Quiz — {_esc(', '.join(topics))}</title>",
        f"<style>{QUIZ_STYLE}</style>",
        "</head><body>",
        f"<h1>{_esc(bank.upper())} Science Quiz</h1>",
        f"<div class='summary'>Generated {date.today().isoformat()} · "
        f"{len(topics)} topic(s) × up to {count} question(s) each · "
        f"ranked easiest → hardest within each topic.</div>",
    ]
    for block in topic_blocks:
        out.append(f"<h2>Topic: {_esc(block['topic'])}</h2>")
        if not block["picked"]:
            out.append("<p><em>No questions matched this topic.</em></p>")
            continue
        for i, q in enumerate(block["picked"], 1):
            school = q.get("school", "")
            orig_id = q.get("parent_question_id", "")
            img_rel = q["_img_rel"]
            out.append("<div class='q'>")
            out.append(f"  <div class='q-num'>{i}.</div>")
            out.append(
                f"  <div class='q-meta'>Source: {_esc(school)} · Q{_esc(orig_id)} "
                f"· difficulty {q['_difficulty']}</div>"
            )
            out.append(
                f"  <img src='{_esc(img_rel)}' alt='Q{_esc(orig_id)} from {_esc(school)}'>"
            )
            out.append("</div>")
    out.append(
        "<div class='footer'>Renumbered within each topic; "
        "1 = easiest of selected set, N = hardest.</div>"
    )
    out.append("</body></html>")
    return "\n".join(out)


def render_answers_html(topic_blocks: list[dict], slug: str, bank: str,
                       topics: list[str]) -> str:
    out = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        f"<title>Answer Key — {_esc(', '.join(topics))}</title>",
        f"<style>{ANSWER_STYLE}</style>",
        "</head><body>",
        f"<h1>{_esc(bank.upper())} Quiz — Answer Key</h1>",
    ]
    for block in topic_blocks:
        out.append(f"<h2>Topic: {_esc(block['topic'])}</h2>")
        if not block["picked"]:
            out.append("<p><em>No questions.</em></p>")
            continue
        out.append("<table>")
        out.append("<tr><th>Quiz #</th><th>Answer</th></tr>")
        for i, q in enumerate(block["picked"], 1):
            out.append(
                f"<tr><td>{i}</td>"
                f"<td class='ans'>{_esc(q.get('correct_option',''))}</td></tr>"
            )
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def select_for_topic(all_mcqs: list[dict], topic: str, level: str,
                    threshold: float, count: int, client: genai.Client,
                    force_validation: bool) -> tuple[list[dict], dict]:
    """Return (selected_questions_sorted_easy_to_hard, summary)."""
    slug = topic_to_slug(topic)
    cache_path = VALIDATED_DIR / f"validated_{slug}_{level.lower()}.json"

    # Fast path: cached validated set with the primary model.
    if not force_validation:
        cached = _load_validated_mcq(cache_path)
        if cached and cached.get("validation_model") == VALIDATION_MODEL:
            kept = cached["validated_questions"]
            print(f"  Topic {topic!r}: using cached validated MCQs "
                  f"({len(kept)} questions)")
            return _rank_and_pick(kept, count), cached.get("processing_summary", {})

    # Broad filter from raw MCQs.
    candidates = broad_python_filter(all_mcqs, topic, level, threshold)
    if not candidates:
        print(f"  Topic {topic!r}: 0 candidates after broad filter — skipping.")
        return [], {}

    # Assign stable IDs for the validation round-trip.
    for i, q in enumerate(candidates, 1):
        q["id"] = f"Q{i:03d}"

    # Validation with primary then fallback model.
    result = _try_validate(VALIDATION_MODEL, candidates, topic, level, client)
    if result is None:
        print(f"  {VALIDATION_MODEL} unavailable, trying {VALIDATION_FALLBACK}…")
        result = _try_validate(VALIDATION_FALLBACK, candidates, topic, level, client)
        if result is None:
            print(f"  Both validation models unavailable. Falling back to broad-filtered set.")
            kept = candidates
            counters = {"misaligned_excluded": 0, "answer_excluded": 0,
                        "corrections_applied": 0}
            corrections = []
            exclusions = []
            model_used = "broad-filter-only"
        else:
            kept, corrections, exclusions, counters = result
            model_used = VALIDATION_FALLBACK
    else:
        kept, corrections, exclusions, counters = result
        model_used = VALIDATION_MODEL

    summary = {
        "topic": topic,
        "level": level,
        "broad_filter_count": len(candidates),
        "validated_count": len(kept),
        **counters,
    }
    _save_validated_mcq(cache_path, topic, level, summary, corrections, exclusions,
                        kept, model_used)
    return _rank_and_pick(kept, count), summary


def _rank_and_pick(kept: list[dict], count: int) -> list[dict]:
    """Score, take top N hardest, return sorted ASC for display."""
    for q in kept:
        q["_difficulty"] = score_difficulty(q)
    kept_sorted = sorted(kept, key=lambda q: q["_difficulty"], reverse=True)
    top = kept_sorted[:count]
    return sorted(top, key=lambda q: q["_difficulty"])


def crop_all(picked: list[dict], bank: str, dpi: int, force: bool,
            client: genai.Client, bbox_cache: dict) -> None:
    """Populate q['_img_rel'] (path relative to OUTPUT_DIR) for each picked question."""
    for q in picked:
        school = q.get("school", "school")
        pdf_filename = q.get("_pdf_filename", "")
        pdf_path = BANKS_DIR / bank / "papers" / pdf_filename if pdf_filename else None
        orig_id = q.get("parent_question_id", "X")
        rel = Path("screenshots") / bank / _safe_dirname(school) / f"q{orig_id}.png"
        out_png = OUTPUT_DIR / rel
        q["_img_rel"] = str(rel).replace("\\", "/")
        if out_png.exists() and not force:
            continue
        if pdf_path is None or not pdf_path.exists():
            print(f"    SKIP crop {school} Q{orig_id}: pdf not found")
            continue
        ok, note = crop_question(
            pdf_path, int(q.get("page_number", 1)), q, out_png,
            dpi=dpi, client=client, bbox_cache=bbox_cache,
        )
        print(f"    {school} Q{orig_id}: {note}")
        # Persist cache after each successful vision call so a crash mid-run keeps progress.
        save_bbox_cache(bbox_cache)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate MCQ quiz HTML pair")
    parser.add_argument("--bank", required=True, choices=["p4", "p5"])
    parser.add_argument("--topics", nargs="+", required=True,
                       help="One or more topic names (quoted if multi-word)")
    parser.add_argument("--count", type=int, default=10,
                       help="Hardest N MCQs per topic (default 10)")
    parser.add_argument("--threshold", type=float, default=BROAD_THRESHOLD,
                       help=f"Broad filter threshold (default {BROAD_THRESHOLD})")
    parser.add_argument("--dpi", type=int, default=200,
                       help="PNG render DPI (default 200)")
    parser.add_argument("--force-screenshots", action="store_true",
                       help="Overwrite cached PNG crops")
    parser.add_argument("--force-validation", action="store_true",
                       help="Re-run Gemini validation even if cached")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    level = level_from_bank(args.bank)
    all_mcqs = load_mcqs(args.bank)
    if not all_mcqs:
        print(f"No MCQs found in banks/{args.bank}/master_mcq.jsonl. "
              f"Run scripts/mcq_extractor.py --bank {args.bank} first.")
        sys.exit(1)
    print(f"Loaded {len(all_mcqs)} MCQ(s) from {args.bank} bank.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bbox_cache = load_bbox_cache()

    topic_blocks: list[dict] = []
    for topic in args.topics:
        print(f"\n--- Topic: {topic} ---")
        picked, summary = select_for_topic(
            all_mcqs, topic, level, args.threshold, args.count, client,
            args.force_validation,
        )
        print(f"  Picked {len(picked)} of {summary.get('validated_count', '?')} validated")
        if picked:
            crop_all(picked, args.bank, args.dpi, args.force_screenshots,
                    client, bbox_cache)
        topic_blocks.append({"topic": topic, "picked": picked, "summary": summary})

    save_bbox_cache(bbox_cache)

    slug = "_".join(topic_to_slug(t) for t in args.topics)[:80]
    quiz_html = render_quiz_html(topic_blocks, slug, args.bank, args.topics, args.count)
    answers_html = render_answers_html(topic_blocks, slug, args.bank, args.topics)
    quiz_path = OUTPUT_DIR / f"quiz_{slug}.html"
    answers_path = OUTPUT_DIR / f"answers_{slug}.html"
    quiz_path.write_text(quiz_html)
    answers_path.write_text(answers_html)
    print(f"\nWrote {quiz_path.relative_to(ROOT)}")
    print(f"Wrote {answers_path.relative_to(ROOT)}")
    print("Done.")


if __name__ == "__main__":
    main()
