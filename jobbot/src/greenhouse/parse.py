"""Parse Greenhouse job posting pages for structured data."""

import json
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.utils.logging import setup_logging
from src.utils.sanitize import (
    extract_company_from_greenhouse_url,
    sanitize_text,
)

logger = setup_logging("jobbot.greenhouse.parse")

REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_greenhouse_job(url: str) -> Optional[dict]:
    """Fetch and parse a Greenhouse job posting.

    Returns dict with keys:
        - title: job title
        - company: company name
        - location: location string
        - description: plain text description
        - department: department (if available)
        - apply_url: URL for the application form
        - raw_html: raw HTML of the job page
    """
    logger.info(f"Parsing Greenhouse job: {url}")

    try:
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try structured data first (JSON-LD)
    result = _parse_json_ld(soup)
    if not result:
        # Fall back to HTML parsing
        result = _parse_html(soup, url)

    if result:
        result["raw_html"] = resp.text
        # Derive apply URL
        result["apply_url"] = _find_apply_url(soup, url)
        # Fill in company from URL if not found
        if not result.get("company"):
            result["company"] = extract_company_from_greenhouse_url(url) or ""

    return result


def _parse_json_ld(soup) -> Optional[dict]:
    """Extract job data from JSON-LD structured data."""
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "JobPosting":
                location = ""
                loc_data = data.get("jobLocation", {})
                if isinstance(loc_data, dict):
                    addr = loc_data.get("address", {})
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                            addr.get("addressCountry", ""),
                        ]
                        location = ", ".join(p for p in parts if p)
                elif isinstance(loc_data, list) and loc_data:
                    addr = loc_data[0].get("address", {})
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                        ]
                        location = ", ".join(p for p in parts if p)

                return {
                    "title": data.get("title", ""),
                    "company": data.get("hiringOrganization", {}).get("name", ""),
                    "location": location,
                    "description": sanitize_text(data.get("description", "")),
                    "department": "",
                }
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _parse_html(soup, url: str) -> Optional[dict]:
    """Parse Greenhouse job page HTML directly."""
    title = ""
    company = ""
    location = ""
    description = ""
    department = ""

    # Title: Greenhouse uses various selectors
    title_selectors = [
        "h1.app-title",
        "h1.heading",
        ".job-title",
        "h1",
    ]
    for sel in title_selectors:
        el = soup.select_one(sel)
        if el:
            title = sanitize_text(el.get_text())
            break

    # Company
    company_selectors = [
        ".company-name",
        'span[class*="company"]',
        'meta[property="og:site_name"]',
    ]
    for sel in company_selectors:
        el = soup.select_one(sel)
        if el:
            company = el.get("content", "") or sanitize_text(el.get_text())
            break

    # Location
    location_selectors = [
        ".location",
        'div[class*="location"]',
        ".job-location",
    ]
    for sel in location_selectors:
        el = soup.select_one(sel)
        if el:
            location = sanitize_text(el.get_text())
            break

    # Department
    dept_selectors = [
        ".department",
        'div[class*="department"]',
    ]
    for sel in dept_selectors:
        el = soup.select_one(sel)
        if el:
            department = sanitize_text(el.get_text())
            break

    # Description
    desc_selectors = [
        "#content",
        ".content",
        'div[class*="description"]',
        ".job-description",
        "#job-description",
    ]
    for sel in desc_selectors:
        el = soup.select_one(sel)
        if el:
            description = sanitize_text(el.get_text())
            break

    if not title:
        logger.warning(f"Could not extract title from {url}")
        return None

    return {
        "title": title,
        "company": company or extract_company_from_greenhouse_url(url) or "",
        "location": location,
        "description": description,
        "department": department,
    }


def _find_apply_url(soup, page_url: str) -> str:
    """Find the application form URL on the page."""
    # Look for "Apply" buttons/links
    apply_selectors = [
        'a[href*="application"]',
        'a[class*="apply"]',
        'a[id*="apply"]',
        'button[class*="apply"]',
    ]
    for sel in apply_selectors:
        el = soup.select_one(sel)
        if el and el.get("href"):
            href = el["href"]
            if href.startswith("/"):
                parsed = urlparse(page_url)
                return f"{parsed.scheme}://{parsed.netloc}{href}"
            return href

    # Greenhouse pattern: append /application to job URL
    if "/jobs/" in page_url:
        return page_url.rstrip("/") + "#application"

    return page_url
