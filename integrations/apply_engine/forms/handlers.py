"""
Form field fill handlers.

Each handler takes a Playwright element (or page + context) and a value,
and attempts to fill it. Returns a FieldFillResult with success/failure status.

Design: deterministic, retry-safe. No LLM here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..observability import get_logger
from .classifier import FormField

_log = get_logger("handlers")

FILL_TIMEOUT_MS = 8_000


@dataclass
class FieldFillResult:
    field_label: str
    field_type: str
    status: str           # filled | skipped | failed | no_value
    value_preview: str    # truncated value for logging (never full PII)
    required: bool = False
    error: str | None = None
    selector_used: str = ""

    @property
    def success(self) -> bool:
        return self.status == "filled"


async def fill_field(
    page: Any,
    field: FormField,
    value: Any,
    resume_path: Path | None = None,
) -> FieldFillResult:
    """
    Fill a form field with the given value.
    Dispatch to the appropriate handler based on field_type.
    """
    req = field.required or field.aria_required

    if value is None and field.field_type != "file":
        return FieldFillResult(
            field_label=field.label,
            field_type=field.field_type,
            status="no_value",
            value_preview="",
            required=req,
        )

    try:
        match field.field_type:
            case "text" | "email" | "phone" | "number" | "date":
                result = await _fill_text(page, field, str(value))
            case "textarea":
                result = await _fill_textarea(page, field, str(value))
            case "select":
                result = await _fill_select(page, field, str(value))
            case "radio":
                result = await _fill_radio(page, field, str(value))
            case "checkbox":
                result = await _fill_checkbox(page, field, value)
            case "file":
                result = await _fill_file(page, field, resume_path)
            case _:
                result = await _fill_text(page, field, str(value))
        result.required = req
        return result
    except Exception as exc:
        _log.warning(
            f"fill_field failed | label={field.label} type={field.field_type} error={exc}"
        )
        return FieldFillResult(
            field_label=field.label,
            field_type=field.field_type,
            status="failed",
            value_preview=_preview(str(value)),
            required=req,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

async def _fill_text(page: Any, field: FormField, value: str) -> FieldFillResult:
    return await _fill_text_like(page, field, value)


async def _fill_textarea(page: Any, field: FormField, value: str) -> FieldFillResult:
    return await _fill_text_like(page, field, value)


async def _fill_select(page: Any, field: FormField, value: str) -> FieldFillResult:
    el = field.locator

    # Native <select>: use select_option
    try:
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = "unknown"

    if tag == "select":
        try:
            # Try by label text first, then by value attribute
            await el.select_option(label=value)
            return FieldFillResult(
                field_label=field.label, field_type=field.field_type,
                status="filled", value_preview=_preview(value),
            )
        except Exception:
            try:
                await el.select_option(value=value)
                return FieldFillResult(
                    field_label=field.label, field_type=field.field_type,
                    status="filled", value_preview=_preview(value),
                )
            except Exception as exc:
                return FieldFillResult(
                    field_label=field.label, field_type=field.field_type,
                    status="failed", value_preview=_preview(value), error=str(exc),
                )

    # Custom dropdown (role=combobox or role=listbox)
    return await _fill_custom_select(page, field, value)


async def _fill_custom_select(page: Any, field: FormField, value: str) -> FieldFillResult:
    """Handle custom dropdown widgets by clicking to open then selecting option."""
    el = field.locator
    try:
        # Click to open the dropdown
        await el.click()
        await asyncio.sleep(0.4)

        # Look for option elements that appeared
        options = await page.query_selector_all("[role='option'], li[data-value], .select-option")
        value_lower = value.lower()
        for opt in options:
            try:
                text = await opt.inner_text()
                if text.strip().lower() == value_lower or value_lower in text.strip().lower():
                    await opt.click()
                    return FieldFillResult(
                        field_label=field.label, field_type=field.field_type,
                        status="filled", value_preview=_preview(value),
                    )
            except Exception:
                continue

        # Fallback: type to filter then click first match
        await el.fill(value)
        await asyncio.sleep(0.3)
        first_opt = await page.query_selector("[role='option']:first-child")
        if first_opt:
            await first_opt.click()
            return FieldFillResult(
                field_label=field.label, field_type=field.field_type,
                status="filled", value_preview=_preview(value),
            )

        return FieldFillResult(
            field_label=field.label, field_type=field.field_type,
            status="failed", value_preview=_preview(value),
            error="no matching option found",
        )
    except Exception as exc:
        return FieldFillResult(
            field_label=field.label, field_type=field.field_type,
            status="failed", value_preview=_preview(value), error=str(exc),
        )


async def _fill_radio(page: Any, field: FormField, value: str) -> FieldFillResult:
    """
    Find and click the radio option matching the value.
    Searches by label text associated with radio inputs.
    """
    value_lower = value.lower()

    # Strategy 1: find radio inputs in the same group by name, match label
    name = field.name_attr
    if name:
        radios = await page.query_selector_all(f"input[type='radio'][name='{name}']")
    else:
        # Fallback: any radio near the field's label
        radios = await page.query_selector_all("input[type='radio']")

    for radio in radios:
        try:
            radio_id = await radio.get_attribute("id") or ""
            radio_val = await radio.get_attribute("value") or ""
            label_el = None
            if radio_id:
                label_el = await page.query_selector(f"label[for='{radio_id}']")

            label_text = ""
            if label_el:
                label_text = await label_el.inner_text()
            if not label_text:
                label_text = radio_val

            if label_text.strip().lower() == value_lower or value_lower in label_text.strip().lower():
                await radio.click()
                return FieldFillResult(
                    field_label=field.label, field_type="radio",
                    status="filled", value_preview=_preview(value),
                )
        except Exception:
            continue

    # Strategy 2: look for yes/no patterns
    yes_variants = {"yes", "true", "y", "1"}
    no_variants = {"no", "false", "n", "0"}
    if value_lower in yes_variants:
        clicked = await _click_radio_by_text(page, "Yes")
        if clicked:
            return FieldFillResult(field_label=field.label, field_type="radio", status="filled", value_preview="Yes")
    if value_lower in no_variants:
        clicked = await _click_radio_by_text(page, "No")
        if clicked:
            return FieldFillResult(field_label=field.label, field_type="radio", status="filled", value_preview="No")

    return FieldFillResult(
        field_label=field.label, field_type="radio",
        status="failed", value_preview=_preview(value),
        error="no matching radio option found",
    )


async def _click_radio_by_text(page: Any, text: str) -> bool:
    """Click a label/radio that contains the given text."""
    try:
        # Try label text
        labels = await page.query_selector_all("label")
        for label in labels:
            label_text = await label.inner_text()
            if label_text.strip().lower() == text.lower():
                await label.click()
                return True
    except Exception:
        pass
    return False


async def _fill_checkbox(page: Any, field: FormField, value: Any) -> FieldFillResult:
    """Check or uncheck a checkbox."""
    el = field.locator
    should_check = value if isinstance(value, bool) else str(value).lower() in {"yes", "true", "1", "y"}

    try:
        is_checked = await el.is_checked()
        if is_checked == should_check:
            return FieldFillResult(
                field_label=field.label, field_type="checkbox",
                status="filled", value_preview="checked" if should_check else "unchecked",
            )
        await el.click()
        return FieldFillResult(
            field_label=field.label, field_type="checkbox",
            status="filled", value_preview="checked" if should_check else "unchecked",
        )
    except Exception as exc:
        return FieldFillResult(
            field_label=field.label, field_type="checkbox",
            status="failed", value_preview="", error=str(exc),
        )


async def _fill_file(page: Any, field: FormField, file_path: Path | None) -> FieldFillResult:
    """Upload a file to a file input."""
    accept = (field.accept_attr or "").strip()
    _log.info(
        "file input detected | "
        f"label={field.label} selector={field.selector_path or 'unknown'} "
        f"accept={accept or '(none)'} input_type={field.input_type or 'file'}"
    )

    if _is_unsupported_image_upload(field):
        _log.info(
            "skipping unsupported image upload target | "
            f"label={field.label} accept={accept or '(none)'}"
        )
        return FieldFillResult(
            field_label=field.label,
            field_type="file",
            status="skipped",
            value_preview="unsupported image upload target",
            selector_used=field.selector_path,
        )

    if not file_path:
        return FieldFillResult(
            field_label=field.label, field_type="file",
            status="skipped", value_preview="no file path configured",
            selector_used=field.selector_path,
        )
    if not file_path.exists():
        return FieldFillResult(
            field_label=field.label, field_type="file",
            status="failed", value_preview=str(file_path),
            error=f"file not found: {file_path}",
            selector_used=field.selector_path,
        )

    el = field.locator
    try:
        await el.set_input_files(str(file_path))
        _log.info(f"file uploaded | label={field.label} filename={file_path.name}")
        return FieldFillResult(
            field_label=field.label, field_type="file",
            status="filled", value_preview=file_path.name,
            selector_used=field.selector_path,
        )
    except Exception as exc:
        return FieldFillResult(
            field_label=field.label, field_type="file",
            status="failed", value_preview=str(file_path), error=str(exc),
            selector_used=field.selector_path,
        )


def _preview(value: str, max_len: int = 40) -> str:
    return value[:max_len] + "..." if len(value) > max_len else value


async def _fill_text_like(page: Any, field: FormField, value: str) -> FieldFillResult:
    target, selector_used = await _resolve_text_target(field)
    normalized_value = _truncate_for_field(value, field.max_length)

    current = await _read_text_value(target)
    if current == normalized_value:
        return FieldFillResult(
            field_label=field.label,
            field_type=field.field_type,
            status="filled",
            value_preview=_preview(normalized_value),
            selector_used=selector_used,
        )

    try:
        await target.fill(normalized_value, timeout=FILL_TIMEOUT_MS)
        await asyncio.sleep(0.1)
        if await _verify_filled_value(target, normalized_value):
            _log.info(
                "filled field via fill() | "
                f"label={field.label} selector={selector_used} preview={_preview(normalized_value)}"
            )
            return FieldFillResult(
                field_label=field.label,
                field_type=field.field_type,
                status="filled",
                value_preview=_preview(normalized_value),
                selector_used=selector_used,
            )
    except Exception as exc:
        _log.debug(
            "fill() failed | "
            f"label={field.label} selector={selector_used} error={exc}"
        )

    try:
        js_set = await _set_value_via_js(target, normalized_value)
        if js_set and await _verify_filled_value(target, normalized_value):
            _log.info(
                "filled field via js-setter | "
                f"label={field.label} selector={selector_used} preview={_preview(normalized_value)}"
            )
            return FieldFillResult(
                field_label=field.label,
                field_type=field.field_type,
                status="filled",
                value_preview=_preview(normalized_value),
                selector_used=selector_used,
            )
    except Exception as exc:
        _log.debug(
            "js setter failed | "
            f"label={field.label} selector={selector_used} error={exc}"
        )

    if len(normalized_value) <= 120:
        try:
            await target.click(timeout=FILL_TIMEOUT_MS)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await target.type(normalized_value, delay=20, timeout=FILL_TIMEOUT_MS)
            if await _verify_filled_value(target, normalized_value):
                _log.info(
                    "filled field via type() | "
                    f"label={field.label} selector={selector_used} preview={_preview(normalized_value)}"
                )
                return FieldFillResult(
                    field_label=field.label,
                    field_type=field.field_type,
                    status="filled",
                    value_preview=_preview(normalized_value),
                    selector_used=selector_used,
                )
        except Exception as exc:
            _log.debug(
                "type() failed | "
                f"label={field.label} selector={selector_used} error={exc}"
            )

    return FieldFillResult(
        field_label=field.label,
        field_type=field.field_type,
        status="failed",
        value_preview=_preview(normalized_value),
        error="unable to set field value via fill, js-setter, or type",
        selector_used=selector_used,
    )


async def _resolve_text_target(field: FormField) -> tuple[Any, str]:
    el = field.locator
    try:
        tag = await el.evaluate("el => (el.tagName || '').toLowerCase()")
        if tag in {"input", "textarea"}:
            _log.debug(
                "resolved text target | "
                f"label={field.label} selector=self:{field.selector_path or tag}"
            )
            return el, f"self:{field.selector_path or tag}"
    except Exception:
        pass

    selectors = [
        "input:not([type='hidden']):not([type='file'])",
        "textarea",
        "[contenteditable='true']",
        "[data-role='illustrated-input'] input",
        "[data-role='illustrated-input'] textarea",
    ]
    for selector in selectors:
        try:
            inner = await el.query_selector(selector)
            if inner:
                _log.info(
                    "resolved inner selector for custom field | "
                    f"label={field.label} selector={selector}"
                )
                return inner, selector
        except Exception:
            continue

    _log.debug(
        "using original field locator | "
        f"label={field.label} selector={field.selector_path or 'original'}"
    )
    return el, field.selector_path or "original"


async def _read_text_value(el: Any) -> str | None:
    try:
        return await el.input_value()
    except Exception:
        pass
    try:
        return await el.evaluate(
            """el => {
                if (el.isContentEditable) return (el.innerText || '').trim();
                if (typeof el.value === 'string') return el.value;
                return '';
            }"""
        )
    except Exception:
        return None


async def _verify_filled_value(el: Any, expected: str) -> bool:
    actual = (await _read_text_value(el)) or ""
    if actual.strip() == expected.strip():
        return True

    actual_compact = _compact_value(actual)
    expected_compact = _compact_value(expected)
    if actual_compact and expected_compact:
        if actual_compact == expected_compact:
            return True
        if len(expected_compact) >= 5 and expected_compact in actual_compact:
            return True

    actual_digits = "".join(ch for ch in actual if ch.isdigit())
    expected_digits = "".join(ch for ch in expected if ch.isdigit())
    if expected_digits and actual_digits == expected_digits:
        return True

    return False


async def _set_value_via_js(el: Any, value: str) -> bool:
    return bool(await el.evaluate(
        """(el, value) => {
            if (!el) return false;
            const isEditable = el.isContentEditable === true;
            if (isEditable) {
                el.focus();
                el.textContent = value;
            } else if (typeof el.value === 'string') {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                if (descriptor && typeof descriptor.set === 'function') {
                    descriptor.set.call(el, value);
                } else {
                    el.value = value;
                }
                el.focus();
            } else {
                return false;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            return true;
        }""",
        value,
    ))


def _truncate_for_field(value: str, max_length: int | None) -> str:
    trimmed = " ".join(value.split())
    if max_length and max_length > 0 and len(trimmed) > max_length:
        return trimmed[:max_length].rstrip()
    return trimmed


def _compact_value(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _is_unsupported_image_upload(field: FormField) -> bool:
    accept = (field.accept_attr or "").lower()
    label = " ".join([
        field.label or "",
        field.context_text or "",
        field.name_attr or "",
        field.id_attr or "",
    ]).lower()

    image_words = ("photo", "avatar", "profile image", "profile photo", "picture", "image")
    is_image_only = (
        bool(accept)
        and "image" in accept
        and not any(token in accept for token in ("pdf", "doc", "docx", "rtf", "odt", "txt"))
    )
    mentions_image_target = any(word in label for word in image_words)
    mentions_svg_only = "svg" in accept and not any(
        token in accept for token in ("pdf", "doc", "docx", "rtf", "odt", "txt")
    )

    return is_image_only or mentions_image_target or mentions_svg_only
