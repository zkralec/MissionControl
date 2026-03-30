import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from core.schema_validate import validate_payload
from event_log import log_event as persist_event_log
from jobs_history_state import record_jobs_notified
from notifications.discord import NotificationConfigError
from notifications.router import send_notification
from task_handlers.errors import NonRetryableTaskError

DEFAULT_ALLOWLIST = (
    "deals_scan_v1,unicorn_deals_poll_v1,unicorn_deals_rank_v1,"
    "jobs_digest_v2,openclaw_apply_draft_v1,ops_report_v1"
)
DEFAULT_DEDUPE_TTL_SECONDS = 21600
REQUIRED_ALLOWLIST_SOURCE_TASK_TYPES = {"ops_report_v1"}


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_allowlist() -> set[str]:
    raw = os.getenv("NOTIFY_DISCORD_ALLOWLIST", DEFAULT_ALLOWLIST)
    allowlist = {item.strip() for item in raw.split(",") if item.strip()}
    allowlist.update(REQUIRED_ALLOWLIST_SOURCE_TASK_TYPES)
    return allowlist


def _default_dedupe_ttl() -> int:
    raw = os.getenv("NOTIFY_DEDUPE_TTL_SECONDS", str(DEFAULT_DEDUPE_TTL_SECONDS))
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise NonRetryableTaskError("NOTIFY_DEDUPE_TTL_SECONDS must be an integer") from exc
    if ttl < 1:
        raise NonRetryableTaskError("NOTIFY_DEDUPE_TTL_SECONDS must be >= 1")
    return ttl


def ensure_notifications_table(conn: Any) -> None:
    dialect = getattr(getattr(conn, "dialect", None), "name", "")
    if dialect == "sqlite":
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS notifications_sent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS notifications_sent (
                    id BIGSERIAL PRIMARY KEY,
                    channel TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )
        )

    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notifications_sent_channel_dedupe_key
            ON notifications_sent(channel, dedupe_key)
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notifications_sent_expires_at
            ON notifications_sent(expires_at)
            """
        )
    )


def _check_dedupe(db: Any, channel: str, dedupe_key: str, now_dt: datetime) -> bool:
    dialect = getattr(getattr(getattr(db, "bind", None), "dialect", None), "name", "")
    if dialect == "sqlite":
        row = db.execute(
            text(
                """
                SELECT 1
                FROM notifications_sent
                WHERE channel = :channel
                  AND dedupe_key = :dedupe_key
                  AND strftime('%s', expires_at) > strftime('%s', 'now')
                LIMIT 1
                """
            ),
            {"channel": channel, "dedupe_key": dedupe_key},
        ).first()
        return row is not None

    row = db.execute(
        text(
            """
            SELECT 1
            FROM notifications_sent
            WHERE channel = :channel
              AND dedupe_key = :dedupe_key
              AND expires_at > :now_dt
            LIMIT 1
            """
        ),
        {"channel": channel, "dedupe_key": dedupe_key, "now_dt": now_dt},
    ).first()
    return row is not None


def _store_dedupe(db: Any, channel: str, dedupe_key: str, expires_at: datetime) -> None:
    db.execute(
        text(
            """
            INSERT INTO notifications_sent(channel, dedupe_key, expires_at)
            VALUES (:channel, :dedupe_key, :expires_at)
            """
        ),
        {"channel": channel, "dedupe_key": dedupe_key, "expires_at": expires_at},
    )


def _format_message(
    task_id: str | None,
    message: str,
    severity: str,
    metadata: dict | None,
    *,
    include_header: bool,
    include_metadata: bool,
) -> tuple[str, str]:
    timestamp = _timestamp_utc()
    parts = [message]
    if include_header:
        header = f"[{severity.upper()}] {timestamp}"
        if task_id:
            header = f"{header} task={task_id}"
        parts.insert(0, header)
    if include_metadata and metadata:
        parts.append(f"meta: {json.dumps(metadata, separators=(',', ':'), ensure_ascii=True, sort_keys=True)}")

    return "\n".join(parts), timestamp


def execute(task: Any, db: Any) -> dict[str, Any]:
    try:
        payload = json.loads(task.payload_json)
    except json.JSONDecodeError as exc:
        raise NonRetryableTaskError(f"notify_v1 payload is invalid JSON: {exc.msg}") from exc

    validate_payload("notify_v1", payload)

    source_task_type = payload.get("source_task_type")
    if not isinstance(source_task_type, str) or not source_task_type.strip():
        raise NonRetryableTaskError("notify_v1 requires payload.source_task_type")

    allowlist = _parse_allowlist()
    if source_task_type not in allowlist:
        raise NonRetryableTaskError(
            f"source_task_type '{source_task_type}' is not allowed by NOTIFY_DISCORD_ALLOWLIST"
        )

    channels = payload.get("channels", [])
    message = str(payload.get("message", ""))
    severity = str(payload.get("severity", "info"))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    include_header = bool(payload.get("include_header", True))
    include_metadata = bool(payload.get("include_metadata", True))

    ttl_seconds = int(payload.get("dedupe_ttl_seconds") or _default_dedupe_ttl())
    if ttl_seconds < 1:
        raise NonRetryableTaskError("dedupe_ttl_seconds must be >= 1")

    disable_dedupe = bool(payload.get("disable_dedupe", False))
    dedupe_key_value = payload.get("dedupe_key")
    dedupe_key = str(dedupe_key_value) if isinstance(dedupe_key_value, str) and dedupe_key_value else None

    final_content, timestamp = _format_message(
        getattr(task, "id", None),
        message,
        severity,
        metadata,
        include_header=include_header,
        include_metadata=include_metadata,
    )

    ensure_notifications_table(db.connection())

    now_dt = datetime.now(timezone.utc)
    deduped = False
    sent = False
    provider_result: dict[str, Any] = {"provider": "discord", "status": "skipped"}

    for channel in channels:
        if not disable_dedupe and dedupe_key and _check_dedupe(db, channel, dedupe_key, now_dt):
            deduped = True
            provider_result = {
                "provider": channel,
                "status": "deduped",
                "http_status": None,
                "rate_limited": False,
            }
            continue

        try:
            channel_results = send_notification([channel], final_content, metadata)
        except NotificationConfigError as exc:
            raise NonRetryableTaskError(
                f"notify_v1 discord configuration error for channel '{channel}': {exc}"
            ) from exc
        except ValueError as exc:
            raise NonRetryableTaskError(
                f"notify_v1 invalid notification channel '{channel}': {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"notify_v1 send failed for channel '{channel}': {type(exc).__name__}: {exc}"
            ) from exc

        provider_result = channel_results.get(channel, {})
        sent = bool(provider_result and provider_result.get("status") in {"sent", "mocked"})

        if not disable_dedupe and dedupe_key and sent:
            expires_at = now_dt + timedelta(seconds=ttl_seconds)
            _store_dedupe(db, channel, dedupe_key, expires_at)
            db.commit()

    preview = final_content[:240]
    if sent and source_task_type == "jobs_digest_v2" and isinstance(metadata, dict):
        history_updates = metadata.get("jobs_history_updates") if isinstance(metadata.get("jobs_history_updates"), list) else []
        if history_updates:
            record_jobs_notified(db, [row for row in history_updates if isinstance(row, dict)], notified_at=now_dt)
            db.commit()
    result_json = {
        "sent": sent,
        "deduped": deduped,
        "channel": "discord",
        "provider_result": provider_result,
        "message_preview": preview,
        "severity": severity,
        "source_task_type": source_task_type,
        "disable_dedupe": disable_dedupe,
        "timestamp": timestamp,
    }

    if sent:
        try:
            persist_event_log(
                event_type="notification_sent",
                source="notify_v1",
                level="INFO",
                message=f"Notification sent for source task type '{source_task_type}'.",
                metadata_json={
                    "task_id": getattr(task, "id", None),
                    "source_task_type": source_task_type,
                    "channel": "discord",
                    "provider_result": provider_result,
                },
            )
        except Exception:
            pass

    return {
        "content_text": final_content,
        "content_json": result_json,
    }
