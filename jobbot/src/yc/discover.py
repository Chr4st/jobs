"""Discover jobs from YC internship pages and extract Greenhouse URLs."""

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.utils.logging import setup_logging
from src.utils.sanitize import is_greenhouse_url, normalize_greenhouse_url, sanitize_text

logger = setup_logging("jobbot.yc.discover")

# YC pages to scrape
YC_SOURCES = [
    "https://www.workatastartup.com/internships",
    "https://www.ycombinator.com/jobs/role/internship",
]

REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def discover_greenhouse_jobs(sources: Optional[list[str]] = None) -> list[dict]:
    """Scrape YC internship pages and return Greenhouse job listings.

    Returns list of dicts with keys:
        - job_url: normalized Greenhouse URL
        - company: company name (if extractable)
        - title: job title (if extractable)
        - source_url: the YC page it came from
    """
    urls = sources or YC_SOURCES
    all_jobs = []
    seen_urls = set()

    for source_url in urls:
        logger.info(f"Scraping YC source: {source_url}")
        try:
            jobs = _scrape_source(source_url)
            for job in jobs:
                norm_url = normalize_greenhouse_url(job["job_url"])
                if norm_url not in seen_urls:
                    seen_urls.add(norm_url)
                    job["job_url"] = norm_url
                    all_jobs.append(job)
        except Exception as e:
            logger.warning(f"Failed to scrape {source_url}: {e}. Continuing.")
            continue

    logger.info(f"Discovered {len(all_jobs)} unique Greenhouse jobs from YC pages")
    return all_jobs


def _scrape_source(url: str) -> list[dict]:
    """Scrape a single YC page for Greenhouse job links."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []

    # Strategy 1: Find all links and filter to Greenhouse
    all_links = soup.find_all("a", href=True)
    for link in all_links:
        href = link["href"]
        # Resolve relative URLs
        full_url = urljoin(url, href)

        if not is_greenhouse_url(full_url):
            continue

        # Try to extract context from surrounding elements
        company, title = _extract_context(link, soup)

        jobs.append({
            "job_url": full_url,
            "company": company,
            "title": title,
            "source_url": url,
        })

    # Strategy 2: Look for embedded job data in script tags (WAAS uses JSON)
    script_jobs = _extract_from_scripts(soup, url)
    for job in script_jobs:
        if job["job_url"] not in {j["job_url"] for j in jobs}:
            jobs.append(job)

    return jobs


def _extract_context(link_tag, soup) -> tuple[str, str]:
    """Try to extract company name and job title from link context."""
    company = ""
    title = ""

    # Check link text
    link_text = sanitize_text(link_tag.get_text())
    if link_text:
        title = link_text

    # Look for company in parent elements
    parent = link_tag.parent
    for _ in range(5):
        if parent is None:
            break

        # Look for company name patterns
        company_el = parent.find(
            ["h2", "h3", "h4", "span", "div"],
            class_=re.compile(r"company|org|name", re.IGNORECASE)
        )
        if company_el:
            company = sanitize_text(company_el.get_text())
            break

        # Check data attributes
        if parent.get("data-company"):
            company = parent["data-company"]
            break

        parent = parent.parent

    return company, title


def _extract_from_scripts(soup, source_url: str) -> list[dict]:
    """Extract job data from embedded JSON in script tags."""
    jobs = []
    scripts = soup.find_all("script")

    for script in scripts:
        text = script.string or ""
        # Look for Greenhouse URLs in JSON data
        gh_urls = re.findall(
            r'(https?://(?:boards|jobs)\.greenhouse\.io/[\w/]+)',
            text
        )
        for gh_url in gh_urls:
            jobs.append({
                "job_url": gh_url,
                "company": "",
                "title": "",
                "source_url": source_url,
            })

        # Look for structured job data
        # WAAS often embeds company+job data as JSON
        company_matches = re.findall(r'"company_?[Nn]ame"\s*:\s*"([^"]+)"', text)
        title_matches = re.findall(r'"(?:title|job_?[Tt]itle)"\s*:\s*"([^"]+)"', text)
        url_matches = re.findall(
            r'"(?:url|apply_?[Uu]rl|job_?[Uu]rl)"\s*:\s*"([^"]*greenhouse[^"]*)"',
            text
        )

        for i, url in enumerate(url_matches):
            if is_greenhouse_url(url):
                jobs.append({
                    "job_url": url,
                    "company": company_matches[i] if i < len(company_matches) else "",
                    "title": title_matches[i] if i < len(title_matches) else "",
                    "source_url": source_url,
                })

    return jobs
