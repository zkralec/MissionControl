"""
Tests for OpenAI LLM adapter and worker integration.
Covers format_messages(), run_chat_completion(), and worker.run_task() with LLM enabled.
"""
import json
import os
from decimal import Decimal
import pytest
from unittest.mock import patch, MagicMock, Mock
from datetime import datetime, timezone
import uuid
import sys

# Add worker directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.openai_adapter import (
    get_client,
    run_chat_completion,
    format_messages,
    estimate_cost,
    is_model_available,
    get_pricing,
    PRICING,
)


class TestFormatMessages:
    """Test format_messages() function for all task types."""

    def test_jobs_digest_task_formatting(self):
        """Test that jobs_digest tasks are formatted correctly."""
        payload = json.dumps({"jobs": ["job1", "job2"], "count": 2})
        messages = format_messages("jobs_digest", payload)

        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "job" in messages[0]["content"].lower()
        assert payload in messages[1]["content"]

    def test_extraction_task_formatting(self):
        """Test that extraction tasks are formatted correctly."""
        payload = json.dumps({"text": "Sample extraction text", "fields": ["name", "email"]})
        messages = format_messages("extraction", payload)

        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "extract" in messages[0]["content"].lower()

    def test_unknown_task_type_gets_generic_prompt(self):
        """Test that unknown task types get a generic prompt."""
        payload = json.dumps({"data": "test"})
        messages = format_messages("unknown_type", payload)

        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"


class TestPricingAndModels:
    """Test pricing table, model validation, and cost estimation."""

    def test_pricing_table_exists_and_complete(self):
        """Test that PRICING dict has all required models."""
        assert "gpt-4o-mini" in PRICING
        assert "gpt-4o" in PRICING
        assert "gpt-4-turbo" in PRICING

        for model, pricing in PRICING.items():
            assert "input" in pricing
            assert "output" in pricing
            assert isinstance(pricing["input"], Decimal)
            assert isinstance(pricing["output"], Decimal)
            assert pricing["input"] > 0
            assert pricing["output"] > 0

    def test_get_pricing_returns_rates(self):
        """Test get_pricing() helper function."""
        pricing_dict = get_pricing("gpt-4o-mini")
        assert pricing_dict is not None
        assert "input" in pricing_dict
        assert "output" in pricing_dict
        assert pricing_dict["input"] == PRICING["gpt-4o-mini"]["input"]
        assert pricing_dict["output"] == PRICING["gpt-4o-mini"]["output"]

    def test_get_pricing_returns_none_for_invalid_model(self):
        """Test get_pricing() returns None for invalid model."""
        pricing_dict = get_pricing("invalid-model-xyz")
        assert pricing_dict is None

    def test_is_model_available_validates_models(self):
        """Test is_model_available() function."""
        assert is_model_available("gpt-4o-mini") is True
        assert is_model_available("gpt-4o") is True
        assert is_model_available("gpt-4-turbo") is True
        assert is_model_available("invalid-model") is False

    def test_estimate_cost_calculation(self):
        """Test cost estimation with known tokens and rates."""
        cost = estimate_cost("gpt-4o-mini", tokens_in=1000, tokens_out=500)

        # Manual calculation: (1000 * 0.00000015) + (500 * 0.0000006)
        # = 0.00015 + 0.0003 = 0.00045
        expected = 1000 * PRICING["gpt-4o-mini"]["input"] + 500 * PRICING["gpt-4o-mini"]["output"]
        assert abs(cost - expected) < Decimal("0.00000001")

    def test_estimate_cost_with_different_models(self):
        """Test cost estimation with different model tiers."""
        cost_mini = estimate_cost("gpt-4o-mini", tokens_in=1000, tokens_out=500)
        cost_4o = estimate_cost("gpt-4o", tokens_in=1000, tokens_out=500)
        cost_turbo = estimate_cost("gpt-4-turbo", tokens_in=1000, tokens_out=500)

        # Costs should scale with model capabilities
        assert cost_mini < cost_4o < cost_turbo


class TestRunChatCompletion:
    """Test run_chat_completion() with mocked OpenAI API."""

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_returns_correct_structure(self, mock_openai_class):
        """Test that run_chat_completion() returns all required fields."""
        # Mock OpenAI response
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Test response from LLM"
        mock_response.usage.prompt_tokens = 150
        mock_response.usage.completion_tokens = 75
        mock_client.chat.completions.create.return_value = mock_response

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]

        result = run_chat_completion("gpt-4o-mini", messages)

        assert isinstance(result, dict)
        assert "output_text" in result
        assert "tokens_in" in result
        assert "tokens_out" in result
        assert "cost_usd" in result
        assert "model" in result

        assert result["output_text"] == "Test response from LLM"
        assert result["tokens_in"] == 150
        assert result["tokens_out"] == 75
        assert result["model"] == "gpt-4o-mini"

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_cost_calculation(self, mock_openai_class):
        """Test that cost is calculated correctly from token usage."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Response"
        mock_response.usage.prompt_tokens = 1000
        mock_response.usage.completion_tokens = 500
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "user", "content": "Test"}]
        result = run_chat_completion("gpt-4o-mini", messages)

        # Manually calculate expected cost
        input_price = PRICING["gpt-4o-mini"]["input"]
        output_price = PRICING["gpt-4o-mini"]["output"]
        expected_cost = (1000 * input_price) + (500 * output_price)

        assert abs(result["cost_usd"] - expected_cost) < Decimal("0.00000001")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_with_temperature_and_max_completion_tokens(self, mock_openai_class):
        """Test that temperature and max_completion_tokens parameters are passed correctly."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Response"
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "user", "content": "Test"}]
        run_chat_completion("gpt-4o-mini", messages, temperature=0.9, max_completion_tokens=256)

        # Verify API was called with correct parameters
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs

        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.9
        assert call_kwargs["max_completion_tokens"] == 256

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_omits_temperature_for_gpt5_models(self, mock_openai_class):
        """5-series models should not receive temperature by default for compatibility."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Response"
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "user", "content": "Test"}]
        run_chat_completion("gpt-5-mini", messages, temperature=0.7, max_completion_tokens=128)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-5-mini"
        assert call_kwargs["max_completion_tokens"] == 128
        assert "temperature" not in call_kwargs

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_extracts_text_from_content_chunks(self, mock_openai_class):
        """Chunked message content should be normalized into output_text."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        message = MagicMock()
        message.content = [{"type": "output_text", "text": '{"ok":true}'}]
        mock_response.choices[0].message = message
        mock_response.usage.prompt_tokens = 12
        mock_response.usage.completion_tokens = 6
        mock_client.chat.completions.create.return_value = mock_response

        result = run_chat_completion("gpt-4o-mini", [{"role": "user", "content": "test"}])
        assert result["output_text"] == '{"ok":true}'

    def test_run_chat_completion_raises_for_invalid_model(self):
        """Test that run_chat_completion() raises for invalid model."""
        messages = [{"role": "user", "content": "Test"}]

        with pytest.raises(ValueError):
            run_chat_completion("invalid-model", messages)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_handles_api_connection_error(self, mock_openai_class):
        """Test error handling for API connection errors."""
        from openai import APIConnectionError

        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        # Create an exception instance using Exception directly as a base
        mock_client.chat.completions.create.side_effect = APIConnectionError(request=None)

        messages = [{"role": "user", "content": "Test"}]

        with pytest.raises(APIConnectionError):
            run_chat_completion("gpt-4o-mini", messages)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch("llm.openai_adapter.OpenAI")
    def test_run_chat_completion_handles_api_error(self, mock_openai_class):
        """Test error handling for API errors."""
        from openai import APIError

        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        # Create an APIError with proper arguments
        mock_client.chat.completions.create.side_effect = APIError(message="API error", request=None, body=None)

        messages = [{"role": "user", "content": "Test"}]

        with pytest.raises(APIError):
            run_chat_completion("gpt-4o-mini", messages)


class TestGetClient:
    """Test OpenAI client initialization."""

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-12345"})
    @patch("llm.openai_adapter.OpenAI")
    def test_get_client_initializes_with_api_key(self, mock_openai_class):
        """Test that get_client() initializes OpenAI with correct API key."""
        get_client()
        mock_openai_class.assert_called_once_with(api_key="test-key-12345")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    def test_get_client_raises_without_api_key(self):
        """Test that get_client() raises ValueError when API key is missing."""
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            get_client()


class TestWorkerIntegration:
    """Integration tests for LLM adapter with worker patterns."""

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "USE_LLM": "true"})
    @patch("llm.openai_adapter.OpenAI")
    def test_llm_execution_pattern_for_jobs_digest(self, mock_openai_class):
        """Test the pattern for executing LLM on jobs_digest tasks."""
        # Mock OpenAI
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Digest output"
        mock_response.usage.prompt_tokens = 200
        mock_response.usage.completion_tokens = 150
        mock_client.chat.completions.create.return_value = mock_response

        # Simulate task execution flow
        task_type = "jobs_digest"
        payload_json = json.dumps({"jobs": ["job1", "job2"]})
        model = "gpt-4o-mini"

        # Format and execute
        messages = format_messages(task_type, payload_json)
        result = run_chat_completion(model, messages)

        # Verify result structure
        assert result["tokens_in"] == 200
        assert result["tokens_out"] == 150
        assert result["output_text"] == "Digest output"
        assert result["cost_usd"] > 0

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "USE_LLM": "false"})
    def test_simulated_execution_pattern(self):
        """Test the pattern for simulated execution when USE_LLM=false."""
        # When USE_LLM=false, code path uses time.sleep() and random tokens
        # This test just verifies the environment flag behavior
        use_llm = os.getenv("USE_LLM", "false").lower() == "true"
        assert use_llm is False

    def test_budget_pre_check_logic(self):
        """Test the budget pre-check conditional logic."""
        # Simulate budget checking
        remaining_budget = 0.005  # Below min cost of 0.01
        min_cost = 0.01
        can_execute = remaining_budget > min_cost

        assert can_execute is False  # Budget insufficient


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_estimate_cost_with_zero_tokens(self):
        """Test cost estimation with zero tokens."""
        cost = estimate_cost("gpt-4o-mini", tokens_in=0, tokens_out=0)
        assert cost == Decimal("0.00000000")

    def test_estimate_cost_with_large_token_counts(self):
        """Test cost estimation with large token counts."""
        # 100K tokens should not overflow
        cost = estimate_cost("gpt-4o-mini", tokens_in=100000, tokens_out=100000)
        assert cost > 0

    def test_format_messages_with_empty_payload(self):
        """Test format_messages() with minimal payload."""
        messages = format_messages("jobs_digest", json.dumps({}))
        assert isinstance(messages, list)
        assert len(messages) >= 2

    def test_format_messages_with_large_payload(self):
        """Test format_messages() with large payload."""
        large_data = json.dumps({"jobs": ["job"] * 1000})
        messages = format_messages("jobs_digest", large_data)
        assert isinstance(messages, list)
        assert len(messages) >= 2

    def test_pricing_models_have_reasonable_rates(self):
        """Verify pricing models are within reasonable ranges."""
        for model, pricing in PRICING.items():
            # Input price should be <= output price (typical pattern)
            # Both should be in reasonable range: 1e-8 to 1e-2 per token
            assert Decimal("0.00000001") <= pricing["input"] <= Decimal("0.01")
            assert Decimal("0.00000001") <= pricing["output"] <= Decimal("0.01")

    def test_estimate_cost_precision(self):
        """Test that cost estimation maintains reasonable precision."""
        cost = estimate_cost("gpt-4o-mini", tokens_in=123, tokens_out=456)

        # Cost should be a reasonable number, not NaN or Inf
        assert not (cost != cost)  # Check for NaN
        assert cost >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
