#!/usr/bin/env python3
"""Populate the Top Questions tab in a cheatsheet HTML using the structured
picks JSON pipeline.py emits at `output/validated/top_questions_{slug}_{level}.json`.

For each pick: crop the parent-question region from the source PDF, render
a `<div class="question-item">` containing the image + one "Show model
answer" flip button per sub-part, and inject into `#tab-questions`. The
LLM no longer renders this tab — pipeline.py drops the "questions" key
from the HTML generation prompt to save ~5K output tokens per cheatsheet.

Reuses the bbox + crop logic from `oe_quiz_generator.py` (extraction-baked
parent_bbox → vision cache → live vision call). PNGs land in the shared
`output/quizzes/screenshots/{bank}/{school}/oe_q{N}.png` location and are
referenced from the cheatsheet via the `output/html/screenshots` symlink.

Called automatically by `pipeline.py` at the end of HTML generation. Can also
be run standalone to refresh existing cheatsheets:

    python scripts/top_questions_images.py --bank p4 --topics "Matter" "Heat"
    python scripts/top_questions_images.py --bank p4 --topics ALL
"""

import argparse
import html as _html
import json
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oe_quiz_generator import (    # noqa: E402
    build_school_pdf_map,
    crop_full_page,
    crop_with_bbox,
    detect_oe_bbox_vision,
    load_bbox_cache,
    save_bbox_cache,
    _bbox_from_extraction,
    _bbox_key,
    _resolve_pdf,
    _safe_dirname,
    _stem_key,
)
from pipeline import level_from_bank, topic_to_slug   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = ROOT / "output" / "html"
VALIDATED_DIR = ROOT / "output" / "validated"
QUIZZES_DIR = ROOT / "output" / "quizzes"
# Cheatsheet HTMLs reference cropped images via a sibling symlink instead of
# `../quizzes/screenshots/...` — the parent-traversal form 404s under
# `python -m http.server --directory output/html`.
HTML_SCREENSHOTS_LINK = HTML_DIR / "screenshots"


def _ensure_screenshots_symlink() -> None:
    """Make output/html/screenshots a symlink to ../quizzes/screenshots so image
    URLs work whether served from output/, output/html/, or file://."""
    if HTML_SCREENSHOTS_LINK.is_symlink():
        return
    if HTML_SCREENSHOTS_LINK.exists():
        return  # something else lives there; don't clobber it
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    HTML_SCREENSHOTS_LINK.symlink_to(Path("..") / "quizzes" / "screenshots")

# Sentinel attributes so we can detect previous post-processing and avoid
# stacking identical <style>/<script> tags on re-runs.
STYLE_TAG = "qi-postprocessed"
SCRIPT_TAG = "qi-postprocessed"

TOP_Q_STYLE = """
#tab-questions .question-item .qi-meta { display: flex; gap: 14px; align-items: center;
                          margin-bottom: 12px; font-size: 0.9em; color: #555;
                          flex-wrap: wrap; }
#tab-questions .question-item .qi-badge-school { background: #f5edff; color: #4a2d8a;
                          padding: 3px 12px; border-radius: 12px;
                          font-weight: 600; font-size: 0.92em; }
#tab-questions .question-item .qi-parts { color: #888; }
#tab-questions .question-item .qi-img { max-width: 100%; display: block;
                         border: 1px solid #ccc; border-radius: 6px;
                         margin: 0 0 16px; cursor: zoom-in; }
#tab-questions .question-item .qi-noimg { padding: 20px; background: #fff8e6;
                         border: 1px dashed #d4a017; border-radius: 6px;
                         color: #8a6300; margin-bottom: 14px; }
#tab-questions .question-item .qi-answers { display: flex; flex-direction: column;
                         gap: 8px; }
#tab-questions .question-item .qi-row { display: flex; flex-direction: column; gap: 4px; }
#tab-questions .question-item .qi-flip { padding: 9px 14px; background: #B388FF;
                         color: #fff; border: none; border-radius: 5px;
                         cursor: pointer; font-size: 0.92em; text-align: left;
                         font-weight: 500; font-family: inherit;
                         transition: background 0.15s; }
#tab-questions .question-item .qi-flip:hover { background: #9970e0; }
#tab-questions .question-item .qi-flip.is-open { background: #6a4cb0; }
#tab-questions .question-item .qi-ans { padding: 12px 14px; background: #faf7ff;
                         border-left: 3px solid #B388FF; border-radius: 0 5px 5px 0;
                         font-size: 0.95em; line-height: 1.55; color: #2a2a3a;
                         white-space: pre-wrap; }
"""

TOP_Q_SCRIPT = """
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.qi-flip');
  if (!btn) return;
  var target = document.getElementById(btn.dataset.tgt);
  if (!target) return;
  var hidden = target.hasAttribute('hidden');
  if (hidden) {
    target.removeAttribute('hidden');
    btn.classList.add('is-open');
    btn.textContent = btn.textContent.replace(/^▸ Show/, '▾ Hide');
  } else {
    target.setAttribute('hidden', '');
    btn.classList.remove('is-open');
    btn.textContent = btn.textContent.replace(/^▾ Hide/, '▸ Show');
  }
});
"""


def _ans_id(slug: str, school: str, parent_id: str, sub: str, idx: int) -> str:
    school_clean = re.sub(r"[^a-z0-9]", "", (school or "").lower())[:12]
    sub_clean = re.sub(r"[^a-z0-9]", "", (sub or "").lower())
    return f"qi-ans-{slug}-{school_clean}-{idx}-q{parent_id}{sub_clean}"


def render_question_item(q: dict, slug: str, idx: int) -> str:
    """Render ONE question-item card (image + per-sub-part flip buttons).
    Class is `question-item` so the cheatsheet's existing `navigateItem`
    machinery cycles through cards."""
    school = q.get("school", "")
    img_rel = q.get("_img_rel", "")
    parent_id = str(q.get("parent_question_id", "") or "")
    all_parts = q.get("all_parts", "")
    is_multi = bool(q.get("is_multipart"))
    sub_parts = q.get("parts") or []

    out = ['<div class="question-item">']
    out.append('  <div class="qi-meta">')
    if school:
        out.append(f'    <span class="qi-badge-school">{_html.escape(school)}</span>')
    meta_bits = []
    if parent_id:
        meta_bits.append(f"Q{_html.escape(parent_id)}")
    if is_multi and all_parts:
        meta_bits.append(f"Parts: {_html.escape(all_parts)}")
    if meta_bits:
        out.append(f'    <span class="qi-parts">{" · ".join(meta_bits)}</span>')
    out.append('  </div>')

    if img_rel:
        alt = _html.escape(f"{school} Q{parent_id}".strip())
        out.append(
            f'  <img class="qi-img" src="{_html.escape(img_rel)}" alt="{alt}" '
            f'onclick="openFullscreen(this)">'
        )
    else:
        out.append(
            '  <div class="qi-noimg">No image available — '
            'the source PDF could not be matched or cropped.</div>'
        )

    answer_rows = []
    for p in sub_parts:
        ans = (p.get("model_answer") or "").strip()
        if not ans:
            continue
        sub = (p.get("sub") or "").lower()
        ans_id = _ans_id(slug, school, parent_id or "x", sub, idx)
        label = (f"Show model answer for Part {sub}" if sub
                 else "Show model answer")
        answer_rows.append(
            '    <div class="qi-row">\n'
            f'      <button class="qi-flip" data-tgt="{ans_id}">▸ {label}</button>\n'
            f'      <div class="qi-ans" id="{ans_id}" hidden>{_html.escape(ans)}</div>\n'
            '    </div>'
        )

    if not answer_rows:
        # Fallback: cheatsheet had no sibling answers — emit at least the focus answer.
        fallback_ans = (q.get("model_answer") or "").strip()
        if fallback_ans:
            sub = (q.get("focus_part") or "").lower()
            ans_id = _ans_id(slug, school, parent_id or "x", sub, idx)
            label = (f"Show model answer for Part {sub}" if sub
                     else "Show model answer")
            answer_rows.append(
                '    <div class="qi-row">\n'
                f'      <button class="qi-flip" data-tgt="{ans_id}">▸ {label}</button>\n'
                f'      <div class="qi-ans" id="{ans_id}" hidden>'
                f'{_html.escape(fallback_ans)}</div>\n'
                '    </div>'
            )

    if answer_rows:
        out.append('  <div class="qi-answers">')
        out.extend(answer_rows)
        out.append('  </div>')

    out.append('</div>')
    return "\n".join(out)


def _ensure_nav_scaffold(soup: BeautifulSoup, tab, level: str):
    """Return the questions-list container, creating section-title + nav-btns
    + #questions-list inside `tab` if any are missing."""
    title = tab.find("h2", class_="section-title")
    if title is None:
        title = soup.new_tag("h2")
        title.attrs["class"] = "section-title"
        title.string = f"Top {level.upper()} Open-Ended Questions"
        tab.insert(0, title)

    nav = tab.find("div", class_="nav-btns")
    if nav is None:
        nav_html = (
            '<div class="nav-btns">'
            '<button class="nav-btn" id="question-prev" '
            'onclick="navigateItem(\'question\', -1)">← Previous</button>'
            '<span id="question-counter" style="font-weight:700;color:#666"></span>'
            '<button class="nav-btn" id="question-next" '
            'onclick="navigateItem(\'question\', 1)">Next →</button>'
            '</div>'
        )
        nav_frag = BeautifulSoup(nav_html, "html.parser")
        title.insert_after(nav_frag.div)
        nav = tab.find("div", class_="nav-btns")

    qlist = tab.find(id="questions-list")
    if qlist is None:
        qlist = soup.new_tag("div")
        qlist.attrs["id"] = "questions-list"
        nav.insert_after(qlist)
    return qlist


def _hydrate_for_crop(q: dict) -> None:
    """Picks JSON entries don't carry a top-level question_text or
    sibling_parts_text — derive them from `parts` so vision/cropping helpers
    that expect those fields keep working."""
    parts = q.get("parts") or []
    focus = next((p for p in parts if p.get("is_focus")),
                 parts[0] if parts else None)
    if focus and not q.get("question_text"):
        q["question_text"] = focus.get("question_text", "")
    if not q.get("sibling_parts_text"):
        q["sibling_parts_text"] = " ".join(
            (p.get("question_text") or "")
            for p in parts
            if p is not focus and p.get("question_text")
        )


def _crop_picks(picked: list[dict], bank: str,
                school_pdf_map: dict[str, Path],
                client: genai.Client | None,
                bbox_cache: dict, dpi: int,
                force_screenshots: bool, force_bbox: bool) -> None:
    """Set q['_img_rel'] (cheatsheet-relative) for every pick, cropping when needed."""
    for q in picked:
        _hydrate_for_crop(q)
        school = q.get("school", "school")
        pdf_path = _resolve_pdf(school, school_pdf_map)
        orig_id = q.get("parent_question_id") or _stem_key(q["question_text"])[:8]
        safe_id = re.sub(r"[^\w]", "_", str(orig_id))
        rel_under_quizzes = (
            Path("screenshots") / bank / _safe_dirname(school) / f"oe_q{safe_id}.png"
        )
        out_png = QUIZZES_DIR / rel_under_quizzes
        # Cheatsheet HTMLs reference images via the screenshots/ symlink that
        # _ensure_screenshots_symlink() points at output/quizzes/screenshots.
        q["_img_rel"] = str(rel_under_quizzes).replace("\\", "/")

        if out_png.exists() and not force_screenshots:
            print(f"    {school} Q{orig_id}: cached PNG")
            continue
        if pdf_path is None:
            print(f"    SKIP crop {school} Q{orig_id}: no PDF mapped")
            q["_img_rel"] = ""
            continue

        bbox = None
        source = None
        if not force_bbox:
            bbox = _bbox_from_extraction(q)
            if bbox is not None:
                source = "extraction"
        if bbox is None and not force_bbox:
            cached = bbox_cache.get(_bbox_key(school, q))
            if cached is not None:
                bbox = cached
                source = "vision-cache"
        if bbox is None:
            if client is None:
                print(f"    SKIP crop {school} Q{orig_id}: no client for vision call")
                q["_img_rel"] = ""
                continue
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


def postprocess_cheatsheet(html_path: Path, slug: str, level: str, bank: str,
                           client: genai.Client | None = None,
                           force_screenshots: bool = False,
                           force_bbox: bool = False, dpi: int = 200) -> bool:
    """Rewrite the Top Questions tab to use cropped images + flip buttons.

    Idempotent — running it twice produces the same final HTML."""
    if not html_path.exists():
        print(f"  postprocess: HTML missing — {html_path}")
        return False

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    tab = soup.find(id="tab-questions")
    if tab is None:
        print(f"  postprocess: no #tab-questions in {html_path.name}")
        return False

    picks_path = (VALIDATED_DIR /
                  f"top_questions_{slug}_{level.lower()}.json")
    legacy_sidecar = html_path.parent / f".{html_path.stem}.picks.json"
    picked = None
    if picks_path.exists():
        try:
            picked = json.loads(picks_path.read_text()).get("picks") or []
        except json.JSONDecodeError:
            picked = None
    if not picked and legacy_sidecar.exists():
        try:
            picked = json.loads(legacy_sidecar.read_text())
            print(f"  postprocess: using legacy sidecar {legacy_sidecar.name} "
                  f"(re-run pipeline.py to regenerate {picks_path.name})")
        except json.JSONDecodeError:
            picked = None
    if not picked:
        print(f"  postprocess: no picks JSON at {picks_path.relative_to(ROOT)} — "
              f"re-run pipeline.py to generate it")
        return False

    school_pdf_map = build_school_pdf_map(bank)
    bbox_cache = load_bbox_cache()

    _ensure_screenshots_symlink()
    _crop_picks(picked, bank, school_pdf_map, client, bbox_cache,
                dpi, force_screenshots, force_bbox)

    qlist = _ensure_nav_scaffold(soup, tab, level)
    # Remove every existing .question-item under the tab — some cheatsheets
    # emit them as direct children of #tab-questions (siblings of an empty
    # #questions-list) rather than inside the list.
    for stale in tab.find_all("div", class_="question-item"):
        stale.extract()
    for idx, q in enumerate(picked, 1):
        card_html = render_question_item(q, slug, idx)
        frag = BeautifulSoup(card_html, "html.parser")
        for el in list(frag.contents):
            qlist.append(el)

    head = soup.find("head")
    if head is not None:
        existing = head.find("style", attrs={"data-tag": STYLE_TAG})
        if existing is None:
            style_tag = soup.new_tag("style")
            style_tag.attrs["data-tag"] = STYLE_TAG
            style_tag.string = TOP_Q_STYLE
            head.append(style_tag)

    body = soup.find("body")
    if body is not None:
        existing = body.find("script", attrs={"data-tag": SCRIPT_TAG})
        if existing is None:
            script_tag = soup.new_tag("script")
            script_tag.attrs["data-tag"] = SCRIPT_TAG
            script_tag.string = TOP_Q_SCRIPT
            body.append(script_tag)

    html_path.write_text(str(soup), encoding="utf-8")
    return True


def _resolve_topics(level: str, topics_arg: list[str]) -> list[str]:
    """Translate the --topics argument into a list of cheatsheet slugs."""
    if [t.upper() for t in topics_arg] == ["ALL"]:
        slugs = []
        for p in sorted(HTML_DIR.glob(f"cheatsheet_*_{level.lower()}.html")):
            m = re.match(rf"cheatsheet_(.+)_{level.lower()}\.html$", p.name)
            if m:
                slugs.append(m.group(1))
        return slugs
    return [topic_to_slug(t) for t in topics_arg]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject cropped images + per-sub-part flip buttons into "
                    "the Top Questions tab of cheatsheet HTML(s)."
    )
    parser.add_argument("--bank", required=True, choices=["p4", "p5"])
    parser.add_argument(
        "--topics", nargs="+", required=True,
        help='Topic names matching cheatsheet slugs, or "ALL" to retrofit '
             'every cheatsheet for the given bank.',
    )
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--force-screenshots", action="store_true")
    parser.add_argument("--force-bbox", action="store_true")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    client = None
    if api_key and api_key != "your_key_here":
        client = genai.Client(api_key=api_key)

    level = level_from_bank(args.bank)
    slugs = _resolve_topics(level, args.topics)
    if not slugs:
        print("No matching cheatsheet HTMLs found.")
        return

    for slug in slugs:
        html_path = HTML_DIR / f"cheatsheet_{slug}_{level.lower()}.html"
        print(f"\n--- {html_path.name} ---")
        if not html_path.exists():
            print("  Missing — skipping.")
            continue
        ok = postprocess_cheatsheet(
            html_path, slug, level, args.bank, client,
            force_screenshots=args.force_screenshots,
            force_bbox=args.force_bbox, dpi=args.dpi,
        )
        print(f"  {'OK — modified' if ok else 'no changes'}")


if __name__ == "__main__":
    main()
