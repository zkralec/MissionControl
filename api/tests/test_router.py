"""
Pytest tests for catalog-backed model routing logic.
"""

from decimal import Decimal
import sys

sys.path.insert(0, "/app")

from models.catalog import get_routing_rules, tier_model
from router import (
    MODELS,
    choose_model,
    get_available_models,
    get_model_info,
    validate_model,
)


def test_default_model_is_standard_for_jobs_digest() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=0.10,
        user_override=None,
    )
    assert model == tier_model("standard")


def test_router_can_choose_advanced_for_advanced_task_type() -> None:
    model = choose_model(
        task_type="slides_outline",
        payload_json="{}",
        remaining_budget_usd=1.0,
        user_override=None,
    )
    assert model == tier_model("advanced")


def test_user_override_with_model_id_wins() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=0.01,
        user_override="gpt-4o",
    )
    assert model == "gpt-4o"


def test_user_override_with_tier_alias_wins() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=0.01,
        user_override="advanced",
    )
    assert model == tier_model("advanced")


def test_invalid_user_override_falls_back_to_routing() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=0.10,
        user_override="not-real",
    )
    assert model == tier_model("standard")


def test_low_budget_forces_cheap_tier() -> None:
    rules = get_routing_rules()
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=float(rules["min_remaining_for_standard"] - Decimal("0.001")),
        user_override=None,
    )
    assert model == tier_model("cheap")


def test_large_payload_forces_cheap_tier() -> None:
    rules = get_routing_rules()
    payload = "x" * (int(rules["payload_size_force_cheap_chars"]) + 1)
    model = choose_model(
        task_type="jobs_digest",
        payload_json=payload,
        remaining_budget_usd=10.0,
        user_override=None,
    )
    assert model == tier_model("cheap")


def test_validate_model_uses_catalog() -> None:
    assert validate_model(tier_model("cheap")) is True
    assert validate_model(tier_model("standard")) is True
    assert validate_model(tier_model("advanced")) is True
    assert validate_model("not-a-catalog-model") is False


def test_get_available_models_returns_catalog_entries() -> None:
    available = get_available_models()
    assert tier_model("cheap") in available
    assert tier_model("standard") in available
    assert tier_model("advanced") in available


def test_get_model_info_has_required_fields() -> None:
    info = get_model_info(tier_model("standard"))
    assert info is not None
    assert "name" in info
    assert "max_context" in info
    assert "tier" in info
    assert "estimated_cost_per_1k_tokens" in info


def test_modeled_index_matches_available_models() -> None:
    assert set(MODELS.keys()) == set(get_available_models())


def test_router_returns_only_catalog_models() -> None:
    cases = [
        ("jobs_digest", "{}", 0.01, None),
        ("jobs_digest", "{}", 0.10, None),
        ("slides_outline", "{}", 1.00, None),
        ("jobs_digest", "{}", 1.00, "advanced"),
        ("jobs_digest", "x" * 60000, 1.00, None),
    ]
    available = set(get_available_models())
    for task_type, payload, budget, override in cases:
        chosen = choose_model(task_type, payload, budget, override)
        assert chosen in available
