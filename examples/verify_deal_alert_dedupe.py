#!/usr/bin/env python3
"""Verify deal alert dedupe/cooldown state with one repeated item."""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worker_dir = repo_root / "worker"
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))

    state_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("DEAL_ALERT_STATE_DB_PATH", str(state_db))
    os.environ.setdefault("DEAL_ALERT_COOLDOWN_SECONDS", "21600")
    os.environ.setdefault("DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT", "3")
    os.environ.setdefault("DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD", "25")

    deals_scan = importlib.import_module("task_handlers.deals_scan_v1")
    alert_state = importlib.import_module("deal_alert_state")

    base_ts = datetime.now(timezone.utc).replace(microsecond=0)
    deal = {
        "source": "bestbuy",
        "title": "Gaming Desktop PC with RTX 5090",
        "url": "https://shop.example/desktop",
        "sku": "SKU-123",
        "price": 3799.0,
        "in_stock": True,
    }
    payload = {"source": "verify-script", "collectors_enabled": False, "deals": [deal]}

    first = deals_scan.build_unicorn_notify_request(
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
        result_json=None,
        run_timestamp=base_ts,
    )
    second = deals_scan.build_unicorn_notify_request(
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
        result_json=None,
        run_timestamp=base_ts + timedelta(minutes=10),
    )
    changed = dict(deal, price=3599.0)
    third = deals_scan.build_unicorn_notify_request(
        payload_json=json.dumps(
            {"source": "verify-script", "collectors_enabled": False, "deals": [changed]},
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        result_json=None,
        run_timestamp=base_ts + timedelta(minutes=20),
    )

    rows = alert_state.list_recent_deal_alert_states(limit=1)
    print(f"deal_alert_state_db={alert_state.get_deal_alert_state_db_path()}")
    print(f"first_notify={first.get('notify_payload') is not None}")
    print(f"second_notify={second.get('notify_payload') is not None}")
    print(f"third_notify_after_price_change={third.get('notify_payload') is not None}")
    if rows:
        print(json.dumps(rows[0], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
