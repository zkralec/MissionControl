"""Unit tests for strict target filtering before unicorn classification."""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _titles(filtered: list[dict]) -> set[str]:
    return {str(item.get("title")) for item in filtered}


def test_gpu_32gb_passes() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [{"title": "RTX 5090 32GB Graphics Card"}]
    filtered = deals_module.filter_target_items(deals)
    assert _titles(filtered) == {"RTX 5090 32GB Graphics Card"}


def test_gpu_24gb_fails() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [{"title": "RTX 4090 24GB Graphics Card"}]
    filtered = deals_module.filter_target_items(deals)
    assert filtered == []


def test_peripherals_fail() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [
        {"title": "RTX 5090 Water Block"},
        {"title": "RTX 5090 Power Cable"},
        {"title": "Gaming Monitor 32 inch"},
    ]
    filtered = deals_module.filter_target_items(deals)
    assert filtered == []


def test_prebuilt_with_rtx_5090_passes_without_32gb_text() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [{"title": "Gaming Desktop PC with RTX 5090 and Ryzen 9"}]
    filtered = deals_module.filter_target_items(deals)
    assert _titles(filtered) == {"Gaming Desktop PC with RTX 5090 and Ryzen 9"}


def test_laptop_5090_is_excluded() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [{"title": "Gaming Laptop RTX 5090, 64GB DDR5 RAM, 2TB NVMe SSD"}]
    filtered = deals_module.filter_target_items(deals)
    assert filtered == []


def test_desktop_with_rtx_5080_16gb_fails() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [{"title": "Gaming Desktop with RTX 5080 16GB"}]
    filtered = deals_module.filter_target_items(deals)
    assert filtered == []


def test_5090_price_thresholds_drive_unicorns() -> None:
    deals_module = importlib.import_module("task_handlers.deals_scan_v1")
    deals = [
        {"title": "RTX 5090 Graphics Card", "price": 1999},
        {"title": "Gaming Desktop PC with RTX 5090", "price": 3999},
        {"title": "RTX 5090 Graphics Card", "price": 2199},
        {"title": "Gaming Laptop RTX 5090", "price": 1999},
        {"title": "Gaming Desktop with RTX 5080", "price": 2999},
    ]
    target = deals_module.filter_target_items(deals)
    unicorns = deals_module.filter_unicorn_deals(
        target,
        gpu_5090_max_price=2000.0,
        pc_5090_max_price=4000.0,
    )
    titles = [item.get("title") for item in unicorns]
    assert "RTX 5090 Graphics Card" in titles
    assert "Gaming Desktop PC with RTX 5090" in titles
    assert "Gaming Laptop RTX 5090" not in titles
    assert "Gaming Desktop with RTX 5080" not in titles
