"""Focused tests for Model Catalog v2 integration."""

from __future__ import annotations

from decimal import Decimal
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.openai_adapter import estimate_cost
from models.catalog import get_available_models, get_model_price
from router import choose_model


def test_router_returns_only_catalog_models() -> None:
    available = set(get_available_models())
    cases = [
        ("jobs_digest", "{}", 0.01, None),
        ("jobs_digest", "{}", 0.10, None),
        ("slides_outline", "{}", 1.00, None),
        ("jobs_digest", "{}", 1.00, "advanced"),
        ("jobs_digest", "x" * 60000, 1.00, None),
    ]

    for task_type, payload, budget, override in cases:
        chosen = choose_model(
            task_type=task_type,
            payload_json=payload,
            remaining_budget_usd=budget,
            user_override=override,
        )
        assert chosen in available


def test_adapter_uses_catalog_pricing_for_cost_math() -> None:
    model = "gpt-5-mini"
    tokens_in = 1500
    tokens_out = 250
    input_per_token, output_per_token = get_model_price(model)

    expected_cost = (
        Decimal(tokens_in) * input_per_token +
        Decimal(tokens_out) * output_per_token
    ).quantize(Decimal("0.00000001"))

    actual_cost = estimate_cost(model, tokens_in=tokens_in, tokens_out=tokens_out)
    assert actual_cost == expected_cost


def test_unknown_model_fails_for_cost_estimation() -> None:
    with pytest.raises(ValueError, match="Unknown model"):
        estimate_cost("not-a-real-model", tokens_in=10, tokens_out=10)
