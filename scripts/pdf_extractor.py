#!/usr/bin/env python3
"""Script 1: Extract open-ended questions from Singapore Science exam PDFs using Gemini."""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
BANKS_DIR = ROOT / "banks"
OUTPUT_JSON_DIR = ROOT / "output" / "json"
PROCESSED_FILE = ROOT / "processed.json"
EXTRACTION_PROMPT_FILE = ROOT / "prompts" / "extraction_prompt.txt"
SYLLABUS_TOPICS_FILE = ROOT / "syllabus_topics.json"

GEMINI_EXTRACTION_MODEL = "gemini-3.5-flash"
GEMINI_EXTRACTION_FALLBACK = "gemini-3-flash-preview"

# Set after genai.Client() is created in main()
client: genai.Client | None = None


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_syllabus_topics(level: str) -> list[str]:
    """Return flat list of topic names for the given level from syllabus_topics.json."""
    if not SYLLABUS_TOPICS_FILE.exists():
        return []
    data = json.loads(SYLLABUS_TOPICS_FILE.read_text())
    level_data = data.get(level.lower(), {})
    if isinstance(level_data, dict):
        return list(level_data.keys())   # new format: {topic_name: {keywords, description}}
    return level_data                    # old format: [topic_name, ...]


def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text())
    return {}


def save_processed(processed: dict) -> None:
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2))


def parse_pdf_filename(filename: str) -> tuple[str, int, str]:
    """Infer (school, year, level) from any PDF filename.

    Handles arbitrary separators and word order, e.g.:
      2025-P5-Science-End Of Year Exam-ACS Junior.pdf
      ACS_Junior_2025_P5.pdf
      Raffles Girls 2024 P5 SA2.pdf
    """
    stem = Path(filename).stem
    text = re.sub(r"[-_]+", " ", stem)

    year_match = re.search(r"\b(20\d{2})\b", text)
    year = int(year_match.group(1)) if year_match else 0

    level_match = re.search(r"\b(P[456]|PSLE)\b", text, re.IGNORECASE)
    level = level_match.group(1).upper() if level_match else "P5"

    _NOISE = {
        "science", "exam", "examination", "test", "paper", "booklet",
        "end", "of", "year", "mid", "term", "sa1", "sa2", "ca1", "ca2",
        "primary", "school", "annual", "semestral", "assessment", "eoy", "mye",
    }
    if year:
        text = text.replace(str(year), " ")
    text = re.sub(r"\b(?:P[456]|PSLE)\b", " ", text, flags=re.IGNORECASE)
    tokens = [t for t in text.split() if t.lower() not in _NOISE and t.strip()]
    school = " ".join(tokens).strip() or stem

    return school, year, level


def flatten_questions(raw_questions: list[dict], school: str) -> list[dict]:
    """Attach parent diagrams_and_tables to each sub-question as parent_diagrams_and_tables."""
    parent_diagrams: dict[str, list] = {}
    for q in raw_questions:
        pid = q.get("parent_question_id", "")
        sub = q.get("sub_question_part", "none")
        if sub in ("none", "", None) and not q.get("question_text", "").strip():
            parent_diagrams[pid] = q.get("diagrams_and_tables", [])

    result = []
    for q in raw_questions:
        pid = q.get("parent_question_id", "")
        sub = q.get("sub_question_part", "none")
        if sub in ("none", "", None) and not q.get("question_text", "").strip() and int(q.get("marks", 0)) == 0:
            continue
        q_copy = dict(q)
        q_copy["parent_diagrams_and_tables"] = parent_diagrams.get(pid, [])
        q_copy["school"] = school
        # Normalise difficulty to int 1-10 (model may return string or old label)
        raw_diff = q_copy.get("difficulty", 5)
        if isinstance(raw_diff, str):
            raw_diff = {"easy": 3, "medium": 5, "hard": 8}.get(raw_diff.lower(), 5)
        q_copy["difficulty"] = max(1, min(10, int(raw_diff))) if raw_diff else 5
        result.append(q_copy)
    return result


def extract_json_array(text: str | None) -> list:
    """Extract a JSON array from a Gemini response that may include markdown fences,
    leading/trailing prose, or a second concatenated value."""
    if not text:
        raise ValueError("Empty Gemini response")
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find a top-level JSON array via brace-counting and parse just that span.
    start = text.find("[")
    if start == -1:
        raise ValueError("No JSON array found in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Unterminated JSON array in response")


# ---------------------------------------------------------------------------
# Gemini extraction
# ---------------------------------------------------------------------------

def extract_questions_from_pdf(pdf_path: Path, school: str, year: int, level: str) -> list[dict]:
    prompt_template = EXTRACTION_PROMPT_FILE.read_text()

    topics = load_syllabus_topics(level)
    topics_str = (
        "\n".join(f"- {t}" for t in topics)
        if topics
        else "(syllabus_topics.json not found — infer topic names from context)"
    )

    prompt = (
        prompt_template
        .replace("{SCHOOL}", school)
        .replace("{YEAR}", str(year))
        .replace("{LEVEL}", level)
        .replace("{TOPICS}", topics_str)
    )

    print(f"  [{pdf_path.name}] Uploading to Gemini Files API…")
    uploaded = client.files.upload(
        file=pdf_path,
        config=types.UploadFileConfig(mime_type="application/pdf"),
    )

    for _ in range(30):
        file_info = client.files.get(name=uploaded.name)
        if file_info.state.name == "ACTIVE":
            break
        if file_info.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {pdf_path.name}")
        time.sleep(2)
    else:
        raise RuntimeError(f"Timed out waiting for Gemini to process {pdf_path.name}")

    def _call(model: str):
        return client.models.generate_content(model=model, contents=[uploaded, prompt])

    print(f"  [{pdf_path.name}] Calling {GEMINI_EXTRACTION_MODEL}…")
    response = None
    fallback_reason = None
    try:
        response = _call(GEMINI_EXTRACTION_MODEL)
        if not (response.text and response.text.strip()):
            fallback_reason = "empty response"
    except Exception as e:
        if _is_503(e):
            fallback_reason = "503 unavailable"
        else:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            raise

    if fallback_reason is not None:
        print(f"  [{pdf_path.name}] {GEMINI_EXTRACTION_MODEL} {fallback_reason}; "
              f"falling back to {GEMINI_EXTRACTION_FALLBACK}…")
        response = _call(GEMINI_EXTRACTION_FALLBACK)

    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    return extract_json_array(response.text)


# ---------------------------------------------------------------------------
# JSONL merge
# ---------------------------------------------------------------------------

def load_master_jsonl(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        records[record["school"]] = record
    return records


def save_master_jsonl(path: Path, records: dict[str, dict]) -> None:
    lines = [json.dumps(rec, ensure_ascii=False) for rec in records.values()]
    path.write_text("\n".join(lines) + "\n")


def dedup_key(q: dict) -> tuple:
    return (q.get("parent_question_id", ""), q.get("sub_question_part", "none"))


def merge_questions(existing: list[dict], new_questions: list[dict]) -> list[dict]:
    seen = {dedup_key(q) for q in existing}
    merged = list(existing)
    for q in new_questions:
        k = dedup_key(q)
        if k not in seen:
            merged.append(q)
            seen.add(k)
    return merged


def update_master_jsonl(bank: str, school: str, new_questions: list[dict],
                        replace: bool = False) -> str:
    """Merge questions into master JSONL. Caller must hold jsonl_lock.

    When `replace` is True the school's existing questions are overwritten
    wholesale (used by --force re-extracts so bbox-augmented entries replace
    older ones, not get deduped against them)."""
    master_path = BANKS_DIR / bank / "master_questions.jsonl"
    records = load_master_jsonl(master_path)

    if school in records and not replace:
        existing = records[school]["questions"]
        merged = merge_questions(existing, new_questions)
        records[school]["questions"] = merged
        added = len(merged) - len(existing)
        note = f"{added} new question(s) added ({len(merged)} total)"
    else:
        prev_n = len(records[school]["questions"]) if school in records else 0
        records[school] = {"school": school, "questions": new_questions}
        note = (f"replaced {prev_n} → {len(new_questions)} question(s)"
                if school in records and replace
                else f"new school, {len(new_questions)} question(s)")

    save_master_jsonl(master_path, records)
    return note


# ---------------------------------------------------------------------------
# Single-PDF worker
# ---------------------------------------------------------------------------

def process_one_pdf(
    pdf_path: Path,
    bank: str,
    output_dir: Path,
    processed: dict,
    proc_lock: threading.Lock,
    jsonl_lock: threading.Lock,
    force: bool = False,
) -> str:
    """Process one PDF. Returns a status string. Raises on failure."""
    key = f"{bank}/{pdf_path.name}"

    if not force:
        with proc_lock:
            if key in processed:
                return "already processed — skipped"

    school, year, level = parse_pdf_filename(pdf_path.name)
    print(f"  [{pdf_path.name}] school={school!r} year={year} level={level}")

    raw_questions = extract_questions_from_pdf(pdf_path, school, year, level)
    questions = flatten_questions(raw_questions, school)
    n = len(questions)

    # Save per-school JSON (thread-safe: unique filename per PDF)
    safe_name = re.sub(r"[^\w\-.]", "_", pdf_path.stem)
    json_path = output_dir / f"{safe_name}_{n}questions.json"
    json_path.write_text(json.dumps(questions, indent=2, ensure_ascii=False))

    with jsonl_lock:
        note = update_master_jsonl(bank, school, questions, replace=force)

    with proc_lock:
        processed[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_processed(processed)

    return f"{n} question(s) — {note}"


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_bank(bank: str, only: str | None = None, force: bool = False) -> None:
    papers_dir = BANKS_DIR / bank / "papers"
    output_dir = OUTPUT_JSON_DIR / bank
    output_dir.mkdir(parents=True, exist_ok=True)

    processed = load_processed()
    all_pdfs = sorted(papers_dir.glob("*.pdf"))

    if only:
        needle = only.lower()
        all_pdfs = [p for p in all_pdfs if needle in p.stem.lower()]
        if not all_pdfs:
            print(f"--only {only!r} matched zero PDFs in {papers_dir}")
            return

    if not all_pdfs:
        print(f"No PDFs found in {papers_dir}")
        return

    if force:
        new_pdfs = all_pdfs
        print(f"--force: re-extracting {len(new_pdfs)} PDF(s) regardless of processed.json")
    else:
        new_pdfs = [p for p in all_pdfs if f"{bank}/{p.name}" not in processed]
        skipped = len(all_pdfs) - len(new_pdfs)
        if skipped:
            print(f"Skipping {skipped} already-processed PDF(s).")
    if not new_pdfs:
        print("Nothing new to process.")
        return

    print(f"New PDFs to process: {len(new_pdfs)}")

    proc_lock = threading.Lock()
    jsonl_lock = threading.Lock()

    def run(pdf_path: Path) -> tuple[Path, str | None, str | None]:
        try:
            msg = process_one_pdf(pdf_path, bank, output_dir, processed,
                                  proc_lock, jsonl_lock, force=force)
            return pdf_path, msg, None
        except Exception as e:
            return pdf_path, None, str(e)

    # --- Pilot: run the first PDF alone to validate config/API key ---
    pilot, *remaining = new_pdfs
    print(f"\n--- Pilot: {pilot.name} ---")
    _, msg, err = run(pilot)
    if err:
        print(f"  FAILED: {err}")
        print("Aborting — fix the error above before processing the rest.")
        return
    print(f"  OK: {msg}")

    if not remaining:
        print("\nAll done (only one PDF).")
        return

    # --- Batch: remaining PDFs, 10 concurrent ---
    print(f"\n--- Batch: {len(remaining)} PDF(s) at 10 concurrent ---")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(run, p): p for p in remaining}
        for future in as_completed(futures):
            p, msg, err = future.result()
            if err:
                print(f"  FAILED [{p.name}]: {err}")
            else:
                print(f"  OK    [{p.name}]: {msg}")

    print(f"\nDone. Processed {len(new_pdfs)} PDF(s).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global client

    parser = argparse.ArgumentParser(description="Extract Science exam questions from PDFs")
    parser.add_argument("--bank", required=True, choices=["p4", "p5"],
                        help="Question bank to process")
    parser.add_argument("--only", help="Process only PDFs whose filename contains this substring")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if already in processed.json")
    args = parser.parse_args()

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    process_bank(args.bank, only=args.only, force=args.force)


if __name__ == "__main__":
    main()
