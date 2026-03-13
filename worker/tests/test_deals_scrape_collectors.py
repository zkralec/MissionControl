"""Unit tests for scraping collectors using static HTML fixtures."""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.mark.parametrize(
    ("module_name", "sample_html", "expected_source", "expected_url"),
    [
        (
            "integrations.bestbuy_scrape",
            """
            <a href="/site/some-gpu/1234567.p">NVIDIA GeForce RTX 5090 Founders Edition</a>
            <div>Was $1,999.99</div>
            <div>Now $1,199.99</div>
            <button>Add to cart</button>
            """,
            "bestbuy",
            "https://www.bestbuy.com/site/some-gpu/1234567.p",
        ),
        (
            "integrations.newegg_scrape",
            """
            <a href="https://www.newegg.com/p/N82E16812345678">GIGABYTE GeForce RTX 5090</a>
            <span>List Price $1,299.99</span>
            <span>Sale $999.99</span>
            <button>Add to cart</button>
            """,
            "newegg",
            "https://www.newegg.com/p/N82E16812345678",
        ),
        (
            "integrations.microcenter_scrape",
            """
            <a href="/product/678901/gpu-card">ASUS GeForce RTX 5090 OC</a>
            <span>Regular $1,399.99</span>
            <span>Now $999.99</span>
            <span>In Stock</span>
            """,
            "microcenter",
            "https://www.microcenter.com/product/678901/gpu-card",
        ),
    ],
)
def test_collectors_parse_normalized_deals(monkeypatch, module_name, sample_html, expected_source, expected_url) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "fetch_html", lambda _url, **_kwargs: sample_html)

    deals, warnings = module.collect_deals()

    assert warnings == []
    assert len(deals) >= 1
    deal = deals[0]
    assert deal["source"] == expected_source
    assert deal["url"] == expected_url
    assert isinstance(deal["title"], str) and deal["title"]
    assert deal["price"] is not None
    assert deal["old_price"] is not None
    assert deal["discount_pct"] is not None
    assert deal["scraped_at"].endswith("Z")
