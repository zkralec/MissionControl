"""Tests for SQLite-backed candidate resume profile persistence."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_profile import delete_resume_profile, get_resume_profile, upsert_resume_profile


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
