"""
Worker-side router tests for effective model selection with catalog-backed routing.
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.catalog import get_routing_rules, tier_model
from router import choose_model, validate_model, get_available_models


def test_choose_model_respects_valid_model_override() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=1.0,
        user_override="gpt-4o",
    )
    assert model == "gpt-4o"
    assert validate_model(model) is True


def test_choose_model_maps_tier_override_to_real_model_id() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=1.0,
        user_override="advanced",
    )
    assert model == tier_model("advanced")
    assert validate_model(model) is True


def test_choose_model_ignores_invalid_override_and_returns_real_model_id() -> None:
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=1.0,
        user_override="not-a-real-model",
    )
    assert model == tier_model("standard")
    assert validate_model(model) is True


def test_choose_model_uses_cheap_tier_for_low_remaining_budget() -> None:
    rules = get_routing_rules()
    low_budget = float(rules["min_remaining_for_standard"] - Decimal("0.01"))
    model = choose_model(
        task_type="jobs_digest",
        payload_json="{}",
        remaining_budget_usd=low_budget,
        user_override=None,
    )
    assert model == tier_model("cheap")


def test_choose_model_uses_cheap_tier_for_huge_payload() -> None:
    rules = get_routing_rules()
    payload = "x" * (int(rules["payload_size_force_cheap_chars"]) + 1)
    model = choose_model(
        task_type="jobs_digest",
        payload_json=payload,
        remaining_budget_usd=1.0,
        user_override=None,
    )
    assert model == tier_model("cheap")


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
