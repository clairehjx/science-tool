#!/usr/bin/env python3
"""Generate two HTML files (questions + answer key) for OPEN-ENDED quiz topics.

Mirrors scripts/quiz_generator.py (which handles MCQ) but for open-ended questions.

For each topic, the script reads the EXISTING cheatsheet HTML
(`output/html/cheatsheet_{slug}_{level}.html`), pulls out the questions in the
"Top Questions" tab (school + verbatim question text + model answer + focus part),
joins each one to its validated-JSON record for the difficulty score, then
selects the top --count hardest with a semantic-diversity filter (skip any
candidate too similar to one already chosen, backfill from the skipped pool
if dedup is too aggressive). Each pick is cropped from its source PDF and
written to:

  output/quizzes/oe_quiz_{slug}.html       — questions, hardest first
  output/quizzes/oe_answers_{slug}.html    — table: Quiz # → model answer
  output/quizzes/screenshots/{bank}/{school}/oe_q{orig#}.png

Bbox lookups are cached in `output/quizzes/oe_bbox_cache.json`. The semantic
dedup uses the same `all-MiniLM-L6-v2` model pipeline.py uses for filtering.
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
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import level_from_bank, topic_to_slug   # noqa: E402
from pdf_extractor import parse_pdf_filename          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BANKS_DIR = ROOT / "banks"
HTML_DIR = ROOT / "output" / "html"
OUTPUT_DIR = ROOT / "output" / "quizzes"
BBOX_CACHE_FILE = OUTPUT_DIR / "oe_bbox_cache.json"

VISION_MODEL = "gemini-3-flash-preview"
VISION_FALLBACK = "gemini-2.5-flash"


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s


# ---------------------------------------------------------------------------
# School → PDF map
# ---------------------------------------------------------------------------

def build_school_pdf_map(bank: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    papers = BANKS_DIR / bank / "papers"
    if not papers.exists():
        return out
    for pdf in sorted(papers.glob("*.pdf")):
        school, _, _ = parse_pdf_filename(pdf.name)
        out[school] = pdf
    return out


def _resolve_pdf(school: str, school_pdf_map: dict[str, Path]) -> Path | None:
    if school in school_pdf_map:
        return school_pdf_map[school]
    norm = school.strip().lower()
    for k, v in school_pdf_map.items():
        if k.strip().lower() == norm:
            return v
    # Loose substring match (e.g., HTML says "ACS", PDF says "ACS Primary")
    for k, v in school_pdf_map.items():
        kn = k.strip().lower()
        if norm in kn or kn in norm:
            return v
    return None


# ---------------------------------------------------------------------------
# Cheatsheet HTML parsing — extract the 10 picked questions per topic
# ---------------------------------------------------------------------------

_PARTS_INFO_RE = re.compile(r"Parts:\s*([^|]+)\|\s*Focus:\s*Part\s*([a-z]+)", re.I)
_SOURCE_RE = re.compile(r"Source:\s*(.*?)\s*[·\-]", re.I)
# Capture letters until whitespace so "Part ci STATE" → "ci", "Part aii ..." → "aii".
_FOCUS_PART_RE = re.compile(r"Part\s+([a-z]+)(?=\s|$)", re.I)


def _is_part_card(tag) -> bool:
    if not getattr(tag, "get", None):
        return False
    classes = tag.get("class") or []
    return "context-part-card" in classes or "focus-part-card" in classes


def _extract_part_record(card) -> dict:
    """Return {sub, question_text, model_answer, is_focus} for one part-card."""
    classes = card.get("class") or []
    is_focus = "focus-part-card" in classes
    header = card.find("div",
                       class_=("focus-part-header" if is_focus else "context-part-header"))
    sub = None
    if header is not None:
        m = _FOCUS_PART_RE.search(_get_text(header))
        if m:
            sub = m.group(1).lower()
    qbox = card.find("div", class_="question-box")
    ans = card.find("div", class_="answer-correct-example")
    return {
        "sub": sub,
        "question_text": _get_text(qbox),
        "model_answer": _get_text(ans),
        "is_focus": is_focus,
    }


def _get_text(node) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


def parse_cheatsheet_top10(html_path: Path) -> list[dict]:
    """Return the up-to-10 questions in the Top Questions tab, in cheatsheet order."""
    soup = BeautifulSoup(html_path.read_text(), "html.parser")
    tab = soup.find(id="tab-questions")
    if tab is None:
        return []
    out: list[dict] = []
    for item in tab.find_all("div", class_="question-item"):
        record: dict = {}
        # Multi-part vs single-part
        wrapper = item.find("div", class_="multipart-wrapper")
        if wrapper:
            banner = wrapper.find("div", class_="multipart-banner")
            parts_info_el = banner.find("div", class_="multipart-parts-info") if banner else None
            source_el = banner.find("div", class_="multipart-source") if banner else None
            focus_card = wrapper.find("div", class_="focus-part-card")
            if focus_card is None:
                continue

            # School from "Source: {school} · P4"
            source_text = _get_text(source_el)
            m = _SOURCE_RE.search(source_text)
            record["school"] = (m.group(1).strip() if m
                              else source_text.replace("Source:", "").strip().split("·")[0].strip())

            # Focus part letter from "Parts: a, b, c | Focus: Part b"
            parts_text = _get_text(parts_info_el)
            m = _PARTS_INFO_RE.search(parts_text)
            record["focus_part"] = m.group(2).lower() if m else None
            record["all_parts"] = (m.group(1).strip() if m else "").replace(" ", "")

            # Focus question text + answer
            qbox = focus_card.find("div", class_="question-box")
            record["question_text"] = _get_text(qbox)
            ans = focus_card.find("div", class_="answer-correct-example")
            record["model_answer"] = _get_text(ans)

            # All parts (context + focus), each with its own answer; sorted a,b,c.
            parts: list[dict] = []
            for card in wrapper.find_all(_is_part_card):
                pr = _extract_part_record(card)
                if (pr.get("model_answer") or "").strip():
                    parts.append(pr)
            parts.sort(key=lambda p: (p.get("sub") is None, p.get("sub") or ""))
            record["parts"] = parts

            # Sibling parts text for vision prompt (helps Gemini locate the parent block).
            record["sibling_parts_text"] = " ".join(
                p["question_text"] for p in parts
                if not p["is_focus"] and p["question_text"]
            )
            record["is_multipart"] = True
        else:
            single = item.find("div", class_="single-question-card")
            if single is None:
                continue
            header = single.find("div", class_="focus-part-header")
            # School comes from the header's school badge
            record["school"] = ""
            if header:
                school_badge = header.find("span", class_="badge-school")
                record["school"] = _get_text(school_badge)
            qbox = single.find("div", class_="question-box")
            record["question_text"] = _get_text(qbox)
            ans = single.find("div", class_="answer-correct-example")
            record["model_answer"] = _get_text(ans)
            record["focus_part"] = None
            record["all_parts"] = ""
            record["sibling_parts_text"] = ""
            record["is_multipart"] = False
            record["parts"] = [{
                "sub": None,
                "question_text": record["question_text"],
                "model_answer": record["model_answer"],
                "is_focus": True,
            }]

        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Match HTML question to its parent_question_id via validated JSON
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _stem_key(s: str, n: int = 60) -> str:
    return _normalize(s)[:n]


def annotate_with_parent_ids(picked: list[dict], topic_slug: str, level: str) -> None:
    """Attach parent_question_id, page_number, and parent_bbox (when present in
    the validated JSON) to each picked record. parent_bbox is only present for
    PDFs extracted with the post-2026-05 prompt; older entries fall back to
    a vision call at crop time."""
    vpath = ROOT / "output" / "validated" / f"validated_{topic_slug}_{level.lower()}.json"
    if not vpath.exists():
        return
    try:
        validated = json.loads(vpath.read_text())
    except json.JSONDecodeError:
        return
    records = validated.get("validated_questions", [])
    by_school: dict[str, list[dict]] = {}
    for r in records:
        by_school.setdefault(_normalize(r.get("school", "")), []).append(r)

    def _find(target_stem: str, pool: list[dict]) -> dict | None:
        m = next((r for r in pool
                 if _stem_key(r.get("question_text", "")).startswith(target_stem[:30])), None)
        if m is None and target_stem[:25]:
            m = next((r for r in pool
                     if target_stem[:25] in _stem_key(r.get("question_text", ""))), None)
        return m

    for q in picked:
        target = _stem_key(q["question_text"])
        if not target:
            continue
        # First try the school's pool; if empty or no match, try every school.
        match = None
        if q.get("school"):
            match = _find(target, by_school.get(_normalize(q["school"]), []))
        if match is None:
            match = _find(target, records)
        if match is not None:
            q["parent_question_id"] = match.get("parent_question_id", "")
            q["sub_question_part"] = match.get("sub_question_part", "")
            if not q.get("school") and match.get("school"):
                q["school"] = match["school"]
            if match.get("difficulty") is not None:
                q["difficulty"] = match["difficulty"]
            if match.get("page_number") is not None:
                q["page_number"] = match["page_number"]
            if match.get("parent_bbox"):
                q["parent_bbox"] = match["parent_bbox"]


# ---------------------------------------------------------------------------
# Bounding-box cache + Gemini Vision lookup over the whole PDF
# ---------------------------------------------------------------------------

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


def _bbox_key(school: str, q: dict) -> str:
    pid = q.get("parent_question_id") or _stem_key(q["question_text"])[:40]
    return f"{school}#{pid}"


VISION_PROMPT_TEMPLATE = """You are given a Singapore Primary School Science exam PDF (Booklet B, open-ended). Find ONE specific question in this PDF and return its bounding box.

The question matches the text below. It may have multiple sub-parts (a, b, c, ...) — your bounding box must enclose ALL sub-parts of the same parent question, plus any shared diagram, but NOT the next parent question.

QUESTION (verbatim, focus part):
{stem}

OTHER SUB-PART TEXTS (same parent question):
{siblings}

Return ONLY a JSON object — no markdown fences, no commentary:
{{
  "page": <int 1-indexed PDF page where the question begins>,
  "top_y_norm": <int 0..1000, top edge of the parent question on that page>,
  "bottom_y_norm": <int 0..1000, bottom edge — if the question ends on a later page, set to 1000>,
  "ends_on_page": <int 1-indexed PDF page where the question ends — equal to "page" if it does not span pages>,
  "ends_at_y_norm": <int 0..1000, bottom edge on `ends_on_page`; equal to "bottom_y_norm" if no spanning>
}}

If the question cannot be found in the PDF, return all fields as null.
Coordinates are normalized to the page height (top of page = 0, bottom = 1000).
Be tight: do NOT include the next question or substantial blank margins.
"""


def detect_oe_bbox_vision(
    pdf_path: Path, q: dict, client: genai.Client,
) -> dict | None:
    """Upload the PDF and ask Gemini for page + bbox of the parent question.

    Returns a dict {page, top_y_norm, bottom_y_norm, ends_on_page, ends_at_y_norm}
    with floats normalized 0..1, or None on any failure.
    """
    print(f"      uploading {pdf_path.name} to Files API…")
    uploaded = client.files.upload(
        file=pdf_path,
        config=types.UploadFileConfig(mime_type="application/pdf"),
    )
    for _ in range(30):
        info = client.files.get(name=uploaded.name)
        if info.state.name == "ACTIVE":
            break
        if info.state.name == "FAILED":
            print(f"      Files API failed for {pdf_path.name}")
            return None
        time.sleep(2)

    prompt = VISION_PROMPT_TEMPLATE.format(
        stem=q["question_text"][:600],
        siblings=q.get("sibling_parts_text", "")[:600] or "(no other sub-parts)",
    )

    def _call(model: str):
        return client.models.generate_content(
            model=model,
            contents=[uploaded, prompt],
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
            print(f"      vision failed: {e}")
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            return None

    if fallback_reason is not None:
        print(f"      {VISION_MODEL} {fallback_reason}; falling back to {VISION_FALLBACK}…")
        try:
            response = _call(VISION_FALLBACK)
        except Exception as e:
            print(f"      vision fallback failed: {e}")
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            return None

    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    if response is None or not response.text:
        return None
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError:
        return None

    page = data.get("page")
    top = data.get("top_y_norm")
    bot = data.get("bottom_y_norm")
    ends_on = data.get("ends_on_page", page)
    ends_at = data.get("ends_at_y_norm", bot)
    if page is None or top is None or bot is None:
        return None
    try:
        return {
            "page": int(page),
            "top": max(0, min(1000, int(top))) / 1000.0,
            "bottom": max(0, min(1000, int(bot))) / 1000.0,
            "ends_on_page": int(ends_on if ends_on is not None else page),
            "ends_at": (max(0, min(1000, int(ends_at))) / 1000.0) if ends_at is not None
                       else (max(0, min(1000, int(bot))) / 1000.0),
        }
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------

def crop_with_bbox(pdf_path: Path, bbox: dict, out_png: Path, dpi: int) -> str:
    """Render the bbox region to a PNG. Stitches across pages if it spans."""
    doc = fitz.open(pdf_path)
    n_pages = doc.page_count
    p1 = max(1, min(n_pages, int(bbox["page"])))
    p_end = max(p1, min(n_pages, int(bbox.get("ends_on_page", p1))))

    page1 = doc[p1 - 1]
    h1, w1 = page1.rect.height, page1.rect.width
    top_y1 = max(0, bbox["top"] * h1 - 4)

    if p_end == p1:
        bot_y1 = min(h1, bbox["bottom"] * h1 + 4)
        if bot_y1 - top_y1 < 30:
            pix = page1.get_pixmap(dpi=dpi)
            note = f"vision-tiny, full-page p{p1}"
        else:
            pix = page1.get_pixmap(clip=fitz.Rect(0, top_y1, w1, bot_y1), dpi=dpi)
            note = f"vision p{p1} y={top_y1:.0f}-{bot_y1:.0f}"
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_png)
        doc.close()
        return note

    # Spans pages: render p1 from top_y1 to bottom, then page p_end from 0 to ends_at, stitch.
    pix1 = page1.get_pixmap(clip=fitz.Rect(0, top_y1, w1, h1), dpi=dpi)
    page2 = doc[p_end - 1]
    h2, w2 = page2.rect.height, page2.rect.width
    bot_y2 = min(h2, bbox.get("ends_at", 1.0) * h2 + 4)
    pix2 = page2.get_pixmap(clip=fitz.Rect(0, 0, w2, bot_y2), dpi=dpi)

    from PIL import Image
    import io
    img1 = Image.open(io.BytesIO(pix1.tobytes("png")))
    img2 = Image.open(io.BytesIO(pix2.tobytes("png")))
    width = max(img1.width, img2.width)
    canvas = Image.new("RGB", (width, img1.height + img2.height), (255, 255, 255))
    canvas.paste(img1, (0, 0))
    canvas.paste(img2, (0, img1.height))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, "PNG")
    doc.close()
    return f"vision spans p{p1}-p{p_end}"


def crop_full_page(pdf_path: Path, page_number: int, out_png: Path, dpi: int) -> str:
    doc = fitz.open(pdf_path)
    p1 = max(1, min(doc.page_count, page_number or 1))
    page = doc[p1 - 1]
    pix = page.get_pixmap(dpi=dpi)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    doc.close()
    return f"vision-failed, full-page p{p1}"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

QUIZ_STYLE = """
body { font-family: Georgia, "Times New Roman", serif; max-width: 880px;
       margin: 2em auto; padding: 0 1em; color: #222; line-height: 1.45; }
h1 { font-size: 1.5em; border-bottom: 2px solid #444; padding-bottom: 6px; }
h2 { font-size: 1.18em; margin-top: 2em; color: #234;
     padding: 8px 12px; background: #f5edff; border-left: 4px solid #B388FF;
     page-break-before: always; }
h2:first-of-type { page-break-before: auto; }
.summary { color: #666; font-size: 0.9em; margin-bottom: 0.5em; }
.q { margin: 1.2em 0 1.8em; page-break-inside: avoid; }
.q-num { font-weight: bold; font-size: 1.1em; color: #234; }
.q-meta { color: #888; font-size: 0.85em; margin-bottom: 6px; }
.q .focus { color: #B388FF; font-weight: 600; }
.q img { max-width: 100%; border: 1px solid #ddd;
         display: block; margin: 6px 0; border-radius: 4px; }
.footer { margin-top: 3em; color: #888; font-size: 0.85em;
          border-top: 1px solid #ccc; padding-top: 8px; }
@media print { body { max-width: none; } h2 { page-break-before: always; } }
"""

ANSWER_STYLE = """
body { font-family: Georgia, "Times New Roman", serif; max-width: 760px;
       margin: 2em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.4em; border-bottom: 2px solid #444; padding-bottom: 6px; }
h2 { font-size: 1.05em; margin-top: 1.6em; color: #234; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 1.5em; }
td, th { border: 1px solid #bbb; padding: 6px 10px; font-size: 0.95em;
         text-align: left; vertical-align: top; }
th { background: #f5edff; }
td.qn { width: 60px; font-weight: bold; }
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _safe_dirname(s: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", s).strip()


def render_quiz_html(topic_blocks: list[dict], bank: str, topics: list[str],
                     count: int) -> str:
    out = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        f"<title>{_esc(bank.upper())} Open-Ended Quiz — {_esc(', '.join(topics))}</title>",
        f"<style>{QUIZ_STYLE}</style>",
        "</head><body>",
        f"<h1>{_esc(bank.upper())} Science — Open-Ended Quiz</h1>",
        f"<div class='summary'>Generated {date.today().isoformat()} · "
        f"{len(topics)} topic(s) · top {count} hardest per topic, "
        f"deduped by concept similarity.</div>",
    ]
    for block in topic_blocks:
        out.append(f"<h2>Topic: {_esc(block['topic'])}</h2>")
        if not block["picked"]:
            out.append("<p><em>No questions found in cheatsheet HTML.</em></p>")
            continue
        for i, q in enumerate(block["picked"], 1):
            school = q.get("school", "")
            focus_part = q.get("focus_part")
            mp = q.get("is_multipart")
            parts_str = ""
            if mp and q.get("all_parts"):
                parts_str = (f" · Parts {q['all_parts']}, "
                            f"<span class='focus'>focus Part {focus_part}</span>")
            elif focus_part:
                parts_str = f" · <span class='focus'>focus Part {focus_part}</span>"
            out.append("<div class='q'>")
            out.append(f"  <div class='q-num'>{i}.</div>")
            out.append(
                f"  <div class='q-meta'>Source: {_esc(school)}{parts_str}</div>"
            )
            out.append(
                f"  <img src='{_esc(q['_img_rel'])}' alt='Q{i} from {_esc(school)}'>"
            )
            out.append("</div>")
    out.append(
        "<div class='footer'>Sorted hardest first; concept-similar duplicates "
        "removed via sentence-transformer cosine similarity.</div>"
    )
    out.append("</body></html>")
    return "\n".join(out)


def render_answers_html(topic_blocks: list[dict], bank: str, topics: list[str]) -> str:
    out = [
        "<!DOCTYPE html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        f"<title>Answer Key — {_esc(', '.join(topics))}</title>",
        f"<style>{ANSWER_STYLE}</style>",
        "</head><body>",
        f"<h1>{_esc(bank.upper())} Open-Ended Quiz — Answer Key</h1>",
    ]
    for block in topic_blocks:
        out.append(f"<h2>Topic: {_esc(block['topic'])}</h2>")
        if not block["picked"]:
            out.append("<p><em>No questions.</em></p>")
            continue
        out.append("<table>")
        out.append("<tr><th>Quiz #</th><th>Model Answer</th></tr>")
        for i, q in enumerate(block["picked"], 1):
            ans = q.get("model_answer", "") or "(answer missing in cheatsheet HTML)"
            out.append(
                f"<tr><td class='qn'>{i}</td><td>{_esc(ans)}</td></tr>"
            )
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Top-N hardest-with-diversity selection
# ---------------------------------------------------------------------------

_EMBEDDER = None  # lazy singleton


def _load_embedder():
    """Lazy-load all-MiniLM-L6-v2. Returns None on failure (we'll skip dedup)."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
        return _EMBEDDER
    except Exception as e:
        print(f"  (semantic dedup unavailable — {e}; falling back to plain top-N)")
        return None


def select_top_n_diverse(picked: list[dict], n: int, sim_threshold: float) -> list[dict]:
    """Pick top-N hardest, skipping concept-duplicates.

    Sort by difficulty desc (cheatsheet order as tiebreak / fallback when
    difficulty is missing), then walk the candidates; a candidate is skipped
    when its question_text embedding has cosine sim >= sim_threshold against
    any already-chosen question. Backfills from skipped pool if dedup is too
    aggressive."""
    if len(picked) <= n:
        return picked

    indexed = list(enumerate(picked))
    indexed.sort(key=lambda t: (-(t[1].get("difficulty") or 0), t[0]))
    candidates = [q for _, q in indexed]

    embedder = _load_embedder()
    if embedder is None:
        return candidates[:n]

    from sentence_transformers import util  # type: ignore

    chosen: list[dict] = []
    chosen_embs = []
    skipped: list[dict] = []
    for q in candidates:
        if len(chosen) >= n:
            break
        text = (q.get("question_text") or "").strip()
        if not text:
            continue
        emb = embedder.encode(text, convert_to_tensor=True, normalize_embeddings=True)
        if chosen_embs:
            max_sim = max(util.cos_sim(emb, ce).item() for ce in chosen_embs)
            if max_sim >= sim_threshold:
                snippet = re.sub(r"\s+", " ", text)[:60]
                diff = q.get("difficulty", "?")
                print(f"    dedup skip (diff={diff}, sim={max_sim:.2f}): {snippet}…")
                skipped.append(q)
                continue
        chosen.append(q)
        chosen_embs.append(emb)

    if len(chosen) < n and skipped:
        print(f"    backfilling {n - len(chosen)} pick(s) from dedup-skipped pool")
        for q in skipped:
            if len(chosen) >= n:
                break
            chosen.append(q)
    return chosen


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def _bbox_from_extraction(q: dict) -> dict | None:
    """Convert the parent_bbox emitted by the extraction prompt (page_number +
    parent_bbox{top_y_norm, bottom_y_norm, ends_on_page, ends_at_y_norm}) into
    the {page, top, bottom, ends_on_page, ends_at} shape that crop_with_bbox
    expects. Returns None if any required field is missing or malformed."""
    page = q.get("page_number")
    pb = q.get("parent_bbox") or {}
    top = pb.get("top_y_norm")
    bot = pb.get("bottom_y_norm")
    if page is None or top is None or bot is None:
        return None
    ends_on = pb.get("ends_on_page", page)
    ends_at = pb.get("ends_at_y_norm", bot)
    try:
        return {
            "page": int(page),
            "top": max(0, min(1000, int(top))) / 1000.0,
            "bottom": max(0, min(1000, int(bot))) / 1000.0,
            "ends_on_page": int(ends_on if ends_on is not None else page),
            "ends_at": (max(0, min(1000, int(ends_at))) / 1000.0) if ends_at is not None
                       else (max(0, min(1000, int(bot))) / 1000.0),
        }
    except (TypeError, ValueError):
        return None


def crop_picks(picked: list[dict], bank: str, school_pdf_map: dict[str, Path],
              client: genai.Client, bbox_cache: dict, dpi: int,
              force_screenshots: bool, force_bbox: bool) -> None:
    for q in picked:
        school = q.get("school", "school")
        pdf_path = _resolve_pdf(school, school_pdf_map)
        orig_id = q.get("parent_question_id") or _stem_key(q["question_text"])[:8]
        # Sanitise filename
        safe_id = re.sub(r"[^\w]", "_", str(orig_id))
        rel = Path("screenshots") / bank / _safe_dirname(school) / f"oe_q{safe_id}.png"
        out_png = OUTPUT_DIR / rel
        q["_img_rel"] = str(rel).replace("\\", "/")

        if out_png.exists() and not force_screenshots:
            print(f"    {school} Q{orig_id}: cached PNG")
            continue
        if pdf_path is None:
            print(f"    SKIP crop {school} Q{orig_id}: no PDF mapped")
            continue

        # Resolution order:
        #  1. parent_bbox baked in at extraction time (no API call)
        #  2. cached vision lookup from a previous run
        #  3. live vision call (and cache the result for next time)
        bbox = None
        source = None
        if not force_bbox:
            bbox = _bbox_from_extraction(q)
            if bbox is not None:
                source = "extraction"
        if bbox is None and not force_bbox:
            cache_key = _bbox_key(school, q)
            cached = bbox_cache.get(cache_key)
            if cached is not None:
                bbox = cached
                source = "vision-cache"
        if bbox is None:
            print(f"    {school} Q{orig_id}: vision-call…")
            bbox = detect_oe_bbox_vision(pdf_path, q, client)
            if bbox is not None:
                bbox_cache[_bbox_key(school, q)] = bbox
                save_bbox_cache(bbox_cache)
                source = "vision-live"

        if bbox is None:
            note = crop_full_page(pdf_path, 1, out_png, dpi)
        else:
            note = f"{source} · " + crop_with_bbox(pdf_path, bbox, out_png, dpi)
        print(f"    {school} Q{orig_id}: {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OE quiz HTML pair")
    parser.add_argument("--bank", required=True, choices=["p4", "p5"])
    parser.add_argument("--topics", nargs="+", required=True)
    parser.add_argument("--count", type=int, default=5,
                       help="Top-N hardest questions per topic (default 5)")
    parser.add_argument("--similarity", type=float, default=0.75,
                       help="Cosine-sim threshold for dedup; higher = more permissive "
                            "(default 0.75)")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--force-screenshots", action="store_true")
    parser.add_argument("--force-bbox", action="store_true",
                       help="Re-run Gemini Vision bbox lookups even if cached")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    level = level_from_bank(args.bank)
    school_pdf_map = build_school_pdf_map(args.bank)
    if not school_pdf_map:
        print(f"No PDFs found in banks/{args.bank}/papers — aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"Mapped {len(school_pdf_map)} school PDFs.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bbox_cache = load_bbox_cache()

    picks_dir = ROOT / "output" / "validated"
    topic_blocks: list[dict] = []
    for topic in args.topics:
        slug = topic_to_slug(topic)
        picks_path = picks_dir / f"top_questions_{slug}_{level.lower()}.json"
        print(f"\n--- Topic: {topic} ({picks_path.name}) ---")
        if not picks_path.exists():
            print(f"  Picks JSON not found at {picks_path.relative_to(ROOT)} — "
                  f"run pipeline.py first.")
            topic_blocks.append({"topic": topic, "picked": []})
            continue
        try:
            picked = json.loads(picks_path.read_text()).get("picks") or []
        except json.JSONDecodeError:
            print(f"  Could not parse {picks_path.name} — skipping.")
            topic_blocks.append({"topic": topic, "picked": []})
            continue
        if not picked:
            print(f"  No picks in {picks_path.name} — skipping.")
            topic_blocks.append({"topic": topic, "picked": []})
            continue

        # The diversity filter expects question_text and difficulty at the top
        # level. Hydrate from `parts` (focus first) and `difficulty_score`.
        for q in picked:
            parts = q.get("parts") or []
            focus = next((p for p in parts if p.get("is_focus")),
                         parts[0] if parts else {})
            if not q.get("question_text"):
                q["question_text"] = focus.get("question_text", "")
            if not q.get("model_answer"):
                q["model_answer"] = focus.get("model_answer", "")
            if q.get("difficulty") is None and q.get("difficulty_score") is not None:
                q["difficulty"] = q["difficulty_score"]
            q.setdefault("focus_part", focus.get("sub"))
            q.setdefault("is_multipart", q.get("is_multipart", False))

        print(f"  Loaded {len(picked)} pick(s) from picks JSON.")
        picked = select_top_n_diverse(picked, args.count, args.similarity)
        print(f"  Selected {len(picked)} after diversity filter (target {args.count}).")
        crop_picks(picked, args.bank, school_pdf_map, client, bbox_cache,
                  args.dpi, args.force_screenshots, args.force_bbox)
        topic_blocks.append({"topic": topic, "picked": picked})

    save_bbox_cache(bbox_cache)

    slug = "_".join(topic_to_slug(t) for t in args.topics)[:80]
    quiz_path = OUTPUT_DIR / f"oe_quiz_{slug}.html"
    answers_path = OUTPUT_DIR / f"oe_answers_{slug}.html"
    quiz_path.write_text(render_quiz_html(topic_blocks, args.bank, args.topics, args.count))
    answers_path.write_text(render_answers_html(topic_blocks, args.bank, args.topics))
    print(f"\nWrote {quiz_path.relative_to(ROOT)}")
    print(f"Wrote {answers_path.relative_to(ROOT)}")
    print("Done.")


if __name__ == "__main__":
    main()
