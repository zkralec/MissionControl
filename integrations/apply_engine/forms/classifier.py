"""
DOM field classifier.

Given a Playwright page element, determine what type of form field it is,
extract its label, options, and attributes. Returns a structured FormField.

Design: no LLM here. Pure DOM inspection using multiple extraction strategies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..observability import get_logger

_log = get_logger("classifier")


@dataclass
class FormField:
    locator: Any                    # Playwright locator for this element
    field_type: str                 # text | select | radio | checkbox | file | textarea | date | phone | email | number
    label: str = ""                 # Extracted label text
    options: list[str] = field(default_factory=list)   # For select/radio
    required: bool = False
    placeholder: str = ""
    name_attr: str = ""
    id_attr: str = ""
    aria_label: str = ""
    aria_required: bool = False
    selector_path: str = ""        # For logging/debugging
    context_text: str = ""         # Surrounding text for fallback matching
    input_type: str = ""           # Raw HTML input type when available
    accept_attr: str = ""          # File input accept metadata
    max_length: int | None = None  # Length constraint for text-like fields


async def detect_fields(page: Any) -> list[FormField]:
    """
    Scan the current page for form fields and return classified FormField list.
    Looks for: input, textarea, select, and custom ARIA widgets.
    """
    fields: list[FormField] = []
    seen_ids: set[str] = set()

    # Standard inputs (excluding hidden, submit, button, reset, image)
    inputs = await page.query_selector_all(
        "input:not([type='hidden']):not([type='submit']):not([type='button'])"
        ":not([type='reset']):not([type='image'])"
    )
    for el in inputs:
        f = await _classify_input(el, page)
        dedupe_key = ""
        if f:
            dedupe_key = _dedupe_key(f)
        if f and dedupe_key not in seen_ids:
            fields.append(f)
            if dedupe_key:
                seen_ids.add(dedupe_key)

    # Textareas
    textareas = await page.query_selector_all("textarea")
    for el in textareas:
        f = await _classify_textarea(el, page)
        if f and f.id_attr not in seen_ids:
            fields.append(f)
            if f.id_attr:
                seen_ids.add(f.id_attr)

    # Native selects
    selects = await page.query_selector_all("select")
    for el in selects:
        f = await _classify_select(el, page)
        if f and f.id_attr not in seen_ids:
            fields.append(f)
            if f.id_attr:
                seen_ids.add(f.id_attr)

    # Custom select widgets (role="listbox", role="combobox" divs)
    # These are common in LinkedIn and Workday
    custom_selects = await page.query_selector_all(
        "[role='combobox'], [role='listbox']"
    )
    for el in custom_selects:
        f = await _classify_custom_select(el, page)
        if f and f.id_attr not in seen_ids:
            fields.append(f)
            if f.id_attr:
                seen_ids.add(f.id_attr)

    # File uploads
    file_inputs = await page.query_selector_all("input[type='file']")
    for el in file_inputs:
        f = await _classify_file(el, page)
        if f and f.id_attr not in seen_ids:
            fields.append(f)
            if f.id_attr:
                seen_ids.add(f.id_attr)

    _log.debug(f"fields detected | count={len(fields)}")
    return fields


async def _get_label_for(el: Any, page: Any) -> str:
    """
    Try multiple strategies to extract a label for an input element.
    Priority: aria-label > htmlFor label > ancestor label > placeholder > aria-labelledby > nearby text
    """
    el_id = await _attr(el, "id") or ""

    # 1. aria-label attribute
    aria = await _attr(el, "aria-label") or ""
    if aria:
        return _clean_label(aria)

    # 2. <label for="..."> matching
    if el_id:
        try:
            label_el = await page.query_selector(f"label[for='{el_id}']")
            if label_el:
                text = await label_el.inner_text()
                if text.strip():
                    return _clean_label(text)
        except Exception:
            pass

    # 3. Ancestor <label> wrapping the input
    try:
        text = await el.evaluate(
            """el => {
                let p = el.parentElement;
                while (p) {
                    if (p.tagName === 'LABEL') return p.innerText;
                    p = p.parentElement;
                    if (p && p.tagName === 'FORM') break;
                }
                return '';
            }"""
        )
        if text and text.strip():
            return _clean_label(text)
    except Exception:
        pass

    # 4. ancestor fieldset / group heading
    try:
        text = await el.evaluate(
            """el => {
                const group = el.closest('fieldset, .application-question, .question, .field, .form-group');
                if (!group) return '';
                const legend = group.querySelector('legend');
                if (legend && legend.innerText.trim()) return legend.innerText;
                const heading = group.querySelector('h1, h2, h3, h4, .question-label, .application-label, .label');
                if (heading && heading.innerText.trim()) return heading.innerText;
                return '';
            }"""
        )
        if text and text.strip():
            return _clean_label(text)
    except Exception:
        pass

    # 5. placeholder
    placeholder = await _attr(el, "placeholder") or ""
    if placeholder:
        return _clean_label(placeholder)

    # 6. aria-labelledby
    labelledby = await _attr(el, "aria-labelledby") or ""
    if labelledby:
        try:
            ref_el = await page.query_selector(f"#{labelledby}")
            if ref_el:
                text = await ref_el.inner_text()
                if text.strip():
                    return _clean_label(text)
        except Exception:
            pass

    # 7. Nearby sibling or parent text (last resort)
    try:
        text = await el.evaluate(
            """el => {
                const parent = el.parentElement;
                if (!parent) return '';
                const clone = parent.cloneNode(true);
                const inputs = clone.querySelectorAll('input, select, textarea');
                inputs.forEach(i => i.remove());
                return clone.innerText;
            }"""
        )
        if text and text.strip() and len(text.strip()) < 120:
            return _clean_label(text)
    except Exception:
        pass

    return ""


async def _classify_input(el: Any, page: Any) -> FormField | None:
    try:
        input_type = (await _attr(el, "type") or "text").lower()
        if input_type in {"hidden", "submit", "button", "reset", "image"}:
            return None

        is_visible = await el.is_visible()
        if not is_visible:
            return None

        label = await _get_label_for(el, page)
        name = await _attr(el, "name") or ""
        id_val = await _attr(el, "id") or ""
        placeholder = await _attr(el, "placeholder") or ""
        required = await _is_required(el)
        aria_label = await _attr(el, "aria-label") or ""
        context_text = await _get_context_text(el)

        # Map HTML input types to our types
        type_map = {
            "text": "text",
            "email": "email",
            "tel": "phone",
            "number": "number",
            "date": "date",
            "checkbox": "checkbox",
            "radio": "radio",
            "file": "file",
            "password": "text",
            "url": "text",
        }
        field_type = type_map.get(input_type, "text")
        options: list[str] = []

        if field_type == "radio":
            group_label = await _get_choice_group_label(el, page)
            if group_label:
                label = group_label
            options = await _get_radio_group_options(el, page)

        return FormField(
            locator=el,
            field_type=field_type,
            label=label,
            options=options,
            required=required,
            placeholder=placeholder,
            name_attr=name,
            id_attr=id_val,
            aria_label=aria_label,
            selector_path=_selector_path("input", input_type, name, id_val),
            context_text=context_text,
            input_type=input_type,
            accept_attr=await _attr(el, "accept") or "",
            max_length=_parse_int(await _attr(el, "maxlength")),
        )
    except Exception as exc:
        _log.debug(f"input classify error | error={exc}")
        return None


async def _classify_textarea(el: Any, page: Any) -> FormField | None:
    try:
        is_visible = await el.is_visible()
        if not is_visible:
            return None
        label = await _get_label_for(el, page)
        return FormField(
            locator=el,
            field_type="textarea",
            label=label,
            required=await _is_required(el),
            placeholder=await _attr(el, "placeholder") or "",
            name_attr=await _attr(el, "name") or "",
            id_attr=await _attr(el, "id") or "",
            aria_label=await _attr(el, "aria-label") or "",
            selector_path=_selector_path(
                "textarea",
                "",
                await _attr(el, "name") or "",
                await _attr(el, "id") or "",
            ),
            context_text=await _get_context_text(el),
            max_length=_parse_int(await _attr(el, "maxlength")),
        )
    except Exception as exc:
        _log.debug(f"textarea classify error | error={exc}")
        return None


async def _classify_select(el: Any, page: Any) -> FormField | None:
    try:
        is_visible = await el.is_visible()
        if not is_visible:
            return None
        label = await _get_label_for(el, page)
        options = await _get_select_options(el)
        return FormField(
            locator=el,
            field_type="select",
            label=label,
            options=options,
            required=await _is_required(el),
            name_attr=await _attr(el, "name") or "",
            id_attr=await _attr(el, "id") or "",
            aria_label=await _attr(el, "aria-label") or "",
            selector_path=_selector_path(
                "select",
                "",
                await _attr(el, "name") or "",
                await _attr(el, "id") or "",
            ),
            context_text=await _get_context_text(el),
        )
    except Exception as exc:
        _log.debug(f"select classify error | error={exc}")
        return None


async def _classify_custom_select(el: Any, page: Any) -> FormField | None:
    """Handle role=combobox / role=listbox custom dropdowns (LinkedIn, Workday)."""
    try:
        is_visible = await el.is_visible()
        if not is_visible:
            return None
        label = await _get_label_for(el, page)
        # Options are usually in a sibling ul or div[role=option]
        options: list[str] = []
        try:
            option_els = await el.query_selector_all("[role='option']")
            for opt in option_els[:30]:
                text = await opt.inner_text()
                if text.strip():
                    options.append(text.strip())
        except Exception:
            pass
        return FormField(
            locator=el,
            field_type="select",
            label=label,
            options=options,
            required=await _is_required(el),
            id_attr=await _attr(el, "id") or "",
            aria_label=await _attr(el, "aria-label") or "",
            selector_path=_selector_path(
                "custom-select",
                "",
                await _attr(el, "name") or "",
                await _attr(el, "id") or "",
            ),
            context_text=await _get_context_text(el),
        )
    except Exception as exc:
        _log.debug(f"custom select classify error | error={exc}")
        return None


async def _classify_file(el: Any, page: Any) -> FormField | None:
    try:
        label = await _get_label_for(el, page)
        accept = await _attr(el, "accept") or ""
        name = await _attr(el, "name") or ""
        id_val = await _attr(el, "id") or ""
        return FormField(
            locator=el,
            field_type="file",
            label=label or "resume upload",
            name_attr=name,
            id_attr=id_val,
            selector_path=_selector_path("input", "file", name, id_val),
            context_text=accept,
            input_type="file",
            accept_attr=accept,
        )
    except Exception as exc:
        _log.debug(f"file classify error | error={exc}")
        return None


async def _get_select_options(el: Any) -> list[str]:
    try:
        return await el.evaluate(
            """el => Array.from(el.options)
               .map(o => o.text.trim())
               .filter(t => t && t !== '--' && t !== 'Select...')"""
        )
    except Exception:
        return []


async def _get_radio_group_options(el: Any, page: Any) -> list[str]:
    name = await _attr(el, "name") or ""
    if not name:
        return []

    options: list[str] = []
    radios = await page.query_selector_all(f"input[type='radio'][name='{name}']")
    for radio in radios:
        try:
            radio_id = await radio.get_attribute("id") or ""
            radio_val = await radio.get_attribute("value") or ""
            text = ""
            if radio_id:
                label_el = await page.query_selector(f"label[for='{radio_id}']")
                if label_el:
                    text = (await label_el.inner_text()).strip()
            if not text:
                text = radio_val.strip()
            cleaned = _clean_label(text)
            if cleaned and cleaned not in options:
                options.append(cleaned)
        except Exception:
            continue
    return options


async def _attr(el: Any, name: str) -> str | None:
    try:
        return await el.get_attribute(name)
    except Exception:
        return None


async def _is_required(el: Any) -> bool:
    try:
        req = await el.get_attribute("required")
        aria_req = await el.get_attribute("aria-required")
        return req is not None or (aria_req or "").lower() == "true"
    except Exception:
        return False


def _clean_label(text: str) -> str:
    """Strip noise characters from label text."""
    text = text.replace("SVGs not supported by this browser.", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[*†‡§]", "", text)  # required markers
    return text.strip()


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _selector_path(tag: str, input_type: str, name_attr: str, id_attr: str) -> str:
    parts = [tag]
    if input_type:
        parts.append(f"type={input_type}")
    if name_attr:
        parts.append(f"name={name_attr}")
    if id_attr:
        parts.append(f"id={id_attr}")
    return " ".join(parts)


async def _get_choice_group_label(el: Any, page: Any) -> str:
    direct_label = await _get_label_for(el, page)
    if direct_label and direct_label.lower() not in {"yes", "no"}:
        return direct_label

    try:
        text = await el.evaluate(
            """el => {
                const group = el.closest(
                    'fieldset, [role="radiogroup"], .application-question, .question, .field, .form-group, [data-ui*="question"], [class*="question"], [class*="Question"]'
                );
                if (!group) return '';
                const legend = group.querySelector('legend');
                if (legend && legend.innerText.trim()) return legend.innerText;
                const heading = group.querySelector(
                    'h1, h2, h3, h4, .label, .question-label, .application-label, [data-ui*="question"], [class*="label"], [class*="Label"]'
                );
                if (heading && heading.innerText.trim()) return heading.innerText;
                const clone = group.cloneNode(true);
                clone.querySelectorAll('input, select, textarea, button, [role="radio"]').forEach(n => n.remove());
                const lines = (clone.innerText || '')
                    .split('\n')
                    .map(s => s.trim())
                    .filter(Boolean)
                    .filter(s => !['yes', 'no'].includes(s.toLowerCase()));
                return lines[0] || '';
            }"""
        )
        cleaned = _clean_label(text or "")
        if cleaned and cleaned.lower() not in {"yes", "no"}:
            return cleaned
    except Exception:
        pass

    return direct_label


async def _get_context_text(el: Any) -> str:
    try:
        text = await el.evaluate(
            """el => {
                const group = el.closest('fieldset, .application-question, .question, .field, .form-group');
                const source = group || el.parentElement;
                if (!source) return '';
                const clone = source.cloneNode(true);
                clone.querySelectorAll('input, select, textarea, button').forEach(n => n.remove());
                return clone.innerText || '';
            }"""
        )
        cleaned = _clean_label(text or "")
        return cleaned[:240]
    except Exception:
        return ""


def _dedupe_key(field: FormField) -> str:
    if field.field_type == "radio" and field.name_attr:
        return f"radio:{field.name_attr}"
    return field.id_attr or field.name_attr or field.label
