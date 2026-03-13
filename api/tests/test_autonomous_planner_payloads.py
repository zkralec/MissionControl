from datetime import datetime, timezone
import json
import sys

sys.path.insert(0, "/app")

import autonomous_planner


def test_materialize_payload_json_for_deals_injects_runtime_fields() -> None:
    now = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    payload = '{"source":"autonomous-planner-rtx5090","collectors_enabled":true,"payload_nonce":"{{uuid4}}"}'

    out = autonomous_planner._materialize_payload_json(
        payload,
        now=now,
        task_type="deals_scan_v1",
        template_id="preset-rtx5090-deals-scan",
        template_name="RTX 5090 deals scan",
    )
    parsed = json.loads(out)

    assert parsed["source"].startswith("autonomous-planner-rtx5090-20260310T120000Z")
    assert parsed["payload_nonce"] != "{{uuid4}}"
    assert parsed["planner_generated_at"] == "2026-03-10T12:00:00+00:00"
    assert parsed["planner_template_id"] == "preset-rtx5090-deals-scan"


def test_materialize_payload_json_for_jobs_rotates_query_and_enables_collectors() -> None:
    now = datetime(2026, 3, 10, 12, 5, 0, tzinfo=timezone.utc)
    payload = (
        '{"request":{"desired_title_keywords":["ai engineer","ml engineer"],"query_rotation_window_seconds":300}}'
    )

    out = autonomous_planner._materialize_payload_json(
        payload,
        now=now,
        task_type="jobs_collect_v1",
        template_id="preset-jobs-digest-scan",
        template_name="Autonomous jobs digest",
    )
    parsed = json.loads(out)
    request = parsed["request"]

    assert request["collectors_enabled"] is True
    assert request["query"] in {"ai engineer", "ml engineer"}
    assert parsed["planner_template_name"] == "Autonomous jobs digest"
