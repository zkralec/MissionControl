import re
from typing import Any

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

SOURCE = "bestbuy"
BASE_URL = "https://www.bestbuy.com"
TARGET_URLS = (
    "https://www.bestbuy.com/site/searchpage.jsp?st=rtx+5090",
)

_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/[^"]+|https?://[^"]+bestbuy\.com[^"]*)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_KEYWORD_RE = re.compile(r"\b(?:rtx|geforce)\s*5090\b", re.IGNORECASE)
_OLD_PRICE_RE = re.compile(
    r"(?:was|regular|orig(?:inal)?|list)\D{0,20}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_CUR_PRICE_RE = re.compile(
    r"(?:now|sale|current|price)\D{0,20}\$([0-9][0-9,]*(?:\.[0-9]{2})?)",
    re.IGNORECASE,
)
_SKU_RE = re.compile(r"(?:sku[-_ ]?id|data-sku-id)\D{0,10}([0-9]{5,})", re.IGNORECASE)
_SKU_FROM_URL_RE = re.compile(r"/([0-9]{5,})\.p(?:\?|$)", re.IGNORECASE)
_SKU_FROM_URL_ALT_RE = re.compile(r"/sku/([0-9]{5,})(?:\?|$)", re.IGNORECASE)
_SKU_CUSTOMER_PRICE_RE = re.compile(
    r'"price"\s*:\s*\{.*?"customerPrice"\s*:\s*(?P<customer>[0-9]+(?:\.[0-9]+)?).*?"skuId"\s*:\s*"(?P<sku>[0-9]{5,})"',
    re.IGNORECASE | re.DOTALL,
)
_SKU_REGULAR_PRICE_RE = re.compile(
    r'"price"\s*:\s*\{.*?"displayableRegularPrice"\s*:\s*(?P<regular>[0-9]+(?:\.[0-9]+)?).*?"skuId"\s*:\s*"(?P<sku>[0-9]{5,})"',
    re.IGNORECASE | re.DOTALL,
)
_CONTEXT_CUSTOMER_PRICE_RE = re.compile(r'"customerPrice"\s*:\s*([0-9]+(?:\.[0-9]+)?)', re.IGNORECASE)
_CONTEXT_REGULAR_PRICE_RE = re.compile(r'"displayableRegularPrice"\s*:\s*([0-9]+(?:\.[0-9]+)?)', re.IGNORECASE)


def _extract_prices(snippet: str) -> tuple[float | None, float | None]:
    current_match = _CUR_PRICE_RE.search(snippet)
    old_match = _OLD_PRICE_RE.search(snippet)
    current = parse_price(current_match.group(1)) if current_match else None
    old = parse_price(old_match.group(1)) if old_match else None

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


def _extract_sku(url: str, snippet: str) -> str | None:
    from_snippet = _SKU_RE.search(snippet)
    if from_snippet:
        return from_snippet.group(1)
    from_alt_url = _SKU_FROM_URL_ALT_RE.search(url)
    if from_alt_url:
        return from_alt_url.group(1)
    from_url = _SKU_FROM_URL_RE.search(url)
    if from_url:
        return from_url.group(1)
    return None


def _extract_sku_price_maps(html_text: str) -> tuple[dict[str, float], dict[str, float]]:
    customer_price_by_sku: dict[str, float] = {}
    regular_price_by_sku: dict[str, float] = {}
    for match in _SKU_CUSTOMER_PRICE_RE.finditer(html_text):
        sku = match.group("sku")
        customer = parse_price(match.group("customer"))
        if sku and customer is not None:
            customer_price_by_sku[sku] = customer
    for match in _SKU_REGULAR_PRICE_RE.finditer(html_text):
        sku = match.group("sku")
        regular = parse_price(match.group("regular"))
        if sku and regular is not None:
            regular_price_by_sku[sku] = regular
    return customer_price_by_sku, regular_price_by_sku


def _title_price_hints(html_text: str, title: str) -> tuple[float | None, float | None]:
    if not title:
        return None, None
    start = html_text.find(title)
    if start < 0:
        escaped_title = title.replace('"', '\\"')
        start = html_text.find(escaped_title)
    if start < 0:
        return None, None

    snippet = html_text[max(0, start - 3500): min(len(html_text), start + 9000)]
    customer_candidates = [parse_price(v) for v in _CONTEXT_CUSTOMER_PRICE_RE.findall(snippet)]
    regular_candidates = [parse_price(v) for v in _CONTEXT_REGULAR_PRICE_RE.findall(snippet)]
    customer_values = [value for value in customer_candidates if value is not None]
    regular_values = [value for value in regular_candidates if value is not None]

    customer = min(customer_values) if customer_values else None
    regular = min((value for value in regular_values if customer is None or value >= customer), default=None)
    return customer, regular


def _parse_page(html_text: str) -> list[dict[str, Any]]:
    deals: list[dict[str, Any]] = []
    customer_price_by_sku, regular_price_by_sku = _extract_sku_price_maps(html_text)
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
        if sku:
            mapped_price = customer_price_by_sku.get(sku)
            mapped_regular = regular_price_by_sku.get(sku)
            if mapped_price is not None:
                price = mapped_price
            if mapped_regular is not None and price is not None and mapped_regular > price:
                old_price = mapped_regular
        if price is None:
            hinted_price, hinted_regular = _title_price_hints(html_text, raw_title)
            if hinted_price is not None:
                price = hinted_price
            if hinted_regular is not None and price is not None and hinted_regular > price:
                old_price = hinted_regular
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
                "raw": {"hint": "bestbuy_search"},
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
