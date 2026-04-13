#!/usr/bin/env python3
"""
OpenAI smoke test — mirrors the exact client/model/call pattern used by the apply engine.

Client init:  OpenAI(api_key=...) — same as runner._try_load_llm_client()
Model:        gpt-5-mini          — same default as ApplyConfig.llm_model
API method:   chat.completions.create — same as answer_engine._try_openai_generated_answer()
Params:       max_completion_tokens (NOT max_tokens), no temperature for gpt-5* models

Usage:
    OPENAI_API_KEY=sk-... python scripts/test_openai.py [--model gpt-4o-mini]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from any directory inside the project
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass  # dotenv optional — OPENAI_API_KEY may already be in the environment


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI smoke test for the apply engine")
    parser.add_argument(
        "--model",
        default="gpt-5-mini",
        help="Model to test (default: gpt-5-mini, same as apply engine default)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1000,
        # gpt-5-mini is a reasoning model: max_completion_tokens covers both the
        # internal reasoning budget AND the visible output.  With a low budget
        # (e.g. 50-220) the model can exhaust all tokens on reasoning and return
        # content=''.  Use at least 500–1000 for short answers; the apply engine
        # uses llm_max_tokens=1500 as its default for gpt-5-mini.
        help="max_completion_tokens for the request (default: 1000)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY is not set in the environment or .env file", file=sys.stderr)
        return 1

    print(f"OPENAI_API_KEY  : {'*' * 8}{api_key[-4:]}")
    print(f"Model           : {args.model}")
    print(f"max_comp_tokens : {args.max_tokens}")
    print()

    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package is not installed. Run: pip install openai", file=sys.stderr)
        return 1

    # Same client construction as runner._try_load_llm_client()
    client = OpenAI(api_key=api_key)

    prompt = "Reply with exactly: hello world"

    # Same request_kwargs logic as answer_engine._try_openai_generated_answer():
    #   - gpt-5* models do NOT accept temperature (omit it)
    #   - all other models get temperature=0.2
    #   - use max_completion_tokens (not the legacy max_tokens)
    request_kwargs: dict = {
        "model": args.model,
        "max_completion_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if not args.model.startswith("gpt-5"):
        # Non gpt-5 models accept temperature; gpt-5* rejects it
        request_kwargs["temperature"] = 0.2

    print(f"Sending request: {request_kwargs}")
    print()

    try:
        response = client.chat.completions.create(**request_kwargs)
    except Exception as exc:
        print(f"ERROR calling chat.completions.create: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # --- Diagnostics (mirrors what answer_engine logs) ---
    response_type = type(response).__name__

    # Extract message — try new SDK (attribute access) then fall back to dict
    message = None
    try:
        message = response.choices[0].message
    except (AttributeError, TypeError, IndexError):
        pass
    if message is None:
        try:
            choices = (
                response.get("choices")
                if isinstance(response, dict)
                else getattr(response, "choices", None)
            )
            if choices:
                first = choices[0]
                message = (
                    first.get("message")
                    if isinstance(first, dict)
                    else getattr(first, "message", None)
                )
        except Exception:
            pass

    # Extract text content
    extracted_text: str | None = None
    if isinstance(message, dict):
        extracted_text = message.get("content")
    elif message is not None:
        extracted_text = getattr(message, "content", None)

    # Normalise to string
    if extracted_text is None:
        extracted_text_str = ""
    elif isinstance(extracted_text, str):
        extracted_text_str = extracted_text
    elif isinstance(extracted_text, list):
        # Content-block list (Responses API style sometimes leaks through)
        parts = []
        for block in extracted_text:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(getattr(block, "text", None) or getattr(block, "content", None) or "")
        extracted_text_str = "\n".join(p for p in parts if p)
    else:
        extracted_text_str = str(extracted_text)

    # finish_reason and usage are key diagnostics:
    #   finish_reason='length' + content='' → reasoning model burned all tokens internally
    #   finish_reason='stop'               → normal completion
    finish_reason = "N/A"
    try:
        finish_reason = response.choices[0].finish_reason
    except Exception:
        pass

    usage = getattr(response, "usage", None)
    usage_repr = repr(usage) if usage is not None else "N/A"

    print(f"response object type : {response_type}")
    print(f"model used           : {getattr(response, 'model', 'N/A')}")
    print(f"finish_reason        : {finish_reason}")
    print(f"usage                : {usage_repr}")
    print(f"raw response repr    : {repr(response)[:400]}")
    print()
    print(f"extracted text repr  : {repr(extracted_text_str)}")
    print(f"extracted text length: {len(extracted_text_str)}")
    print()

    if extracted_text_str.strip():
        print("SMOKE TEST PASSED — got a non-empty text response")
        return 0
    else:
        print("SMOKE TEST FAILED — extracted text is empty", file=sys.stderr)
        print(f"  message repr: {repr(message)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
