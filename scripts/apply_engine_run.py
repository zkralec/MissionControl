#!/usr/bin/env python3
"""
CLI entrypoint for the apply engine.

Usage:
    python scripts/apply_engine_run.py <job_url> [options]

Examples:
    # Run against a LinkedIn job posting (visible browser for debugging)
    python scripts/apply_engine_run.py "https://www.linkedin.com/jobs/view/12345" --no-headless

    # Run headless with a specific profile
    python scripts/apply_engine_run.py "https://boards.greenhouse.io/company/jobs/456" \
        --profile config/applicant_profile.yaml

    # Use saved LinkedIn auth state
    python scripts/apply_engine_run.py "https://linkedin.com/jobs/view/789" \
        --auth-state /data/auth/linkedin_auth.json

    # Enable LLM fallback for unknown fields
    python scripts/apply_engine_run.py "https://..." --enable-llm

    # Print result as JSON
    python scripts/apply_engine_run.py "https://..." --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.apply_engine.observability import get_logger
from integrations.apply_engine.runner import ApplyConfig, ApplyResult, run_apply

_log = get_logger("cli")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply Engine — fill job applications via Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("job_url", help="URL of the job posting to apply to")
    parser.add_argument(
        "--profile", "-p",
        default=str(ROOT / "config" / "applicant_profile.yaml"),
        help="Path to applicant profile YAML (default: config/applicant_profile.yaml)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=str(ROOT / "data" / "apply_engine_runs"),
        help="Directory for screenshots and run summaries",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for debugging)",
    )
    parser.add_argument(
        "--auth-state",
        default=None,
        help="Path to saved browser auth state (cookies + localStorage) JSON file",
    )
    parser.add_argument(
        "--browser-profile",
        default=None,
        help="Path to a persistent Chrome/Chromium profile directory",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        default=False,
        help="Enable OpenAI fallback for unmatched long-form prompts (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--llm-budget",
        type=int,
        default=10,
        help="Max OpenAI fallback calls per run (default: 10)",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-5-mini",
        help="OpenAI model for long-form answer generation (default: gpt-5-mini)",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=1500,
        help="Max completion tokens for long-form answer generation (default: 1500)",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow down Playwright actions by N ms (useful for watching execution)",
    )
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        default=False,
        help=(
            "When the review page is reached, prompt for confirmation and submit "
            "the application if you type 'yes' or 'submit'. "
            "Without this flag the engine stops at draft_ready (safe default)."
        ),
    )
    parser.add_argument(
        "--avature-email",
        default=None,
        help="Email for Avature portal login (overrides AVATURE_EMAIL env var)",
    )
    # Note: Avature password is intentionally NOT a CLI flag — use AVATURE_PASSWORD
    # in your .env file to keep it out of shell history and process listings.
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Print result as JSON to stdout",
    )
    return parser.parse_args()


async def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    _log.debug(f"env loaded | openai_api_key_present={bool(os.getenv('OPENAI_API_KEY'))}")

    args = parse_args()

    profile_path = Path(args.profile)
    if not profile_path.exists():
        print(f"ERROR: Profile not found: {profile_path}", file=sys.stderr)
        print(f"  Copy config/applicant_profile.example.yaml to {profile_path} and fill it in.", file=sys.stderr)
        return 1

    config = ApplyConfig(
        job_url=args.job_url,
        profile_path=profile_path,
        output_dir=Path(args.output_dir),
        headless=not args.no_headless,
        storage_state_path=args.auth_state,
        browser_profile_dir=args.browser_profile,
        enable_llm=args.enable_llm,
        llm_budget=args.llm_budget,
        llm_model=args.llm_model,
        llm_max_tokens=args.llm_max_tokens,
        slow_mo_ms=args.slow_mo,
        auto_submit=args.auto_submit,
        avature_email=args.avature_email,
        # avature_password is loaded from AVATURE_PASSWORD env var inside run_apply()
    )

    print(f"Starting apply engine for: {args.job_url}", file=sys.stderr)
    print(f"Profile: {profile_path}", file=sys.stderr)
    print(f"Headless: {config.headless}", file=sys.stderr)

    result: ApplyResult = await run_apply(config)

    if args.output_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_result(result)

    return 0 if result.success else 1


def _print_result(result: ApplyResult) -> None:
    width = 60
    print("\n" + "=" * width)
    print(f"  Apply Engine Result")
    print("=" * width)
    print(f"  Run ID:        {result.run_id}")
    print(f"  Site:          {result.site_name} (adapter: {result.adapter_name})")
    print(f"  Status:        {result.status}")
    print(f"  Submitted:     {getattr(result, 'submitted', False)}")
    print(f"  Review reached: {result.review_reached}")
    print(f"  Fields filled: {result.fields_filled_count}")
    print(f"  Fields failed: {result.fields_failed_count}")
    print(f"  Steps:         {result.step_count}")
    print(f"  LLM calls:     {result.llm_calls_used}")
    print(f"  Screenshots:   {len(result.screenshots)}")
    if result.summary_path:
        from pathlib import Path
        fields_log = Path(result.summary_path).with_name(
            Path(result.summary_path).name.replace("-summary.json", "-fields.log")
        )
        print(f"  Summary saved: {result.summary_path}")
        if fields_log.exists():
            print(f"  Fields log:    {fields_log}")
    if result.failure_reason:
        print(f"\n  FAILURE: {result.failure_reason}")
    if result.notes:
        print("\n  Notes:")
        for note in result.notes:
            print(f"    - {note}")
    if result.screenshots:
        print("\n  Screenshots:")
        for s in result.screenshots:
            print(f"    {s}")
    print("=" * width + "\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
