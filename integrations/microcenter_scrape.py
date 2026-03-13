import re
from typing import Any
from urllib.error import HTTPError

from integrations.scrape_common import (
    absolute_url,
    clean_html_text,
    compute_discount_pct,
    dedupe_deals,
    extract_price_values,
    fetch_html,
    infer_stock,
    now_utc_iso,
    parse_price,
)

SOURCE = "microcenter"
BASE_URL = "https://www.microcenter.com"
TARGET_URLS = (
    "https://www.microcenter.com/search/search_results.aspx?Ntt=rtx+5090",
)
_MICROCENTER_HEADERS = {
    # Micro Center rejects very minimal bot-like headers; keep MissionControl token.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 MissionControl/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": "https://www.microcenter.com/",
}

_PRODUCT_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/product/[0-9]+/[^"]+|https?://[^"]+microcenter\.com/product/[0-9]+/[^"]*)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_KEYWORD_RE = re.compile(r"\b(?:rtx|geforce)\s*5090\b", re.IGNORECASE)
_OLD_PRICE_RE = re.compile(
    r"(?:was|regular|orig(?:inal)?|list)\D{0,20}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_CUR_PRICE_RE = re.compile(
    r"(?:now|sale|current|price|our price)\D{0,20}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_SKU_RE = re.compile(r"/product/([0-9]{4,})/", re.IGNORECASE)
_DATA_PRICE_RE = re.compile(r'data-price="([0-9][0-9,]*(?:\.[0-9]{1,2})?)"', re.IGNORECASE)
_TAGGED_PRICE_RE = re.compile(
    r'itemprop="price"[^>]*>\s*(?:<[^>]+>\s*)*\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
    re.IGNORECASE,
)


def _extract_prices(snippet: str) -> tuple[float | None, float | None]:
    current_match = _CUR_PRICE_RE.search(snippet)
    old_match = _OLD_PRICE_RE.search(snippet)
    current = parse_price(current_match.group(1)) if current_match else None
    old = parse_price(old_match.group(1)) if old_match else None

    if current is None:
        data_price_match = _DATA_PRICE_RE.search(snippet)
        if data_price_match:
            current = parse_price(data_price_match.group(1))
    if current is None:
        tagged_price_match = _TAGGED_PRICE_RE.search(snippet)
        if tagged_price_match:
            current = parse_price(tagged_price_match.group(1))

    prices = extract_price_values(snippet)
    if current is None and prices:
        current = min(prices)
    if old is None and len(prices) >= 2:
        max_price = max(prices)
        if current is not None and max_price > current:
            old = max_price

    if old is not None and current is not None and old <= current:
        old = None
    return current, old


def _extract_sku(url: str) -> str | None:
    sku_match = _SKU_RE.search(url)
    if sku_match:
        return sku_match.group(1)
    return None


def _parse_page(html_text: str) -> list[dict[str, Any]]:
    deals: list[dict[str, Any]] = []
    scraped_at = now_utc_iso()
    for match in _PRODUCT_LINK_RE.finditer(html_text):
        raw_title = clean_html_text(match.group("title") or "")
        if not raw_title or not _KEYWORD_RE.search(raw_title):
            continue
        if raw_title.lower().startswith("quick view"):
            continue

        href = match.group("href") or ""
        url = absolute_url(BASE_URL, href)
        start, end = match.span()
        snippet = html_text[max(0, start - 700): min(len(html_text), end + 1400)]
        price, old_price = _extract_prices(snippet)
        sku = _extract_sku(url)
        in_stock = infer_stock(snippet)

        deals.append(
            {
                "source": SOURCE,
                "title": raw_title,
                "url": url,
                "price": price,
                "old_price": old_price,
                "discount_pct": compute_discount_pct(price, old_price),
                "sku": sku,
                "in_stock": in_stock,
                "scraped_at": scraped_at,
                "raw": {"hint": "microcenter_search"},
            }
        )
        if len(deals) >= 40:
            break
    return dedupe_deals(deals)


def collect_deals() -> tuple[list[dict[str, Any]], list[str]]:
    deals: list[dict[str, Any]] = []
    warnings: list[str] = []

    for url in TARGET_URLS:
        try:
            html_text = fetch_html(url, extra_headers=_MICROCENTER_HEADERS)
        except HTTPError as exc:
            # Keep this explicit so operators know this is an anti-bot block and not parser logic.
            if exc.code == 403:
                warnings.append(f"fetch_blocked_403 url={url}")
            else:
                warnings.append(f"fetch_failed url={url} error=HTTPError: {exc}")
            continue
        except Exception as exc:
            warnings.append(f"fetch_failed url={url} error={type(exc).__name__}: {exc}")
            continue

        try:
            page_deals = _parse_page(html_text)
        except Exception as exc:
            warnings.append(f"parse_failed url={url} error={type(exc).__name__}: {exc}")
            continue

        deals.extend(page_deals)

    return dedupe_deals(deals), warnings
