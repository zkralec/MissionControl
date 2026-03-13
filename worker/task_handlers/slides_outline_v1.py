import json
from typing import Any


def _parse_payload(payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _deterministic_outline(payload: dict[str, Any]) -> dict[str, Any]:
    topic = str(payload.get("topic", "Untitled presentation"))
    goals = payload.get("goals", [])
    points = payload.get("points", [])

    if not isinstance(goals, list):
        goals = []
    if not isinstance(points, list):
        points = []

    sections = [
        {"title": "Opening", "talking_points": [f"Why {topic} matters", "Agenda and goals"]},
        {"title": "Context", "talking_points": [str(g) for g in goals[:3]] or ["Current state", "Problem framing"]},
        {"title": "Core Insights", "talking_points": [str(p) for p in points[:5]] or ["Insight 1", "Insight 2", "Insight 3"]},
        {"title": "Action Plan", "talking_points": ["Milestones", "Ownership", "Timeline"]},
        {"title": "Q&A", "talking_points": ["Risks", "Dependencies", "Decision requests"]},
    ]

    return {
        "topic": topic,
        "sections": sections,
        "slide_count_estimate": 2 + len(sections) * 2,
    }


def execute(task: Any, db: Any) -> dict[str, Any]:
    del db
    payload = _parse_payload(task.payload_json)
    outline = _deterministic_outline(payload)

    return {
        "artifact_type": "slides_outline",
        "content_text": f"Slides outline generated for topic: {outline['topic']}",
        "content_json": outline,
        "llm": {
            "messages": [
                {"role": "system", "content": "You improve presentation outlines while staying concise."},
                {"role": "user", "content": f"Improve this outline and return a concise speaker-ready version:\n\n{json.dumps(outline, ensure_ascii=True)}"},
            ],
            "temperature": 0.3,
            "max_tokens": 350,
        },
    }
