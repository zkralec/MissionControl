"""Tests for SQLite-backed candidate resume profile persistence."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_profile import delete_resume_profile, get_resume_profile, upsert_resume_profile
from task_handlers.jobs_pipeline_common import resolve_profile_context


@pytest.fixture()
def candidate_profile_db(tmp_path, monkeypatch):
    db_path = tmp_path / "candidate_profile.sqlite3"
    monkeypatch.setenv("CANDIDATE_PROFILE_DB_PATH", str(db_path))
    return db_path


def test_upsert_get_delete_resume_profile(candidate_profile_db) -> None:
    row = upsert_resume_profile(
        resume_text="Experienced backend engineer with distributed systems background.",
        resume_name="Resume v1",
    )
    assert row["resume_name"] == "Resume v1"
    assert row["resume_char_count"] > 0

    loaded = get_resume_profile(include_text=True)
    assert loaded is not None
    assert loaded["resume_name"] == "Resume v1"
    assert "distributed systems" in loaded["resume_text"]

    deleted = delete_resume_profile()
    assert deleted is True
    assert get_resume_profile(include_text=True) is None


def test_resolve_profile_context_exposes_stored_contact_profile(candidate_profile_db) -> None:
    upsert_resume_profile(
        resume_text="Zachary Kralec\nzkralec@icloud.com\n240-555-0101",
        resume_name="Resume v2",
        metadata_json={
            "contact_profile": {
                "city": "Saint Mary's City",
                "state_or_province": "MD",
                "postal_code": "20686",
                "country": "United States",
                "primary_phone_number": "240-555-0101",
                "phone_type": "mobile",
            }
        },
    )

    profile_context = resolve_profile_context({"profile_mode": "resume_profile"})

    assert profile_context["source"] == "stored_profile"
    assert profile_context["metadata_json"]["contact_profile"]["city"] == "Saint Mary's City"
    assert profile_context["contact_profile"]["postal_code"] == "20686"
    assert profile_context["contact_profile"]["phone_type"] == "mobile"
