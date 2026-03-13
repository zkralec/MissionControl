"""Payload schema validation utilities for Mission Control tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from jsonschema import Draft202012Validator


class PayloadValidationError(ValueError):
    """Raised when payload validation fails for a task type."""


def _schema_dirs() -> tuple[Path, ...]:
    base_dir = Path(__file__).resolve().parent.parent
    candidates = [
        # Canonical payload schemas for this repo live here.
        base_dir / "worker" / "schemas" / "task_payloads",
        # Compatibility fallbacks for older layouts.
        base_dir / "schemas" / "task_payloads",
        base_dir.parent / "worker" / "schemas" / "task_payloads",
        base_dir.parent / "schemas" / "task_payloads",
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _schema_path(task_type: str) -> Path:
    filename = f"{task_type}.schema.json"
    for schema_dir in _schema_dirs():
        candidate = schema_dir / filename
        if candidate.exists():
            return candidate

    searched = ", ".join(str(path) for path in _schema_dirs())
    raise PayloadValidationError(
        f"No schema found for task_type '{task_type}'. Expected '{filename}' in: {searched}"
    )


def _format_error_path(parts: Iterable[object]) -> str:
    path = "/".join(str(part) for part in parts)
    return f"/{path}" if path else "$"


def validate_payload(task_type: str, payload: dict) -> None:
    """Validate a payload dict against task_type schema.

    Raises PayloadValidationError with readable deterministic messages.
    """
    if not isinstance(payload, dict):
        raise PayloadValidationError(
            f"Payload for task_type '{task_type}' must be a JSON object (dict), got {type(payload).__name__}"
        )

    schema_file = _schema_path(task_type)
    with schema_file.open("r", encoding="utf-8") as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    lines = [
        f"{_format_error_path(err.absolute_path)}: {err.message}"
        for err in errors
    ]
    raise PayloadValidationError(
        f"Payload validation failed for task_type '{task_type}' using '{schema_file.name}': "
        + " | ".join(lines)
    )
