import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from deal_alert_state import evaluate_and_record_deal_alerts
from event_log import log_event as persist_event_log

DEFAULT_UNICORN_MAX_ITEMS_IN_MESSAGE = 5
DEFAULT_UNICORN_NOTIFY_SEVERITY = "info"
DEFAULT_NOTIFY_DEDUPE_TTL_SECONDS = 21600
DEFAULT_DEAL_ALERT_COOLDOWN_SECONDS = DEFAULT_NOTIFY_DEDUPE_TTL_SECONDS
DEFAULT_DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT = 3.0
DEFAULT_DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD = 25.0
DEFAULT_UNICORN_5090_GPU_MAX_PRICE = 2000.0
DEFAULT_UNICORN_5090_PC_MAX_PRICE = 4000.0

logger = logging.getLogger(__name__)

_PERIPHERAL_KEYWORDS = (
    "mouse",
    "keyboard",
    "headset",
    "earbuds",
    "speaker",
    "monitor",
    "webcam",
    "dock",
    "docking station",
    "ssd",
    "hdd",
    "nvme",
    "ram",
    "ddr4",
    "ddr5",
    "psu",
    "power supply",
    "motherboard",
    "mobo",
    "case",
    "chassis",
    "cooler",
    "heatsink",
    "thermal paste",
    "thermal pad",
    "fan",
    "cable",
    "adapter",
    "riser",
    "water block",
    "backplate",
    "mount",
    "bracket",
    "hub",
    "controller",
)
_GPU_TERMS_RE = re.compile(
    r"\b("
    r"rtx\s*\d{4}|"
    r"geforce\s*rtx\s*\d{4}|"
    r"radeon\s*(?:rx\s*)?\d{4}|"
    r"graphics card|"
    r"video card|"
    r"\bgpu\b"
    r")\b",
    re.IGNORECASE,
)
_COMPUTER_TERMS_RE = re.compile(
    r"\b("
    r"desktop|"
    r"gaming pc|"
    r"\bpc\b|"
    r"prebuilt|"
    r"workstation|"
    r"tower|"
    r"gaming computer"
    r")\b",
    re.IGNORECASE,
)
_STRONG_COMPUTER_TERMS_RE = re.compile(
    r"\b("
    r"desktop|"
    r"gaming pc|"
    r"prebuilt|"
    r"workstation|"
    r"tower|"
    r"gaming computer"
    r")\b",
    re.IGNORECASE,
)
_RTX_5090_RE = re.compile(r"\b(?:rtx|geforce)\s*5090\b", re.IGNORECASE)
_LAPTOP_TERMS_RE = re.compile(r"\b(laptop|notebook)\b", re.IGNORECASE)


def _redis_client() -> Redis:
    return Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))


def _cache_key(payload_json: str) -> str:
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return f"deals_scan_v1:{digest}"


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
    if stripped.endswith("%"):
        stripped = stripped[:-1]
    try:
        return float(stripped)
    except ValueError:
        return None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "in stock", "available"}:
            return True
        if low in {"false", "no", "out of stock", "sold out", "unavailable"}:
            return False
    return None


def _compute_discount_pct(price_value: float | None, old_price_value: float | None) -> float | None:
    if price_value is None or old_price_value is None:
        return None
    if old_price_value <= 0 or price_value < 0:
        return None
    discount = ((old_price_value - price_value) / old_price_value) * 100.0
    return round(discount, 2)


def _pick_first_str(deal: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = deal.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _pick_first_value(deal: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in deal:
            value = deal.get(key)
            if value not in (None, ""):
                return value
    return None


def _extract_raw_deal_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]

    if isinstance(raw, dict):
        items: list[dict[str, Any]] = []
        for key in ("deals", "items", "results", "top_deals", "highest_risk"):
            value = raw.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))

        if items:
            return items

        if any(
            key in raw
            for key in (
                "title",
                "name",
                "url",
                "link",
                "price",
                "old_price",
                "discount_pct",
                "score",
            )
        ):
            return [raw]
    return []


def _payload_object(payload_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_deals(raw: Any) -> list[dict[str, Any]]:
    """
    Normalize deals from payload/result/collector structures into a stable shape.
    """
    normalized: list[dict[str, Any]] = []
    for deal in _extract_raw_deal_items(raw):
        title = _pick_first_str(deal, ("title", "name", "deal_title")) or "Untitled deal"
        url = _pick_first_str(deal, ("url", "link", "deal_url", "product_url"))
        source = _pick_first_str(deal, ("source", "origin", "feed"))

        price_raw = _pick_first_value(deal, ("price", "current_price", "sale_price", "amount"))
        old_price_raw = _pick_first_value(deal, ("old_price", "list_price", "original_price", "msrp", "price_before"))
        explicit_discount_raw = _pick_first_value(
            deal,
            ("discount_pct", "discount_percent", "discount", "pct_off", "savings_pct"),
        )
        score_raw = _pick_first_value(deal, ("score", "unicorn_score", "risk_score"))
        sku = _pick_first_str(deal, ("sku", "product_id", "item_number"))
        vendor = _pick_first_str(deal, ("vendor", "merchant", "store"))
        scraped_at = _pick_first_str(deal, ("scraped_at",))
        in_stock = _to_bool(_pick_first_value(deal, ("in_stock", "available", "is_available")))
        raw_payload = deal.get("raw") if isinstance(deal.get("raw"), dict) else {}

        price_value = _to_float(price_raw)
        old_price_value = _to_float(old_price_raw)
        explicit_discount_pct = _to_float(explicit_discount_raw)
        computed_discount_pct = _compute_discount_pct(price_value, old_price_value)
        score = _to_float(score_raw)

        normalized.append(
            {
                "source": source,
                "title": title,
                "url": url,
                "price": price_value,
                "old_price": old_price_value,
                "discount_pct": explicit_discount_pct,
                "computed_discount_pct": computed_discount_pct,
                "sku": sku,
                "in_stock": in_stock,
                "scraped_at": scraped_at,
                "score": score,
                "vendor": vendor,
                "raw": raw_payload,
                "size_usd": _to_float(_pick_first_value(deal, ("size_usd",))),
                "debt_ratio": _to_float(_pick_first_value(deal, ("debt_ratio",))),
                "customer_churn": _to_float(_pick_first_value(deal, ("customer_churn",))),
            }
        )

    return normalized


def _effective_discount_pct(deal: dict[str, Any]) -> float | None:
    discount_pct = _to_float(deal.get("discount_pct"))
    if discount_pct is not None:
        return discount_pct
    return _to_float(deal.get("computed_discount_pct"))


def _title_lower(deal: dict[str, Any]) -> str:
    title = deal.get("title")
    if not isinstance(title, str):
        return ""
    return title.strip().lower()


def _is_peripheral_title(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return True
    return any(keyword in low for keyword in _PERIPHERAL_KEYWORDS)


def _is_gpu_title(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return False
    if _LAPTOP_TERMS_RE.search(low):
        return False
    if _is_computer_title(low):
        return False
    return bool(_GPU_TERMS_RE.search(low))


def _is_computer_title(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return False
    return bool(_COMPUTER_TERMS_RE.search(low))


def _is_rtx_5090_title(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return False
    return bool(_RTX_5090_RE.search(low))


def _is_laptop_title(title: str) -> bool:
    low = title.strip().lower()
    if not low:
        return False
    return bool(_LAPTOP_TERMS_RE.search(low))


def filter_target_items(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Keep only:
    1) GPU listings that explicitly indicate RTX 5090.
    2) Computer/prebuilt listings that explicitly indicate RTX 5090.
    Drop peripherals/accessories and uncertain titles.
    """
    filtered: list[dict[str, Any]] = []
    for deal in deals:
        title = _title_lower(deal)
        if not title:
            continue
        if _is_laptop_title(title):
            continue
        is_gpu = _is_gpu_title(title)
        is_computer = _is_computer_title(title)
        if not is_gpu and not is_computer:
            continue
        if not _is_rtx_5090_title(title):
            continue
        if is_gpu:
            if _is_peripheral_title(title):
                continue
            filtered.append(deal)
            continue
        if is_computer:
            # Desktop titles often include component words (RAM/SSD);
            # keep those when the title still clearly describes a full computer.
            if _is_peripheral_title(title) and not _STRONG_COMPUTER_TERMS_RE.search(title):
                continue
            filtered.append(deal)
            continue
    return filtered


def filter_unicorn_deals(
    deals: list[dict[str, Any]],
    *,
    gpu_5090_max_price: float,
    pc_5090_max_price: float,
) -> list[dict[str, Any]]:
    unicorns: list[dict[str, Any]] = []
    for deal in deals:
        price = _to_float(deal.get("price"))
        title = _title_lower(deal)
        if _is_laptop_title(title):
            continue

        is_5090_gpu = _is_gpu_title(title) and _is_rtx_5090_title(title)
        is_5090_pc = _is_computer_title(title) and _is_rtx_5090_title(title)
        qualifies_by_price = (
            is_5090_gpu and price is not None and price <= gpu_5090_max_price
        ) or (
            is_5090_pc and price is not None and price <= pc_5090_max_price
        )

        if qualifies_by_price:
            unicorn_deal = dict(deal)
            unicorn_deal["effective_discount_pct"] = _effective_discount_pct(deal)
            unicorn_deal["unicorn_reason"] = "price_threshold"
            unicorns.append(unicorn_deal)

    unicorns.sort(
        key=lambda item: (
            _to_float(item.get("price")) or float("inf"),
            str(item.get("title") or ""),
        ),
    )
    return unicorns


def _format_price_value(deal: dict[str, Any]) -> str | None:
    numeric = _to_float(deal.get("price"))
    if numeric is None:
        return None
    return f"${numeric:.2f}"


def format_unicorn_message(unicorn_deals: list[dict[str, Any]], max_items: int) -> str:
    max_lines = max(1, int(max_items))
    lines = [f"🦄 Unicorn deals found: {len(unicorn_deals)}"]
    for deal in unicorn_deals[:max_lines]:
        title = str(deal.get("title") or "Untitled deal")
        price_text = _format_price_value(deal)
        url = str(deal.get("url") or "").strip()

        detail_parts: list[str] = []
        if price_text:
            detail_parts.append(price_text)

        line = f"• {title}"
        if detail_parts:
            line += " — " + " ".join(detail_parts)
        if url:
            line += f" {url}"
        lines.append(line)
    return "\n".join(lines)


def _sanitize_notify_severity(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"info", "warn", "urgent"}:
        return value
    return DEFAULT_UNICORN_NOTIFY_SEVERITY


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(value, minimum)


def _payload_float(payload_obj: dict[str, Any], keys: tuple[str, ...], fallback: float) -> float:
    for key in keys:
        value = _to_float(payload_obj.get(key))
        if value is not None:
            return value
    return fallback


def _payload_int(payload_obj: dict[str, Any], keys: tuple[str, ...], fallback: int, minimum: int = 1) -> int:
    for key in keys:
        raw = payload_obj.get(key)
        if raw is None:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        return max(parsed, minimum)
    return fallback


def _source_counts(deals: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for deal in deals:
        source = str(deal.get("source") or "unknown").strip().lower()
        counts[source] = counts.get(source, 0) + 1
    return counts


def _alert_reason_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        reasons = decision.get("reasons")
        if not isinstance(reasons, list):
            continue
        for reason in reasons:
            key = str(reason).strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
    return counts


def _unicorn_dedupe_key(
    unicorn_deals: list[dict[str, Any]],
    bucket_dt: datetime,
    *,
    max_items: int,
) -> str:
    hour_bucket = bucket_dt.astimezone(timezone.utc).strftime("%Y%m%d-%H")
    selected_urls: list[str] = []
    seen: set[str] = set()
    for deal in unicorn_deals:
        url = str(deal.get("url") or "").strip()
        if not url or url in seen:
            continue
        selected_urls.append(url)
        seen.add(url)
        if len(selected_urls) >= max_items:
            break

    if not selected_urls:
        for deal in unicorn_deals:
            title = str(deal.get("title") or "").strip()
            if not title or title in seen:
                continue
            selected_urls.append(title)
            seen.add(title)
            if len(selected_urls) >= max_items:
                break

    digest = hashlib.sha256("|".join(selected_urls).encode("utf-8")).hexdigest()[:16]
    return f"unicorn:{hour_bucket}:{digest}"


def _dedupe_normalized_deals(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for deal in deals:
        source = str(deal.get("source") or "").strip().lower()
        url = str(deal.get("url") or "").strip()
        title = str(deal.get("title") or "").strip().lower()
        key = (source, url, title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(deal)
    return deduped


def _collectors_enabled(payload_obj: dict[str, Any]) -> bool:
    override = payload_obj.get("collectors_enabled")
    if isinstance(override, bool):
        return override
    # Keep payload-driven tests deterministic and avoid network when deals are provided explicitly.
    return "deals" not in payload_obj


def _collect_scraped_deals() -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    site_counts = {"bestbuy": 0, "newegg": 0, "microcenter": 0}
    warnings: list[str] = []
    collected: list[dict[str, Any]] = []

    try:
        from integrations.bestbuy_scrape import collect_deals as bestbuy_collect
        from integrations.microcenter_scrape import collect_deals as microcenter_collect
        from integrations.newegg_scrape import collect_deals as newegg_collect
    except Exception as exc:
        warnings.append(f"collector_import_failed: {type(exc).__name__}: {exc}")
        return [], site_counts, warnings

    collectors = (
        ("bestbuy", bestbuy_collect),
        ("newegg", newegg_collect),
        ("microcenter", microcenter_collect),
    )
    for source, collector in collectors:
        try:
            raw_deals, site_warnings = collector()
        except Exception as exc:
            warnings.append(f"{source}: collector_failed: {type(exc).__name__}: {exc}")
            continue

        normalized = normalize_deals(raw_deals)
        for deal in normalized:
            if not deal.get("source"):
                deal["source"] = source
            if not deal.get("scraped_at"):
                deal["scraped_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        site_counts[source] = len(normalized)
        collected.extend(normalized)
        warnings.extend(f"{source}: {warning}" for warning in (site_warnings or []))

    return _dedupe_normalized_deals(collected), site_counts, warnings


def build_unicorn_notify_request(
    *,
    payload_json: str,
    result_json: dict[str, Any] | None,
    run_timestamp: datetime | None = None,
) -> dict[str, Any]:
    payload_obj = _payload_object(payload_json)

    max_items = _env_int("UNICORN_MAX_ITEMS_IN_MESSAGE", DEFAULT_UNICORN_MAX_ITEMS_IN_MESSAGE, minimum=1)
    severity = _sanitize_notify_severity(
        os.getenv("UNICORN_NOTIFY_SEVERITY", DEFAULT_UNICORN_NOTIFY_SEVERITY)
    )
    gpu_5090_max_price = _payload_float(
        payload_obj,
        ("unicorn_gpu_5090_max_price", "gpu_5090_max_price"),
        _env_float("UNICORN_5090_GPU_MAX_PRICE", DEFAULT_UNICORN_5090_GPU_MAX_PRICE),
    )
    pc_5090_max_price = _payload_float(
        payload_obj,
        ("unicorn_pc_5090_max_price", "pc_5090_max_price"),
        _env_float("UNICORN_5090_PC_MAX_PRICE", DEFAULT_UNICORN_5090_PC_MAX_PRICE),
    )
    dedupe_ttl_seconds = _env_int(
        "NOTIFY_DEDUPE_TTL_SECONDS",
        DEFAULT_NOTIFY_DEDUPE_TTL_SECONDS,
        minimum=1,
    )
    cooldown_seconds = _payload_int(
        payload_obj,
        ("deal_alert_cooldown_seconds", "cooldown_seconds"),
        _env_int(
            "DEAL_ALERT_COOLDOWN_SECONDS",
            DEFAULT_DEAL_ALERT_COOLDOWN_SECONDS,
            minimum=1,
        ),
        minimum=1,
    )
    material_price_change_pct = _payload_float(
        payload_obj,
        ("deal_alert_material_price_change_pct", "material_price_change_pct"),
        _env_float(
            "DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT",
            DEFAULT_DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT,
        ),
    )
    material_price_change_abs_usd = _payload_float(
        payload_obj,
        ("deal_alert_material_price_change_abs_usd", "material_price_change_abs_usd"),
        _env_float(
            "DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD",
            DEFAULT_DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD,
        ),
    )

    normalized = normalize_deals(result_json.get("deals")) if isinstance(result_json, dict) else []
    if not normalized:
        normalized = normalize_deals(payload_obj.get("deals"))
    if not normalized and isinstance(result_json, dict):
        normalized = normalize_deals(result_json)
    normalized = _dedupe_normalized_deals(normalized)
    logger.debug("deals_scan unicorn pipeline: pre_target_filter=%d", len(normalized))
    target_items = filter_target_items(normalized)
    logger.debug("deals_scan unicorn pipeline: post_target_filter=%d", len(target_items))
    logger.debug("deals_scan unicorn pipeline: pre_unicorn_filter=%d", len(target_items))

    unicorns = filter_unicorn_deals(
        target_items,
        gpu_5090_max_price=gpu_5090_max_price,
        pc_5090_max_price=pc_5090_max_price,
    )
    logger.debug("deals_scan unicorn pipeline: post_unicorn_filter=%d", len(unicorns))

    source = payload_obj.get("source") or payload_obj.get("scan_source") or "scrape"
    sources = sorted({str(d.get("source")).strip().lower() for d in target_items if d.get("source")})
    metadata = {
        "scan_source": source,
        "sources": sources,
        "source_counts": _source_counts(target_items),
        "deals_count": len(target_items),
        "pre_target_count": len(normalized),
        "price_thresholds": {
            "gpu_5090_max_price": gpu_5090_max_price,
            "pc_5090_max_price": pc_5090_max_price,
        },
        "unicorn_count": len(unicorns),
    }

    if not unicorns:
        return {
            "deals_count": len(target_items),
            "unicorn_count": 0,
            "alertable_unicorn_count": 0,
            "notify_payload": None,
            "metadata": metadata,
        }

    bucket_dt = run_timestamp or datetime.now(timezone.utc)
    policy = evaluate_and_record_deal_alerts(
        unicorns,
        now=bucket_dt,
        cooldown_seconds=cooldown_seconds,
        material_price_change_pct=material_price_change_pct,
        material_price_change_abs_usd=material_price_change_abs_usd,
    )
    decisions = policy.get("decisions") if isinstance(policy, dict) else []
    if not isinstance(decisions, list):
        decisions = []
    alertable_unicorns = policy.get("alertable_deals") if isinstance(policy, dict) else []
    if not isinstance(alertable_unicorns, list):
        alertable_unicorns = []

    suppressed_keys = [
        str(decision.get("deal_key"))
        for decision in decisions
        if isinstance(decision, dict) and not bool(decision.get("should_alert"))
    ][:20]
    metadata["alert_policy"] = {
        "cooldown_seconds": cooldown_seconds,
        "material_price_change_pct": material_price_change_pct,
        "material_price_change_abs_usd": material_price_change_abs_usd,
        "evaluated_count": len(unicorns),
        "alertable_count": len(alertable_unicorns),
        "suppressed_count": max(len(unicorns) - len(alertable_unicorns), 0),
        "reason_counts": _alert_reason_counts(decisions),
        "suppressed_keys": suppressed_keys,
    }
    metadata["unicorn_count_total"] = len(unicorns)
    metadata["unicorn_count_alertable"] = len(alertable_unicorns)

    if not alertable_unicorns:
        return {
            "deals_count": len(target_items),
            "unicorn_count": len(unicorns),
            "alertable_unicorn_count": 0,
            "notify_payload": None,
            "metadata": metadata,
        }

    dedupe_key = _unicorn_dedupe_key(alertable_unicorns, bucket_dt, max_items=max_items)
    message = format_unicorn_message(alertable_unicorns, max_items=max_items)
    notify_payload = {
        "source_task_type": "deals_scan_v1",
        "channels": ["discord"],
        "message": message,
        "severity": severity,
        "include_header": False,
        "include_metadata": False,
        "dedupe_key": dedupe_key,
        "dedupe_ttl_seconds": dedupe_ttl_seconds,
        "metadata": metadata,
    }
    return {
        "deals_count": len(target_items),
        "unicorn_count": len(unicorns),
        "alertable_unicorn_count": len(alertable_unicorns),
        "notify_payload": notify_payload,
        "metadata": metadata,
    }


def _risk_score(deal: dict[str, Any]) -> float:
    size = _to_float(deal.get("size_usd")) or 0.0
    debt_ratio = _to_float(deal.get("debt_ratio")) or 0.0
    churn = _to_float(deal.get("customer_churn")) or 0.0
    return min((size / 10_000_000.0) * 0.2 + debt_ratio * 0.5 + churn * 0.3, 1.0)


def execute(task: Any, db: Any) -> dict[str, Any]:
    del db
    payload_json = task.payload_json
    payload_obj = _payload_object(payload_json)
    collectors_enabled = _collectors_enabled(payload_obj)
    cache_key = _cache_key(payload_json)

    if not collectors_enabled:
        try:
            cache = _redis_client()
            cached = cache.get(cache_key)
        except RedisError:
            cache = None
            cached = None

        if cached:
            try:
                cached_json = json.loads(cached)
                return {
                    "artifact_type": "deals",
                    "content_text": cached_json.get("summary_text"),
                    "content_json": cached_json,
                    "llm": None,
                }
            except json.JSONDecodeError:
                pass
    else:
        cache = None

    manual_deals = normalize_deals(payload_obj.get("deals"))
    payload_source = payload_obj.get("source")
    if isinstance(payload_source, str) and payload_source.strip():
        for deal in manual_deals:
            if not deal.get("source"):
                deal["source"] = payload_source.strip().lower()

    scraped_deals: list[dict[str, Any]] = []
    site_counts = {"bestbuy": 0, "newegg": 0, "microcenter": 0}
    warnings: list[str] = []
    if collectors_enabled:
        scraped_deals, site_counts, warnings = _collect_scraped_deals()

    all_deals = _dedupe_normalized_deals(manual_deals + scraped_deals)
    source_counts = _source_counts(all_deals)

    analyzed = []
    for deal in all_deals:
        analyzed.append(
            {
                "name": deal.get("title"),
                "source": deal.get("source"),
                "risk_score": round(_risk_score(deal), 4),
                "price": deal.get("price"),
                "discount_pct": _effective_discount_pct(deal),
                "url": deal.get("url"),
            }
        )
    analyzed_sorted = sorted(analyzed, key=lambda item: item["risk_score"], reverse=True)

    summary_payload = {
        "scanned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "collectors_enabled": collectors_enabled,
        "total_deals": len(all_deals),
        "site_counts": site_counts,
        "source_counts": source_counts,
        "warnings": warnings,
        "highest_risk": analyzed_sorted[:5],
        "deals": all_deals,
    }

    summary_text = (
        f"Collected {summary_payload['total_deals']} deals "
        f"(bestbuy={site_counts['bestbuy']}, newegg={site_counts['newegg']}, microcenter={site_counts['microcenter']})."
    )
    if warnings:
        summary_text += f" Warnings: {len(warnings)}."
        for warning in warnings[:20]:
            try:
                persist_event_log(
                    event_type="scraper_warning",
                    source="deals_scan_v1",
                    level="WARNING",
                    message=f"Scraper warning: {warning}",
                    metadata_json={
                        "task_id": getattr(task, "id", None),
                        "warning": warning,
                        "total_warnings": len(warnings),
                    },
                )
            except Exception:
                pass

    result_payload = {
        "artifact_type": "deals",
        "content_text": summary_text,
        "content_json": summary_payload,
        "llm": {
            "messages": [
                {"role": "system", "content": "You summarize GPU deal scans concisely."},
                {"role": "user", "content": f"Summarize this deal scan:\n\n{json.dumps(summary_payload, ensure_ascii=True)}"},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
        },
    }
    if cache is not None:
        try:
            cache.setex(cache_key, 3600, json.dumps({**summary_payload, "summary_text": summary_text}))
        except RedisError:
            pass
    return result_payload
