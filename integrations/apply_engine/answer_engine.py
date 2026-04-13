"""
Answer engine: resolve form field values.

Priority order:
  1. Deterministic structured field mapping
  2. Deterministic long-form template mapping
  3. Guarded fuzzy matching within long-form templates only
  4. Optional OpenAI fallback for long-form prompts only

Returns AnswerResult with value + confidence + source so callers can
decide whether to trust the answer or skip.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .observability import get_logger
from .profile import ApplicantProfile

_log = get_logger("answer_engine")


class AnswerSource(str, Enum):
    CANONICAL_KEY = "canonical_key"
    FUZZY_LABEL = "fuzzy_label"
    TEMPLATE = "template"
    LLM_GENERATED = "llm_generated"
    LLM_CACHE = "llm_cache"
    NOT_FOUND = "not_found"


@dataclass
class FormQuestion:
    label: str                     # Visible label text
    field_type: str                # text | select | radio | checkbox | file | textarea
    options: list[str] = field(default_factory=list)   # For select/radio
    required: bool = False
    placeholder: str = ""
    name_attr: str = ""            # HTML name attribute
    id_attr: str = ""              # HTML id attribute
    context_text: str = ""         # Nearby text for additional context
    site: str = ""                 # Which adapter is active
    company_name: str = ""
    role_title: str = ""
    role_family: str = ""


@dataclass
class AnswerResult:
    value: Any                     # The answer value
    confidence: float              # 0.0 – 1.0
    source: AnswerSource
    canonical_key: str | None = None
    reasoning: str | None = None

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.70

    @property
    def found(self) -> bool:
        return self.source != AnswerSource.NOT_FOUND and self.value is not None


CANONICAL_LABEL_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("full_name", ("full name", "your full name", "legal name")),
    ("first_name", ("first name", "given name", "first")),
    ("last_name", ("last name", "family name", "surname", "last")),
    ("email", ("email address", "email")),
    ("primary_phone_number", ("primary phone number", "phone number", "mobile number", "phone")),
    ("phone_type", ("phone type",)),
    ("current_location", ("current location", "location", "where are you located", "current city")),
    ("current_company", ("current company", "current employer", "employer")),
    ("address_line_1", ("address line 1", "street address", "mailing address", "address")),
    ("city", ("city",)),
    ("state_or_province", ("state or province", "state", "province", "region")),
    ("postal_code", ("zip postal code", "zip code", "postal code", "postcode", "zip")),
    ("country", ("country",)),
    ("linkedin_url", ("linkedin", "linkedin profile", "linkedin url")),
    ("twitter_url", ("twitter", "twitter profile", "twitter url", "x profile", "x url")),
    ("github_url", ("github", "github profile", "github url")),
    ("portfolio_url", ("portfolio", "portfolio url", "personal website", "website", "website url")),
    ("greater_boston_area", (
        "greater boston area",
        "greater boston",
        "boston area",
        "currently reside in the greater boston area",
    )),
    ("onsite_boston_4_days", (
        "4 days per week",
        "working environment you are seeking",
        "work environment you are seeking",
        "onsite boston 4 days",
        "on-site boston 4 days",
        "boston working environment",
    )),
    ("work_authorized_us", (
        "authorized to work in the united states without sponsorship",
        "authorized to work in the us without sponsorship",
        "legally authorized to work",
        "authorized to work",
        "work in the united states",
        "work in the us",
    )),
    ("needs_sponsorship_now_or_future", (
        "now or in the future require sponsorship",
        "require sponsorship",
        "need sponsorship",
        "visa sponsorship",
        "future sponsorship",
        "require work authorization sponsorship",
    )),
    ("security_clearance", ("security clearance", "active clearance", "government clearance", "clearance")),
    ("polygraph", ("polygraph",)),
    ("background_check", ("background check",)),
    ("drug_screen", ("drug screen", "drug test")),
    ("worked_here_before", ("worked here before", "worked for this company", "previously worked here")),
    ("referred", ("referred by employee", "know anyone at company", "know anyone here", "referred to this role")),
    ("interviewed_here_before", ("interviewed here before", "previously interviewed", "interviewed with this company")),
    ("hear_about_us", ("how did you hear about us", "how did you hear", "hear about us", "source of referral")),
    ("relocation", ("willing to relocate", "open to relocation", "relocation")),
    ("travel", ("willing to travel", "travel", "travel requirements")),
    ("employment_type", ("employment type", "type of employment", "full-time or part-time")),
    ("desired_salary", (
        "desired salary", "salary expectation", "compensation expectation",
        "salary requirements", "expected salary", "annual salary",
        "minimum salary", "target salary",
    )),
    ("available_start_date", ("available start date", "earliest start date", "start date", "when can you start")),
    ("accommodation_capability", ("essential functions", "reasonable accommodation", "accommodation")),
    ("text_message_opt_in", ("text message", "sms opt", "mobile alerts")),
    ("veteran_status", ("veteran status", "are you a veteran", "veteran")),
    ("gender", ("gender",)),
    ("disability_status", ("disability status", "disability", "disability status voluntary self identification")),
    ("ethnicity_race", ("ethnicity", "race", "race/ethnicity", "hispanic or latino")),
    ("years_of_experience", (
        "years of experience", "how many years", "years experience",
        "how long have you", "total experience",
    )),
    ("current_title", ("current job title", "current title", "current position", "most recent title")),
    ("highest_education", ("highest level of education", "highest education", "degree")),
]


LONG_FORM_TEMPLATE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("professional_summary", (
        "tell us about yourself",
        "professional summary",
        "personal summary",
        "introduction",
        "introduce yourself",
        "cover letter",
        "summary",
    )),
    ("why_this_role", (
        "why are you interested in this role",
        "why this role",
        "why are you interested",
        "why do you want this role",
        "why are you a fit",
        "why should we hire you",
    )),
    ("why_this_company", (
        "why do you want to work here",
        "why this company",
        "why us",
        "why do you want to join",
        "why our company",
    )),
    ("ai_project_example", (
        "something cool you've done with ai",
        "something cool you have done with ai",
        "project with ai",
        "artificial intelligence project",
        "llm project",
        "machine learning project",
    )),
    ("automation_project_example", (
        "automation project",
        "process automation",
        "workflow automation",
        "system you automated",
        "operational improvement",
    )),
    ("interesting_dataset", (
        "interesting data set",
        "interesting dataset",
        "data set you've worked with",
        "dataset you've worked with",
    )),
    ("first_day_question", (
        "first question you would ask on your first day",
        "first day question",
        "your first day",
    )),
    ("work_authorization_answer", (
        "work authorization",
        "authorized to work",
        "employment authorization",
    )),
    ("sponsorship_answer", (
        "require sponsorship",
        "visa sponsorship",
        "need sponsorship",
    )),
    ("relocation_answer", (
        "open to relocate",
        "willing to relocate",
        "relocation",
    )),
    ("onsite_answer", (
        "on site",
        "onsite",
        "work in office",
        "hybrid or onsite",
    )),
    ("boston_area_answer", (
        "boston area",
        "greater boston",
        "boston metro",
    )),
    ("current_company", ("current company", "current employer")),
    ("current_location", ("current location", "where are you located")),
]


class AnswerEngine:
    """
    Resolves form field answers from the applicant profile with optional OpenAI fallback.
    """

    def __init__(
        self,
        profile: ApplicantProfile,
        llm_client: Any = None,
        llm_model: str = "gpt-5-mini",
        enable_llm: bool = True,
        llm_call_budget: int = 10,
        llm_max_tokens: int = 1500,
    ) -> None:
        self._profile = profile
        self._llm = llm_client
        self._llm_model = llm_model
        self._enable_llm = enable_llm and llm_client is not None
        self._llm_budget = llm_call_budget
        self._llm_max_tokens = llm_max_tokens
        self._llm_calls = 0
        self._llm_cache: dict[str, Any] = {}

    @property
    def llm_calls_used(self) -> int:
        return self._llm_calls

    def answer(self, question: FormQuestion) -> AnswerResult:
        candidate_labels = _candidate_labels(question)

        if _is_long_form_question(question):
            result = self._try_exact_template_answer(question, candidate_labels)
            if result:
                _log.debug(
                    f"answered via template | label={question.label} "
                    f"key={result.canonical_key} value={_safe_preview(result.value)}"
                )
                return result

            if self._enable_llm and self._llm_calls < self._llm_budget:
                result = self._try_openai_generated_answer(question, candidate_labels)
                if result:
                    if result.source == AnswerSource.LLM_CACHE:
                        _log.debug(
                            f"answered via llm_cache | label={question.label} "
                            f"value={_safe_preview(result.value)}"
                        )
                    else:
                        _log.debug(
                            f"answered via llm_generated | label={question.label} "
                            f"value={_safe_preview(result.value)}"
                        )
                    return result

            _log.debug(f"no long-form answer found | label={question.label}")
            return AnswerResult(value=None, confidence=0.0, source=AnswerSource.NOT_FOUND)

        result = self._try_canonical_exact(candidate_labels, question)
        if result:
            _log.debug(
                f"answered via canonical_key | label={question.label} "
                f"key={result.canonical_key} value={_safe_preview(result.value)}"
            )
            return result

        result = self._try_fuzzy_label(candidate_labels, question)
        if result:
            _log.debug(
                f"answered via fuzzy_label | label={question.label} "
                f"key={result.canonical_key} value={_safe_preview(result.value)}"
            )
            return result

        result = self._try_attribute_match(question)
        if result:
            _log.debug(
                f"answered via attribute_match | label={question.label} "
                f"value={_safe_preview(result.value)}"
            )
            return result

        _log.debug(f"no answer found | label={question.label} field_type={question.field_type}")
        return AnswerResult(value=None, confidence=0.0, source=AnswerSource.NOT_FOUND)

    def _try_canonical_exact(self, candidate_labels: list[str], question: FormQuestion) -> AnswerResult | None:
        for label_norm in candidate_labels:
            if not label_norm:
                continue
            for canonical_key, patterns in CANONICAL_LABEL_PATTERNS:
                for pattern in patterns:
                    if pattern == label_norm or label_norm.startswith(pattern) or pattern in label_norm:
                        value = self._profile.get(canonical_key, site=question.site or None)
                        if not _has_usable_value(value):
                            continue
                        value = self._coerce_value(value, question)
                        if value is None:
                            continue
                        return AnswerResult(
                            value=value,
                            confidence=0.95,
                            source=AnswerSource.CANONICAL_KEY,
                            canonical_key=canonical_key,
                        )
        return None

    def _try_fuzzy_label(self, candidate_labels: list[str], question: FormQuestion) -> AnswerResult | None:
        best_score = 0.0
        best_key = None

        for label_norm in candidate_labels:
            if not label_norm:
                continue
            for canonical_key, patterns in CANONICAL_LABEL_PATTERNS:
                for pattern in patterns:
                    if not _fuzzy_key_allowed(label_norm, canonical_key):
                        continue
                    score = _fuzzy_score(label_norm, pattern)
                    if score > best_score:
                        best_score = score
                        best_key = canonical_key

        if best_key and best_score >= 0.65:
            value = self._profile.get(best_key, site=question.site or None)
            if _has_usable_value(value):
                value = self._coerce_value(value, question)
                if value is not None:
                    return AnswerResult(
                        value=value,
                        confidence=best_score * 0.9,
                        source=AnswerSource.FUZZY_LABEL,
                        canonical_key=best_key,
                    )
        return None

    def _try_attribute_match(self, question: FormQuestion) -> AnswerResult | None:
        candidates: list[str] = []
        for attr in (question.name_attr, question.id_attr):
            if not attr:
                continue
            candidates.append(_normalize(attr.replace("_", " ").replace("-", " ")))

        result = self._try_canonical_exact(candidates, question)
        if result:
            return AnswerResult(
                value=result.value,
                confidence=result.confidence * 0.85,
                source=AnswerSource.FUZZY_LABEL,
                canonical_key=result.canonical_key,
            )

        result = self._try_fuzzy_label(candidates, question)
        if result:
            return AnswerResult(
                value=result.value,
                confidence=result.confidence * 0.85,
                source=AnswerSource.FUZZY_LABEL,
                canonical_key=result.canonical_key,
            )
        return None

    def _try_exact_template_answer(
        self,
        question: FormQuestion,
        candidate_labels: list[str],
    ) -> AnswerResult | None:
        template_key, confidence = _select_exact_template_key(candidate_labels)
        if not template_key:
            return None

        if template_key in {"current_company", "current_location"}:
            value = (
                self._profile.render_template(template_key, variables=_template_variables(question), site=question.site or None)
                or self._profile.get_str(template_key, site=question.site or None)
            )
        else:
            value = self._profile.render_template(
                template_key,
                variables=_template_variables(question),
                site=question.site or None,
            )
        if not value:
            return None

        return AnswerResult(
            value=value,
            confidence=confidence,
            source=AnswerSource.TEMPLATE,
            canonical_key=template_key,
        )

    def _try_openai_generated_answer(
        self,
        question: FormQuestion,
        candidate_labels: list[str],
    ) -> AnswerResult | None:
        cache_key = _long_form_cache_key(question)
        if cache_key in self._llm_cache:
            cached = self._llm_cache[cache_key]
            return AnswerResult(
                value=cached.get("answer"),
                confidence=float(cached.get("confidence", 0.5)),
                source=AnswerSource.LLM_CACHE,
                reasoning=cached.get("reasoning"),
            )

        self._llm_calls += 1
        _log.info(
            f"openai fallback | label={question.label} field_type={question.field_type} "
            f"call_number={self._llm_calls}"
        )

        prompt = _build_openai_prompt(
            question,
            self._build_long_form_context(question, candidate_labels),
        )

        try:
            request_kwargs: dict[str, Any] = {
                "model": self._llm_model,
                "max_completion_tokens": self._llm_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if not self._llm_model.startswith("gpt-5"):
                request_kwargs["temperature"] = 0.2
            response = self._llm.chat.completions.create(**request_kwargs)
            # Direct extraction matching the working smoke-test pattern:
            #   response.choices[0].message.content → str or None
            # gpt-5* models are reasoning models; content is always a plain string
            # in the new SDK — no dict or content-block list variants needed here.
            content = response.choices[0].message.content
            raw = content.strip() if isinstance(content, str) else ""
            _log.debug(
                f"openai response | label={question.label} "
                f"finish_reason={response.choices[0].finish_reason} "
                f"extracted_repr={repr(raw)[:120]} length={len(raw)}"
            )
            parsed = _parse_long_form_response(raw)
            if not parsed:
                _log.debug(
                    f"openai response rejected | label={question.label} "
                    f"reason=parse_failed extracted_length={len(raw)}"
                )
                return None

            value = str(parsed.get("answer") or "").strip()
            if not value:
                _log.debug(
                    f"openai response rejected | label={question.label} "
                    f"reason=empty_answer extracted_length={len(raw)}"
                )
                return None

            _log.debug(
                f"openai parsed answer | label={question.label} "
                f"preview={_safe_preview(value, 240)} confidence={parsed.get('confidence', 0.5)}"
            )
            self._llm_cache[cache_key] = parsed
            return AnswerResult(
                value=value,
                confidence=float(parsed.get("confidence", 0.5)),
                source=AnswerSource.LLM_GENERATED,
                reasoning=parsed.get("reasoning"),
            )
        except Exception as exc:
            _log.warning(f"openai fallback failed | label={question.label} error={exc}")
            return None

    def generate_long_form_answer(
        self,
        question: str,
        company_name: str | None = None,
        role_title: str | None = None,
        role_family: str | None = None,
    ) -> str | None:
        form_question = FormQuestion(
            label=question,
            field_type="textarea",
            company_name=company_name or "",
            role_title=role_title or "",
            role_family=role_family or "",
        )
        result = self.answer(form_question)
        return str(result.value) if result.found else None

    def _coerce_value(self, value: Any, question: FormQuestion) -> Any:
        if value is None:
            return None

        if question.field_type in {"select", "radio"} and question.options:
            return _best_option_match(str(value), question.options)

        if question.field_type == "checkbox":
            if isinstance(value, bool):
                return value
            return str(value).lower() in {"yes", "true", "1", "y"}

        return value

    def _build_long_form_context(self, question: FormQuestion, candidate_labels: list[str]) -> str:
        p = self._profile
        supporting_keys = _supporting_template_keys(question, candidate_labels)
        lines = [
            f"Professional summary: {p.get_template('professional_summary') or ''}",
            f"Headline: {p.get_template('narrative_headline') or ''}",
            f"Target roles: {p.get_template('target_roles') or ''}",
            f"Superpowers: {p.get_template('superpowers') or ''}",
            f"Exit story: {p.get_template('exit_story') or ''}",
            f"Current company: {p.get_str('current_company') or ''}",
            f"Current title: {p.get_str('current_title') or ''}",
            f"Current location: {p.get_str('current_location') or ''}",
            f"Work preference: {p.get_str('work_preference') or ''}",
            f"Relocation: {p.get_str('relocation') or ''}",
            f"Work authorization: {p.get_template('work_authorization_answer') or ''}",
            f"Sponsorship: {p.get_template('sponsorship_answer') or ''}",
        ]
        for key in supporting_keys:
            value = p.get_template(key)
            if value:
                lines.append(f"{key}: {value}")
        return "\n".join(line for line in lines if line.split(":", 1)[1].strip())


def _normalize(text: str) -> str:
    text = re.sub(r"[*?()\[\]:]", "", (text or "").lower())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _candidate_labels(question: FormQuestion) -> list[str]:
    candidates: list[str] = []
    for raw in (question.label, question.context_text, question.placeholder):
        normalized = _normalize(raw)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _is_long_form_question(question: FormQuestion) -> bool:
    if question.field_type == "textarea":
        return True
    if question.field_type != "text":
        return False
    combined = " ".join(_candidate_labels(question))
    return any(
        phrase in combined
        for phrase in (
            "tell us",
            "describe",
            "why",
            "share",
            "explain",
            "first day",
            "cover letter",
            "project",
        )
    )


def _select_exact_template_key(candidate_labels: list[str]) -> tuple[str | None, float]:
    for label_norm in candidate_labels:
        for template_key, patterns in LONG_FORM_TEMPLATE_PATTERNS:
            for pattern in patterns:
                if pattern == label_norm or label_norm.startswith(pattern):
                    return template_key, 0.97
    return None, 0.0


def _supporting_template_keys(question: FormQuestion, candidate_labels: list[str]) -> list[str]:
    keys: list[str] = []
    for default_key in (
        "mission_control_project",
        "automation_project_example",
        "ai_project_example",
        "interesting_dataset",
        "why_this_role",
        "why_this_company",
    ):
        if default_key not in keys:
            keys.append(default_key)

    exact_key, _ = _select_exact_template_key(candidate_labels)
    if exact_key and exact_key not in keys:
        keys.insert(0, exact_key)

    if _looks_like_project_prompt(candidate_labels):
        project_key = _choose_project_template(question, candidate_labels)
        if project_key not in keys:
            keys.insert(0, project_key)

    return keys[:6]


def _looks_like_project_prompt(candidate_labels: list[str]) -> bool:
    combined = " ".join(candidate_labels)
    return any(
        phrase in combined
        for phrase in (
            "describe a project",
            "relevant project",
            "project relevant to this role",
            "project you're proud of",
            "project you are proud of",
            "tell us about a project",
        )
    )


def _choose_project_template(question: FormQuestion, candidate_labels: list[str]) -> str:
    combined = " ".join(candidate_labels + [
        _normalize(question.role_family),
        _normalize(question.role_title),
    ])

    if any(term in combined for term in ("ai", "llm", "machine learning", "ml", "data science")):
        return "ai_project_example"
    if any(term in combined for term in ("automation", "workflow", "operations", "platform", "infrastructure")):
        return "automation_project_example"
    if any(term in combined for term in ("mobile", "consumer app", "ios", "product")):
        return "sprint_start_pro_project"
    if any(term in combined for term in ("misinformation", "news", "classification", "dataset")):
        return "ai_fake_news_detector_project"
    return "mission_control_project"


def _template_variables(question: FormQuestion) -> dict[str, str]:
    return {
        "company_name": question.company_name,
        "role_title": question.role_title,
        "role_family": question.role_family,
    }


def _fuzzy_score(label: str, pattern: str) -> float:
    label_words = set(label.split())
    pattern_words = set(pattern.split())
    if not pattern_words:
        return 0.0
    overlap = len(label_words & pattern_words)
    return overlap / len(pattern_words)


def _has_usable_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _fuzzy_key_allowed(label: str, canonical_key: str) -> bool:
    location_terms = (
        "boston", "reside", "onsite", "on-site", "4 days per week",
        "working environment", "work environment", "environment you are seeking",
        "location", "greater boston",
    )
    demographic_keys = {"veteran_status", "gender", "disability_status", "ethnicity_race"}
    if any(term in label for term in location_terms) and canonical_key in demographic_keys:
        return False
    return True


def _best_option_match(value: str, options: list[str]) -> str | None:
    value_norm = _normalize(value)

    for opt in options:
        if _normalize(opt) == value_norm:
            return opt

    for opt in options:
        opt_norm = _normalize(opt)
        if opt_norm.startswith(value_norm) or value_norm.startswith(opt_norm):
            return opt

    for opt in options:
        opt_norm = _normalize(opt)
        if value_norm in opt_norm or opt_norm in value_norm:
            return opt

    if value_norm in {"yes", "true", "1", "y"}:
        for opt in options:
            if _normalize(opt) in {"yes", "true"}:
                return opt
    if value_norm in {"no", "false", "0", "n"}:
        for opt in options:
            if _normalize(opt) in {"no", "false"}:
                return opt

    return None


def _build_openai_prompt(question: FormQuestion, profile_context: str) -> str:
    lines = [
        "You are filling out a job application form.",
        "Write a short professional answer using the provided applicant baseline.",
        "Stay truthful, specific, and concise. Prefer 2-4 sentences.",
        "Use an early-career tone.",
        "Do not invent experience, employers, projects, education, or locations.",
        "If the profile does not support a claim, do not make it.",
        "Reply with plain prose only. No JSON, no markdown, no headers.",
        "",
        f"Field label: {question.label}",
        f"Field type: {question.field_type}",
    ]
    if question.placeholder:
        lines.append(f"Placeholder: {question.placeholder}")
    if question.context_text:
        lines.append(f"Nearby context: {question.context_text[:200]}")
    if question.company_name:
        lines.append(f"Company name: {question.company_name}")
    if question.role_title:
        lines.append(f"Role title: {question.role_title}")
    if question.role_family:
        lines.append(f"Role family: {question.role_family}")
    lines += [
        "",
        "Applicant baseline:",
        profile_context,
        "",
        "Write 2 to 5 sentences unless the question clearly calls for a shorter answer.",
    ]
    return "\n".join(lines)


def generate_long_form_answer(
    question: str,
    profile: ApplicantProfile,
    company_name: str | None = None,
    role_title: str | None = None,
    role_family: str | None = None,
    openai_client: Any = None,
    enable_llm: bool = False,
    llm_model: str = "gpt-5-mini",
    llm_max_tokens: int = 1500,
) -> str | None:
    engine = AnswerEngine(
        profile=profile,
        llm_client=openai_client,
        llm_model=llm_model,
        enable_llm=enable_llm,
        llm_max_tokens=llm_max_tokens,
    )
    return engine.generate_long_form_answer(
        question=question,
        company_name=company_name,
        role_title=role_title,
        role_family=role_family,
    )


def _long_form_cache_key(question: FormQuestion) -> str:
    label = _normalize(question.label)
    company = _normalize(question.company_name)
    role = _normalize(question.role_title)
    return f"{label}|{company}|{role}"


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _parse_long_form_response(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None

    # Opportunistically extract from JSON if the model happened to return it.
    parsed = _parse_llm_json(raw)
    if parsed and isinstance(parsed, dict):
        answer = str(parsed.get("answer") or parsed.get("value") or "").strip()
        if answer:
            return {
                "answer": answer,
                "confidence": float(parsed.get("confidence", 0.7)),
                "reasoning": parsed.get("reasoning"),
            }

    # Primary path: plain prose is the expected format.
    return {
        "answer": raw,
        "confidence": 0.7,
        "reasoning": "plain_text",
    }


def _safe_preview(value: Any, max_len: int = 40) -> str:
    s = str(value or "")
    return s[:max_len] + "..." if len(s) > max_len else s
