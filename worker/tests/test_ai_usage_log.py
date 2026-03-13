"""Tests for SQLite AI usage logging helpers and adapter instrumentation."""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai_usage_log import get_ai_usage_summary, list_ai_usage_today, log_ai_usage
from llm.openai_adapter import run_chat_completion


@pytest.fixture()
def usage_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ai_usage.sqlite3"
    monkeypatch.setenv("AI_USAGE_DB_PATH", str(db_path))
    return db_path


def test_log_ai_usage_and_summary(usage_db) -> None:
    log_ai_usage(
        task_run_id="run-123",
        agent_name="jobs_digest_v1",
        model="gpt-4o-mini",
        tokens_in=120,
        tokens_out=80,
        total_tokens=None,
        cost_usd=Decimal("0.00123456"),
        latency_ms=321,
        status="succeeded",
        error_text=None,
    )

    rows = list_ai_usage_today()
    assert rows
    assert rows[0]["task_run_id"] == "run-123"
    assert rows[0]["total_tokens"] == 200
    assert rows[0]["cost_usd"] == pytest.approx(0.00123456, rel=1e-9)

    now = datetime.now(timezone.utc)
    summary = get_ai_usage_summary(now - timedelta(hours=1), now + timedelta(hours=1))
    assert summary["requests_total"] >= 1
    assert summary["succeeded_total"] >= 1
    assert summary["failed_total"] >= 0


def test_list_ai_usage_today_respects_operational_day_timezone(usage_db, monkeypatch) -> None:
    monkeypatch.setenv("MISSION_CONTROL_DAY_BOUNDARY_TZ", "America/New_York")
    now = datetime.now(timezone.utc)
    utc_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_start = now.astimezone(ZoneInfo("America/New_York")).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc)

    if local_start > utc_start:
        divergent_ts = utc_start + ((local_start - utc_start) / 2)
        divergent_counts_in_operational_day = False
    elif local_start < utc_start:
        divergent_ts = local_start + ((utc_start - local_start) / 2)
        divergent_counts_in_operational_day = True
    else:  # pragma: no cover - should not happen for America/New_York
        pytest.skip("Timezone boundary unexpectedly equals UTC boundary")

    log_ai_usage(
        task_run_id="run-divergent",
        agent_name="jobs_digest_v1",
        model="gpt-4o-mini",
        tokens_in=10,
        tokens_out=10,
        cost_usd=Decimal("0.001"),
        status="succeeded",
        created_at=divergent_ts,
    )
    log_ai_usage(
        task_run_id="run-now",
        agent_name="jobs_digest_v1",
        model="gpt-4o-mini",
        tokens_in=10,
        tokens_out=10,
        cost_usd=Decimal("0.001"),
        status="succeeded",
        created_at=now,
    )

    rows = list_ai_usage_today()
    task_run_ids = {row["task_run_id"] for row in rows}
    assert "run-now" in task_run_ids
    assert ("run-divergent" in task_run_ids) is divergent_counts_in_operational_day


@patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
@patch("llm.openai_adapter.OpenAI")
def test_run_chat_completion_logs_ai_usage(mock_openai_class, usage_db) -> None:
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "hello"
    mock_response.usage.prompt_tokens = 42
    mock_response.usage.completion_tokens = 21
    mock_response.usage.total_tokens = 63
    mock_client.chat.completions.create.return_value = mock_response

    result = run_chat_completion(
        "gpt-4o-mini",
        [{"role": "user", "content": "say hi"}],
        task_run_id="task-run-abc",
        agent_name="verify-agent",
    )
    assert result["output_text"] == "hello"

    rows = list_ai_usage_today()
    assert rows
    row = rows[0]
    assert row["task_run_id"] == "task-run-abc"
    assert row["agent_name"] == "verify-agent"
    assert row["model"] == "gpt-4o-mini"
    assert row["status"] == "succeeded"
    assert row["tokens_in"] == 42
    assert row["tokens_out"] == 21
    assert row["total_tokens"] == 63
    assert row["cost_usd"] is not None
