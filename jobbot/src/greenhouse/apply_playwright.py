"""Playwright-based Greenhouse application form filler and submitter."""
from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from src.utils.logging import setup_logging
from src.utils.sanitize import safe_filename

logger = setup_logging("jobbot.greenhouse.apply")

PROOFS_SUBMITTED = Path(__file__).resolve().parents[2] / "proofs" / "submitted"
PROOFS_NEEDS_HUMAN = Path(__file__).resolve().parents[2] / "proofs" / "needs_human"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BROWSER_PROFILE_DIR = PROJECT_ROOT / "data" / "browser_profile"

# Timeouts in milliseconds
PAGE_LOAD_TIMEOUT = 30000
ELEMENT_TIMEOUT = 10000
SUBMIT_TIMEOUT = 15000

# Realistic Chrome user agents (updated quarterly)
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.116 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.140 Safari/537.36",
]

# Realistic viewport sizes (avoid exactly 1280x900 fingerprint)
_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1512, "height": 982},
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
]

# Patterns that indicate human verification is needed
CAPTCHA_PATTERNS = [
    "captcha", "recaptcha", "hcaptcha", "turnstile",
    "verify you are human", "i'm not a robot", "i am not a robot",
    "security check", "challenge",
]

OTP_PATTERNS = [
    "verification code", "one-time", "otp", "verify your email",
    "verify your phone", "enter code", "confirmation code",
]

LOGIN_PATTERNS = [
    "sign in", "log in", "login", "sign up", "create account",
    "password",
]


# ── Human-behavior helpers ────────────────────────────────────────────────

def _human_delay(min_ms: int = 80, max_ms: int = 350):
    """Random pause to simulate human reaction time."""
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


def _human_type(el, text: str):
    """Type text character by character with variable speed, like a human."""
    el.click()
    _human_delay(100, 200)
    for char in text:
        el.type(char)
        # Occasional short pause mid-word (thinking / typo correction feel)
        if random.random() < 0.08:
            time.sleep(random.uniform(0.15, 0.4))
        else:
            time.sleep(random.uniform(0.04, 0.12))


def _human_scroll(page: Page, direction: str = "down"):
    """Scroll naturally, as a human reading the page would."""
    scroll_steps = random.randint(2, 4)
    for _ in range(scroll_steps):
        amount = random.randint(150, 450) * (1 if direction == "down" else -1)
        page.mouse.wheel(0, amount)
        time.sleep(random.uniform(0.25, 0.7))


def _human_hover_click(page: Page, el):
    """Move mouse to element, hover briefly, then click — avoids instant teleport."""
    try:
        box = el.bounding_box()
        if box:
            # Land slightly off-center (humans don't click the exact center)
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            page.mouse.move(x, y)
            _human_delay(60, 180)
            page.mouse.click(x, y)
            return
    except Exception:
        pass
    el.click()


# ── Browser stealth ───────────────────────────────────────────────────────

_STEALTH_JS = """
() => {
    // 1. Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Realistic plugin list
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            p.__proto__ = PluginArray.prototype;
            return p;
        }
    });

    // 3. Realistic language settings
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // 4. Chrome runtime stub (missing in headless)
    if (!window.chrome) {
        window.chrome = { runtime: {}, app: {}, webstore: {} };
    }

    // 5. Permissions API — avoid 'denied' for notifications (fingerprint tell)
    const _origPerms = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origPerms(params);

    // 6. Hide automation-specific properties
    delete window.__playwright;
    delete window.__pw_manual;
    delete window._playwrightContext;
}
"""


def _apply_stealth(page: Page):
    """Inject stealth overrides to mask browser automation signals."""
    try:
        page.add_init_script(_STEALTH_JS)
    except Exception as e:
        logger.debug(f"Stealth injection failed (non-fatal): {e}")


def _get_proxy() -> Optional[dict]:
    """Load a random proxy from PROXY_LIST env var. Returns None if not configured."""
    proxy_list_raw = os.environ.get("PROXY_LIST", "").strip()
    if not proxy_list_raw:
        return None
    proxies = [p.strip() for p in proxy_list_raw.split(",") if p.strip()]
    if not proxies:
        return None
    chosen = random.choice(proxies)
    return {"server": chosen}


def apply_to_greenhouse(
    job_url: str,
    profile: dict,
    resume_path: str,
    company: str = "",
    role_title: str = "",
    dry_run: bool = False,
    resolved_fields: Optional[list[dict]] = None,
    policy: str = "pause_at_submit",
) -> dict:
    """Fill and optionally submit a Greenhouse application form.

    Args:
        job_url: URL of the Greenhouse job posting
        profile: dict with keys: first_name, last_name, email, phone, linkedin, etc.
        resume_path: path to resume PDF relative to project root
        company: company name for proof filenames
        role_title: role title for proof filenames
        dry_run: if True, fill form but do not submit (overrides policy)
        resolved_fields: list of dicts from auto_map.resolve_all_fields() with
            keys: field_key, label, value, source. If provided, these are used
            to fill custom questions instead of the built-in heuristic.
        policy: 'pause_at_submit' (default) — fill then stop before submit;
                'auto_if_safe' — fill and auto-submit if no blockers detected.

    Returns:
        dict with keys:
            - success: bool
            - status: 'SUBMITTED' | 'FILLED_AWAITING_SUBMIT' | 'NEEDS_HUMAN' | 'ERROR'
            - proof_path: path to screenshot
            - confirmation_text: any confirmation message
            - error: error message if failed
            - blocked_reason: reason if NEEDS_HUMAN
    """
    result = {
        "success": False,
        "status": "ERROR",
        "proof_path": "",
        "confirmation_text": "",
        "error": "",
        "blocked_reason": "",
    }

    abs_resume = PROJECT_ROOT / resume_path
    if not abs_resume.exists():
        result["error"] = f"Resume not found: {abs_resume}"
        logger.error(result["error"])
        return result

    PROOFS_SUBMITTED.mkdir(parents=True, exist_ok=True)
    PROOFS_NEEDS_HUMAN.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_prefix = safe_filename(f"{company}_{role_title}_{timestamp}")

    # Build resolved_fields lookup for fast access
    _resolved_map: dict[str, str] = {}
    if resolved_fields:
        for rf in resolved_fields:
            if rf.get("value") is not None and rf.get("source") != "file_upload":
                _resolved_map[rf["field_key"]] = str(rf["value"])

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    rotate_ua = os.environ.get("ROTATE_USER_AGENT", "true").lower() != "false"
    user_agent = random.choice(_USER_AGENTS) if rotate_ua else _USER_AGENTS[0]
    viewport = random.choice(_VIEWPORTS)
    proxy = _get_proxy()

    logger.debug(f"UA: {user_agent[:60]}... viewport: {viewport}")
    if proxy:
        logger.info(f"Using proxy: {proxy['server'].split('@')[-1]}")

    launch_kwargs = dict(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=False,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        viewport=viewport,
        user_agent=user_agent,
    )
    if proxy:
        launch_kwargs["proxy"] = proxy

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.new_page()
        _apply_stealth(page)

        try:
            # Navigate to job page
            logger.info(f"Navigating to {job_url}")
            page.goto(job_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # Let JS render

            # Check for blockers — if CAPTCHA, wait for user to solve it
            blocker = _detect_blocker(page)
            if blocker:
                blocker = _wait_for_human(page, blocker)
                if blocker:
                    proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.png"
                    page.screenshot(path=str(proof_path), full_page=True)
                    html_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.html"
                    html_path.write_text(page.content())
                    result["status"] = "NEEDS_HUMAN"
                    result["blocked_reason"] = blocker
                    result["proof_path"] = str(proof_path)
                    logger.warning(f"Blocked by {blocker}. Screenshot: {proof_path}")
                    return result

            # Try to navigate to application form
            _navigate_to_form(page, job_url)

            # Natural scroll — simulate reading the page before filling
            _human_scroll(page, "down")
            _human_delay(400, 900)

            # Check again for blockers after navigation
            blocker = _detect_blocker(page)
            if blocker:
                blocker = _wait_for_human(page, blocker)
                if blocker:
                    proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.png"
                    page.screenshot(path=str(proof_path), full_page=True)
                    html_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.html"
                    html_path.write_text(page.content())
                    result["status"] = "NEEDS_HUMAN"
                    result["blocked_reason"] = blocker
                    result["proof_path"] = str(proof_path)
                    logger.warning(f"Blocked by {blocker}. Screenshot: {proof_path}")
                    return result

            # Fill the form
            _fill_form(page, profile, str(abs_resume), _resolved_map)

            # Take pre-submit screenshot
            PROOFS_FILLED = Path(__file__).resolve().parents[2] / "proofs" / "filled"
            PROOFS_FILLED.mkdir(parents=True, exist_ok=True)
            pre_submit_path = PROOFS_FILLED / f"{file_prefix}_pre_submit.png"
            page.screenshot(path=str(pre_submit_path), full_page=True)

            if dry_run:
                logger.info("DRY RUN: Skipping form submission")
                result["success"] = True
                result["status"] = "FILLED_AWAITING_SUBMIT"
                result["proof_path"] = str(pre_submit_path)
                result["confirmation_text"] = "[DRY RUN] Form filled but not submitted"
                return result

            # Validate fill before deciding whether to submit
            fill_ok, fill_errors = _validate_fill(page, _resolved_map)
            if not fill_ok:
                logger.warning(f"Fill validation failed: {fill_errors}")

            # Apply policy
            if policy == "pause_at_submit":
                logger.info("Policy=pause_at_submit: stopping before submit")
                result["success"] = True
                result["status"] = "FILLED_AWAITING_SUBMIT"
                result["proof_path"] = str(pre_submit_path)
                result["confirmation_text"] = "Form filled and ready for review"
                return result

            # policy == "auto_if_safe": check safety before submitting
            if not fill_ok:
                result["status"] = "NEEDS_HUMAN"
                result["blocked_reason"] = f"Fill validation errors: {'; '.join(fill_errors)}"
                result["proof_path"] = str(pre_submit_path)
                return result

            # Submit
            _submit_form(page)

            # Wait for confirmation
            page.wait_for_timeout(3000)

            # Check for post-submit blockers — wait for human if CAPTCHA
            blocker = _detect_blocker(page)
            if blocker:
                blocker = _wait_for_human(page, f"Post-submit: {blocker}")
                if blocker:
                    proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_post_submit.png"
                    page.screenshot(path=str(proof_path), full_page=True)
                    html_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_post_submit.html"
                    html_path.write_text(page.content())
                    result["status"] = "NEEDS_HUMAN"
                    result["blocked_reason"] = blocker
                    result["proof_path"] = str(proof_path)
                    return result

            # Capture confirmation
            confirmation = _extract_confirmation(page)
            proof_path = PROOFS_SUBMITTED / f"{file_prefix}.png"
            page.screenshot(path=str(proof_path), full_page=True)
            html_path = PROOFS_SUBMITTED / f"{file_prefix}.html"
            html_path.write_text(page.content())

            result["success"] = True
            result["status"] = "SUBMITTED"
            result["proof_path"] = str(proof_path)
            result["confirmation_text"] = confirmation
            logger.info(f"Successfully applied. Proof: {proof_path}")

        except PlaywrightTimeout as e:
            logger.error(f"Timeout during application: {e}")
            proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_timeout.png"
            try:
                page.screenshot(path=str(proof_path), full_page=True)
            except Exception:
                pass
            result["error"] = f"Timeout: {e}"
            result["proof_path"] = str(proof_path)

        except Exception as e:
            logger.error(f"Error during application: {e}")
            proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_error.png"
            try:
                page.screenshot(path=str(proof_path), full_page=True)
            except Exception:
                pass
            result["error"] = str(e)
            result["proof_path"] = str(proof_path)

        finally:
            # Close all tabs but keep the persistent profile
            for p in context.pages:
                try:
                    p.close()
                except Exception:
                    pass
            context.close()

    return result


def _wait_for_human(page: Page, blocker: str, timeout_seconds: int = 120) -> Optional[str]:
    """Wait for a human to solve a CAPTCHA or other blocker in the visible browser.

    Polls every 3 seconds to check if the blocker has been resolved.
    Returns None if resolved, or the blocker reason if still blocked after timeout.
    """
    logger.warning(f"🔔 HUMAN ACTION NEEDED: {blocker}")
    logger.warning(f"   Please solve it in the browser window. Waiting up to {timeout_seconds}s...")

    elapsed = 0
    poll_interval = 3000  # ms
    while elapsed < timeout_seconds * 1000:
        page.wait_for_timeout(poll_interval)
        elapsed += poll_interval
        remaining = _detect_blocker(page)
        if not remaining:
            logger.info("✓ Blocker resolved by human! Continuing...")
            return None
        logger.info(f"   Still waiting... ({elapsed // 1000}s / {timeout_seconds}s)")

    logger.warning(f"⏰ Timeout waiting for human to resolve: {blocker}")
    return blocker


def _detect_blocker(page: Page) -> Optional[str]:
    """Check if page has CAPTCHA, OTP, or login requirements.

    Only flags real blockers — avoids false positives from job descriptions
    that mention words like 'challenge' or 'password'.
    """
    # Check for CAPTCHA iframes (most reliable signal)
    try:
        captcha_frames = page.query_selector_all(
            'iframe[src*="captcha"], iframe[src*="recaptcha"], '
            'iframe[src*="hcaptcha"], iframe[src*="turnstile"]'
        )
        if captcha_frames:
            return "CAPTCHA iframe detected"
    except Exception:
        pass

    # Check for visible CAPTCHA widgets (not in job description text)
    try:
        captcha_widgets = page.query_selector_all(
            '.g-recaptcha, .h-captcha, [data-sitekey], '
            '#captcha, .captcha-container, .cf-turnstile'
        )
        for w in captcha_widgets:
            if w.is_visible():
                return "CAPTCHA widget detected"
    except Exception:
        pass

    # Check for OTP/verification forms specifically
    try:
        otp_inputs = page.query_selector_all(
            'input[name*="otp"], input[name*="verification_code"], '
            'input[name*="confirm_code"], input[autocomplete="one-time-code"]'
        )
        for inp in otp_inputs:
            if inp.is_visible():
                return "OTP/verification code required"
    except Exception:
        pass

    # Check for login forms (only if the page is actually a login page, not a job page)
    try:
        login_forms = page.query_selector_all(
            'form[action*="login"], form[action*="sign_in"], form[action*="signin"]'
        )
        if login_forms:
            # Make sure this isn't a Greenhouse application page
            url = page.url.lower()
            if "greenhouse" not in url and "boards.greenhouse.io" not in url:
                return "Login required"
    except Exception:
        pass

    return None


def _navigate_to_form(page: Page, job_url: str):
    """Navigate to the application form section."""
    # Try clicking "Apply" button
    apply_selectors = [
        'a:has-text("Apply")',
        'button:has-text("Apply")',
        'a[class*="apply"]',
        'a[href*="application"]',
        '#apply_button',
        '.apply-button',
    ]

    for sel in apply_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                return
        except Exception:
            continue

    # If no apply button, check if form is already visible
    form = page.query_selector('#application_form, form[action*="application"]')
    if form:
        return

    # Try scrolling to form
    try:
        page.evaluate('document.querySelector("#application")?.scrollIntoView()')
        page.wait_for_timeout(1000)
    except Exception:
        pass


def _fill_form(page: Page, profile: dict, resume_path: str,
               resolved_map: Optional[dict[str, str]] = None):
    """Fill Greenhouse application form fields.

    If resolved_map is provided (from auto_map), it is used as an override
    for custom question answers before falling back to built-in heuristics.
    """
    if resolved_map is None:
        resolved_map = {}

    # Standard Greenhouse form field mappings
    field_mappings = [
        # (selector patterns, profile key, fill method)
        (['#first_name', 'input[name*="first_name"]', 'input[autocomplete="given-name"]'],
         "first_name", "text"),
        (['#last_name', 'input[name*="last_name"]', 'input[autocomplete="family-name"]'],
         "last_name", "text"),
        (['#email', 'input[name*="email"]', 'input[type="email"]'],
         "email", "text"),
        (['#phone', 'input[name*="phone"]', 'input[type="tel"]'],
         "phone", "text"),
        (['input[name*="location"]', 'input[id*="location"]',
          'input[autocomplete="address-level2"]'],
         "location", "text"),
    ]

    for selectors, key, method in field_mappings:
        # Check resolved_map first, then profile
        value = resolved_map.get(key, "") or profile.get(key, "")
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    _human_hover_click(page, el)
                    _human_delay(80, 200)
                    el.triple_click()  # select all existing text
                    _human_type(el, str(value))
                    logger.debug(f"Filled {key} using selector {sel}")
                    break
            except Exception as e:
                logger.debug(f"Could not fill {sel}: {e}")
                continue

    # Fill LinkedIn/GitHub/Portfolio URLs
    _fill_url_fields(page, profile)

    # Upload resume
    _upload_resume(page, resume_path)

    # Handle custom questions (dropdowns, radios, checkboxes, text)
    _fill_custom_questions(page, profile, resolved_map)


def _fill_url_fields(page: Page, profile: dict):
    """Fill URL fields (LinkedIn, GitHub, portfolio)."""
    url_fields = {
        "linkedin": profile.get("linkedin", ""),
        "github": profile.get("github", ""),
        "portfolio": profile.get("portfolio", ""),
    }

    # Greenhouse often uses labeled text inputs for URLs
    for label_text, url_value in url_fields.items():
        if not url_value:
            continue

        # Try finding by label
        try:
            labels = page.query_selector_all("label")
            for label in labels:
                label_content = (label.inner_text() or "").lower()
                if label_text in label_content or (
                    label_text == "portfolio" and "website" in label_content
                ):
                    for_attr = label.get_attribute("for")
                    if for_attr:
                        input_el = page.query_selector(f"#{for_attr}")
                        if input_el and input_el.is_visible():
                            input_el.fill(url_value)
                            break
        except Exception:
            pass

        # Try by input name/placeholder
        selectors = [
            f'input[name*="{label_text}"]',
            f'input[placeholder*="{label_text}"]',
            f'input[id*="{label_text}"]',
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(url_value)
                    break
            except Exception:
                continue


# Keyword → preferred answer for common Greenhouse custom questions
_CUSTOM_QUESTION_ANSWERS = {
    # Work authorization
    "authorized to work": "Yes",
    "legally authorized": "Yes",
    "work authorization": "Yes",
    "right to work": "Yes",
    "eligible to work": "Yes",
    # Sponsorship — requires sponsorship (update profile.yaml if this changes)
    "visa sponsorship": "Yes",
    "require sponsorship": "Yes",
    "immigration sponsorship": "Yes",
    "need sponsorship": "Yes",
    "will you require": "Yes",
    "sponsor": "Yes",
    # Location/relocation
    "willing to relocate": "Yes",
    "open to relocation": "Yes",
    "relocate": "Yes",
    # Start date
    "start date": "ASAP",
    "earliest start": "ASAP",
    "available to start": "Immediately",
    # How did you hear
    "how did you hear": "Online job board",
    "how did you find": "Online job board",
    "where did you hear": "Online job board",
    "referral source": "Online job board",
    # Gender / demographics (prefer not to say)
    "gender": "Prefer not to say",
    "race": "Prefer not to say",
    "ethnicity": "Prefer not to say",
    "veteran": "Prefer not to say",
    "disability": "Prefer not to say",
    # Years of experience
    "years of experience": "1",
    "years of relevant": "1",
    # Salary
    "salary": "Flexible",
    "compensation": "Flexible",
}


def _normalize_field_key(name: str) -> str:
    """Replicate schema_extract.py's normalizeFieldKey normalization."""
    s = re.sub(r"\[\]", "", name)           # strip []
    s = re.sub(r"[^a-z0-9_]", "_", s, flags=re.IGNORECASE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").lower()


def _fill_custom_questions(page: Page, profile: dict,
                           resolved_map: Optional[dict[str, str]] = None):
    """Fill Greenhouse custom questions: selects, radios, checkboxes, text areas.

    If resolved_map is provided, it is checked first for answers before
    falling back to the built-in _CUSTOM_QUESTION_ANSWERS heuristic.
    """
    if resolved_map is None:
        resolved_map = {}

    # --- Handle <select> dropdowns ---
    try:
        selects = page.query_selector_all("select")
        for sel_el in selects:
            try:
                if not sel_el.is_visible():
                    continue
                # Already has a non-default value selected?
                current_val = sel_el.input_value()
                if current_val and current_val != "":
                    continue

                # Find the associated label
                sel_id = sel_el.get_attribute("id") or ""
                sel_name = sel_el.get_attribute("name") or ""
                label_text = _get_label_text(page, sel_id, sel_name, sel_el)

                # Get all option values
                options = sel_el.query_selector_all("option")
                option_texts = []
                for opt in options:
                    val = opt.get_attribute("value") or ""
                    txt = (opt.inner_text() or "").strip()
                    if val and txt and txt.lower() not in ("select", "select...",
                                                           "choose", "-- select --", ""):
                        option_texts.append((val, txt))

                if not option_texts:
                    continue

                # Try to match by custom question answers
                chosen = _pick_best_option(label_text, option_texts, resolved_map)
                if chosen:
                    sel_el.select_option(value=chosen)
                    logger.info(f"Selected '{chosen}' for dropdown: {label_text[:60]}")
                else:
                    # Default: pick first non-empty option
                    sel_el.select_option(value=option_texts[0][0])
                    logger.info(f"Selected first option for dropdown: {label_text[:60]}")
            except Exception as e:
                logger.debug(f"Error filling select: {e}")
    except Exception:
        pass

    # --- Handle radio buttons ---
    try:
        # Group radios by name
        radios = page.query_selector_all('input[type="radio"]')
        radio_groups: dict[str, list] = {}
        for r in radios:
            name = r.get_attribute("name") or ""
            if name:
                radio_groups.setdefault(name, []).append(r)

        for name, group in radio_groups.items():
            # Skip if one is already checked
            if any(r.is_checked() for r in group):
                continue

            # Find label for the group
            label_text = _get_label_text(page, "", name, group[0])

            # Try to pick the right answer
            best_answer = _match_custom_answer(label_text, resolved_map)
            if best_answer:
                for r in group:
                    r_label = _get_radio_label(page, r)
                    if best_answer.lower() in r_label.lower():
                        r.check()
                        logger.info(f"Checked radio '{r_label}' for: {label_text[:60]}")
                        break
                else:
                    # If no exact match, check the first option
                    group[0].check()
                    logger.info(f"Checked first radio for: {label_text[:60]}")
            else:
                # Default: check first
                group[0].check()
                logger.info(f"Checked first radio for: {label_text[:60]}")
    except Exception:
        pass

    # --- Handle checkboxes: resolved_map first, then consent/acknowledgement ---
    try:
        checkboxes = page.query_selector_all('input[type="checkbox"]')
        # Track name counts to replicate schema_extract deduplication (DOM order)
        seen_cb_names: dict[str, int] = {}
        for cb in checkboxes:
            try:
                if not cb.is_visible() or cb.is_checked():
                    continue
                cb_id = cb.get_attribute("id") or ""
                cb_name = cb.get_attribute("name") or cb_id
                label_text = _get_label_text(page, cb_id, cb_name, cb)
                lt = label_text.lower()

                # Compute field_key matching schema_extract normalization
                raw_key = cb_name or cb_id or label_text or f"unknown_{len(seen_cb_names)}"
                base_fk = _normalize_field_key(raw_key)
                count = seen_cb_names.get(base_fk, 0)
                field_key = base_fk if count == 0 else f"{base_fk}_{count}"
                seen_cb_names[base_fk] = count + 1

                # 1. resolved_map says to check this specific field_key
                if resolved_map.get(field_key) == "Yes":
                    cb.check()
                    logger.info(f"Checked checkbox [{field_key}]: {label_text[:60]}")
                    continue

                # 2. Consent/acknowledgement checkboxes
                if any(w in lt for w in ["acknowledge", "consent", "agree",
                                          "confirm", "accept", "privacy",
                                          "terms", "i have read"]):
                    cb.check()
                    logger.info(f"Checked consent checkbox: {label_text[:60]}")
            except Exception:
                continue
    except Exception:
        pass

    # --- Handle empty text inputs and textareas (custom questions) ---
    try:
        text_fields = page.query_selector_all(
            'textarea, input[type="text"]:not([name*="name"]):not([name*="email"])'
            ':not([name*="phone"]):not([name*="linkedin"]):not([name*="github"])'
        )
        for tf in text_fields:
            try:
                if not tf.is_visible():
                    continue
                current = tf.input_value()
                if current and current.strip():
                    continue  # Already filled

                tf_id = tf.get_attribute("id") or ""
                tf_name = tf.get_attribute("name") or ""
                label_text = _get_label_text(page, tf_id, tf_name, tf)
                answer = _match_custom_answer(label_text, resolved_map)
                if answer:
                    _human_hover_click(page, tf)
                    _human_delay(60, 150)
                    _human_type(tf, answer)
                    logger.info(f"Filled text field: {label_text[:60]} → {answer[:30]}")
            except Exception:
                continue
    except Exception:
        pass


def _get_label_text(page: Page, el_id: str, el_name: str, el) -> str:
    """Find the label text for a form element."""
    # Try by 'for' attribute
    if el_id:
        try:
            label = page.query_selector(f'label[for="{el_id}"]')
            if label:
                return (label.inner_text() or "").strip()
        except Exception:
            pass
    # Try parent label
    try:
        parent = el.evaluate_handle("el => el.closest('label')")
        if parent:
            text = parent.evaluate("el => el.innerText || ''")
            if text and text.strip():
                return text.strip()
    except Exception:
        pass
    # Try previous sibling label or parent div's label
    try:
        label = el.evaluate_handle(
            "el => el.parentElement?.querySelector('label') || "
            "el.parentElement?.parentElement?.querySelector('label')"
        )
        if label:
            text = label.evaluate("el => el.innerText || ''")
            if text and text.strip():
                return text.strip()
    except Exception:
        pass
    return el_name or el_id


def _get_radio_label(page: Page, radio) -> str:
    """Get the label text for a specific radio button."""
    r_id = radio.get_attribute("id") or ""
    if r_id:
        try:
            label = page.query_selector(f'label[for="{r_id}"]')
            if label:
                return (label.inner_text() or "").strip()
        except Exception:
            pass
    # Try adjacent text
    try:
        parent = radio.evaluate_handle("el => el.parentElement")
        if parent:
            text = parent.evaluate("el => el.innerText || ''")
            return text.strip()
    except Exception:
        pass
    return radio.get_attribute("value") or ""


def _match_custom_answer(label_text: str,
                         resolved_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """Match a label to a predefined answer.

    Checks resolved_map first (from auto_map), then built-in heuristics.
    """
    # Check resolved_map by normalized label
    if resolved_map:
        lt_norm = re.sub(r"[^a-z0-9_]", "_", label_text.lower().strip())
        if lt_norm in resolved_map:
            return resolved_map[lt_norm]
        # Also try matching by substring of resolved_map keys
        for rk, rv in resolved_map.items():
            if rk in lt_norm or lt_norm in rk:
                return rv

    lt = label_text.lower()
    for keyword, answer in _CUSTOM_QUESTION_ANSWERS.items():
        if keyword in lt:
            return answer
    return None


def _pick_best_option(label_text: str, options: list[tuple[str, str]],
                      resolved_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """Pick the best dropdown option based on the label and predefined answers."""
    answer = _match_custom_answer(label_text, resolved_map)
    if answer:
        # Try exact match first
        for val, txt in options:
            if answer.lower() == txt.lower():
                return val
        # Try partial match
        for val, txt in options:
            if answer.lower() in txt.lower() or txt.lower() in answer.lower():
                return val
    return None


def _upload_resume(page: Page, resume_path: str):
    """Upload resume file to the form."""
    # Greenhouse uses file inputs for resume upload
    file_selectors = [
        'input[type="file"][name*="resume"]',
        'input[type="file"][name*="cv"]',
        'input[type="file"][id*="resume"]',
        'input[type="file"]',  # fallback: first file input
    ]

    for sel in file_selectors:
        try:
            file_inputs = page.query_selector_all(sel)
            for file_input in file_inputs:
                file_input.set_input_files(resume_path)
                logger.info(f"Uploaded resume via {sel}")
                return
        except Exception as e:
            logger.debug(f"Could not upload via {sel}: {e}")
            continue

    logger.warning("Could not find file upload input for resume")


def _submit_form(page: Page):
    """Submit the application form."""
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Send")',
        '#submit_app',
        '.submit-button',
    ]

    for sel in submit_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                logger.info(f"Clicked submit button: {sel}")
                page.wait_for_timeout(2000)
                return
        except Exception:
            continue

    logger.warning("Could not find submit button — trying form submit")
    try:
        page.evaluate(
            'document.querySelector("form[action*=\\"application\\"]")?.submit()'
        )
    except Exception as e:
        logger.error(f"Form submit fallback failed: {e}")


def _validate_fill(page: Page, resolved_map: dict) -> tuple[bool, list[str]]:
    """Check the filled form for visible errors and disabled submit button.

    Returns (ok: bool, errors: list[str]).
    """
    errors = []

    # Check for visible error messages
    try:
        error_els = page.query_selector_all(
            '.error, .field-error, .validation-error, [class*="error"], '
            '[aria-invalid="true"], .invalid-feedback'
        )
        for el in error_els:
            if el.is_visible():
                msg = (el.inner_text() or "").strip()
                if msg:
                    errors.append(msg[:120])
    except Exception:
        pass

    # Check if submit button is disabled
    try:
        submit_btns = page.query_selector_all(
            'button[type="submit"], input[type="submit"], #submit_app'
        )
        for btn in submit_btns:
            if btn.is_visible() and btn.is_disabled():
                errors.append("Submit button is disabled")
                break
    except Exception:
        pass

    return (len(errors) == 0, errors)


def _extract_confirmation(page: Page) -> str:
    """Extract confirmation text after submission."""
    confirmation_selectors = [
        ".confirmation", ".success", ".thank-you",
        'div[class*="confirm"]', 'div[class*="success"]',
        'h1:has-text("Thank")', 'h2:has-text("Thank")',
        'p:has-text("Thank")', 'p:has-text("received")',
    ]

    for sel in confirmation_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el.inner_text().strip()[:500]
        except Exception:
            continue

    # Fallback: check page title or first heading
    try:
        title = page.title()
        if any(word in title.lower() for word in ["thank", "confirm", "success", "received"]):
            return f"Page title: {title}"
    except Exception:
        pass

    return ""
