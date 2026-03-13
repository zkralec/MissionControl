"""
OpenAI LLM adapter for Mission Control.
Handles chat completions with token tracking and catalog-driven cost calculation.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import logging

from openai import OpenAI, APIError, APIConnectionError

from ai_usage_log import log_ai_usage
from models.catalog import get_available_models, get_model_price, is_allowed_model

logger = logging.getLogger(__name__)
EIGHT_DP = Decimal("0.00000001")


def _build_pricing_index() -> dict[str, dict[str, Decimal]]:
    """Build per-token pricing index from the catalog."""
    pricing: dict[str, dict[str, Decimal]] = {}
    for model_id in get_available_models():
        input_per_token, output_per_token = get_model_price(model_id)
        pricing[model_id] = {
            "input": input_per_token,
            "output": output_per_token,
        }
    return pricing


# Backward-compatible export used in tests; values come from catalog (not hardcoded).
PRICING = _build_pricing_index()


def get_client() -> OpenAI:
    """
    Get or create OpenAI client.
    Uses OPENAI_API_KEY from environment.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")
    return OpenAI(api_key=api_key)


def _extract_output_text(message: object) -> str:
    if message is None:
        return ""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content

    def _chunk_text(chunk: object) -> str:
        if isinstance(chunk, str):
            return chunk
        if isinstance(chunk, dict):
            for key in ("text", "content", "value"):
                value = chunk.get(key)
                if isinstance(value, str):
                    return value
        for attr in ("text", "content", "value"):
            value = getattr(chunk, attr, None)
            if isinstance(value, str):
                return value
        return ""

    if isinstance(content, list):
        parts = [_chunk_text(item).strip() for item in content]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined

    if content is not None:
        fallback = _chunk_text(content).strip()
        if fallback:
            return fallback

    refusal = getattr(message, "refusal", None)
    if isinstance(refusal, str):
        return refusal
    return ""


def run_chat_completion(
    model: str,
    messages: list[dict],
    temperature: Optional[float] = None,
    max_completion_tokens: Optional[int] = None,
    # Backward-compatible alias. Do not send this parameter to OpenAI.
    max_tokens: Optional[int] = None,
    task_run_id: Optional[str] = None,
    agent_name: str = "worker",
) -> dict:
    """
    Execute a chat completion call to OpenAI.

    Args:
        model: Model name (must exist in catalog)
        messages: List of message dicts with 'role' and 'content'
        temperature: Sampling temperature (0.0 to 2.0), applied for non-5-series models.
        max_completion_tokens: Maximum completion tokens (modern API parameter).
        max_tokens: Deprecated alias, mapped to max_completion_tokens for compatibility.
        task_run_id: Optional task run identifier for usage analytics correlation.
        agent_name: Logical caller name for usage analytics.

    Returns:
        {
            "output_text": str,         # Generated response
            "tokens_in": int,           # Prompt tokens
            "tokens_out": int,          # Completion tokens
            "cost_usd": Decimal,        # Total cost (8dp)
            "model": str,               # Model used
            "openai_request_id": str | None,
        }

    Raises:
        ValueError: If model not found in catalog
        APIError: If OpenAI API call fails
    """
    pricing = get_pricing(model)
    if pricing is None:
        raise ValueError(
            f"Unknown model '{model}' in catalog. Available: {get_available_models()}"
        )
    if max_completion_tokens is not None and max_tokens is not None:
        raise ValueError("Provide only one of max_completion_tokens or max_tokens")

    client = get_client()
    resolved_max_completion_tokens = max_completion_tokens
    if resolved_max_completion_tokens is None:
        resolved_max_completion_tokens = max_tokens

    request_kwargs: dict[str, object] = {
        "model": model,
        "messages": messages,
    }
    if resolved_max_completion_tokens is not None:
        request_kwargs["max_completion_tokens"] = int(resolved_max_completion_tokens)
    if temperature is not None and not model.startswith("gpt-5"):
        request_kwargs["temperature"] = float(temperature)

    start_time = time.perf_counter()

    def _safe_log_usage(**kwargs: object) -> None:
        try:
            log_ai_usage(**kwargs)
        except Exception:
            logger.exception("ai_usage_logging_failed")

    try:
        response = client.chat.completions.create(**request_kwargs)

        usage = getattr(response, "usage", None)
        usage_tokens_in = getattr(usage, "prompt_tokens", None)
        usage_tokens_out = getattr(usage, "completion_tokens", None)
        usage_total_tokens = getattr(usage, "total_tokens", None)

        tokens_in = response.usage.prompt_tokens
        tokens_out = response.usage.completion_tokens
        output_text = _extract_output_text(response.choices[0].message)
        openai_request_id = getattr(response, "_request_id", None)

        cost_usd = (
            Decimal(tokens_in) * pricing["input"] +
            Decimal(tokens_out) * pricing["output"]
        ).quantize(EIGHT_DP, rounding=ROUND_HALF_UP)
        latency_ms = int((time.perf_counter() - start_time) * 1000)

        _safe_log_usage(
            task_run_id=task_run_id,
            agent_name=agent_name,
            model=model,
            tokens_in=usage_tokens_in,
            tokens_out=usage_tokens_out,
            total_tokens=usage_total_tokens,
            cost_usd=cost_usd if usage_tokens_in is not None and usage_tokens_out is not None else None,
            latency_ms=latency_ms,
            status="succeeded",
            error_text=None,
        )

        return {
            "output_text": output_text,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "model": model,
            "openai_request_id": openai_request_id,
        }

    except APIConnectionError as e:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        _safe_log_usage(
            task_run_id=task_run_id,
            agent_name=agent_name,
            model=model,
            tokens_in=None,
            tokens_out=None,
            total_tokens=None,
            cost_usd=None,
            latency_ms=latency_ms,
            status="failed",
            error_text=f"{type(e).__name__}: {e}",
        )
        logger.error(
            "OpenAI API connection error: %s (request_id=%s)",
            e,
            getattr(e, "request_id", None),
        )
        raise
    except APIError as e:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        _safe_log_usage(
            task_run_id=task_run_id,
            agent_name=agent_name,
            model=model,
            tokens_in=None,
            tokens_out=None,
            total_tokens=None,
            cost_usd=None,
            latency_ms=latency_ms,
            status="failed",
            error_text=f"{type(e).__name__}: {e}",
        )
        logger.error(
            "OpenAI API error: %s (request_id=%s)",
            e,
            getattr(e, "request_id", None),
        )
        raise
    except Exception as e:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        _safe_log_usage(
            task_run_id=task_run_id,
            agent_name=agent_name,
            model=model,
            tokens_in=None,
            tokens_out=None,
            total_tokens=None,
            cost_usd=None,
            latency_ms=latency_ms,
            status="failed",
            error_text=f"{type(e).__name__}: {e}",
        )
        raise


def format_messages(task_type: str, payload_json: str) -> list[dict]:
    """
    Format task payload into OpenAI messages format.

    Args:
        task_type: Type of task (e.g., "jobs_digest", "deals_scan")
        payload_json: Raw JSON string of task data

    Returns:
        List of message dicts for OpenAI API
    """
    system_prompt = _get_system_prompt(task_type)

    return [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": f"Please process the following data:\n\n{payload_json}",
        },
    ]


def _get_system_prompt(task_type: str) -> str:
    """Get system prompt for task type."""
    prompts = {
        "jobs_digest": (
            "You are a job opportunity analyst. "
            "Analyze the provided job data and create a concise digest highlighting "
            "key opportunities, salary ranges, required skills, and growth potential."
        ),
        "deals_scan": (
            "You are a business deal analyzer. "
            "Review the provided business deal information and provide a structured analysis "
            "including key terms, risks, opportunities, and recommendation."
        ),
        "slides_outline": (
            "You are a presentation expert. "
            "Based on the provided content, create a logical outline for a presentation "
            "including key sections, talking points, and visual suggestions."
        ),
        "extraction": (
            "You are a data extraction specialist. "
            "Extract and structure the key information from the provided content "
            "into a clean, organized format."
        ),
    }
    return prompts.get(task_type, "You are a helpful assistant.")


def get_pricing(model: str) -> Optional[dict[str, Decimal]]:
    """Get per-token pricing for a model from catalog-derived index."""
    if not is_model_available(model):
        return None
    return PRICING[model]


def is_model_available(model: str) -> bool:
    """Check if model is available in catalog."""
    return is_allowed_model(model)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """
    Estimate cost for given tokens and model.

    Args:
        model: Model name
        tokens_in: Input tokens
        tokens_out: Output tokens

    Returns:
        Estimated cost in USD
    """
    pricing = get_pricing(model)
    if pricing is None:
        raise ValueError(
            f"Unknown model '{model}' in catalog. Available: {get_available_models()}"
        )

    return (
        Decimal(tokens_in) * pricing["input"] +
        Decimal(tokens_out) * pricing["output"]
    ).quantize(EIGHT_DP, rounding=ROUND_HALF_UP)
