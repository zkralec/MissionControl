from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = ROOT / "worker"

DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE = 0.85
DEFAULT_FILL_MIN_CONFIDENCE = 0.85
MOTIVATION_CONFIDENCE = 0.86
FALLBACK_MOTIVATION_CONFIDENCE = 0.72

SELF_ID_KEYS = {
    "veteran_status",
    "gender",
    "disability_status",
    "ethnicity",
    "race",
    "ethnicity_race",
}

DECLINE_OPTION_TEXT = "Prefer not to say"

DEFAULT_ANSWER_PROFILE: dict[str, Any] = {
    "work_authorized_us": "Yes",
    "needs_sponsorship_now_or_future": "No",
    "sponsorship_required": "No",
    "security_clearance_active": "No",
    "security_clearance": "No",
    "polygraph_active": "No",
    "polygraph": "No",
    "background_check_ok": "Yes",
    "drug_screen_ok": "Yes",
    "phone": "4104566443",
    "primary_phone_number": "4104566443",
    "email": "zkralec@icloud.com",
    "email_address": "zkralec@icloud.com",
    "city": "Monkton",
    "address_line_1": "3233 Vance Rd",
    "state_full": "Maryland",
    "state_abbrev": "MD",
    "state": "MD",
    "state_or_province": "MD",
    "postal_code": "21111",
    "zip": "21111",
    "country": "United States",
    "phone_type": "Mobile",
    "relocation_ok": "Yes",
    "relocation": "Yes",
    "work_preference": "Any",
    "default_work_preference_priority": ["onsite", "hybrid", "remote"],
    "travel_ok": "Yes",
    "travel": "100%",
    "travel_amount_default": "100%",
    "employment_type_default": "Full time",
    "employment_type": "Full time",
    "employment_type_fallback": "Permanent",
    "worked_here_before": "No",
    "worked_with_company_recruiter_before": "No",
    "referred_by_employee": "No",
    "referred": "No",
    "know_anyone_at_company": "No",
    "interviewed_here_before": "No",
    "employed_by_company_or_affiliate_before": "No",
    "company_affiliation_prior": "No",
    "hear_about_us_default": "LinkedIn",
    "hear_about_us": "LinkedIn",
    "accommodation_capability": "Yes",
    "can_perform_essential_functions_with_or_without_accommodation": "Yes",
    "desired_salary": "100000",
    "available_start_date": "05/18/2026",
    "earliest_start_date": "05/18/2026",
    "veteran_status": "Not a veteran",
    "gender": "Male",
    "disability_status": "No disability",
    "ethnicity_race": "White (not Hispanic)",
    "ethnicity": "White (not Hispanic)",
    "race": "White",
    "self_id_mode": "review",
    "optional_demographic_behavior": "auto_when_possible",
    "text_message_opt_in": "Yes",
    "reason_seeking_new_role": "Wanting to advance my career and handle more complex work and responsibility",
    "anything_else_to_know": "None",
    "auto_submit_allowed": True,
    "auto_submit_min_confidence": DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE,
}

CANONICAL_KEY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("first_name", ("first name", "given name")),
    ("last_name", ("last name", "family name", "surname")),
    ("email", ("email address", "email")),
    ("address_line_1", ("address line 1", "street address", "mailing address", "address")),
    ("city", ("city",)),
    ("state_or_province", ("state or province", "state", "province", "region")),
    ("postal_code", ("zip postal code", "zip code", "postal code", "postcode", "zip")),
    ("country", ("country",)),
    ("primary_phone_number", ("primary phone number", "phone number", "phone")),
    ("phone_type", ("phone type", "type")),
    (
        "work_authorized_us",
        (
            "authorized to work",
            "legally authorized",
            "work in the united states",
            "work in the us",
            "authorized to work in the united states without sponsorship",
            "authorized to work in the us without sponsorship",
        ),
    ),
    (
        "sponsorship_required",
        (
            "require sponsorship",
            "need sponsorship",
            "visa sponsorship",
            "future sponsorship",
            "now or in the future require sponsorship",
        ),
    ),
    ("security_clearance", ("security clearance", "government issued security clearance", "active clearance", "clearance active")),
    ("polygraph", ("polygraph", "active polygraph")),
    ("background_check_ok", ("background check",)),
    ("drug_screen_ok", ("drug screen", "drug test")),
    ("worked_here_before", ("worked here before", "ever worked here before", "worked for this company before")),
    (
        "worked_with_company_recruiter_before",
        ("worked with company recruiter before", "recruiter before", "worked with a recruiter from", "contacted by our recruiter"),
    ),
    ("referred", ("referred by employee", "referred", "know anyone at company", "know anyone here", "referred to this role")),
    ("interviewed_here_before", ("interviewed here before", "interviewed with this company", "previously interviewed")),
    (
        "company_affiliation_prior",
        (
            "company or affiliate",
            "employed by this company",
            "worked for this company",
            "affiliate before",
            "subsidiary before",
        ),
    ),
    ("hear_about_us", ("hear about us", "how did you hear", "source of referral", "how did you hear about us")),
    ("relocation", ("willing to relocate", "relocation")),
    ("travel", ("travel", "willing to travel")),
    ("employment_type", ("employment type", "full time", "full-time", "type of employment")),
    ("desired_salary", ("desired salary", "salary expectation", "compensation expectation", "salary requirements", "annual salary")),
    ("available_start_date", ("available start date", "earliest start date", "start date", "when can you start")),
    ("accommodation_capability", ("essential functions", "accommodation")),
    ("text_message_opt_in", ("text message", "sms", "mobile alerts", "text updates")),
    ("veteran_status", ("veteran",)),
    ("gender", ("gender",)),
    ("disability_status", ("disability",)),
    ("ethnicity_race", ("ethnicity", "race", "hispanic")),
    (
        "reason_for_interest",
        (
            "why are you interested",
            "why do you want",
            "why this role",
            "why this company",
            "why are you applying",
            "motivation",
            "why should we hire you",
        ),
    ),
    ("reason_seeking_new_role", ("why are you looking", "why seeking a new role", "reason for leaving", "reason for change")),
    ("anything_else_to_know", ("anything else", "additional information", "anything more to share")),
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", _text(value).lower())).strip()


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _text(value).lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_default_answer_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = dict(DEFAULT_ANSWER_PROFILE)
    target = payload.get("application_target") if isinstance(payload.get("application_target"), dict) else {}
    explicit_profile = payload.get("default_answer_profile") if isinstance(payload.get("default_answer_profile"), dict) else {}
    candidate_profile = payload.get("candidate_profile") if isinstance(payload.get("candidate_profile"), dict) else {}
    contact_profile = payload.get("contact_profile") if isinstance(payload.get("contact_profile"), dict) else {}
    nested_default = candidate_profile.get("default_answer_profile") if isinstance(candidate_profile.get("default_answer_profile"), dict) else {}

    for source in (nested_default, explicit_profile, contact_profile, candidate_profile.get("contact_profile"), candidate_profile):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if value is None:
                continue
            profile[str(key)] = value

    first_name = _text(profile.get("first_name") or contact_profile.get("first_name") or candidate_profile.get("first_name"))
    last_name = _text(profile.get("last_name") or contact_profile.get("last_name") or candidate_profile.get("last_name"))
    if not first_name or not last_name:
        resume_variant = payload.get("resume_variant") if isinstance(payload.get("resume_variant"), dict) else {}
        resume_text = _text(resume_variant.get("resume_variant_text"))
        for line in resume_text.splitlines():
            stripped = _text(line)
            if not stripped or "@" in stripped:
                continue
            parts = [part for part in stripped.split() if part]
            if len(parts) >= 2:
                first_name = first_name or parts[0]
                last_name = last_name or parts[-1]
                break
    if first_name:
        profile["first_name"] = first_name
    if last_name:
        profile["last_name"] = last_name

    email = _text(profile.get("email") or profile.get("email_address"))
    if email:
        profile["email"] = email
        profile["email_address"] = email

    phone = _text(profile.get("primary_phone_number") or profile.get("phone"))
    if phone:
        profile["phone"] = phone
        profile["primary_phone_number"] = phone

    state_abbrev = _text(profile.get("state_abbrev") or profile.get("state") or profile.get("state_or_province"))
    if state_abbrev:
        profile["state"] = state_abbrev
        profile["state_or_province"] = state_abbrev
    postal_code = _text(profile.get("postal_code") or profile.get("zip"))
    if postal_code:
        profile["postal_code"] = postal_code
        profile["zip"] = postal_code

    profile["auto_submit_allowed"] = _as_bool(profile.get("auto_submit_allowed"), default=True)
    profile["auto_submit_min_confidence"] = _as_float(
        profile.get("auto_submit_min_confidence"),
        default=DEFAULT_AUTO_SUBMIT_MIN_CONFIDENCE,
    )
    if target:
        profile["company_name"] = _text(target.get("company"))
        profile["job_title"] = _text(target.get("title"))
    return profile


def normalize_canonical_key(label: str, *, context_text: str | None = None) -> dict[str, Any] | None:
    combined = _normalized_text(" ".join(part for part in (_text(context_text), _text(label)) if _text(part)))
    if not combined:
        return None
    for canonical_key, phrases in CANONICAL_KEY_PATTERNS:
        for phrase in phrases:
            normalized_phrase = _normalized_text(phrase)
            if normalized_phrase and re.search(rf"(?:^| ){re.escape(normalized_phrase)}(?: |$)", combined):
                return {
                    "canonical_key": canonical_key,
                    "confidence": 0.95,
                    "matched_phrase": phrase,
                    "normalized_label": combined,
                }
    return None


def is_self_id_key(canonical_key: str) -> bool:
    return canonical_key in SELF_ID_KEYS


def answer_value_for_canonical(profile: dict[str, Any], canonical_key: str) -> str | None:
    aliases = {
        "email": ("email", "email_address"),
        "address_line_1": ("address_line_1",),
        "state_or_province": ("state_or_province", "state", "state_abbrev", "state_full"),
        "postal_code": ("postal_code", "zip"),
        "primary_phone_number": ("primary_phone_number", "phone"),
        "work_authorized_us": ("work_authorized_us",),
        "sponsorship_required": ("sponsorship_required", "needs_sponsorship_now_or_future"),
        "security_clearance": ("security_clearance", "security_clearance_active"),
        "polygraph": ("polygraph", "polygraph_active"),
        "relocation": ("relocation", "relocation_ok"),
        "hear_about_us": ("hear_about_us", "hear_about_us_default"),
        "available_start_date": ("available_start_date", "earliest_start_date"),
        "employment_type": ("employment_type", "employment_type_default"),
        "travel": ("travel", "travel_amount_default"),
        "accommodation_capability": ("accommodation_capability", "can_perform_essential_functions_with_or_without_accommodation"),
        "referred": ("referred", "referred_by_employee", "know_anyone_at_company"),
        "company_affiliation_prior": ("company_affiliation_prior", "employed_by_company_or_affiliate_before", "worked_here_before"),
        "worked_here_before": ("worked_here_before", "company_affiliation_prior", "employed_by_company_or_affiliate_before"),
        "worked_with_company_recruiter_before": ("worked_with_company_recruiter_before",),
        "interviewed_here_before": ("interviewed_here_before",),
        "background_check_ok": ("background_check_ok",),
        "drug_screen_ok": ("drug_screen_ok",),
        "text_message_opt_in": ("text_message_opt_in",),
        "ethnicity": ("ethnicity", "ethnicity_race"),
        "race": ("race", "ethnicity_race"),
    }
    for key in aliases.get(canonical_key, (canonical_key,)):
        value = _text(profile.get(key))
        if value:
            return value
    return None


def resolve_default_answer(
    *,
    profile: dict[str, Any],
    canonical_key: str,
    required: bool,
    field_label: str,
    field_type: str | None,
) -> dict[str, Any]:
    value = answer_value_for_canonical(profile, canonical_key)
    if value:
        return {
            "action": "answer",
            "canonical_key": canonical_key,
            "value": value,
            "source": "default_profile",
            "confidence": 0.95,
            "required": required,
            "self_id_handling_mode": "direct_default" if is_self_id_key(canonical_key) else "standard",
        }
    if is_self_id_key(canonical_key) and not required:
        return {
            "action": "skip",
            "canonical_key": canonical_key,
            "value": None,
            "source": "self_id_optional_skip",
            "confidence": 0.7,
            "required": required,
            "self_id_handling_mode": "skip_optional",
        }
    return {
        "action": "review",
        "canonical_key": canonical_key,
        "value": None,
        "source": "missing_default",
        "confidence": 0.0,
        "required": required,
        "self_id_handling_mode": "review" if is_self_id_key(canonical_key) else "standard",
        "reason": f"no_safe_default_for:{canonical_key}",
    }


def motivation_answer(
    *,
    profile: dict[str, Any],
    application_target: dict[str, Any],
    question_text: str,
) -> dict[str, Any]:
    company = _text(application_target.get("company")) or "the company"
    title = _text(application_target.get("title")) or "this role"
    role_reason = _text(profile.get("reason_seeking_new_role")) or _text(DEFAULT_ANSWER_PROFILE.get("reason_seeking_new_role"))
    deterministic = (
        f"I am interested in the {title} opportunity at {company} because it aligns with my background in software, "
        f"AI, and automation, and I am looking for a role with more complex responsibility and growth."
    )
    if os.getenv("USE_LLM", "false").strip().lower() != "true":
        return {
            "answer": deterministic,
            "source": "deterministic_fallback",
            "confidence": FALLBACK_MOTIVATION_CONFIDENCE,
            "reason": "llm_disabled",
        }

    try:
        if str(WORKER_ROOT) not in sys.path:
            sys.path.insert(0, str(WORKER_ROOT))
        from llm.openai_adapter import run_chat_completion  # type: ignore
        from models.catalog import tier_model  # type: ignore

        prompt = {
            "task": "Draft a short truthful job-application motivation answer.",
            "question_text": question_text,
            "company": company,
            "job_title": title,
            "candidate_context": {
                "reason_seeking_new_role": role_reason,
                "city": _text(profile.get("city")),
                "work_authorized_us": _text(profile.get("work_authorized_us")),
                "core_summary": "Background in software, AI, and automation tools.",
            },
            "rules": [
                "Keep it to 2-3 sentences.",
                "Be specific to the company and role if possible.",
                "Do not invent credentials or company facts not provided.",
            ],
        }
        result = run_chat_completion(
            model=tier_model("standard"),
            messages=[
                {"role": "system", "content": "You write concise, truthful job-application motivation answers."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
            ],
            temperature=0.2,
            max_completion_tokens=220,
            agent_name="openclaw_apply_motivation",
        )
        answer = _text(result.get("output_text"))
        if not answer:
            raise ValueError("empty_motivation_answer")
        return {
            "answer": answer,
            "source": "llm_generated",
            "confidence": MOTIVATION_CONFIDENCE,
            "reason": "llm_generated",
        }
    except Exception as exc:
        return {
            "answer": deterministic,
            "source": "deterministic_fallback",
            "confidence": FALLBACK_MOTIVATION_CONFIDENCE,
            "reason": f"llm_fallback:{type(exc).__name__}",
        }
