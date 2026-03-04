"""Playwright-based Greenhouse application form filler and submitter."""

import os
import re
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

# Timeouts in milliseconds
PAGE_LOAD_TIMEOUT = 30000
ELEMENT_TIMEOUT = 10000
SUBMIT_TIMEOUT = 15000

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


def apply_to_greenhouse(
    job_url: str,
    profile: dict,
    resume_path: str,
    company: str = "",
    role_title: str = "",
    dry_run: bool = False,
) -> dict:
    """Fill and submit a Greenhouse application form.

    Args:
        job_url: URL of the Greenhouse job posting
        profile: dict with keys: first_name, last_name, email, phone, linkedin, etc.
        resume_path: path to resume PDF relative to project root
        company: company name for proof filenames
        role_title: role title for proof filenames
        dry_run: if True, fill form but do not submit

    Returns:
        dict with keys:
            - success: bool
            - status: 'SUBMITTED' | 'NEEDS_HUMAN' | 'ERROR'
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            # Navigate to job page
            logger.info(f"Navigating to {job_url}")
            page.goto(job_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # Let JS render

            # Check for blockers before proceeding
            blocker = _detect_blocker(page)
            if blocker:
                proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.png"
                page.screenshot(path=str(proof_path), full_page=True)
                # Save HTML too
                html_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}.html"
                html_path.write_text(page.content())

                result["status"] = "NEEDS_HUMAN"
                result["blocked_reason"] = blocker
                result["proof_path"] = str(proof_path)
                logger.warning(f"Blocked by {blocker}. Screenshot: {proof_path}")
                return result

            # Try to navigate to application form
            _navigate_to_form(page, job_url)

            # Check again for blockers after navigation
            blocker = _detect_blocker(page)
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
            _fill_form(page, profile, str(abs_resume))

            # Take pre-submit screenshot
            pre_submit_path = PROOFS_SUBMITTED / f"{file_prefix}_pre_submit.png"
            page.screenshot(path=str(pre_submit_path), full_page=True)

            if dry_run:
                logger.info("DRY RUN: Skipping form submission")
                result["success"] = True
                result["status"] = "SUBMITTED"  # Mark as submitted for tracking in dry run
                result["proof_path"] = str(pre_submit_path)
                result["confirmation_text"] = "[DRY RUN] Form filled but not submitted"
                return result

            # Submit
            _submit_form(page)

            # Wait for confirmation
            page.wait_for_timeout(3000)

            # Check for post-submit blockers
            blocker = _detect_blocker(page)
            if blocker:
                proof_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_post_submit.png"
                page.screenshot(path=str(proof_path), full_page=True)
                html_path = PROOFS_NEEDS_HUMAN / f"{file_prefix}_post_submit.html"
                html_path.write_text(page.content())

                result["status"] = "NEEDS_HUMAN"
                result["blocked_reason"] = f"Post-submit: {blocker}"
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
            browser.close()

    return result


def _detect_blocker(page: Page) -> Optional[str]:
    """Check if page has CAPTCHA, OTP, or login requirements."""
    try:
        page_text = page.inner_text("body").lower()
    except Exception:
        return None

    page_html = ""
    try:
        page_html = page.content().lower()
    except Exception:
        pass

    # Check for CAPTCHAs
    for pattern in CAPTCHA_PATTERNS:
        if pattern in page_text or pattern in page_html:
            return f"CAPTCHA detected: {pattern}"

    # Check for CAPTCHA iframes
    captcha_frames = page.query_selector_all(
        'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'
    )
    if captcha_frames:
        return "CAPTCHA iframe detected"

    # Check for OTP/verification
    for pattern in OTP_PATTERNS:
        if pattern in page_text:
            return f"Verification required: {pattern}"

    # Check for login requirements (but not on job pages that mention "login" in description)
    login_form = page.query_selector_all('form[action*="login"], form[action*="sign"]')
    if login_form:
        return "Login required"

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


def _fill_form(page: Page, profile: dict, resume_path: str):
    """Fill Greenhouse application form fields."""
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
    ]

    for selectors, key, method in field_mappings:
        value = profile.get(key, "")
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    el.fill(str(value))
                    logger.debug(f"Filled {key} using selector {sel}")
                    break
            except Exception as e:
                logger.debug(f"Could not fill {sel}: {e}")
                continue

    # Fill LinkedIn/GitHub/Portfolio URLs
    _fill_url_fields(page, profile)

    # Upload resume
    _upload_resume(page, resume_path)


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
