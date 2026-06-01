#!/usr/bin/env python3
"""
image_generator.py — Generate and embed Gemini SVG diagrams into cheat sheet HTML.

Reads gemini-placeholder divs directly from the HTML file. Each div contains the
full prompt text, model config (data-model, data-steps), and tab name (data-tab).
No external JSON file needed.

Diagrams already replaced (placeholder no longer present) are skipped automatically.

Usage:
  python scripts/image_generator.py --topic "Cycles in Water" --bank p5
  python scripts/image_generator.py --topic "Cycles in Water" --bank p5 --tab questions
  python scripts/image_generator.py --topic "Cycles in Water" --bank p5 --tab mistakes distinctions
  python scripts/image_generator.py --topic "Cycles in Water" --bank p5 --only q2-focus
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_HTML_DIR  = ROOT / "output" / "html"
CONCEPTVIZ_DIR   = ROOT / "conceptviz"
CONCEPTVIZ_IMAGE_STYLE_FILE = ROOT / "templates" / "conceptviz_image_style.txt"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
SVG_MODEL_SIMPLE   = "gemini-3.5-flash"
SVG_MODEL_FALLBACK = "gemini-3-flash-preview"

# Prompt-shaper: takes structured content + style template → final image prompts.
PROMPT_SHAPER_MODEL = "gemini-3.5-flash"

# "nano banana 2" — Gemini image generation model for complex PNG diagrams.
PNG_MODEL = "gemini-3.1-flash-image-preview"

MAX_RETRIES  = 3
RETRY_DELAY  = 8
DEFAULT_VARIANTS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def topic_to_slug(topic: str) -> str:
    return re.sub(r"[^\w]+", "_", topic.strip().lower()).strip("_")


def level_from_bank(bank: str) -> str:
    return bank.upper()


def _strip_svg(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:svg)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(<svg[\s\S]*?</svg>)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s or "429" in s or "RESOURCE_EXHAUSTED" in s


def _thinking_config(level: str | None) -> "types.ThinkingConfig | None":
    if not level:
        return None
    mapping = {"medium": types.ThinkingLevel.MEDIUM, "high": types.ThinkingLevel.HIGH}
    tl = mapping.get(level.lower())
    return types.ThinkingConfig(thinking_level=tl) if tl else None


def find_placeholders(html: str) -> dict[str, dict]:
    """Return dict of diagram entries from gemini-placeholder and conceptviz-placeholder divs.

    Each entry has a 'type' key: 'gemini' or 'conceptviz'.
    Already-replaced placeholders (div is gone) are naturally skipped.
    """
    result: dict[str, dict] = {}

    # --- gemini-placeholder divs ---
    for m in re.finditer(
        r'<div(\s[^>]*class="gemini-placeholder"[^>]*)>([\s\S]*?)</div>',
        html,
    ):
        attrs = m.group(1)
        content = m.group(2).strip()

        def get_attr(name: str, _attrs: str = attrs) -> str:
            am = re.search(r'data-' + re.escape(name) + r'="([^"]*)"', _attrs)
            return am.group(1) if am else ""

        diagram_id = get_attr("diagram-id")
        if not diagram_id:
            continue

        model_key = get_attr("model") or SVG_MODEL_SIMPLE
        output = get_attr("output") or ("png" if model_key == "nano-banana-2" else "svg")
        tab = get_attr("tab") or "unknown"

        if output == "svg":
            steps = int(get_attr("steps") or "1")
            if steps == 2 and "---STEP2---" in content:
                step1, step2 = content.split("---STEP2---", 1)
                prompt = step1.strip()
                step2_prompt = step2.strip()
            else:
                prompt = content
                step2_prompt = ""
        else:
            prompt = content
            step2_prompt = ""
            steps = 1

        result[diagram_id] = {
            "type": "gemini",
            "prompt": prompt,
            "step2_prompt": step2_prompt,
            "model": model_key,
            "steps": steps,
            "output": output,
            "tab": tab,
        }

    # --- conceptviz-placeholder divs ---
    for m in re.finditer(
        r'<div(\s[^>]*class="conceptviz-placeholder"[^>]*)>([\s\S]*?)</div>',
        html,
    ):
        attrs = m.group(1)
        content = m.group(2).strip()

        def get_cv_attr(name: str, _attrs: str = attrs) -> str:
            am = re.search(r'data-' + re.escape(name) + r'="([^"]*)"', _attrs)
            return am.group(1) if am else ""

        diagram_id = get_cv_attr("diagram-id")
        if not diagram_id:
            continue

        result[diagram_id] = {
            "type": "conceptviz",
            "structured_content": content,   # YAML-style content from HTML LLM
            "topic": get_cv_attr("topic"),
            "level": get_cv_attr("level"),
            "tab": "summary",
            "output": "png",
        }

    return result


def replace_placeholder(
    html: str, diagram_id: str, replacement: str, class_name: str = "gemini-placeholder"
) -> str:
    """Replace the named placeholder div (gemini-placeholder or conceptviz-placeholder)."""
    esc_class = re.escape(class_name)
    esc_id    = re.escape(diagram_id)
    pattern = (
        r'<div\s[^>]*class="' + esc_class + r'"[^>]*'
        r'data-diagram-id="' + esc_id + r'"[^>]*>[\s\S]*?</div>'
    )
    result = re.sub(pattern, replacement, html, count=1)
    if result == html:
        pattern2 = (
            r'<div\s[^>]*data-diagram-id="' + esc_id + r'"[^>]*'
            r'class="' + esc_class + r'"[^>]*>[\s\S]*?</div>'
        )
        result = re.sub(pattern2, replacement, html, count=1)
    return result


# ---------------------------------------------------------------------------
# Gemini SVG generation
# ---------------------------------------------------------------------------

def call_gemini_svg(
    prompt: str,
    model: str,
    thinking: str | None,
    client: genai.Client,
    diagram_id: str,
) -> str | None:
    config_kwargs: dict = {"max_output_tokens": 8192}
    tc = _thinking_config(thinking)
    if tc:
        config_kwargs["thinking_config"] = tc

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            svg = _strip_svg(response.text)
            if not svg.startswith("<svg"):
                print(f"    WARNING: response for {diagram_id} doesn't look like SVG "
                      f"(starts with: {svg[:60]!r})")
            return svg
        except Exception as e:
            if _is_503(e) and attempt < MAX_RETRIES:
                print(f"    {model} unavailable (attempt {attempt}), retrying in {RETRY_DELAY}s…")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ERROR generating {diagram_id}: {e}")
                return None

    return None


def generate_svg_for_entry(entry: dict, client: genai.Client, diagram_id: str) -> str | None:
    """Generate SVG for a gemini entry. Handles 1-step and 2-step generation."""
    model_key = entry.get("model", SVG_MODEL_SIMPLE)
    thinking: str | None = None
    if "thinking-high" in model_key:
        thinking = "high"
        model_key = re.sub(r"-thinking[-_]high", "", model_key)
    elif "thinking-medium" in model_key:
        thinking = "medium"
        model_key = re.sub(r"-thinking[-_]medium", "", model_key)

    if "3-flash" in model_key and "3.1" not in model_key and "lite" not in model_key:
        model_id = SVG_MODEL_SIMPLE
    else:
        model_id = model_key

    steps = entry.get("steps", 1)
    prompt = entry.get("prompt", "")

    if steps == 2:
        step2_template = entry.get("step2_prompt", "")
        if not step2_template:
            print(f"    WARNING: {diagram_id} has steps=2 but no step2_prompt; falling back to 1-step")
            steps = 1
        else:
            print(f"    {diagram_id}: 2-step (model={model_id}, thinking={thinking})")
            step1 = call_gemini_svg(prompt, model_id, thinking, client, f"{diagram_id}-step1")
            if not step1:
                return None
            return call_gemini_svg(
                step2_template.replace("{STEP1_OUTPUT}", step1),
                model_id, thinking, client, diagram_id,
            )

    print(f"    {diagram_id}: 1-step (model={model_id}, thinking={thinking})")
    return call_gemini_svg(prompt, model_id, thinking, client, diagram_id)


# ---------------------------------------------------------------------------
# PNG generation (nano banana 2 / Imagen)
# ---------------------------------------------------------------------------

def generate_png_for_entry(entry: dict, client: genai.Client, diagram_id: str) -> str | None:
    """Call gemini-3.1-flash-image-preview (nano banana 2) and return a <figure> HTML string."""
    prompt = entry.get("prompt", "").strip()
    if not prompt:
        print(f"    ERROR: {diagram_id} has no prompt")
        return None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=PNG_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                ),
            )
            # Find the first image part in the response
            image_bytes = None
            mime_type = "image/png"
            for candidate in response.candidates or []:
                for part in candidate.content.parts or []:
                    blob = getattr(part, "inline_data", None)
                    if blob is not None:
                        image_bytes = blob.data
                        mime_type = blob.mime_type or "image/png"
                        break
                if image_bytes:
                    break

            if not image_bytes:
                print(f"    WARNING: {diagram_id} — no image in response")
                return None

            b64 = base64.b64encode(image_bytes).decode("ascii")
            return (
                '<figure class="diagram-container" style="text-align:center">'
                f'<img src="data:{mime_type};base64,{b64}" '
                f'alt="{diagram_id}" '
                'style="max-width:100%;border-radius:8px;display:block;margin:0 auto;cursor:zoom-in" '
                'onclick="openFullscreen(this)"/>'
                '<figcaption class="diagram-caption">Tap to view full screen</figcaption>'
                '</figure>'
            )
        except Exception as e:
            if _is_503(e) and attempt < MAX_RETRIES:
                print(f"    {PNG_MODEL} unavailable (attempt {attempt}), retrying in {RETRY_DELAY}s…")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ERROR generating PNG for {diagram_id}: {e}")
                return None

    return None


# ---------------------------------------------------------------------------
# ConceptViz: prompt-shaper + multi-variant nano-banana generation + embed
# ---------------------------------------------------------------------------

def _figure_html_from_png(png_path: Path, topic: str) -> str:
    """Read a PNG file from disk and wrap it as a base64 <figure>."""
    b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
    return (
        '<figure class="conceptviz-figure">'
        f'<img src="data:image/png;base64,{b64}" '
        f'alt="Core Concept Overview — {topic}" class="conceptviz-image" '
        'onclick="openFullscreen(this)" title="Click or tap to view full screen" '
        'style="max-width:100%;border-radius:10px;display:block;margin:0 auto;cursor:zoom-in"/>'
        '<figcaption class="diagram-caption">Tap to view full screen</figcaption>'
        '</figure>'
    )


def shape_image_prompts(
    structured_content: str, topic: str, level: str,
    num_variants: int, client: genai.Client,
) -> list[str]:
    """Call the prompt-shaper LLM to combine structured content + visual style template
    into N nano-banana-2 prompts. Returns a list of prompt strings."""
    style_template = CONCEPTVIZ_IMAGE_STYLE_FILE.read_text(encoding="utf-8")

    shaper_prompt = f"""You are a prompt-shaper for nano-banana-2 (a Gemini image generation model).
Combine the STRUCTURED CONTENT and the VISUAL STYLE TEMPLATE below into {num_variants}
DIFFERENT but equally valid image-generation prompts for the same topic. Each variant
should make different creative choices about: scene composition, icon selection, card
layout direction, badge colour rotation, and typography emphasis — while still rendering
the SAME content accurately.

Output ONLY a JSON array of {num_variants} strings — no markdown fences, no commentary.
Each string is a complete standalone prompt ready to send to nano-banana-2.

Each prompt MUST:
  - Be 1800-3000 characters (denser content needs a longer prompt)
  - Begin with a clear render-style sentence (watercolour textbook illustration, 1180x820 px)
  - Describe the watercolour background as FADED / desaturated / behind cards (build on SCENE_HINT — vary the angle/composition between variants)
  - List each numbered card with its name, summary line, AND every detail-bullet rendered as a labelled mini-icon (3-5 per card)
  - Render PROCESS_FLOW as a horizontal arrow chain across the canvas centre IF the structured content includes a non-empty PROCESS_FLOW block (otherwise omit)
  - Render CATEGORY_STRIP as a horizontal labelled icon row directly above the KEY_TAKEAWAY strip IF non-empty (otherwise omit)
  - Render CLASSIFICATION ONCE only on the right edge — never duplicate it
  - Include the KEY_TAKEAWAY strip at the bottom (soft yellow), allowing two lines if needed
  - End with explicit "no garbled text, all labels readable, classification chip rendered only once" constraint

CRITICAL — DO NOT leak technical syntax into the image prompt. nano-banana will render
ANY string you write into the visible image. NEVER include in your output:
  - Hex colour codes like #4D96FF — describe colours by NAME only ("blue badge", not "blue (#4D96FF)")
  - Square brackets around stage labels — write "Seed → Germinates → Seedling", not "[Seed] → [Germinates]"
  - YAML/section names like SUB_TOPICS, PROCESS_FLOW, CATEGORY_STRIP, KEY_TAKEAWAY, SCENE_HINT — translate them into natural-language sentences ("a horizontal labelled icon strip", not "the CATEGORY_STRIP")
  - Curly braces, angle brackets, or template placeholders

TOPIC: {topic}    LEVEL: {level}

STRUCTURED CONTENT:
{structured_content}

VISUAL STYLE TEMPLATE:
{style_template}
"""

    response = client.models.generate_content(
        model=PROMPT_SHAPER_MODEL,
        contents=shaper_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM),
            max_output_tokens=16384,
        ),
    )

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        prompts = json.loads(raw)
    except json.JSONDecodeError:
        print(f"    ERROR: prompt-shaper returned invalid JSON")
        print(f"    First 400 chars: {raw[:400]}")
        return []

    if not isinstance(prompts, list):
        print(f"    ERROR: prompt-shaper did not return a list (got {type(prompts).__name__})")
        return []

    prompts = [str(p) for p in prompts if isinstance(p, str) and p.strip()]
    print(f"    Prompt-shaper returned {len(prompts)} variant(s) "
          f"(lengths: {[len(p) for p in prompts]})")
    return prompts


def generate_conceptviz_png(prompt: str, client: genai.Client) -> bytes | None:
    """Single nano-banana-2 call. Returns image bytes or None on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=PNG_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            for cand in response.candidates or []:
                for part in cand.content.parts or []:
                    blob = getattr(part, "inline_data", None)
                    if blob is not None:
                        return blob.data
            return None
        except Exception as e:
            if _is_503(e) and attempt < MAX_RETRIES:
                print(f"    {PNG_MODEL} unavailable (attempt {attempt}), retrying in {RETRY_DELAY}s…")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ERROR generating ConceptViz PNG: {e}")
                return None
    return None


def handle_conceptviz_entry(
    entry: dict, client: genai.Client, num_variants: int,
) -> str | None:
    """Full ConceptViz flow:
       1. If conceptviz/{slug}_{level}.png already exists → embed it (don't regenerate).
       2. Else: parse structured content, call prompt-shaper for N variants,
          generate N PNGs to conceptviz/{slug}_{level}_v{i}.png, copy v1 to canonical,
          embed it.
    """
    topic = entry.get("topic", "")
    level = entry.get("level", "")
    slug  = topic_to_slug(topic)
    canon_path = CONCEPTVIZ_DIR / f"{slug}_{level.lower()}.png"

    # Already-generated → embed and exit
    if canon_path.exists():
        print(f"    {canon_path.name} already exists — embedding")
        return _figure_html_from_png(canon_path, topic)

    structured = entry.get("structured_content", "").strip()
    if not structured:
        print(f"    ERROR: no structured content found inside conceptviz-placeholder")
        return None

    print(f"    Shaping {num_variants} prompt variant(s) with {PROMPT_SHAPER_MODEL}…")
    prompts = shape_image_prompts(structured, topic, level, num_variants, client)
    if not prompts:
        return None

    CONCEPTVIZ_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, prompt in enumerate(prompts, 1):
        print(f"    Variant {i}/{len(prompts)}: calling {PNG_MODEL} ({len(prompt)} chars)…")
        img_bytes = generate_conceptviz_png(prompt, client)
        if not img_bytes:
            print(f"      Variant {i} failed.")
            continue
        v_path = CONCEPTVIZ_DIR / f"{slug}_{level.lower()}_v{i}.png"
        v_path.write_bytes(img_bytes)
        # Save the prompt next to the PNG for inspection
        (CONCEPTVIZ_DIR / f"{slug}_{level.lower()}_v{i}.prompt.txt").write_text(
            prompt, encoding="utf-8")
        print(f"      Saved {v_path.name} ({len(img_bytes):,} bytes)")
        saved.append(v_path)

    if not saved:
        print(f"    All variants failed.")
        return None

    # Promote v1 to the canonical filename so subsequent runs embed it.
    canon_path.write_bytes(saved[0].read_bytes())
    print(f"    Promoted {saved[0].name} → {canon_path.name}")
    print(f"    Other variants saved to disk — replace {canon_path.name} manually to swap.")
    return _figure_html_from_png(canon_path, topic)


# ---------------------------------------------------------------------------
# Tab selection
# ---------------------------------------------------------------------------

def get_tab_selection(diagrams: dict[str, dict], tab_args: list[str] | None) -> list[str]:
    """Return the list of diagram IDs to process.

    If --tab was given on the CLI, filter to those tabs.
    Otherwise, show an interactive menu and ask the user.
    """
    by_tab: dict[str, list[str]] = {}
    for did, entry in diagrams.items():
        tab = entry.get("tab", "unknown")
        by_tab.setdefault(tab, []).append(did)
    tab_names = sorted(by_tab.keys())

    if tab_args:
        selected = []
        for t in tab_args:
            if t in by_tab:
                selected.extend(by_tab[t])
            else:
                print(f"  Warning: no diagrams found for tab '{t}'")
        return selected or list(diagrams.keys())

    # Interactive
    total = len(diagrams)
    print(f"\nDiagram placeholders: {total} diagram(s) across {len(tab_names)} tab(s)")
    print("\nAvailable tabs:")
    for i, tab in enumerate(tab_names, 1):
        n = len(by_tab[tab])
        print(f"  {i}. {tab:<22} ({n} diagram{'s' if n != 1 else ''})")
    print(f"  0. all                    ({total} diagrams)")

    while True:
        raw = input("\nWhich tab to generate? Enter number, name, comma-list, or 0/all: ").strip().lower()
        if not raw or raw in ("0", "all"):
            return list(diagrams.keys())
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(tab_names):
                tab = tab_names[idx]
                print(f"  → {tab} ({len(by_tab[tab])} diagram(s))")
                return by_tab[tab]
        if raw in by_tab:
            print(f"  → {raw} ({len(by_tab[raw])} diagram(s))")
            return by_tab[raw]
        # Comma-separated list
        parts = [p.strip() for p in raw.split(",")]
        bad = [p for p in parts if p not in by_tab]
        if bad:
            print(f"  Unknown tab(s): {bad}. Valid: {tab_names}")
            continue
        selected = []
        for p in parts:
            selected.extend(by_tab[p])
        print(f"  → {parts} ({len(selected)} diagram(s))")
        return selected


# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

def run_image_generation(
    topic: str,
    bank: str,
    tab_args: list[str] | None,
    only: list[str] | None,
    client: genai.Client,
    num_variants: int = DEFAULT_VARIANTS,
) -> None:
    level = level_from_bank(bank)
    slug  = topic_to_slug(topic)

    html_path = OUTPUT_HTML_DIR / f"cheatsheet_{slug}_{level.lower()}.html"

    if not html_path.exists():
        print(f"ERROR: HTML not found at {html_path}", file=sys.stderr)
        sys.exit(1)

    html = html_path.read_text(encoding="utf-8")
    diagrams = find_placeholders(html)

    if not diagrams:
        print("No placeholders found — all diagrams already generated.")
        return

    print(f"Found {len(diagrams)} placeholder(s): {sorted(diagrams.keys())}")

    # Determine targets
    if only:
        targets = [t for t in only if t in diagrams]
        missing = [t for t in only if t not in diagrams]
        if missing:
            print(f"  WARNING: not found in HTML (already replaced or wrong ID): {missing}")
        print(f"Restricted by --only: {targets}")
    else:
        targets = get_tab_selection(diagrams, tab_args)

    changed = False
    skipped = generated = failed = 0

    for diagram_id in targets:
        if diagram_id not in diagrams:
            skipped += 1
            continue

        entry = diagrams[diagram_id]
        entry_type = entry.get("type", "gemini")

        if entry_type == "conceptviz":
            print(f"  [conceptviz] {diagram_id}")
            result = handle_conceptviz_entry(entry, client, num_variants)
            class_name = "conceptviz-placeholder"
        elif entry.get("output") == "png":
            print(f"  [imagen] {diagram_id}")
            result = generate_png_for_entry(entry, client, diagram_id)
            class_name = "gemini-placeholder"
        else:
            print(f"  [gemini] {diagram_id}")
            result = generate_svg_for_entry(entry, client, diagram_id)
            class_name = "gemini-placeholder"

        if result:
            html = replace_placeholder(html, diagram_id, result, class_name)
            changed = True
            generated += 1
        else:
            failed += 1

    if changed:
        html_path.write_text(html, encoding="utf-8")
        print(f"\nSaved HTML → {html_path.relative_to(ROOT)}")

    print(f"\nDone. Generated: {generated} | Skipped: {skipped} | Failed: {failed}")
    if failed:
        print("Re-run to retry failed diagrams.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Gemini SVG diagrams for a cheat sheet")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--bank",  required=True, choices=["p4", "p5"])
    parser.add_argument("--tab",   nargs="+", metavar="TAB",
                        help="Process only these tab(s), e.g. --tab questions mistakes. "
                             "If omitted, you are prompted interactively.")
    parser.add_argument("--only",  nargs="+", metavar="DIAGRAM_ID",
                        help="Process only these specific diagram IDs (overrides --tab)")
    parser.add_argument("--variants", type=int, default=DEFAULT_VARIANTS,
                        metavar="N",
                        help=f"Number of ConceptViz variants to generate (default {DEFAULT_VARIANTS}); "
                             "v1 is promoted to canonical, v2/v3 saved alongside")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    run_image_generation(args.topic, args.bank, args.tab, args.only, client,
                         num_variants=max(1, args.variants))


if __name__ == "__main__":
    main()
