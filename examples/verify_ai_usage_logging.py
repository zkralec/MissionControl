#!/usr/bin/env python3
"""Make one mocked model call and print stored AI usage row."""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worker_dir = repo_root / "worker"
    sys.path.insert(0, str(worker_dir))

    default_usage_db = repo_root / "task_run_history.sqlite3"
    os.environ.setdefault("AI_USAGE_DB_PATH", str(default_usage_db))
    os.environ.setdefault("OPENAI_API_KEY", "test-key")

    adapter = importlib.import_module("llm.openai_adapter")
    usage = importlib.import_module("ai_usage_log")

    with patch("llm.openai_adapter.OpenAI") as mock_openai_class:
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Mocked AI response"
        mock_response.usage.prompt_tokens = 123
        mock_response.usage.completion_tokens = 45
        mock_response.usage.total_tokens = 168
        mock_client.chat.completions.create.return_value = mock_response

        adapter.run_chat_completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello"}],
            task_run_id="verify-ai-usage-run",
            agent_name="verify-script",
        )

    rows = usage.list_ai_usage_today()
    if not rows:
        raise RuntimeError("No rows found in ai_usage table")

    print(f"ai_usage_db={usage.get_ai_usage_db_path()}")
    print(json.dumps(rows[0], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
