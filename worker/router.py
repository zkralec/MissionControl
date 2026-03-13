"""
Model routing module.
Determines which LLM model to use from catalog-defined tiers and routing rules.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional

from models.catalog import (
    get_available_models as catalog_available_models,
    get_model_info as catalog_model_info,
    get_routing_rules,
    is_allowed_model,
    tier_model,
)


ADVANCED_TASK_TYPES = {"slides_outline", "slides_outline_v1", "jobs_rank_v1", "jobs_digest_v2"}
LEGACY_FALLBACK_MODEL = "gpt-4o-mini"


def _is_model_accessible(model_id: str) -> bool:
    """
    Allow environment-based model disablement for deploy-time fallback behavior.
    Example: OPENAI_UNAVAILABLE_MODELS=gpt-5-mini,gpt-5
    """
    unavailable_raw = os.getenv("OPENAI_UNAVAILABLE_MODELS", "")
    unavailable = {model.strip() for model in unavailable_raw.split(",") if model.strip()}
    return model_id not in unavailable


def _resolve_tier_model(tier: str) -> str:
    """Resolve tier to model, with safety fallback to legacy mini model."""
    candidate = tier_model(tier)
    if is_allowed_model(candidate) and _is_model_accessible(candidate):
        return candidate

    if is_allowed_model(LEGACY_FALLBACK_MODEL) and _is_model_accessible(LEGACY_FALLBACK_MODEL):
        return LEGACY_FALLBACK_MODEL

    available = [model_id for model_id in catalog_available_models() if _is_model_accessible(model_id)]
    if not available:
        raise RuntimeError("Model catalog has no allowed models")
    return available[0]


def _preferred_tier_for_task(task_type: str, remaining_budget_usd: Decimal) -> str:
    """Choose tier from routing rules using task type and remaining budget."""
    rules = get_routing_rules()
    min_for_standard = rules["min_remaining_for_standard"]
    min_for_advanced = rules["min_remaining_for_advanced"]

    if remaining_budget_usd < min_for_standard:
        return "cheap"

    if task_type in ADVANCED_TASK_TYPES and remaining_budget_usd >= min_for_advanced:
        return "advanced"

    return "standard"


def choose_model(
    task_type: str,
    payload_json: str,
    remaining_budget_usd: Decimal | float | int | str,
    user_override: Optional[str] = None,
) -> str:
    """
    Choose an effective model ID from the catalog.

    Priority:
    1. Valid user model override
    2. Tier alias override (cheap/standard/advanced)
    3. Payload size gate -> cheap tier
    4. Budget-based tiering
    """
    if user_override:
        if validate_model(user_override):
            return user_override

        if user_override in ("cheap", "standard", "advanced"):
            return _resolve_tier_model(user_override)

    rules = get_routing_rules()
    payload_size_chars = len(payload_json)
    if payload_size_chars > int(rules["payload_size_force_cheap_chars"]):
        return _resolve_tier_model("cheap")

    remaining_budget = Decimal(str(remaining_budget_usd))
    chosen_tier = _preferred_tier_for_task(task_type, remaining_budget)
    return _resolve_tier_model(chosen_tier)


def validate_model(model_name: str) -> bool:
    """Validate that model_name exists in the catalog."""
    return is_allowed_model(model_name)


def get_model_info(model_name: str) -> Optional[dict[str, Any]]:
    """Return model info from catalog (or None)."""
    return catalog_model_info(model_name)


def get_available_models() -> list[str]:
    """Return all catalog model IDs."""
    return catalog_available_models()


MODELS: dict[str, dict[str, Any]] = {
    model_id: info
    for model_id in get_available_models()
    for info in [get_model_info(model_id)]
    if info is not None
}
