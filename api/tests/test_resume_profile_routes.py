"""Tests for resume profile API routes."""

from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, "/app")

from main import app


client = TestClient(app)


def test_resume_profile_upload_text_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CANDIDATE_PROFILE_DB_PATH", str(tmp_path / "candidate_profile.sqlite3"))

    response = client.post(
        "/profile/resume/upload",
        files={"file": ("resume.txt", b"Backend engineer with ML platform experience", "text/plain")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_resume"] is True
    assert payload["resume_name"] == "resume.txt"
    assert payload["resume_char_count"] > 0


def test_resume_profile_upload_legacy_doc_rejected(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CANDIDATE_PROFILE_DB_PATH", str(tmp_path / "candidate_profile.sqlite3"))

    response = client.post(
        "/profile/resume/upload",
        files={"file": ("legacy_resume.doc", b"binary-data", "application/msword")},
    )
    assert response.status_code == 400
    assert ".docx or PDF" in response.json()["detail"]
