#!/usr/bin/env python3
"""Repo-local backend for Mission Control's OpenClaw draft runner.

It receives the normalized apply-draft request from Mission Control via stdin or
`--input-json-file`, translates it into conservative `openclaw browser`
operations, and always stops before final submit.

Examples:
- `python3 scripts/openclaw_apply_browser_backend.py < payload.json`
- `python3 scripts/openclaw_apply_browser_backend.py --input-json-file payload.json`
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.openclaw_apply_browser_backend import main


if __name__ == "__main__":
    raise SystemExit(main())
