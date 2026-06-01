#!/usr/bin/env python3
"""Gemini Bridge — delegated API calls per the Orchestrator Mandate.

Centralises Gemini calls used across the pipeline so high-volume work
(MCQ validation, bulk extraction, batch shaping) routes through a single
client with a tiered model class + automatic fallback chain.

Model classes mirror the mandate:
  PRO         — architectural / complex pedagogical reasoning
  FLASH       — standard coding, doc analysis, general reviews
  FLASH_LITE  — high-volume data cleaning, bulk boilerplate (cheapest text)
  NANO_BANANA — image generation (mockups, infographics, diagrams)

Fallback rule: try the latest-gen model first; on 503/quota fall through to
older generations of the SAME class before giving up. Caller (Claude) is the
final safety net per the mandate.

Usage as a module:
    from agents.gemini_bridge import call, ModelClass
    text = call(ModelClass.FLASH_LITE, "Summarise: …")
    obj  = call(ModelClass.FLASH, prompt, json_mode=True)

CLI:
    python .agents/gemini_bridge.py --class flash-lite --prompt "…"
    python .agents/gemini_bridge.py --class pro --prompt-file p.txt --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
# API key is read from the global environment (set GEMINI_API_KEY in your shell).


class ModelClass(str, Enum):
    PRO = "pro"
    FLASH = "flash"
    FLASH_LITE = "flash-lite"
    NANO_BANANA = "nano-banana"


# Fallback chains — first entry is the primary, subsequent entries are tried
# on 503 / quota / explicit retryable errors. Match the constants already in
# pipeline.py / image_generator.py so the bridge stays in sync with CLAUDE.md.
FALLBACK_CHAINS: dict[ModelClass, list[str]] = {
    ModelClass.PRO:         ["gemini-3.5-flash", "gemini-3-flash-preview"],
    ModelClass.FLASH:       ["gemini-3.5-flash", "gemini-3-flash-preview"],
    ModelClass.FLASH_LITE:  ["gemini-3.5-flash", "gemini-3-flash-preview"],
    ModelClass.NANO_BANANA: ["gemini-3.1-flash-image-preview"],
}

# Per-class reasoning depth (gemini-3.5-flash = "3.5 Flash"). PRO does the
# deep reasoning work (HTML generation, complex pedagogy) at thinking=high;
# FLASH_LITE runs cheap high-volume bulk work (data-cleaning / boilerplate) at
# thinking=minimal; plain FLASH uses the model default (no thinking_config).
CLASS_THINKING: dict[ModelClass, str] = {
    ModelClass.PRO:        "high",
    ModelClass.FLASH_LITE: "minimal",
}

RETRYABLE_SUBSTRINGS = ("503", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "quota")


@dataclass
class CallResult:
    text: str
    model_used: str
    attempts: list[tuple[str, str]] = field(default_factory=list)  # (model, status)
    raw_response: object | None = None

    def json(self) -> object:
        return json.loads(self.text)


_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set in environment")
        _client = genai.Client(api_key=api_key)
    return _client


def _is_retryable(err: Exception) -> bool:
    s = str(err)
    return any(t in s for t in RETRYABLE_SUBSTRINGS)


def call(
    model_class: ModelClass | str,
    prompt: str,
    *,
    json_mode: bool = False,
    system_instruction: str | None = None,
    thinking_budget: int | None = None,
    temperature: float | None = None,
    max_retries_per_model: int = 1,
    verbose: bool = True,
) -> CallResult:
    """Send `prompt` through the fallback chain for `model_class`.

    Returns a CallResult with the response text, the model that succeeded, and
    a per-model attempt log. Raises the last exception if every model fails.
    """
    if isinstance(model_class, str):
        model_class = ModelClass(model_class)
    if model_class is ModelClass.NANO_BANANA:
        raise ValueError(
            "NANO_BANANA is image-only — use image_generator.py / a dedicated helper, not call()."
        )

    chain = FALLBACK_CHAINS[model_class]
    client = get_client()

    config_kwargs: dict = {}
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    else:
        thinking = CLASS_THINKING.get(model_class)
        if thinking:
            # google-genai accepts the level as a raw string ("minimal".."high").
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)

    attempts: list[tuple[str, str]] = []
    last_err: Exception | None = None

    for model in chain:
        for retry in range(max_retries_per_model + 1):
            try:
                if verbose:
                    print(f"[bridge] {model_class.value} → {model}"
                          + (f" (retry {retry})" if retry else ""), file=sys.stderr)
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs) if config_kwargs else None,
                )
                attempts.append((model, "ok"))
                return CallResult(text=resp.text, model_used=model, attempts=attempts, raw_response=resp)
            except Exception as e:
                last_err = e
                attempts.append((model, type(e).__name__ + ": " + str(e)[:120]))
                if not _is_retryable(e) or retry == max_retries_per_model:
                    if verbose:
                        print(f"[bridge] {model} failed: {e}", file=sys.stderr)
                    break
                time.sleep(2 ** retry)

    raise RuntimeError(
        f"All models in chain {chain} failed for class {model_class.value}. "
        f"Attempts: {attempts}. Last error: {last_err}"
    ) from last_err


def main() -> int:
    ap = argparse.ArgumentParser(description="Gemini bridge CLI")
    ap.add_argument("--class", dest="cls", required=True,
                    choices=[c.value for c in ModelClass if c is not ModelClass.NANO_BANANA])
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--prompt", help="Inline prompt text")
    grp.add_argument("--prompt-file", type=Path, help="Read prompt from file")
    ap.add_argument("--json", action="store_true", help="Request JSON response (mime application/json)")
    ap.add_argument("--system", help="System instruction")
    ap.add_argument("--temperature", type=float)
    args = ap.parse_args()

    prompt = args.prompt if args.prompt else args.prompt_file.read_text()
    result = call(
        ModelClass(args.cls),
        prompt,
        json_mode=args.json,
        system_instruction=args.system,
        temperature=args.temperature,
    )
    print(result.text)
    print(f"\n--- bridge stats: model={result.model_used} attempts={result.attempts}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
