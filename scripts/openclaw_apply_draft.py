#!/usr/bin/env python3
"""Mission Control draft-only OpenClaw runner.

This script accepts Mission Control's structured apply-draft payload from either
stdin or `--input-json-file`, delegates the browser work to an OpenClaw adapter,
captures deterministic local artifacts, and always stops before final submit.

Example:
`python3 scripts/openclaw_apply_draft.py < payload.json`

Example with file input:
`python3 scripts/openclaw_apply_draft.py --input-json-file payload.json`
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.openclaw_apply_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
