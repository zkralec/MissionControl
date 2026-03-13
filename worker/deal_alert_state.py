"""SQLite-backed state for per-item deal alert dedupe and cooldown decisions."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DB_PATH_ENV = "DEAL_ALERT_STATE_DB_PATH"
FALLBACK_ENV_PATHS = (
    "TASK_RUN_HISTORY_DB_PATH",
    "AI_USAGE_DB_PATH",
    "EVENT_LOG_DB_PATH",
)
DEFAULT_DB_FILENAME = "task_run_history.sqlite3"


def get_deal_alert_state_db_path() -> Path:
    raw_path = os.getenv(DB_PATH_ENV)
    if not raw_path:
        for env_name in FALLBACK_ENV_PATHS:
            raw_path = os.getenv(env_name)
            if raw_path:
                break
    if not raw_path:
        raw_path = DEFAULT_DB_FILENAME

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _to_iso(ts: datetime | None = None) -> str:
    value = ts or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip().replace(",", "")
    if not stripped:
        return None
    if stripped.startswith("$"):
        stripped = stripped[1:]
    try:
        return float(stripped)
    except ValueError:
        return None


def _normalize_url_for_key(raw_url: str | None) -> str:
    if not isinstance(raw_url, str):
        return ""
    url = raw_url.strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()

    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k and not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    normalized_query = urlencode(sorted(query_items))
    normalized_path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            (parts.scheme or "https").lower(),
            parts.netloc.lower(),
            normalized_path,
            normalized_query,
            "",
        )
    )


def _status_key(deal: dict[str, Any]) -> str:
    in_stock = deal.get("in_stock")
    if isinstance(in_stock, bool):
        return "in_stock" if in_stock else "out_of_stock"
    if isinstance(in_stock, str):
        low = in_stock.strip().lower()
        if low in {"in stock", "available", "true", "yes"}:
            return "in_stock"
        if low in {"out of stock", "sold out", "false", "no", "unavailable"}:
            return "out_of_stock"
    return "unknown"


def build_deal_alert_key(deal: dict[str, Any]) -> str:
    source = str(deal.get("source") or "unknown").strip().lower() or "unknown"
    sku_raw = deal.get("sku")
    sku = str(sku_raw).strip().lower() if sku_raw is not None else ""
    if sku:
        return f"{source}|sku|{sku}"

    url = _normalize_url_for_key(deal.get("url"))
    if url:
        return f"{source}|url|{url}"

    title = str(deal.get("title") or "untitled").strip().lower()
    return f"{source}|title|{title}"


def _material_price_change(
    previous_price: float | None,
    current_price: float | None,
    *,
    pct_threshold: float,
    abs_threshold: float,
) -> bool:
    if previous_price is None or current_price is None:
        return False

    delta = abs(current_price - previous_price)
    if delta >= max(abs_threshold, 0.0):
        return True

    if previous_price <= 0:
        return False

    pct = (delta / previous_price) * 100.0
    return pct >= max(pct_threshold, 0.0)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_deal_alert_state_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deal_alert_state (
            item_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            sku TEXT,
            url TEXT,
            title TEXT,
            last_seen_at TEXT NOT NULL,
            last_alerted_at TEXT,
            cooldown_until TEXT,
            last_price REAL,
            last_status TEXT,
            last_payload_json TEXT,
            alert_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_deal_alert_state_last_seen_at ON deal_alert_state(last_seen_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_deal_alert_state_last_alerted_at ON deal_alert_state(last_alerted_at DESC)"
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload_raw = row["last_payload_json"]
    payload_json: Any = None
    if payload_raw:
        try:
            payload_json = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload_json = payload_raw
    return {
        "item_key": row["item_key"],
        "source": row["source"],
        "sku": row["sku"],
        "url": row["url"],
        "title": row["title"],
        "last_seen_at": row["last_seen_at"],
        "last_alerted_at": row["last_alerted_at"],
        "cooldown_until": row["cooldown_until"],
        "last_price": row["last_price"],
        "last_status": row["last_status"],
        "last_payload_json": payload_json,
        "alert_count": int(row["alert_count"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_deal_alert_state(item_key: str) -> dict[str, Any] | None:
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT item_key, source, sku, url, title, last_seen_at, last_alerted_at,
                   cooldown_until, last_price, last_status, last_payload_json,
                   alert_count, created_at, updated_at
            FROM deal_alert_state
            WHERE item_key = ?
            LIMIT 1
            """,
            (item_key,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_recent_deal_alert_states(limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT item_key, source, sku, url, title, last_seen_at, last_alerted_at,
                   cooldown_until, last_price, last_status, last_payload_json,
                   alert_count, created_at, updated_at
            FROM deal_alert_state
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _fetch_existing_states(conn: sqlite3.Connection, keys: list[str]) -> dict[str, sqlite3.Row]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT item_key, source, sku, url, title, last_seen_at, last_alerted_at,
               cooldown_until, last_price, last_status, last_payload_json,
               alert_count, created_at, updated_at
        FROM deal_alert_state
        WHERE item_key IN ({placeholders})
        """,
        tuple(keys),
    ).fetchall()
    return {row["item_key"]: row for row in rows}


def evaluate_and_record_deal_alerts(
    deals: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = 21600,
    material_price_change_pct: float = 3.0,
    material_price_change_abs_usd: float = 25.0,
) -> dict[str, Any]:
    if not deals:
        return {
            "decisions": [],
            "alertable_deals": [],
            "suppressed_deals": [],
        }

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = _to_iso(now_dt)
    cooldown_seconds = max(int(cooldown_seconds), 1)

    indexed: list[tuple[str, dict[str, Any]]] = [(build_deal_alert_key(deal), deal) for deal in deals]
    keys = [item_key for item_key, _ in indexed]

    decisions: list[dict[str, Any]] = []
    alertable_deals: list[dict[str, Any]] = []
    suppressed_deals: list[dict[str, Any]] = []
    processed_keys: set[str] = set()

    with _connect() as conn:
        _ensure_schema(conn)
        existing = _fetch_existing_states(conn, keys)

        for item_key, deal in indexed:
            if item_key in processed_keys:
                suppressed_deals.append(deal)
                decisions.append(
                    {
                        "deal_key": item_key,
                        "should_alert": False,
                        "reasons": ["duplicate_in_batch"],
                        "previous_price": None,
                        "current_price": _to_float(deal.get("price")),
                        "previous_status": None,
                        "current_status": _status_key(deal),
                        "last_alerted_at": None,
                        "cooldown_until": None,
                    }
                )
                continue
            processed_keys.add(item_key)

            source = str(deal.get("source") or "unknown").strip().lower() or "unknown"
            sku_value = deal.get("sku")
            sku = str(sku_value).strip() if sku_value is not None else None
            url = _normalize_url_for_key(deal.get("url")) or None
            title = str(deal.get("title") or "").strip() or None
            price = _to_float(deal.get("price"))
            status = _status_key(deal)

            previous = existing.get(item_key)
            previous_price = _to_float(previous["last_price"]) if previous is not None else None
            previous_status = str(previous["last_status"]) if previous is not None and previous["last_status"] else None
            previous_last_alerted_at = str(previous["last_alerted_at"]) if previous is not None else None
            previous_cooldown_until = str(previous["cooldown_until"]) if previous is not None else None
            previous_alert_count = int(previous["alert_count"] or 0) if previous is not None else 0
            created_at = str(previous["created_at"]) if previous is not None else now_iso

            reasons: list[str] = []
            if previous is None:
                reasons.append("new_item")

            material_change = _material_price_change(
                previous_price,
                price,
                pct_threshold=material_price_change_pct,
                abs_threshold=material_price_change_abs_usd,
            )
            if material_change:
                reasons.append("material_price_change")

            status_changed = (
                previous_status is not None
                and previous_status != status
                and {previous_status, status} != {"unknown"}
            )
            if status_changed:
                reasons.append("status_changed")

            last_alerted_dt = _parse_iso(previous_last_alerted_at)
            cooldown_until_dt = _parse_iso(previous_cooldown_until)
            if cooldown_until_dt is None and last_alerted_dt is not None:
                cooldown_until_dt = last_alerted_dt + timedelta(seconds=cooldown_seconds)
            cooldown_expired = (
                last_alerted_dt is None
                or cooldown_until_dt is None
                or now_dt >= cooldown_until_dt
            )
            if cooldown_expired and previous is not None:
                reasons.append("cooldown_expired")

            should_alert = last_alerted_dt is None or material_change or status_changed or cooldown_expired
            if should_alert:
                alertable_deals.append(deal)
                new_last_alerted_at = now_iso
                new_cooldown_until = _to_iso(now_dt + timedelta(seconds=cooldown_seconds))
                alert_count = previous_alert_count + 1
                persisted_price = price
                persisted_status = status
            else:
                suppressed_deals.append(deal)
                reasons.append("in_cooldown")
                new_last_alerted_at = previous_last_alerted_at
                new_cooldown_until = previous_cooldown_until
                alert_count = previous_alert_count
                persisted_price = previous_price
                persisted_status = previous_status

            snapshot = {
                "source": source,
                "sku": sku,
                "url": url,
                "title": title,
                "price": price,
                "status": status,
            }
            conn.execute(
                """
                INSERT INTO deal_alert_state (
                    item_key, source, sku, url, title, last_seen_at, last_alerted_at,
                    cooldown_until, last_price, last_status, last_payload_json,
                    alert_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key)
                DO UPDATE SET
                    source = excluded.source,
                    sku = excluded.sku,
                    url = excluded.url,
                    title = excluded.title,
                    last_seen_at = excluded.last_seen_at,
                    last_alerted_at = excluded.last_alerted_at,
                    cooldown_until = excluded.cooldown_until,
                    last_price = excluded.last_price,
                    last_status = excluded.last_status,
                    last_payload_json = excluded.last_payload_json,
                    alert_count = excluded.alert_count,
                    updated_at = excluded.updated_at
                """,
                (
                    item_key,
                    source,
                    sku,
                    url,
                    title,
                    now_iso,
                    new_last_alerted_at,
                    new_cooldown_until,
                    persisted_price,
                    persisted_status,
                    json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True),
                    alert_count,
                    created_at,
                    now_iso,
                ),
            )

            decisions.append(
                {
                    "deal_key": item_key,
                    "should_alert": should_alert,
                    "reasons": reasons,
                    "previous_price": previous_price,
                    "current_price": price,
                    "previous_status": previous_status,
                    "current_status": status,
                    "last_alerted_at": new_last_alerted_at,
                    "cooldown_until": new_cooldown_until,
                }
            )

        conn.commit()

    return {
        "decisions": decisions,
        "alertable_deals": alertable_deals,
        "suppressed_deals": suppressed_deals,
    }
