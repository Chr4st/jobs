"""Apply to jobs through WAAS (Work at a Startup) flow.

Logs into YC account, navigates to job listings, and submits applications
using the WAAS built-in apply system. When CAPTCHA/verification is needed,
opens a visible browser window for the user to complete.
"""

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Page,
    BrowserContext,
    TimeoutError as PwTimeout,
    sync_playwright,
)

from src.utils.logging import setup_logging
from src.utils.sanitize import safe_filename

logger = setup_logging("jobbot.yc.apply_waas")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROOFS_SUBMITTED = PROJECT_ROOT / "proofs" / "submitted"
PROOFS_NEEDS_HUMAN = PROJECT_ROOT / "proofs" / "needs_human"
STATE_DIR = PROJECT_ROOT / "data"

PAGE_TIMEOUT = 30000
ELEMENT_TIMEOUT = 10000

CAPTCHA_INDICATORS = [
    "captcha", "recaptcha", "hcaptcha", "turnstile",
    "verify you are human", "i'm not a robot",
    "security check", "challenge-platform",
]


def _detect_captcha(page: Page) -> bool:
    """Check if a CAPTCHA is present on the page."""
    html = page.content().lower()
    for indicator in CAPTCHA_INDICATORS:
        if indicator in html:
            return True
    frames = page.query_selector_all('iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"]')
    if frames:
        return True
    return False


def _wait_for_human(page: Page, reason: str, proof_path: str):
    """Pause and wait for the user to resolve a blocker in the visible browser.

    The browser is already visible (headless=False). We just wait until the
    blocker clears or the user signals done.
    """
    logger.warning(f"HUMAN ACTION NEEDED: {reason}")
    logger.warning(f"A browser window is open. Please complete the action, then the bot will continue.")
    logger.warning(f"Screenshot saved: {proof_path}")

    print("\n" + "=" * 60)
    print(f"  HUMAN ACTION NEEDED: {reason}")
    print(f"  Complete the action in the open browser window.")
    print(f"  The bot will auto-detect when you're done and continue.")
    print("=" * 60 + "\n")

    # Poll until CAPTCHA/blocker clears
    for attempt in range(120):  # Wait up to 4 minutes
        time.sleep(2)
        try:
            if not _detect_captcha(page):
                logger.info("Blocker appears resolved. Continuing...")
                time.sleep(2)  # Brief pause to let page settle
                return True
        except Exception:
            pass

    logger.warning("Timed out waiting for human action.")
    return False


def login_to_yc(page: Page, username: str, password: str) -> bool:
    """Log into YC via the WAAS site's own Sign-in link (top-right nav).

    This keeps the session on the workatastartup.com domain so apply
    links don't bounce through account.ycombinator.com again.
    """
    logger.info("Logging into YC account...")

    # 1. Go to WAAS jobs page
    page.goto("https://www.workatastartup.com/jobs",
              timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    # 2. Check if already logged in (look for nav sign-in link absence)
    sign_in_link = _find_sign_in_link(page)
    if sign_in_link is None:
        logger.info("Already logged in (no sign-in link in nav)")
        return True

    # 3. Click the sign-in / log-in link in the nav
    logger.info("Clicking sign-in link in top nav...")
    sign_in_link.click()
    page.wait_for_timeout(4000)
    logger.info(f"After sign-in click → URL: {page.url}")

    # 4. Now we're on account.ycombinator.com — fill the auth form
    return _complete_yc_auth(page, username, password)


def _find_sign_in_link(page: Page):
    """Find the Sign-in / Log-in link in the WAAS top-right nav.

    Returns the element handle or None if not found (meaning user is
    already logged in).
    """
    # Try common selectors for the sign-in link
    for sel in [
        'a:has-text("Log in")', 'a:has-text("Sign in")',
        'a:has-text("Log In")', 'a:has-text("Sign In")',
        'a[href*="authenticate"]', 'a[href*="login"]',
        'a[href*="sign_in"]',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue

    # Broader: look in nav/header for any link with login-like text
    try:
        links = page.query_selector_all('nav a, header a')
        for a in links:
            txt = (a.inner_text() or "").strip().lower()
            if any(w in txt for w in ["log in", "sign in", "login", "signin"]):
                return a
    except Exception:
        pass

    return None


def _complete_yc_auth(page: Page, username: str, password: str) -> bool:
    """Fill username + password on the YC auth page and submit.

    YC auth uses username (not email) + password.
    Handles the two-step flow (username → password) and CAPTCHAs.
    """
    # Handle pre-login CAPTCHA
    if _detect_captcha(page):
        proof = str(PROOFS_NEEDS_HUMAN / "yc_login_captcha.png")
        page.screenshot(path=proof)
        resolved = _wait_for_human(page, "CAPTCHA on login page", proof)
        if not resolved:
            return False

    # --- Fill username/email field ---
    # YC auth may label it "username", "email", or just a text input.
    # We always fill the username value (e.g. "Chr4st").
    username_filled = False
    for sel in ['input[name="username"]', 'input[type="email"]',
                'input[name="email"]', '#email', '#username',
                'input[type="text"]']:
        try:
            inp = page.query_selector(sel)
            if inp and inp.is_visible():
                inp.fill(username)
                logger.info(f"Filled username via {sel}")
                username_filled = True
                break
        except Exception:
            continue

    if not username_filled:
        try:
            first = page.query_selector('input:visible')
            if first:
                first.fill(username)
                username_filled = True
                logger.info("Filled username via first visible input")
        except Exception:
            pass

    if not username_filled:
        logger.error("Could not find username/email input")
        return _manual_login_fallback(page)

    _click_submit_button(page)
    page.wait_for_timeout(3000)

    # Already redirected back to WAAS?
    if "workatastartup.com" in page.url:
        logger.info("Logged in after username step")
        return True

    # CAPTCHA after username
    if _detect_captcha(page):
        proof = str(PROOFS_NEEDS_HUMAN / "yc_login_captcha_post_email.png")
        page.screenshot(path=proof)
        resolved = _wait_for_human(page, "CAPTCHA after username", proof)
        if not resolved:
            return False

    # --- Fill password ---
    pw_filled = False
    for sel in ['input[type="password"]', 'input[name="password"]', '#password']:
        try:
            inp = page.query_selector(sel)
            if inp and inp.is_visible():
                inp.fill(password)
                logger.info(f"Filled password via {sel}")
                pw_filled = True
                break
        except Exception:
            continue

    if pw_filled:
        _click_submit_button(page)
        page.wait_for_timeout(4000)

        if _detect_captcha(page):
            proof = str(PROOFS_NEEDS_HUMAN / "yc_login_captcha_post.png")
            page.screenshot(path=proof)
            resolved = _wait_for_human(page, "CAPTCHA after password", proof)
            if not resolved:
                return False

        # Should redirect back to WAAS
        if "workatastartup.com" in page.url:
            logger.info("Successfully logged in via site nav")
            return True

        # Force navigate back to WAAS
        page.goto("https://www.workatastartup.com/jobs",
                  timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        logger.info(f"Post-login → {page.url}")
        return True
    else:
        logger.warning("No password field — falling back to manual")
        return _manual_login_fallback(page)


def _click_submit_button(page: Page):
    """Find and click the most likely submit/continue button."""
    for sel in ['button[type="submit"]', 'input[type="submit"]']:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return
        except Exception:
            continue
    # Fallback: any button with submit-like text
    try:
        btns = page.query_selector_all('button:visible')
        for btn in btns:
            txt = (btn.inner_text() or "").strip().lower()
            if any(w in txt for w in ["continue", "log in", "sign in",
                                       "next", "submit"]):
                btn.click()
                return
    except Exception:
        pass


def _manual_login_fallback(page: Page) -> bool:
    """Show browser and wait for user to log in manually."""
    logger.warning("Waiting for manual login in the browser window...")
    print("\n" + "=" * 60)
    print("  MANUAL LOGIN NEEDED")
    print("  Please log in at: https://account.ycombinator.com/authenticate")
    print("  The bot will detect when you're done and continue.")
    print("=" * 60 + "\n")

    page.goto("https://account.ycombinator.com/authenticate",
              timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

    for _ in range(150):  # 5 min
        time.sleep(2)
        try:
            if "workatastartup.com" in page.url:
                logger.info("Manual login succeeded")
                return True
        except Exception:
            pass

    logger.error("Timed out waiting for manual login")
    return False


def _reauthenticate(page: Page, email: str, password: str):
    """Re-authenticate if an apply link bounces through YC auth.

    Delegates to the same auth-form helper used during initial login.
    """
    _complete_yc_auth(page, email, password)


def _collect_apply_hrefs(page: Page) -> list[dict]:
    """Collect all Apply button hrefs + context from the current page."""
    results = page.evaluate("""() => {
        const links = document.querySelectorAll('a');
        const items = [];
        for (const a of links) {
            const text = (a.innerText || '').trim();
            if (text.toLowerCase() !== 'apply') continue;
            const href = a.getAttribute('href') || '';
            // Walk up to find job context
            let el = a;
            let company = '', title = '';
            for (let i = 0; i < 10; i++) {
                el = el.parentElement;
                if (!el) break;
                const lines = (el.innerText || '').split('\\n').map(l => l.trim()).filter(l => l);
                if (lines.length >= 3) {
                    company = lines[0];
                    title = lines[1];
                    break;
                }
            }
            items.push({ href, company, title });
        }
        return items;
    }""")
    return results


def apply_to_waas_jobs(
    profile: dict,
    job_indices: Optional[list[int]] = None,
    max_applications: int = 20,
    dry_run: bool = False,
) -> dict:
    """Log into WAAS and apply to jobs via the visible browser.

    Opens a VISIBLE browser so user can handle CAPTCHAs.
    Collects all apply hrefs first, then visits each one.
    """
    PROOFS_SUBMITTED.mkdir(parents=True, exist_ok=True)
    PROOFS_NEEDS_HUMAN.mkdir(parents=True, exist_ok=True)

    yc_email = profile.get("yc_email", profile.get("email", ""))
    yc_username = profile.get("yc_username", yc_email)  # fallback to email
    yc_password = profile.get("yc_password", "")

    if not yc_username or not yc_password:
        return {"error": "YC credentials not found in profile.yaml",
                "applied": 0, "needs_human": 0, "errors": 1, "details": []}

    summary = {
        "total_found": 0, "applied": 0, "already_applied": 0,
        "needs_human": 0, "errors": 0, "details": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        state_path = STATE_DIR / "yc_auth_state.json"
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            storage_state=str(state_path) if state_path.exists() else None,
        )
        page = context.new_page()

        try:
            # --- Step 1: Login ---
            if not login_to_yc(page, yc_username, yc_password):
                logger.error("Failed to log into YC")
                summary["errors"] += 1
                summary["details"].append({"error": "YC login failed"})
                return summary

            context.storage_state(path=str(state_path))
            logger.info("Saved YC auth state")

            # --- Step 2: Collect jobs from multiple pages ---
            all_jobs: list[dict] = []
            seen_hrefs: set[str] = set()

            for url_label, url in [
                ("internship", "https://www.workatastartup.com/jobs?jobType=internship"),
                ("all", "https://www.workatastartup.com/jobs"),
            ]:
                logger.info(f"Scanning {url_label} jobs page...")
                page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Scroll to load
                for _ in range(25):
                    prev = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    if page.evaluate("document.body.scrollHeight") == prev:
                        break

                jobs = _collect_apply_hrefs(page)
                for j in jobs:
                    href = j.get("href", "")
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        all_jobs.append(j)

            summary["total_found"] = len(all_jobs)
            logger.info(f"Collected {len(all_jobs)} unique Apply links")

            if job_indices:
                targets = [all_jobs[i] for i in job_indices if i < len(all_jobs)]
            else:
                targets = all_jobs[:max_applications]

            # --- Step 3: Apply to each ---
            applied_count = 0
            for i, job in enumerate(targets):
                if applied_count >= max_applications:
                    break

                href = job.get("href", "")
                company = job.get("company", f"Unknown_{i}")
                title = job.get("title", f"Job_{i}")

                # Clean company name
                match = re.match(r'^(.+?)\s*\([WSFX]\d{2}\)', company)
                if match:
                    company = match.group(1).strip()

                logger.info(f"[{applied_count+1}/{max_applications}] {title} @ {company}")

                try:
                    # Navigate to the apply link
                    if href.startswith("http"):
                        page.goto(href, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                    elif href.startswith("/"):
                        page.goto(f"https://www.workatastartup.com{href}",
                                  timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                    else:
                        logger.warning(f"Skipping unknown href: {href[:100]}")
                        continue

                    page.wait_for_timeout(3000)
                    logger.info(f"  After click → URL: {page.url[:120]}")

                    # --- If we landed on YC auth, re-login to pass through ---
                    if "account.ycombinator.com" in page.url:
                        logger.info("  Hit YC auth wall — re-authenticating...")
                        _reauthenticate(page, yc_username, yc_password)
                        page.wait_for_timeout(4000)
                        logger.info(f"  After re-auth → URL: {page.url[:120]}")

                    # Handle CAPTCHA
                    if _detect_captcha(page):
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        proof = str(PROOFS_NEEDS_HUMAN / f"{safe_filename(company)}_{ts}_captcha.png")
                        page.screenshot(path=proof)
                        resolved = _wait_for_human(page, f"CAPTCHA for {title} @ {company}", proof)
                        if not resolved:
                            summary["needs_human"] += 1
                            summary["details"].append({
                                "company": company, "title": title,
                                "status": "NEEDS_HUMAN", "reason": "CAPTCHA timeout"
                            })
                            continue

                    # Check page state
                    page_text = page.inner_text("body").lower()
                    current_url = page.url

                    # Very specific "already applied" phrases only
                    already_phrases = [
                        "you've already applied",
                        "you have already applied",
                        "already submitted your application",
                        "application already submitted",
                        "you already applied",
                    ]
                    if any(phrase in page_text for phrase in already_phrases):
                        logger.info(f"Already applied: {title} @ {company}")
                        summary["already_applied"] += 1
                        summary["details"].append({
                            "company": company, "title": title, "status": "ALREADY_APPLIED"
                        })
                        continue

                    # If still stuck on the auth page, skip
                    if "account.ycombinator.com" in current_url:
                        logger.warning(f"  Still on auth page after re-auth — skipping {title}")
                        summary["errors"] += 1
                        summary["details"].append({
                            "company": company, "title": title,
                            "status": "ERROR", "error": "Auth redirect failed"
                        })
                        continue

                    # WAAS application page — check if it's the actual form
                    # or the jobs listing (one-click apply that redirected back)
                    if "workatastartup.com/application" in current_url:
                        # We're on the actual application page — submit it
                        pass  # fall through to _fill_and_submit_waas
                    elif "workatastartup.com/jobs" in current_url:
                        # Redirected back to jobs — one-click apply worked
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        prefix = safe_filename(f"{company}_{title}_{ts}")
                        proof = str(PROOFS_SUBMITTED / f"{prefix}_oneclick.png")
                        page.screenshot(path=proof)
                        logger.info(f"One-click apply: {title} @ {company}")
                        applied_count += 1
                        summary["applied"] += 1
                        summary["details"].append({
                            "company": company, "title": title,
                            "status": "SUBMITTED", "proof_path": proof,
                        })
                        continue

                    # Try to fill and submit
                    result = _fill_and_submit_waas(page, profile, company, title, dry_run)

                    if result["status"] == "SUBMITTED":
                        applied_count += 1
                        summary["applied"] += 1
                    elif result["status"] == "NEEDS_HUMAN":
                        summary["needs_human"] += 1
                    else:
                        summary["errors"] += 1

                    summary["details"].append({"company": company, "title": title, **result})

                except Exception as e:
                    logger.error(f"Error applying to {title} @ {company}: {e}")
                    summary["errors"] += 1
                    summary["details"].append({
                        "company": company, "title": title,
                        "status": "ERROR", "error": str(e)
                    })

            # Save final auth state
            context.storage_state(path=str(state_path))

        except Exception as e:
            logger.error(f"Fatal error in WAAS apply flow: {e}", exc_info=True)
            summary["errors"] += 1
        finally:
            browser.close()

    logger.info(f"WAAS Apply Summary: applied={summary['applied']}, "
                f"needs_human={summary['needs_human']}, errors={summary['errors']}")
    return summary


def _extract_job_context(apply_btn, page: Page) -> dict:
    """Try to extract company name and job title from the area near an Apply button."""
    try:
        # Walk up to find the job card container
        info = page.evaluate("""(btn) => {
            let el = btn;
            // Walk up to find a card-like container
            for (let i = 0; i < 10; i++) {
                el = el.parentElement;
                if (!el) break;
                const text = el.innerText || '';
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                if (lines.length >= 2) {
                    return { company: lines[0], title: lines[1], fullText: lines.slice(0, 5).join(' | ') };
                }
            }
            return { company: '', title: '', fullText: '' };
        }""", apply_btn)

        company = info.get("company", "")
        title = info.get("title", "")

        # Clean up: company often has batch code like "Acme (W24) •Description"
        if company:
            match = re.match(r'^(.+?)\s*\([WSFX]\d{2}\)', company)
            if match:
                company = match.group(1).strip()

        return {"company": company, "title": title}
    except Exception:
        return {"company": "", "title": ""}


def _fill_and_submit_waas(page: Page, profile: dict, company: str,
                          title: str, dry_run: bool) -> dict:
    """Fill and submit a WAAS application form."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefix = safe_filename(f"{company}_{title}_{ts}")

    result = {
        "status": "ERROR",
        "proof_path": "",
        "error": "",
    }

    try:
        # WAAS application forms typically auto-fill from YC profile
        # but may have additional fields. Let's check what's on the page.
        page.wait_for_timeout(2000)

        # Look for resume upload
        file_inputs = page.query_selector_all('input[type="file"]')
        if file_inputs:
            # Upload resume - pick based on role classification
            from src.utils.role_classify import classify_role, get_resume_path
            role_family = classify_role(title, "")
            resume_path = get_resume_path(role_family or "fullstack")
            abs_resume = PROJECT_ROOT / resume_path

            if abs_resume.exists() and abs_resume.stat().st_size > 0:
                for fi in file_inputs:
                    try:
                        fi.set_input_files(str(abs_resume))
                        logger.info(f"Uploaded resume: {resume_path}")
                        break
                    except Exception:
                        continue

        # Fill any text fields that match profile data
        _fill_form_fields(page, profile)

        # Take pre-submit screenshot
        proof_path = str(PROOFS_SUBMITTED / f"{prefix}_pre.png")
        page.screenshot(path=proof_path, full_page=True)
        result["proof_path"] = proof_path

        if dry_run:
            logger.info(f"DRY RUN: Would submit application for {title} at {company}")
            result["status"] = "SUBMITTED"
            result["proof_path"] = proof_path
            return result

        # Find and click submit
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            'button:has-text("Send")',
            'input[type="submit"]',
        ]

        submitted = False
        for sel in submit_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info(f"Clicked submit: {sel}")
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            result["error"] = "Could not find submit button"
            result["status"] = "ERROR"
            return result

        page.wait_for_timeout(3000)

        # Handle post-submit CAPTCHA
        if _detect_captcha(page):
            captcha_proof = str(PROOFS_NEEDS_HUMAN / f"{prefix}_captcha.png")
            page.screenshot(path=captcha_proof)
            resolved = _wait_for_human(page, f"Post-submit CAPTCHA for {title}", captcha_proof)
            if not resolved:
                result["status"] = "NEEDS_HUMAN"
                result["proof_path"] = captcha_proof
                return result

        # Capture confirmation
        final_proof = str(PROOFS_SUBMITTED / f"{prefix}.png")
        page.screenshot(path=final_proof, full_page=True)
        result["proof_path"] = final_proof
        result["status"] = "SUBMITTED"
        logger.info(f"Successfully applied to {title} at {company}")

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        logger.error(f"Error submitting application: {e}")

    return result


def _fill_form_fields(page: Page, profile: dict):
    """Fill common form fields from profile data."""
    field_map = {
        "first_name": ['input[name*="first"]', '#first_name'],
        "last_name": ['input[name*="last"]', '#last_name'],
        "email": ['input[name*="email"]', 'input[type="email"]'],
        "phone": ['input[name*="phone"]', 'input[type="tel"]'],
        "linkedin": ['input[name*="linkedin"]', 'input[placeholder*="linkedin"]'],
        "github": ['input[name*="github"]', 'input[placeholder*="github"]'],
    }

    for key, selectors in field_map.items():
        value = profile.get(key, "")
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    current = el.input_value()
                    if not current:  # Only fill if empty (don't overwrite auto-filled)
                        el.fill(str(value))
                    break
            except Exception:
                continue
