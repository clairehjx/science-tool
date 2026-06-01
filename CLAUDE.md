# Singapore P4/P5 Science Cheat Sheet Pipeline

Open-ended pipeline from exam PDFs. P4 = age 10, P5/PSLE = age 11.

- **OE pipeline** — Booklet B open-ended questions → 8-tab cheat sheet HTML,
  plus a printable open-ended quiz (questions + answer-key) pair.

## ARCHITECTURE

### Cheat-sheet build

```
pdf_extractor.py --bank [p4|p5]   [--only PATTERN] [--force]
  └── 1 Gemini call per new PDF → output/json/[bank]/School_Year_Level_Nquestions.json
      Merges into banks/[bank]/master_questions.jsonl  (incremental, dedupe)
      Multi-part questions emit ONE parent entry (sub_question_part="none")
      that owns `page_number` + `parent_bbox{top_y_norm, bottom_y_norm,
      ends_on_page, ends_at_y_norm}` plus any shared diagrams/intro text.
      Sub-parts (a, b, c, …) reference the parent via parent_question_id and
      do NOT duplicate the bbox. Single-part questions emit one entry only,
      with bbox on that entry.

pipeline.py --topic "X" --bank [p4|p5]   [--force-validation] [--force-html]
                                          [--threshold N] [--wide-theme]
  ├── Pure Python: load → semantic filter → classify → score → group → sort
  ├── Tier-2 helper-doc selective injection (keyword match on topic + questions)
  ├── 1 Gemini call: validation       (skipped if validated JSON exists)
  ├── pure-Python: dump top-10 hardest picks → output/validated/top_questions_{slug}_{level}.json
  ├── 1 Gemini call: HTML generation  (skipped if HTML exists). Renders SEVEN
  │   tabs only — Top Questions tab is left empty; LLM no longer reads or
  │   writes its content (saves ~5K output + ~1.5K input tokens per topic).
  └── post-process: read picks JSON → crop parent-question PNGs from source
      PDFs → inject `<div class="question-item">` cards w/ image + per-sub-part
      "Show model answer" flip buttons into #tab-questions (extraction-baked
      bbox / cache / vision-fallback resolution).
      Saves: output/html/cheatsheet_{slug}_{level}.html
            output/validated/validated_{slug}_{level}.json
            output/validated/top_questions_{slug}_{level}.json   (10 picks for tab + OE quiz)
            output/conceptviz_prompts/{slug}_{level}.txt         (structured ConceptViz content)
            output/quizzes/screenshots/{bank}/{school}/oe_q{N}.png  (shared with OE quiz)

image_generator.py --topic "X" --bank [p4|p5]   [--tab ...] [--only ID] [--variants N]
  ├── gemini-placeholder PNG divs → 1 nano-banana-2 call each (rare — D-PNG only)
  └── conceptviz-placeholder div:
       • If conceptviz/{slug}_{level}.png exists → embed it (no API)
       • Else 3-stage: prompt-shaper LLM → N nano-banana-2 calls →
         save v1..vN PNGs, promote v1 to canonical, embed v1
```

TYPE D-SVG diagrams are inlined by the HTML LLM via the helper DSL (see below).
image_generator only handles ConceptViz + rare D-PNG placeholders.

### Open-ended quiz (printable)

```
oe_quiz_generator.py --bank [p4|p5] --topics "X" "Y" …
                                     [--count 5] [--similarity 0.75] [--dpi 200]
                                     [--force-screenshots] [--force-bbox]
  ├── Reads top_questions_{slug}_{level}.json (produced by pipeline.py).
  ├── Picks --count (default 5) hardest with all-MiniLM-L6-v2 cosine-sim
  │   dedup at threshold --similarity; backfills from skipped pool if needed.
  ├── Bbox resolution: extraction-baked → oe_bbox_cache.json → live vision.
  └── Emits oe_quiz_{slug}.html + oe_answers_{slug}.html (hardest first).
```

The OE quiz reuses `pipeline.py` helpers but writes independent outputs.

## HELPER DSL (templates/script.js, ~45 helpers)

The HTML LLM emits compact helper calls instead of raw HTML/SVG (cuts output ~50–79%).

**Card helpers** (high-boilerplate tabs):
- `kCard({id, word, tip, cat, freq, usage})` — Keywords (filter via `data-category`)
- `mCard({id, title, marks, wrong, missing, right, keyword, fix, diagram?})` — Mistakes
- `tCard({id, trap, description, truth, whyMatters})` — Traps
- `dCardCompare({id, leftLabel, rightLabel, leftItems[], rightItems[], leftDiagram?, rightDiagram?, note?, trap?})` — Distinctions

**Tier 1 SVG frames**: `dCompare`(440×155 two-panel) · `dDistinct`(220×150) · `dScene`(500×280) · `dFlow`(500×200, nodes+edges).

**Tier 2 sub-shapes** — selectively injected by `select_tier2_domains()` /
`build_tier2_docs()` in pipeline.py based on topic + question keywords; fenced
`<!--HELPER:domain-->…<!--/HELPER:domain-->` at the bottom of `generation_prompt.txt`:
- `water_heat_matter`: `dSun · dFlame · dBeaker · dPuddle · dDroplet · dCloud · dThermo · dContainer · dParticleGrid · dKettle · dPlate · dCylinder · dRays · dCup · dFunnel · dBalance · dIceCube · dBottle · dTray · dMist · dSpring`
- `biology`: `dPlant · dFlower · dLeaf · dAnimal('fish'|'bird'|'mammal'|'insect') · dOrgan('heart'|'lung'|'stomach'|'kidney') · dSeed · dRoot` *(bioicons-style)*
- `electricity`: `dBattery · dBulb · dSwitch · dWire · dCircuit(id, components, wires, opts?)`

**Generic primitives** (always): `dArrow · dLabel · dBox · dDashedLine` + `dPal` palette.
**Tier 3 ECharts wrappers**: `ecBar / ecLine / ecPie` (replaces `echarts.init` boilerplate).
**Other**: `tsTable` (sortable table), `rvt/rvah/rvNode` (Rough.js).

**Iterating the catalogue**: paste `prompts/iterate_tier2_helpers.txt` into a
fresh Claude conversation with the new HTML. Pass A = gap analysis (raw `<svg>`
fallbacks → bioicons / SVGRepo helpers + worked examples); Pass B = retrofit
audit of existing helpers (separate `=== RETROFITS ===` patch).

## CONCEPTVIZ — 3-STAGE PIPELINE

| Stage | Script | Model | Output |
|---|---|---|---|
| 1. Content extraction | pipeline.py | gemini-3.5-flash thinking-high | YAML in conceptviz-placeholder div + `output/conceptviz_prompts/{slug}_{level}.txt` |
| 2. Prompt shaping | image_generator.py | gemini-3.5-flash thinking-medium | JSON array of N variant prompts |
| 3. Image rendering | image_generator.py | nano-banana-2 | `conceptviz/{slug}_{level}_v{1..N}.png` + `.prompt.txt` sidecar; v1 → canonical `{slug}_{level}.png` |

Default cost: 2 calls (`--variants N` for N alternates). Swap variant by copying the chosen v{N} over the canonical PNG, then re-run image_generator (detects canonical, no API calls).

## CALL COUNT (typical topic, first run)

- pipeline.py: **2 calls** (validation + 7-tab HTML; Top Questions tab is
  filled by the post-processor from the picks JSON, no LLM round-trip)
- post-process: **0 calls** when extraction-baked bbox exists; up to 10
  vision calls for legacy PDFs (cached after first run)
- image_generator.py: **2 calls** (1 ConceptViz shaper + 1 nano-banana-2)
- Re-runs / cached: **0 calls**

## MODELS (constants atop pipeline.py / image_generator.py)

| Step | Model |
|---|---|
| OE PDF extraction (`pdf_extractor.py`) | `gemini-3.5-flash` thinking-medium (fallback `gemini-3-flash-preview`) |
| Validation (`pipeline.VALIDATION_MODEL`) | `gemini-3.5-flash` thinking-medium (fallback `gemini-3-flash-preview`) |
| HTML generation | `gemini-3.5-flash` thinking-high (fallback `gemini-3-flash-preview`) |
| ConceptViz prompt-shaper | `gemini-3.5-flash` (thinking-medium) |
| Diagram PNG / ConceptViz image | `gemini-3.1-flash-image-preview` (nano-banana-2) |
| OE-quiz bbox detection (`oe_quiz_generator.py`) | `gemini-3.5-flash` (fallback `gemini-3-flash-preview`, vision) |

**`gemini-3.5-flash`** = "3.5 Flash" — current primary for all text/vision work; fallback `gemini-3-flash-preview` = "3 Flash". PDF extraction and validation run at thinking-medium, HTML generation at thinking-high. **Do NOT use:** gemini-2.x / gemini-3.1-pro-preview (superseded).

### Delegation bridge — `.agents/gemini_bridge.py`

Centralised entry for new bulk work (Orchestrator Mandate). Existing scripts use `genai.Client` directly; that's fine. Classes: `PRO · FLASH · FLASH_LITE · NANO_BANANA` (image-only). CLI: `python .agents/gemini_bridge.py --class flash-lite --prompt "…"` or Python: `call(ModelClass.FLASH_LITE, prompt, json_mode=True).json()`. Keep `FALLBACK_CHAINS` in sync with the MODELS table above.

## FILE LAYOUT

```
science_cheatsheet/
├── CLAUDE.md · SETUP.html · requirements.txt   (API key via env var, not .env)
├── processed.json                          extractor state
├── .agents/gemini_bridge.py                Delegation bridge (Orchestrator Mandate)
├── banks/[p4|p5]/
│   ├── papers/                             PDFs (Year-Level-…-{School}.pdf)
│   └── master_questions.jsonl              OE questions (one line per school)
├── conceptviz/{slug}_{level}.png           canonical + _v{1..N} variants
├── output/json/[p4|p5]/                    raw extraction JSONs
├── output/validated/
│   ├── validated_{slug}_{level}.json       full validated set
│   └── top_questions_{slug}_{level}.json   top-10 picks (Top Questions tab + OE quiz)
├── output/conceptviz_prompts/{slug}_{level}.txt
├── output/html/
│   ├── cheatsheet_{slug}_{level}.html      OE study guides
│   └── screenshots → ../quizzes/screenshots  symlink for image URLs
├── output/quizzes/
│   ├── oe_quiz_/oe_answers_{slug}.html     OE printable quiz
│   ├── screenshots/{bank}/{school}/oe_q{N}.png
│   └── oe_bbox_cache.json                  vision bbox cache
├── prompts/
│   ├── extraction_prompt.txt · validation_prompt.txt
│   ├── generation_prompt.txt               (Tier-2 fences at bottom)
│   └── iterate_tier2_helpers.txt           two-pass copy-paste retrofit prompt
├── templates/
│   ├── shell.html · styles.css · script.js (~45 helpers)
│   ├── available_classes.txt · conceptviz_requirements.txt (LLM-injected)
│   └── conceptviz_image_style.txt          (image_generator-injected)
└── scripts/
    ├── pdf_extractor.py · pipeline.py · image_generator.py
    ├── oe_quiz_generator.py
    └── top_questions_images.py             Top Questions post-processor
                                            (auto-run by pipeline.py)
```

`slug` = lowercase + underscores ("Plant Reproduction" → `plant_reproduction`). `level` = `p4` or `p5`.

## PIPELINE.PY INTERNALS

**Pure Python (no API):** load JSONL → semantic filter (`all-MiniLM-L6-v2`,
~80MB auto-download; default threshold 0.25) → classify type (EXPLAIN,
COMPARE, DESCRIBE, STATE, SUGGEST, PREDICT, GIVE_REASON, CALCULATE,
OTHER_OPEN_ENDED) → score difficulty → group multi-part (parent-only entries
are scaffolding; the hardest answerable sub-part becomes the focus and
inherits the parent's `page_number` + `parent_bbox`) → sort hardest first →
dump top-10 to `top_questions_{slug}_{level}.json` → select Tier-2 helper
domains → strip Tier-2 fences → inject filtered docs.

**LLM calls:**
- **Validation** (`response_mime_type="application/json"`): ALIGNMENT
  (ALIGNED/PARTIAL/MISALIGNED/AMBIGUOUS) + ACCURACY (PASS/CORRECT/EXCLUDE).
  MISALIGNED applied strictly — distinct named topics (Light, Magnets, Heat)
  are MISALIGNED, not AMBIGUOUS.
- **HTML generation** (`prompts/generation_prompt.txt`): JSON with **7**
  dynamic-tab HTML strings (Top Questions excluded — the Python post-processor
  injects cropped PDF images + per-sub-part flip buttons). Wrapped into
  `templates/shell.html`.

## DIAGRAM TYPES (chosen by HTML LLM)

| Type | Use | Mechanism |
|---|---|---|
| **A1** Tailwind table | Comparing items × properties | `tsTable()` script + `<div id="tbl-X">` |
| **A2** ECharts | Bar/line/pie data | `ecBar/ecLine/ecPie()` (preferred) — raw `echarts.init` fallback |
| **A3** Rough.js sketch | 3-5 step process flows, formula tab | inline `<svg>` + `rough.svg()` + global `rvt`/`rvah`/`rvNode` |
| **B** ConceptViz | Master Summary only | `conceptviz-placeholder` div with structured YAML inside |
| **D-SVG** Inline SVG | Default complex diagram | `dCompare/dDistinct/dScene/dFlow` (preferred) — raw inline `<svg>` fallback |
| **D-PNG** placeholder | Photorealistic only (rare) | `gemini-placeholder` with `data-output="png"` → image_generator |

**SVG is the strong default.** PNG only when realistic appearance IS the exam content.

**Routing rule:** check `diagrams_and_tables[0].type`:
- `table` → A1 · `graph`/`chart` → A2 · everything else → D-SVG (rarely D-PNG)

**Question scenario diagrams:** SCENARIO ONLY — no answer keywords, no BECAUSE arrows.
**Tab 7 (Formula) and Tab 8 (Master Summary):** keywords + BECAUSE arrows allowed.

## TAB STRUCTURE (8 dynamic + static Progress)

1. **Overview** — 6–9 concept cards, 2 centered stat cards (Total Questions + Schools), schools list. No diagram.
2. **Top Questions** — 10 picks. **Python-injected** from `top_questions_*.json`: cropped PDF image + per-sub-part "Show model answer" flip buttons. LLM does not render this tab.
3. **Common Mistakes** — ≥8 `mCard()` calls
4. **Critical Distinctions** — ≥4 `dCardCompare()` calls
5. **Keywords** — 15–20 `kCard()` calls (filter via `data-category`)
6. **Wrong Answers** — ≥8 `tCard()` calls
7. **Answer Formula** — one Rough.js card per non-OTHER question type
8. **Master Summary** — single ConceptViz placeholder

**Text fidelity (tabs 3–8):** question_text and model_answer VERBATIM. Allowed wraps only: `<span class="keyword" data-tip="…">…</span>` and `<span class="link-word">…</span>` for: because, due to, therefore, as a result, which causes, leading to, while, whereas.

## FORBIDDEN CONCEPTS (never include — all levels)

Kinetic/potential energy · atoms/electrons/protons/neutrons · chemical reactions
· osmosis/diffusion · photosynthesis formula · Newton's laws by name · Ohm's Law
· DNA/genetics · cellular respiration equation · pressure formulas · secondary-school maths.

## COMMON COMMANDS

```bash
# OE track ---------------------------------------------------------------
python scripts/pdf_extractor.py --bank p5                               # extract new PDFs
python scripts/pipeline.py --topic "Cycles in Water" --bank p5          # build cheatsheet (cached if outputs exist)
python scripts/pipeline.py --topic "Matter" --bank p4 --force-html      # force re-run (also: --force-validation)
python scripts/pipeline.py --topic "Heat" --bank p5 --threshold 0.20    # loosen broad filter
python scripts/pipeline.py --topic "Cycles" --bank p5 --wide-theme      # one HTML per sub-topic
python scripts/image_generator.py --topic "Heat" --bank p4 --variants 5 # ConceptViz + rare D-PNGs

# OE quiz ----------------------------------------------------------------
python scripts/oe_quiz_generator.py --bank p4 --topics "Heat" --count 5 --similarity 0.75

# Top Questions post-process (auto-runs inside pipeline.py; standalone form
# for re-cropping after a bbox cache fix without re-running the LLM)
python scripts/top_questions_images.py --bank p4 --topics ALL

# Re-extract ONE PDF after a prompt change
python scripts/pdf_extractor.py --bank p4 --only "ACS" --force

# Delegation bridge
python .agents/gemini_bridge.py --class flash-lite --prompt "Summarise: …"
```

## ITERATION POINTERS

| Symptom | Edit |
|---|---|
| Helper API or examples | `templates/script.js` AND examples in `prompts/generation_prompt.txt` |
| LLM falls back to raw SVG often | Run `prompts/iterate_tier2_helpers.txt` (Pass A) → add new Tier-2 helpers |
| Existing helper looks chunky / childish | Run `prompts/iterate_tier2_helpers.txt` (Pass B) → bioicons retrofit |
| Tier-2 wrong domains selected | `TIER2_DOMAIN_KEYWORDS` in `scripts/pipeline.py` |
| HTML structure / tab content issues | `prompts/generation_prompt.txt` |
| Validation too loose / too strict | `prompts/validation_prompt.txt` |
| ConceptViz content (what LLM should mine) | `templates/conceptviz_requirements.txt` |
| ConceptViz visual style | `templates/conceptviz_image_style.txt` |
| New CSS classes | `templates/styles.css` AND `templates/available_classes.txt` |
| Quiz crop misses parts of a question | delete the entry in `output/quizzes/oe_bbox_cache.json` + the bad PNG; rerun the post-processor with `--force-bbox` |
| Top Questions tab card layout / styling | `TOP_Q_STYLE` / `render_question_item` in `scripts/top_questions_images.py` |
| Extraction-time bbox is loose / cuts off | tighten BBOX RULES in `prompts/extraction_prompt.txt`; re-extract with `--force --only PATTERN` |
| Bridge model registry stale | `FALLBACK_CHAINS` in `.agents/gemini_bridge.py` (sync with MODELS table) |

User runs the scripts; iterate on prompts/templates between runs.

---
*Last refreshed: 2026-06-01 — Models upgraded to gemini-3.5-flash (PDF extraction
& validation thinking-medium, HTML thinking-high). MCQ track removed (mcq_extractor.py,
quiz_generator.py + their prompts) — repo is now OE-only: cheat sheets +
open-ended quiz. Top Questions tab is Python-injected from
`top_questions_{slug}_{level}.json`; HTML LLM produces 7 tabs.*
