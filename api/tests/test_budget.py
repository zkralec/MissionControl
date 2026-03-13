"""
Pytest tests for budget computations and gating logic.
Tests cover:
- Budget computation from runs.cost_usd
- Budget buffer enforcement
- Task creation gating
- Run creation gating
- Stats endpoint
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys
sys.path.insert(0, "/app")

from main import (
    Base, Task, Run, TaskStatus, RunStatus,
    today_spend_usd, is_budget_available,
    DAILY_BUDGET_USD, BUDGET_BUFFER_USD,
    now_utc
)

D = Decimal


@pytest.fixture(scope="function")
def test_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def test_today_spend_usd_empty(test_db):
    """Test spend calculation with no runs."""
    spend = today_spend_usd(test_db)
    assert spend == D("0")


def test_today_spend_usd_single_run(test_db):
    """Test spend calculation with a single run."""
    task_id = "task-1"
    run_id = "run-1"
    now = now_utc()

    run = Run(
        id=run_id,
        task_id=task_id,
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=D("0.05"),
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    spend = today_spend_usd(test_db)
    assert spend == D("0.05")


def test_today_spend_usd_multiple_runs(test_db):
    """Test spend calculation with multiple runs."""
    now = now_utc()

    for i in range(3):
        run = Run(
            id=f"run-{i}",
            task_id=f"task-{i}",
            attempt=1,
            status=RunStatus.success,
            started_at=now,
            ended_at=now + timedelta(seconds=2),
            cost_usd=D("0.02"),
            created_at=now,
        )
        test_db.add(run)
    test_db.commit()

    spend = today_spend_usd(test_db)
    assert spend == D("0.06")


def test_today_spend_usd_includes_failed_attempt_costs(test_db):
    """Failed attempts with cost still count toward daily spend."""
    now = now_utc()
    test_db.add(
        Run(
            id="run-failed-cost",
            task_id="task-failed",
            attempt=1,
            status=RunStatus.failed,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            cost_usd=D("0.00120000"),
            created_at=now,
        )
    )
    test_db.add(
        Run(
            id="run-success-cost",
            task_id="task-success",
            attempt=1,
            status=RunStatus.success,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            cost_usd=D("0.00230000"),
            created_at=now,
        )
    )
    test_db.commit()

    spend = today_spend_usd(test_db)
    assert spend == D("0.00350000")


def test_today_spend_usd_ignores_yesterday(test_db):
    """Test that spend calculation ignores yesterday's runs."""
    now = now_utc()
    yesterday = now - timedelta(days=1)

    # Add run from yesterday
    old_run = Run(
        id="old-run",
        task_id="old-task",
        attempt=1,
        status=RunStatus.success,
        started_at=yesterday,
        ended_at=yesterday + timedelta(seconds=2),
        cost_usd=D("0.10"),
        created_at=yesterday,
    )
    test_db.add(old_run)

    # Add run from today
    new_run = Run(
        id="new-run",
        task_id="new-task",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=D("0.02"),
        created_at=now,
    )
    test_db.add(new_run)
    test_db.commit()

    spend = today_spend_usd(test_db)
    # Should only count today's run
    assert spend == D("0.02")


def test_today_spend_usd_respects_operational_day_timezone(test_db, monkeypatch):
    """Test that daily spend uses configured operational day boundary, not strict UTC midnight."""
    monkeypatch.setenv("MISSION_CONTROL_DAY_BOUNDARY_TZ", "America/New_York")
    now = now_utc()
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

    test_db.add(
        Run(
            id="run-divergent",
            task_id="task-divergent",
            attempt=1,
            status=RunStatus.success,
            started_at=divergent_ts,
            ended_at=divergent_ts + timedelta(seconds=1),
            cost_usd=D("0.05"),
            created_at=divergent_ts,
        )
    )
    test_db.add(
        Run(
            id="run-now",
            task_id="task-now",
            attempt=1,
            status=RunStatus.success,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            cost_usd=D("0.02"),
            created_at=now,
        )
    )
    test_db.commit()

    spend = today_spend_usd(test_db)
    expected = D("0.02") + (D("0.05") if divergent_counts_in_operational_day else D("0"))
    assert spend == expected


def test_today_spend_usd_ignores_null_cost(test_db):
    """Test that runs without cost_usd are excluded."""
    now = now_utc()

    # Run without cost
    run1 = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.running,
        started_at=now,
        cost_usd=None,  # No cost yet
        created_at=now,
    )
    test_db.add(run1)

    # Run with cost
    run2 = Run(
        id="run-2",
        task_id="task-2",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=D("0.03"),
        created_at=now,
    )
    test_db.add(run2)
    test_db.commit()

    spend = today_spend_usd(test_db)
    # Should only count the run with cost_usd set
    assert spend == D("0.03")


def test_is_budget_available_empty(test_db):
    """Test budget availability with no runs (should be available)."""
    is_available, remaining, spend = is_budget_available(test_db)
    assert is_available is True
    assert spend == D("0")
    assert remaining == DAILY_BUDGET_USD


def test_is_budget_available_under_budget(test_db):
    """Test budget availability when under budget (should be available)."""
    now = now_utc()
    cost_usd = D("0.10")

    run = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=cost_usd,
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    is_available, remaining, spend = is_budget_available(test_db)
    assert is_available is True
    assert spend == cost_usd
    assert remaining == DAILY_BUDGET_USD - cost_usd


def test_is_budget_available_within_buffer(test_db):
    """Test budget availability when remaining < buffer (should NOT be available)."""
    now = now_utc()
    # Spend so much that remaining < buffer
    spend_amount = DAILY_BUDGET_USD - (BUDGET_BUFFER_USD / D("2"))

    run = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=spend_amount,
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    is_available, remaining, spend = is_budget_available(test_db)
    assert is_available is False
    assert spend == spend_amount
    assert remaining == DAILY_BUDGET_USD - spend_amount


def test_is_budget_available_exceeds_budget(test_db):
    """Test budget availability when spend > budget (should NOT be available)."""
    now = now_utc()
    spend_amount = DAILY_BUDGET_USD + D("0.10")

    run = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=spend_amount,
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    is_available, remaining, spend = is_budget_available(test_db)
    assert is_available is False
    assert spend == spend_amount


def test_is_budget_available_exactly_at_budget(test_db):
    """Test budget availability when remaining == budget (should NOT be available)."""
    now = now_utc()
    spend_amount = DAILY_BUDGET_USD

    run = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=spend_amount,
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    is_available, remaining, spend = is_budget_available(test_db)
    assert is_available is False
    assert spend == spend_amount
    assert remaining == D("0")


def test_task_creation_blocked_by_budget(test_db):
    """Test that task creation is blocked when budget is exhausted."""
    now = now_utc()
    # Spend close to budget limit
    spend_amount = DAILY_BUDGET_USD - (BUDGET_BUFFER_USD / D("2"))

    run = Run(
        id="run-1",
        task_id="task-1",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=spend_amount,
        created_at=now,
    )
    test_db.add(run)
    test_db.commit()

    # Check that budget check would block
    is_available, _, _ = is_budget_available(test_db)
    assert is_available is False


def test_budget_computation_precision(test_db):
    """Test that budget computation maintains precision to 4 decimals."""
    now = now_utc()

    # Test with various costs
    costs = [D("0.0001"), D("0.0002"), D("0.0003")]
    for i, cost in enumerate(costs):
        run = Run(
            id=f"run-{i}",
            task_id=f"task-{i}",
            attempt=1,
            status=RunStatus.success,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            cost_usd=cost,
            created_at=now,
        )
        test_db.add(run)
    test_db.commit()

    spend = today_spend_usd(test_db)
    expected = sum(costs)
    assert spend == expected


def test_budget_only_counts_runs_with_started_at(test_db):
    """Test that only runs with started_at timestamps are included."""
    now = now_utc()

    # Run that hasn't started yet (no started_at)
    queued_run = Run(
        id="queued",
        task_id="task-1",
        attempt=1,
        status=RunStatus.queued,
        started_at=None,
        cost_usd=D("0.05"),  # Cost set but not started
        created_at=now,
    )
    test_db.add(queued_run)

    # Run that has started
    active_run = Run(
        id="active",
        task_id="task-2",
        attempt=1,
        status=RunStatus.success,
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        cost_usd=D("0.03"),
        created_at=now,
    )
    test_db.add(active_run)
    test_db.commit()

    spend = today_spend_usd(test_db)
    # Should only count the one with started_at
    assert spend == D("0.03")
