"""
Apply Engine — Playwright-first job application automation.

Entry points:
  from integrations.apply_engine.runner import run_apply, ApplyConfig, ApplyResult
  from integrations.apply_engine.profile import ApplicantProfile
  from integrations.apply_engine.answer_engine import AnswerEngine, FormQuestion, AnswerResult

Architecture:
  runner.py          — main orchestrator (input URL → AdapterResult)
  session.py         — Playwright browser session management
  profile.py         — YAML applicant profile loader
  answer_engine.py   — deterministic answer lookup + optional LLM fallback
  observability.py   — structured logging + screenshot utilities

  adapters/
    base.py          — SiteAdapter interface + AdapterResult
    linkedin.py      — LinkedIn Easy Apply (Phase 1)
    greenhouse.py    — Greenhouse ATS
    lever.py         — Lever ATS
    workday.py       — Workday (Phase 2 placeholder)
    generic.py       — Generic fallback

  forms/
    classifier.py    — DOM field detection → FormField
    handlers.py      — Fill each field type (text, select, radio, file, ...)
    detector.py      — Step type detection, Next/Submit button finding
"""
from .runner import ApplyConfig, ApplyResult, run_apply

__all__ = ["run_apply", "ApplyConfig", "ApplyResult"]
