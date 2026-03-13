import json
import os
import time
from typing import Any
from urllib import error, request


class NotificationConfigError(Exception):
    pass


def _is_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _retry_after_seconds(headers: Any, response_body: str | None) -> float:
    header_value = headers.get("Retry-After") if headers is not None else None
    if header_value:
        try:
            return max(float(header_value), 0.0)
        except ValueError:
            pass

    if response_body:
        try:
            body_json = json.loads(response_body)
        except json.JSONDecodeError:
            body_json = None
        if isinstance(body_json, dict) and "retry_after" in body_json:
            try:
                return max(float(body_json["retry_after"]), 0.0)
            except (TypeError, ValueError):
                pass

    return 1.0


def _post_once(url: str, payload: dict[str, Any]) -> tuple[int, Any, str | None]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MissionControl/1.0",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=10) as resp:
            resp_body_bytes = resp.read()
            resp_body = resp_body_bytes.decode("utf-8", errors="replace") if resp_body_bytes else None
            return int(resp.getcode()), resp.headers, resp_body
    except error.HTTPError as exc:
        err_body_bytes = exc.read()
        err_body = err_body_bytes.decode("utf-8", errors="replace") if err_body_bytes else None
        return int(exc.code), exc.headers, err_body
    except error.URLError as exc:
        raise RuntimeError("discord webhook request failed") from exc


def send_discord_webhook(content: str, metadata: dict | None = None) -> dict:
    del metadata

    if _is_true(os.getenv("NOTIFY_DEV_MODE")):
        return {"provider": "discord", "status": "mocked", "http_status": None, "rate_limited": False}

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise NotificationConfigError("DISCORD_WEBHOOK_URL is required for live Discord notifications")

    payload: dict[str, Any] = {"content": content}
    username = os.getenv("DISCORD_USERNAME")
    avatar_url = os.getenv("DISCORD_AVATAR_URL")
    if username:
        payload["username"] = username
    if avatar_url:
        payload["avatar_url"] = avatar_url

    status_code, headers, response_body = _post_once(webhook_url, payload)
    rate_limited = False

    if status_code == 429:
        rate_limited = True
        wait_seconds = _retry_after_seconds(headers, response_body)
        time.sleep(wait_seconds)
        status_code, _, _ = _post_once(webhook_url, payload)

    if 200 <= status_code < 300:
        return {
            "provider": "discord",
            "status": "sent",
            "http_status": status_code,
            "rate_limited": rate_limited,
        }

    raise RuntimeError(f"discord webhook failed: status={status_code} body={response_body}")
