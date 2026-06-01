# Science Cheat Sheet Pipeline (Singapore P4/P5)

Open-ended (Booklet B) exam PDFs → 8-tab cheat-sheet HTML + a printable OE quiz.
P4 = age 10, P5/PSLE = age 11. API key from the `GEMINI_API_KEY` env var **only** —
never add `.env` / `python-dotenv`. User runs the scripts; iterate on
prompts/templates between runs.

## Pipeline (scripts/)

1. **`pdf_extractor.py --bank [p4|p5] [--only PAT] [--force]`** — 1 Gemini call/PDF →
   `output/json/[bank]/…`, merged (dedupe) into `banks/[bank]/master_questions.jsonl`.
   Multi-part Qs: ONE parent entry (`sub_question_part="none"`) owns `page_number` +
   `parent_bbox{top_y_norm,bottom_y_norm,ends_on_page,ends_at_y_norm}` + shared diagrams;
   sub-parts (a,b,c…) reference it via `parent_question_id` and do NOT repeat the bbox.
   Single-part = one entry with its own bbox.
2. **`pipeline.py --topic "X" --bank [p4|p5] [--force-validation] [--force-html] [--threshold N] [--wide-theme]`** —
   pure-Python (load → semantic filter → classify → score → group multi-part → sort), then
   **2 Gemini calls**: validation, then 7-tab HTML gen (Top Questions tab left empty for the
   post-processor). Dumps top-10 picks → `output/validated/top_questions_{slug}_{level}.json`,
   then the post-processor crops parent-Q PNGs and injects Top-Questions cards (image +
   per-sub-part "Show model answer" flips) into `#tab-questions`. Outputs:
   `output/html/cheatsheet_{slug}_{level}.html`, `output/validated/validated_…json`,
   `top_questions_…json`, `output/conceptviz_prompts/{slug}_{level}.txt`.
3. **`image_generator.py --topic "X" --bank [p4|p5] [--tab …] [--only ID] [--variants N]`** —
   fills placeholders only: rare `gemini-placeholder` D-PNG divs (1 nano-banana call each) and
   the `conceptviz-placeholder` (3-stage: shaper LLM → N nano-banana PNGs → promote v1 to
   canonical `conceptviz/{slug}_{level}.png`; re-embeds existing canonical with no API).
   D-SVG diagrams are inlined by the HTML LLM, not here.
4. **`oe_quiz_generator.py --bank [p4|p5] --topics … [--count 5] [--similarity 0.75] [--force-bbox]`** —
   reads the picks JSON, picks N hardest (MiniLM cosine-dedup), crops
   (extraction bbox → `oe_bbox_cache.json` → live vision) → `oe_quiz_{slug}.html` + `oe_answers_{slug}.html`.
5. **`top_questions_images.py`** — the Top-Questions post-processor (auto-run by `pipeline.py`;
   standalone to re-crop without re-running the LLM).

`slug` = lowercased, non-word→`_` ("Plant Reproduction"→`plant_reproduction`). `level` = `p4`|`p5`.

## Models (constants atop pipeline.py / image_generator.py / pdf_extractor.py)

| Step | Model · thinking |
|---|---|
| PDF extraction · Validation · ConceptViz shaper | `gemini-3.5-flash` · **medium** |
| HTML generation (incl. ConceptViz content extraction) | `gemini-3.5-flash` · **high** |
| OE-quiz bbox (vision) | `gemini-3.5-flash` · default |
| Diagram / ConceptViz image | `gemini-3.1-flash-image-preview` (nano-banana-2) |

All text/vision = **`gemini-3.5-flash`**, 503-fallback **`gemini-3-flash-preview`**.
**Never** use gemini-2.x / gemini-3.1-pro (superseded). `.agents/gemini_bridge.py`
(PRO/FLASH/FLASH_LITE/NANO_BANANA) is the entry for *new* bulk work — keep its
`FALLBACK_CHAINS` in sync with this table; existing scripts call `genai.Client` directly (fine).

## Tabs (8 dynamic + static Progress)

Overview · **Top Questions** (Python-injected, NOT the LLM) · Common Mistakes (≥8 `mCard`) ·
Critical Distinctions (≥4 `dCardCompare`) · Keywords (15–20 `kCard`) · Wrong Answers (≥8 `tCard`) ·
Answer Formula (Rough.js per question type) · Master Summary (ConceptViz).
**Text fidelity (tabs 3–8):** `question_text`/`model_answer` VERBATIM; only allowed wraps are
`<span class="keyword" data-tip="…">` and `<span class="link-word">` (because, due to, therefore,
as a result, which causes, leading to, while, whereas).

## Helper DSL (templates/script.js, ~45 helpers)

HTML LLM emits helper calls, not raw HTML/SVG (−50–79% output).
Cards: `kCard · mCard · tCard · dCardCompare`. Tier-1 SVG frames: `dCompare · dDistinct · dScene · dFlow`.
Tier-2 (domain-gated, injected by `select_tier2_domains`/`build_tier2_docs` from fenced
`<!--HELPER:domain-->` blocks at the end of `generation_prompt.txt`): `water_heat_matter`,
`biology`, `electricity`. Primitives: `dArrow · dLabel · dBox · dDashedLine · dPal`.
ECharts: `ecBar/ecLine/ecPie`. Other: `tsTable`, Rough.js `rvt/rvah/rvNode`.
Grow it via `prompts/iterate_tier2_helpers.txt` (Pass A = new helpers from raw-SVG fallbacks;
Pass B = retrofit chunky helpers).

## Diagram routing (HTML LLM picks via `diagrams_and_tables[0].type`)

`table`→A1 `tsTable` · `graph`/`chart`→A2 ECharts · else→D-SVG (`dCompare/dDistinct/dScene/dFlow`,
raw `<svg>` fallback). D-PNG (`gemini-placeholder data-output="png"`) only when photorealism IS the
content. ConceptViz = Master Summary only. Question scenario diagrams: SCENARIO ONLY (no answer
keywords / BECAUSE arrows); only the Formula + Master Summary tabs may use those.

## Forbidden concepts (all levels)

Kinetic/potential energy · atoms/electrons/protons/neutrons · chemical reactions · osmosis/diffusion ·
photosynthesis formula · Newton's laws by name · Ohm's Law · DNA/genetics · cellular respiration
equation · pressure formulas · secondary-school maths.

## Commands

```bash
python scripts/pdf_extractor.py --bank p5                        # extract new PDFs (--only PAT --force = one)
python scripts/pipeline.py --topic "Cycles in Water" --bank p5   # build cheatsheet (cached); --force-html/-validation, --threshold 0.20, --wide-theme
python scripts/image_generator.py --topic "Heat" --bank p4 --variants 5   # ConceptViz + rare D-PNGs
python scripts/oe_quiz_generator.py --bank p4 --topics "Heat" --count 5   # printable OE quiz
python scripts/top_questions_images.py --bank p4 --topics ALL             # re-crop Top Questions only
python .agents/gemini_bridge.py --class flash-lite --prompt "…"           # delegation bridge
```

## Where to fix things

- HTML / tab content / helper examples → `prompts/generation_prompt.txt` (+ helper code in `templates/script.js`)
- Validation too loose/strict → `prompts/validation_prompt.txt`. MISALIGNED is strict: distinct named
  topics (Light, Magnets, Heat) are MISALIGNED, not AMBIGUOUS. Verdicts = ALIGNMENT
  (ALIGNED/PARTIAL/MISALIGNED/AMBIGUOUS) + ACCURACY (PASS/CORRECT/EXCLUDE); JSON response.
- Tier-2 domain selection → `TIER2_DOMAIN_KEYWORDS` in `pipeline.py`; frequent raw-SVG fallbacks → iterate Tier-2 helpers
- ConceptViz content / style → `templates/conceptviz_requirements.txt` / `conceptviz_image_style.txt`
- New CSS classes → `templates/styles.css` + `available_classes.txt`
- Extraction bbox loose / cut off → BBOX RULES in `prompts/extraction_prompt.txt`, re-extract `--force --only PAT`;
  bad crop → delete its entry in `oe_bbox_cache.json` + the PNG, rerun with `--force-bbox`
- Top-Questions card styling → `TOP_Q_STYLE` / `render_question_item` in `top_questions_images.py`
- Bridge registry stale → `FALLBACK_CHAINS` in `.agents/gemini_bridge.py`

## Layout

`banks/[p4|p5]/{papers/, master_questions.jsonl}` · `output/{json,validated,conceptviz_prompts,html,quizzes}` ·
`conceptviz/` · `prompts/` · `templates/` · `scripts/` · `.agents/gemini_bridge.py` ·
`processed.json` (extractor state). Cheatsheets reference crops via the
`output/html/screenshots → ../quizzes/screenshots` symlink. Semantic filter uses
`all-MiniLM-L6-v2` (~80 MB auto-download, default threshold 0.25).

<!-- 2026-06-01: gemini-3.5-flash (extraction+validation medium, HTML high); OE-only (MCQ track removed). -->
