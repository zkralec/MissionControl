"""
Applicant profile loader.

Reads a YAML config and provides a clean interface for answer lookups.
Config-driven: no hardcoded defaults here — those live in the YAML file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:
    raise ImportError("PyYAML is required: pip install pyyaml") from e


class ApplicantProfile:
    """
    Loaded from a YAML file. Provides answer lookup by canonical key or
    normalized label string, with optional per-site overrides.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self._flat = self._flatten(data)

    @classmethod
    def load(cls, path: str | Path) -> "ApplicantProfile":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Profile not found: {p}")
        with p.open() as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    def get(self, key: str, site: str | None = None) -> Any:
        if site:
            site_val = self._get_site_override(site, key)
            if site_val is not None:
                return site_val
        return self._flat.get(key)

    def get_template(self, key: str, site: str | None = None) -> str | None:
        if site:
            overrides = self._data.get("site_overrides") or {}
            site_data = overrides.get(site) or {}
            site_templates = site_data.get("templates") or {}
            if isinstance(site_templates, dict):
                value = site_templates.get(key)
                if value is not None:
                    return str(value).strip() or None

        templates = self._data.get("templates") or {}
        if not isinstance(templates, dict):
            return None
        value = templates.get(key)
        if value is None:
            return None
        return str(value).strip() or None

    def render_template(
        self,
        key: str,
        variables: dict[str, Any] | None = None,
        site: str | None = None,
    ) -> str | None:
        template = self.get_template(key, site=site)
        if not template:
            return None

        context: dict[str, Any] = dict(self._flat)
        context.setdefault("full_name", self.full_name)
        context.setdefault("current_location", f"{self.city}, {self.state}".strip(", "))
        if variables:
            context.update(variables)

        rendered = template.format_map(_TemplateVarMap(context))
        rendered = re.sub(r"[ \t]+\n", "\n", rendered)
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)
        return rendered.strip()

    def get_str(self, key: str, site: str | None = None) -> str | None:
        val = self.get(key, site)
        if val is None:
            return None
        return str(val).strip() or None

    def get_bool(self, key: str, site: str | None = None) -> bool | None:
        val = self.get(key, site)
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        s = str(val).lower().strip()
        if s in {"true", "yes", "1", "y"}:
            return True
        if s in {"false", "no", "0", "n"}:
            return False
        return None

    def yes_no(self, key: str, site: str | None = None) -> str | None:
        val = self.get_bool(key, site)
        if val is None:
            return None
        return "Yes" if val else "No"

    @property
    def first_name(self) -> str:
        return self.get_str("first_name") or ""

    @property
    def last_name(self) -> str:
        return self.get_str("last_name") or ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def email(self) -> str:
        return self.get_str("email") or ""

    @property
    def phone(self) -> str:
        return self.get_str("phone") or ""

    @property
    def resume_path(self) -> Path | None:
        raw = (self._data.get("resume") or {}).get("path")
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p if p.exists() else None

    @property
    def linkedin_url(self) -> str | None:
        return (self._data.get("resume") or {}).get("linkedin_url")

    @property
    def desired_salary(self) -> str | None:
        return self.get_str("desired_salary")

    @property
    def city(self) -> str:
        return self.get_str("city") or ""

    @property
    def state(self) -> str:
        return self.get_str("state") or ""

    @property
    def zip_code(self) -> str:
        return self.get_str("postal_code") or ""

    @property
    def country(self) -> str:
        return self.get_str("country") or "United States"

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def _get_site_override(self, site: str, key: str) -> Any:
        overrides = self._data.get("site_overrides") or {}
        site_data = overrides.get(site) or {}
        return site_data.get(key)

    def _flatten(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        for section in ("personal", "work_eligibility", "preferences", "answers", "demographics"):
            section_data = data.get(section)
            if isinstance(section_data, dict):
                result.update(section_data)

        resume = data.get("resume") or {}
        if isinstance(resume, dict):
            for k, v in resume.items():
                if k != "path":
                    result[k] = v

        exp = data.get("experience") or {}
        if isinstance(exp, dict):
            result.update(exp)

        for key in ("highest_education",):
            if key in data and data[key] is not None:
                result[key] = data[key]

        yes_no_keys = {
            "authorized_us", "needs_sponsorship", "relocation",
            "remote", "hybrid", "onsite", "background_check",
            "drug_screen", "polygraph", "security_clearance",
        }
        for key in yes_no_keys:
            if key in result and isinstance(result[key], bool):
                result[key] = "Yes" if result[key] else "No"

        aliases = {
            "authorized_us": "work_authorized_us",
            "needs_sponsorship": "needs_sponsorship_now_or_future",
            "postal_code": "zip",
            "state": "state_or_province",
            "phone": "primary_phone_number",
        }
        for src, dst in aliases.items():
            if src in result and dst not in result:
                result[dst] = result[src]

        if result.get("first_name") and result.get("last_name") and "full_name" not in result:
            result["full_name"] = f"{result['first_name']} {result['last_name']}".strip()

        city = str(result.get("city") or "").strip()
        state = str(result.get("state") or result.get("state_or_province") or "").strip()
        country = str(result.get("country") or "").strip()
        if "current_location" not in result:
            if city and state:
                result["current_location"] = f"{city}, {state}"
            elif city and country:
                result["current_location"] = f"{city}, {country}"
            elif city:
                result["current_location"] = city

        template_current_company = self._template_scalar(data, "current_company")
        if template_current_company and "current_company" not in result:
            result["current_company"] = template_current_company

        template_current_location = self._template_scalar(data, "current_location")
        if template_current_location and "current_location" not in result:
            result["current_location"] = template_current_location

        return result

    def _template_scalar(self, data: dict[str, Any], key: str) -> str | None:
        templates = data.get("templates") or {}
        if not isinstance(templates, dict):
            return None
        value = templates.get(key)
        if value is None:
            return None
        return str(value).strip() or None


class _TemplateVarMap(dict):
    def __missing__(self, key: str) -> str:
        return ""
