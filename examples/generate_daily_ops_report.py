"""Generate and print a deterministic daily AI operations report."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _parse_date_arg(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid date '{raw}'. Use YYYY-MM-DD.") from exc


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip())


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    _load_env_file(repo_root / ".env")
    default_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("DAILY_OPS_REPORT_DB_PATH", str(default_db))

    api_dir = repo_root / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    import daily_ops_report  # type: ignore

    if len(sys.argv) > 1:
        report_date = _parse_date_arg(sys.argv[1])
    else:
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    result = daily_ops_report.generate_daily_ai_ops_report(report_date)

    print(result["report_text"])
    print("\n---")
    print(f"severity={result.get('severity')}")
    print(f"report_db={daily_ops_report.get_daily_ops_report_db_path()}")


if __name__ == "__main__":
    main()
