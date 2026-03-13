"""
Model catalog loader and helpers.
Provides a single source of truth for model IDs, tier mapping, pricing, and routing rules.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

MILLION = Decimal("1000000")


class ModelCatalogError(RuntimeError):
    """Raised when the model catalog cannot be loaded or validated."""


def _catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "models.json"


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive guard
        raise ModelCatalogError(f"Invalid decimal value for '{field_name}': {value}") from exc


def _validate_catalog(data: dict[str, Any]) -> None:
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        raise ModelCatalogError("Catalog must contain a non-empty 'models' object")

    tiers = data.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        raise ModelCatalogError("Catalog must contain a non-empty 'tiers' object")

    routing_rules = data.get("routing_rules")
    if not isinstance(routing_rules, dict):
        raise ModelCatalogError("Catalog must contain a 'routing_rules' object")

    required_tiers = ("cheap", "standard", "advanced")
    for tier in required_tiers:
        model_id = tiers.get(tier)
        if not isinstance(model_id, str) or not model_id:
            raise ModelCatalogError(f"Tier '{tier}' must map to a model ID")

    for model_id, model_info in models.items():
        if not isinstance(model_info, dict):
            raise ModelCatalogError(f"Model '{model_id}' config must be an object")

        input_per_1m = _to_decimal(model_info.get("input_per_1m"), f"models.{model_id}.input_per_1m")
        output_per_1m = _to_decimal(model_info.get("output_per_1m"), f"models.{model_id}.output_per_1m")
        if input_per_1m < 0 or output_per_1m < 0:
            raise ModelCatalogError(
                f"Model '{model_id}' rates must be non-negative: input={input_per_1m}, output={output_per_1m}"
            )

    _to_decimal(routing_rules.get("min_remaining_for_standard"), "routing_rules.min_remaining_for_standard")
    _to_decimal(routing_rules.get("min_remaining_for_advanced"), "routing_rules.min_remaining_for_advanced")

    payload_gate = routing_rules.get("payload_size_force_cheap_chars")
    if not isinstance(payload_gate, int) or payload_gate < 0:
        raise ModelCatalogError("routing_rules.payload_size_force_cheap_chars must be a non-negative integer")


def _load_catalog() -> dict[str, Any]:
    path = _catalog_path()
    if not path.exists():
        raise ModelCatalogError(f"Model catalog file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as file_obj:
            data: dict[str, Any] = json.load(file_obj)
    except json.JSONDecodeError as exc:
        raise ModelCatalogError(f"Invalid JSON in model catalog: {path}") from exc

    _validate_catalog(data)
    return data


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """Load and cache the validated model catalog."""
    return _load_catalog()


def get_available_models() -> list[str]:
    """Return all allowed model IDs."""
    return list(load_catalog()["models"].keys())


def is_allowed_model(model_id: str) -> bool:
    """Return True when model_id exists in the catalog."""
    return model_id in load_catalog()["models"]


def tier_model(tier: str) -> str:
    """Resolve a tier alias (cheap/standard/advanced) to a model ID."""
    model_id = load_catalog()["tiers"].get(tier)
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(f"Unknown model tier '{tier}'")
    return model_id


def get_model_price(model_id: str) -> tuple[Decimal, Decimal]:
    """Return (input_per_token, output_per_token) as Decimal values."""
    model_info = load_catalog()["models"].get(model_id)
    if model_info is None:
        raise ValueError(f"Unknown model '{model_id}' in catalog")

    input_per_1m = _to_decimal(model_info["input_per_1m"], f"models.{model_id}.input_per_1m")
    output_per_1m = _to_decimal(model_info["output_per_1m"], f"models.{model_id}.output_per_1m")
    return input_per_1m / MILLION, output_per_1m / MILLION


def get_routing_rules() -> dict[str, Decimal | int]:
    """Return routing rules with strongly typed values."""
    rules = load_catalog()["routing_rules"]
    return {
        "min_remaining_for_standard": _to_decimal(
            rules["min_remaining_for_standard"],
            "routing_rules.min_remaining_for_standard",
        ),
        "min_remaining_for_advanced": _to_decimal(
            rules["min_remaining_for_advanced"],
            "routing_rules.min_remaining_for_advanced",
        ),
        "payload_size_force_cheap_chars": int(rules["payload_size_force_cheap_chars"]),
    }


def get_model_info(model_id: str) -> Optional[dict[str, Any]]:
    """Return model metadata compatible with router debug endpoints/tests."""
    model_info = load_catalog()["models"].get(model_id)
    if model_info is None:
        return None

    tier_lookup = {
        mapped_model: tier_name
        for tier_name, mapped_model in load_catalog()["tiers"].items()
    }
    input_per_token, output_per_token = get_model_price(model_id)
    estimated_cost_per_1k_tokens = float((input_per_token + output_per_token) * Decimal("1000"))

    max_context_raw = model_info.get("max_context", 0)
    max_context = int(max_context_raw) if isinstance(max_context_raw, int) else 0

    return {
        "name": model_id,
        "max_context": max_context,
        "tier": tier_lookup.get(model_id, "catalog"),
        "estimated_cost_per_1k_tokens": estimated_cost_per_1k_tokens,
    }
