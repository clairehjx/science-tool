#!/usr/bin/env python3
"""Extract MCQ questions from Booklet A of Singapore Science exam PDFs using Gemini.

Mirrors scripts/pdf_extractor.py but targets multiple-choice questions only.
Output: banks/{bank}/master_mcq.jsonl (one JSON line per school).
"""

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

# Reuse existing helpers — keep them in one place.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_extractor import (        # noqa: E402
    parse_pdf_filename,
    extract_json_array,
    load_master_jsonl,
    save_master_jsonl,
    load_syllabus_topics,
)

ROOT = Path(__file__).resolve().parent.parent
BANKS_DIR = ROOT / "banks"
OUTPUT_JSON_DIR = ROOT / "output" / "json"
PROCESSED_FILE = ROOT / "processed_mcq.json"
EXTRACTION_PROMPT_FILE = ROOT / "prompts" / "mcq_extraction_prompt.txt"

GEMINI_EXTRACTION_MODEL = "gemini-3.5-flash"
GEMINI_EXTRACTION_FALLBACK = "gemini-3-flash-preview"

client: genai.Client | None = None


def _is_503(e: Exception) -> bool:
    s = str(e)
    return "503" in s or "UNAVAILABLE" in s


def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text())
    return {}


def save_processed(processed: dict) -> None:
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2))


def normalise_mcq(raw_questions: list[dict], school: str) -> list[dict]:
    """Stamp question_kind=mcq, attach school, drop incomplete records."""
    result = []
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        options = q.get("options") or {}
        if not isinstance(options, dict) or not all(k in options for k in ("A", "B", "C", "D")):
            continue
        if not q.get("correct_option") or not q.get("question_text"):
            continue
        q_copy = dict(q)
        q_copy["question_kind"] = "mcq"
        q_copy["school"] = school
        # Marks default
        try:
            q_copy["marks"] = int(q_copy.get("marks", 2))
        except (TypeError, ValueError):
            q_copy["marks"] = 2
        # Page number must be int
        try:
            q_copy["page_number"] = int(q_copy.get("page_number", 0))
        except (TypeError, ValueError):
            q_copy["page_number"] = 0
        # Difficulty fallback
        raw_diff = q_copy.get("difficulty", 5)
        if isinstance(raw_diff, str):
            raw_diff = {"easy": 3, "medium": 5, "hard": 8}.get(raw_diff.lower(), 5)
        q_copy["difficulty"] = max(1, min(10, int(raw_diff))) if raw_diff else 5
        result.append(q_copy)
    return result


def extract_mcqs_from_pdf(pdf_path: Path, school: str, year: int, level: str) -> list[dict]:
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


def dedup_key(q: dict) -> tuple:
    return (q.get("parent_question_id", ""),)


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
                       pdf_filename: str, replace: bool = False) -> str:
    master_path = BANKS_DIR / bank / "master_mcq.jsonl"
    records = load_master_jsonl(master_path)
    if school in records and not replace:
        existing = records[school].get("questions", [])
        merged = merge_questions(existing, new_questions)
        records[school]["questions"] = merged
        records[school].setdefault("pdf_filename", pdf_filename)
        added = len(merged) - len(existing)
        note = f"{added} new MCQ(s) added ({len(merged)} total)"
    else:
        prev_n = len(records[school]["questions"]) if school in records else 0
        records[school] = {
            "school": school,
            "pdf_filename": pdf_filename,
            "questions": new_questions,
        }
        note = (f"replaced {prev_n} → {len(new_questions)} MCQ(s)"
                if prev_n and replace
                else f"new school, {len(new_questions)} MCQ(s)")
    save_master_jsonl(master_path, records)
    return note


def process_one_pdf(pdf_path: Path, bank: str, output_dir: Path,
                    processed: dict, proc_lock: threading.Lock,
                    jsonl_lock: threading.Lock, force: bool = False) -> str:
    key = f"{bank}/{pdf_path.name}"
    if not force:
        with proc_lock:
            if key in processed:
                return "already processed — skipped"

    school, year, level = parse_pdf_filename(pdf_path.name)
    print(f"  [{pdf_path.name}] school={school!r} year={year} level={level}")

    raw = extract_mcqs_from_pdf(pdf_path, school, year, level)
    questions = normalise_mcq(raw, school)
    n = len(questions)

    safe_name = re.sub(r"[^\w\-.]", "_", pdf_path.stem)
    json_path = output_dir / f"{safe_name}_{n}mcqs.json"
    json_path.write_text(json.dumps(questions, indent=2, ensure_ascii=False))

    with jsonl_lock:
        note = update_master_jsonl(bank, school, questions, pdf_path.name,
                                   replace=force)

    with proc_lock:
        processed[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_processed(processed)

    return f"{n} MCQ(s) — {note}"


def process_bank(bank: str, only: str | None = None, force: bool = False) -> None:
    papers_dir = BANKS_DIR / bank / "papers"
    output_dir = OUTPUT_JSON_DIR / bank / "_mcq"
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

    def run(pdf_path: Path):
        try:
            return pdf_path, process_one_pdf(pdf_path, bank, output_dir,
                                             processed, proc_lock, jsonl_lock,
                                             force=force), None
        except Exception as e:
            return pdf_path, None, str(e)

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


def main() -> None:
    global client
    parser = argparse.ArgumentParser(description="Extract MCQs from Booklet A of exam PDFs")
    parser.add_argument("--bank", required=True, choices=["p4", "p5"])
    parser.add_argument("--only", help="Process only PDFs whose filename contains this substring")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if already in processed_mcq.json")
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
