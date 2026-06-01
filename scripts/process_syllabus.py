#!/usr/bin/env python3
"""One-time script: extract topic taxonomy from syllabus.pdf → syllabus_topics.json.

Run once. Safe to re-run — skips if syllabus_topics.json already exists.
Delete syllabus_topics.json to force a re-process.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
SYLLABUS_PDF = ROOT / "syllabus.pdf"
OUTPUT_FILE = ROOT / "syllabus_topics.json"
MODEL = "gemini-3.5-flash"
MODEL_FALLBACK = "gemini-3-flash-preview"

PROMPT = """
You are extracting the complete topic taxonomy from a Singapore Primary School Science syllabus.

Extract ALL topics and subtopics organised by Primary level (P3, P4, P5, P6).
For each topic, also extract the key vocabulary words students must know and a one-sentence description.

Output ONLY this JSON, no other text:
{
  "p3": {
    "Topic Name": {
      "description": "One sentence describing what this topic covers at this level.",
      "keywords": ["keyword1", "keyword2", "keyword3", ...]
    }
  },
  "p4": { ... },
  "p5": { ... },
  "p6": { ... }
}

Rules:
- Topic names in title case: "Plant Reproduction", "Electrical Circuits", "The Digestive System"
- Include both broad topics and specific subtopics as separate top-level keys
- If a topic spans multiple levels, include it under each level
- keywords: 8-15 specific science vocabulary words students encounter in exam questions on this topic
  (process names, organ names, material names, scientific terms — NOT generic words like "explain" or "describe")
- description: written for a primary school level, one sentence
- Output ONLY the JSON, no markdown, no explanation
"""


def main() -> None:
    if OUTPUT_FILE.exists():
        data = json.loads(OUTPUT_FILE.read_text())
        print("syllabus_topics.json already exists:")
        for level, level_data in data.items():
            n = len(level_data) if isinstance(level_data, dict) else len(level_data)
            print(f"  {level.upper()}: {n} topics")
        print("Delete syllabus_topics.json to re-process.")
        return

    if not SYLLABUS_PDF.exists():
        print(f"ERROR: {SYLLABUS_PDF} not found", file=sys.stderr)
        sys.exit(1)

    # API key is read from the global environment (set GEMINI_API_KEY in your shell).
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("ERROR: Set the GEMINI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Uploading syllabus.pdf…")
    uploaded = client.files.upload(
        file=SYLLABUS_PDF,
        config=types.UploadFileConfig(mime_type="application/pdf"),
    )

    for _ in range(30):
        info = client.files.get(name=uploaded.name)
        if info.state.name == "ACTIVE":
            break
        if info.state.name == "FAILED":
            print("ERROR: Gemini file processing failed", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
    else:
        print("ERROR: Timed out waiting for file to process", file=sys.stderr)
        sys.exit(1)

    print(f"Extracting topics with {MODEL}…")
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[uploaded, PROMPT],
        )
    except Exception as e:
        if "503" in str(e) or "UNAVAILABLE" in str(e):
            print(f"  {MODEL} unavailable; falling back to {MODEL_FALLBACK}…")
            response = client.models.generate_content(
                model=MODEL_FALLBACK,
                contents=[uploaded, PROMPT],
            )
        else:
            raise

    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    text = response.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    topics = json.loads(text)

    OUTPUT_FILE.write_text(json.dumps(topics, indent=2, ensure_ascii=False))
    print(f"\nSaved → syllabus_topics.json")
    for level, level_data in topics.items():
        n = len(level_data) if isinstance(level_data, dict) else len(level_data)
        print(f"  {level.upper()}: {n} topics")


if __name__ == "__main__":
    main()
