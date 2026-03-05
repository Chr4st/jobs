"""Extract and normalize Greenhouse application form fields into a JSON schema.

Two extraction paths:
  1. API-first: ``extract_schema_from_api(slug, job_id)`` — hits the public
     Greenhouse boards API ``?questions=true``.  No browser needed, fast & free.
  2. Playwright fallback: ``extract_form_schema(job_url)`` — headless browser
     visit when the API path is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import Page, sync_playwright

from src.utils.logging import setup_logging

logger = setup_logging("jobbot.greenhouse.schema")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BROWSER_PROFILE = PROJECT_ROOT / "data" / "browser_profile"
REQUEST_TIMEOUT = 12


# ── Standard Greenhouse fields (always present on every app) ─────────────────

_STANDARD_FIELDS: list[dict] = [
    {"field_key": "first_name", "field_type": "text", "label": "First Name",
     "required": True, "options": [], "html_name": "first_name", "html_id": "first_name"},
    {"field_key": "last_name", "field_type": "text", "label": "Last Name",
     "required": True, "options": [], "html_name": "last_name", "html_id": "last_name"},
    {"field_key": "email", "field_type": "email", "label": "Email",
     "required": True, "options": [], "html_name": "email", "html_id": "email"},
    {"field_key": "phone", "field_type": "tel", "label": "Phone",
     "required": False, "options": [], "html_name": "phone", "html_id": "phone"},
    {"field_key": "resume", "field_type": "file", "label": "Resume/CV",
     "required": True, "options": [], "html_name": "resume", "html_id": "resume"},
]


# ── API-first extraction ─────────────────────────────────────────────────────

def extract_schema_from_api(
    slug: str,
    job_id: str | int,
) -> Optional[dict]:
    """Extract form schema via Greenhouse Boards API (no browser needed).

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true

    Returns the same schema dict shape as ``extract_form_schema``, or None on
    failure.
    """
    api_url = (
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        "?questions=true"
    )
    try:
        resp = requests.get(
            api_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"API schema fetch HTTP {resp.status_code} for {slug}/{job_id}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"API schema fetch failed for {slug}/{job_id}: {e}")
        return None

    questions = data.get("questions") or []
    if not questions:
        logger.debug(f"No questions returned for {slug}/{job_id}")
        return None

    fields: list[dict] = list(_STANDARD_FIELDS)  # copy standard fields

    for q in questions:
        label = (q.get("label") or "").strip()
        required = bool(q.get("required"))
        q_fields = q.get("fields") or []

        for f in q_fields:
            field_type_raw = (f.get("type") or "input_text").lower()
            field_type = _map_api_field_type(field_type_raw)
            name = f.get("name") or ""
            values = f.get("values") or []
            options = [v.get("label", v.get("value", "")) for v in values if v]

            field_key = _normalize_field_key(name or label)

            fields.append({
                "field_key": field_key,
                "field_type": field_type,
                "label": _clean_label(label),
                "required": required,
                "options": options,
                "html_name": name,
                "html_id": "",
            })

    keys_str = "|".join(sorted(f["field_key"] for f in fields))
    schema_hash = hashlib.sha256(keys_str.encode()).hexdigest()[:16]

    job_url = data.get("absolute_url", f"https://boards.greenhouse.io/{slug}/jobs/{job_id}")
    schema = {
        "job_url": job_url,
        "schema_hash": schema_hash,
        "fields": fields,
    }
    logger.info(f"API schema: {len(fields)} fields ({len(questions)} questions), hash={schema_hash}")
    return schema


def _map_api_field_type(raw: str) -> str:
    """Map Greenhouse API field types to our normalized types."""
    mapping = {
        "input_text": "text",
        "input_file": "file",
        "input_hidden": "hidden",
        "textarea": "textarea",
        "multi_value_single_select": "select",
        "multi_value_multi_select": "checkbox",
    }
    return mapping.get(raw, "text")


def _normalize_field_key(raw: str) -> str:
    """Produce a stable, lowercase, underscore-delimited key."""
    key = re.sub(r"\[\]", "", raw)
    key = re.sub(r"[^a-z0-9_]", "_", key, flags=re.IGNORECASE)
    key = re.sub(r"_+", "_", key).strip("_").lower()
    return key or "unknown"


def _clean_label(label: str) -> str:
    return label.replace("*", "").strip()[:200]


# ── Playwright-based extraction (fallback) ────────────────────────────────────

def extract_form_schema(job_url: str, headless: bool = True) -> Optional[dict]:
    """Visit a Greenhouse job page, navigate to the application form,
    and extract all fields into a normalized schema.

    Returns:
        {
            "job_url": str,
            "schema_hash": str,         # stable hash of field_keys
            "fields": [
                {
                    "field_key": str,    # stable identifier (e.g. "first_name", "custom_q_12345")
                    "field_type": str,   # text | email | tel | select | radio | checkbox | textarea | file
                    "label": str,
                    "required": bool,
                    "options": [str],    # for select/radio
                    "html_name": str,
                    "html_id": str,
                }
            ]
        }
    """
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            page.goto(job_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Try clicking "Apply" to get to the form
            _click_apply(page)
            page.wait_for_timeout(2000)

            fields = _extract_fields(page)
            if not fields:
                logger.warning(f"No form fields found on {job_url}")
                return None

            # Build stable schema hash from sorted field keys
            keys_str = "|".join(sorted(f["field_key"] for f in fields))
            schema_hash = hashlib.sha256(keys_str.encode()).hexdigest()[:16]

            schema = {
                "job_url": job_url,
                "schema_hash": schema_hash,
                "fields": fields,
            }
            logger.info(f"Extracted {len(fields)} fields, hash={schema_hash}")
            return schema

        except Exception as e:
            logger.error(f"Schema extraction failed for {job_url}: {e}")
            return None
        finally:
            browser.close()


def _click_apply(page: Page):
    """Try to navigate to the application form."""
    for sel in [
        'a:has-text("Apply")', 'button:has-text("Apply")',
        'a[href*="application"]', '#apply_button', '.apply-button',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return
        except Exception:
            continue


def _extract_fields(page: Page) -> list[dict]:
    """Extract all visible form fields from the page using JS evaluation."""
    raw = page.evaluate("""() => {
        const fields = [];
        const seen_keys = new Set();
        const form = document.querySelector('#application_form, form[action*="application"], form');
        if (!form) return fields;

        function getLabel(el, id) {
            // 1. <label for="id">
            if (id) {
                const lbl = document.querySelector(`label[for="${id}"]`);
                if (lbl) return lbl.innerText.trim();
            }
            // 2. aria-label attribute
            const ariaLabel = el.getAttribute('aria-label');
            if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
            // 3. placeholder attribute
            const placeholder = el.getAttribute('placeholder');
            if (placeholder && placeholder.trim()) return placeholder.trim();
            // 4. Wrapping <label> element (parent traversal)
            const wrappingLabel = el.closest('label');
            if (wrappingLabel) {
                const clone = wrappingLabel.cloneNode(true);
                // Remove nested inputs to get only label text
                clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
                const txt = clone.innerText.trim();
                if (txt) return txt;
            }
            // 5. Nearest preceding sibling label or parent container label/legend
            const parent = el.closest('.field, .form-group, .question, .input-group, div, fieldset');
            if (parent) {
                const lbl = parent.querySelector('label, .label, legend, .field-label');
                if (lbl && lbl !== el) return lbl.innerText.trim();
            }
            return '';
        }

        function normalizeFieldKey(raw) {
            return raw
                .replace(/\\[\\]/g, '')        // strip []
                .replace(/[^a-z0-9_]/gi, '_') // non-alnum → _
                .replace(/_+/g, '_')           // collapse runs
                .replace(/^_|_$/g, '')         // trim
                .toLowerCase();
        }

        function isRequired(el, labelText) {
            if (el.required) return true;
            if (el.getAttribute('aria-required') === 'true') return true;
            // Label text contains asterisk
            if (labelText && labelText.includes('*')) return true;
            // Parent has a required indicator
            const parent = el.closest('.field, .form-group, .question, div');
            if (parent) {
                const indicator = parent.querySelector('.required, [aria-label*="required"], abbr[title*="required"]');
                if (indicator) return true;
            }
            return false;
        }

        // Text/email/tel/file inputs
        form.querySelectorAll('input, textarea, select').forEach(el => {
            if (el.type === 'hidden' || el.type === 'submit') return;
            if (el.offsetParent === null) return; // not visible

            const name = el.name || '';
            const id = el.id || '';
            const type = el.tagName === 'SELECT' ? 'select'
                       : el.tagName === 'TEXTAREA' ? 'textarea'
                       : (el.type || 'text');

            const label = getLabel(el, id);

            const required = isRequired(el, label);

            // Options for select
            let options = [];
            if (el.tagName === 'SELECT') {
                el.querySelectorAll('option').forEach(opt => {
                    const v = opt.value;
                    const t = opt.textContent.trim();
                    const tl = t.toLowerCase();
                    if (v && t && !['', 'select', 'select...', 'choose', '-- select --'].includes(tl)) {
                        options.push(t);
                    }
                });
            }

            // Build field_key: prefer name, fall back to id, then label
            let raw_key = name || id || label || `unknown_${fields.length}`;
            let field_key = normalizeFieldKey(raw_key);

            // Deduplicate: append index if collision
            let unique_key = field_key;
            let idx = 1;
            while (seen_keys.has(unique_key)) {
                unique_key = `${field_key}_${idx++}`;
            }
            seen_keys.add(unique_key);

            // Clean label: strip HTML entities, collapse whitespace, strip asterisk
            const clean_label = label.replace(/\\*/g, '').replace(/\\s+/g, ' ').trim();

            fields.push({
                field_key: unique_key,
                field_type: type,
                label: clean_label.substring(0, 200),
                required: required,
                options: options,
                html_name: name,
                html_id: id,
            });
        });

        return fields;
    }""")
    return raw or []

