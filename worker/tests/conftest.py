import json
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def jobs_v2_samples() -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "jobs_v2_samples.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))

