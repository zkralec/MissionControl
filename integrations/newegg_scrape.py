import re
from typing import Any
from integrations.scrape_common import pick_plausible_price

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

SOURCE = "newegg"
BASE_URL = "https://www.newegg.com"
TARGET_URLS = (
    "https://www.newegg.com/p/pl?d=rtx+5090",
)

_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/[^"]+|https?://[^"]+newegg\.com[^"]*)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_KEYWORD_RE = re.compile(r"\b(?:rtx|geforce)\s*5090\b", re.IGNORECASE)
_OLD_PRICE_RE = re.compile(
    r"(?:was|regular|orig(?:inal)?|list|before)\D{0,30}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_CUR_PRICE_RE = re.compile(
    r"(?:now|sale|current)\D{0,20}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_ITEM_NUMBER_RE = re.compile(r"(?:item=|/p/)(N82E168[0-9A-Z]+)", re.IGNORECASE)


def _extract_prices(snippet: str) -> tuple[float | None, float | None]:
    current_match = _CUR_PRICE_RE.search(snippet)
    old_match = _OLD_PRICE_RE.search(snippet)
    current = parse_price(current_match.group(1)) if current_match else None
    old = parse_price(old_match.group(1)) if old_match else None

    prices = extract_price_values(snippet)
    if current is None and prices:
        current = pick_plausible_price(prices, title=None)
    if old is None and len(prices) >= 2:
        max_price = max(prices)
        if current is not None and max_price > current:
            old = max_price

    if old is not None and current is not None and old <= current:
        old = None
    return current, old


def _extract_sku(url: str, snippet: str) -> str | None:
    item_match = _ITEM_NUMBER_RE.search(url)
    if item_match:
        return item_match.group(1)
    snippet_match = _ITEM_NUMBER_RE.search(snippet)
    if snippet_match:
        return snippet_match.group(1)
    return None


def _parse_page(html_text: str) -> list[dict[str, Any]]:
    deals: list[dict[str, Any]] = []
    scraped_at = now_utc_iso()
    for match in _LINK_RE.finditer(html_text):
        raw_title = clean_html_text(match.group("title") or "")
        if not raw_title or not _KEYWORD_RE.search(raw_title):
            continue

        href = match.group("href") or ""
        url = absolute_url(BASE_URL, href)
        start, end = match.span()
        snippet = html_text[max(0, start - 700): min(len(html_text), end + 1400)]
        price, old_price = _extract_prices(snippet)
        sku = _extract_sku(url, snippet)
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
                "raw": {"hint": "newegg_search"},
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
            html_text = fetch_html(url)
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
