"""Discover jobs from YC internship pages and find their Greenhouse postings.

Approach:
1. Scrape WAAS (Work at a Startup) for company names and job titles
2. For each company, check if they have a Greenhouse job board
3. If yes, extract all matching job URLs from their Greenhouse board
"""

import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from src.utils.logging import setup_logging
from src.utils.sanitize import (
    extract_company_from_greenhouse_url,
    is_greenhouse_url,
    normalize_greenhouse_url,
    sanitize_text,
)

logger = setup_logging("jobbot.yc.discover")

# WAAS pages for discovering companies/jobs
WAAS_URLS = [
    "https://www.workatastartup.com/jobs?jobType=internship",
]

PAGE_TIMEOUT = 45000
REQUEST_TIMEOUT = 15

# Common slug transformations: company name -> greenhouse slug
SLUG_TRANSFORMS = [
    lambda s: s,                          # as-is
    lambda s: s.replace(" ", ""),          # no spaces
    lambda s: s.replace(" ", "-"),         # hyphenated
    lambda s: s.replace(" ", "").lower(),  # lowered no spaces
    lambda s: re.sub(r'[^a-z0-9]', '', s.lower()),  # alphanumeric only
]


def discover_greenhouse_jobs(sources: Optional[list[str]] = None) -> list[dict]:
    """Discover YC internship companies, then find their Greenhouse job postings.

    Returns list of dicts with keys:
        - job_url: normalized Greenhouse URL
        - company: company name
        - title: job title (if extractable from Greenhouse board)
        - source_url: the WAAS page it came from
    """
    # Step 1: Get companies + roles from WAAS
    waas_jobs = _scrape_waas_jobs(sources or WAAS_URLS)
    logger.info(f"Found {len(waas_jobs)} jobs from WAAS")

    # Step 2: Get unique companies
    companies = {}
    for job in waas_jobs:
        cname = job["company"]
        if cname and cname not in companies:
            companies[cname] = job

    logger.info(f"Found {len(companies)} unique companies from WAAS")

    # Step 3: For each company, try to find their Greenhouse board
    all_greenhouse_jobs = []
    seen_urls = set()

    for company_name, sample_job in companies.items():
        gh_jobs = _find_greenhouse_jobs(company_name)
        for job in gh_jobs:
            norm_url = normalize_greenhouse_url(job["job_url"])
            if norm_url not in seen_urls:
                seen_urls.add(norm_url)
                job["job_url"] = norm_url
                job["company"] = company_name
                job["source_url"] = sample_job.get("source_url", "WAAS")
                all_greenhouse_jobs.append(job)

    logger.info(f"Discovered {len(all_greenhouse_jobs)} Greenhouse jobs from {len(companies)} YC companies")
    return all_greenhouse_jobs


def _scrape_waas_jobs(urls: list[str]) -> list[dict]:
    """Scrape WAAS for company names, job titles, and metadata."""
    all_jobs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        for url in urls:
            logger.info(f"Scraping WAAS: {url}")
            try:
                page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Scroll to load more jobs
                for _ in range(15):
                    prev_h = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)
                    if page.evaluate("document.body.scrollHeight") == prev_h:
                        break

                # Parse the rendered text
                text = page.inner_text("body")
                jobs = _parse_waas_text(text, url)
                all_jobs.extend(jobs)

            except Exception as e:
                logger.warning(f"Failed to scrape WAAS {url}: {e}")

        browser.close()

    return all_jobs


def _parse_waas_text(text: str, source_url: str) -> list[dict]:
    """Parse the visible text of a WAAS page into structured job data.

    WAAS renders jobs in a pattern like:
        CompanyName (BatchCode) •Description(time ago)
        Job Title
        TypeLocationCategory
        Apply
    """
    jobs = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    current_company = ""
    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect company lines: usually contain batch code like (W24), (S23), (F25)
        batch_match = re.search(r'\(([WSFX]\d{2})\)', line)
        if batch_match:
            # Extract company name (everything before the batch code)
            company_part = line[:batch_match.start()].strip()
            # Clean up: remove trailing dots, bullets
            company_part = re.sub(r'[\s•·]+$', '', company_part)
            if company_part:
                current_company = company_part

        # Detect job title lines: they follow company lines and don't look like metadata
        elif current_company and line not in ("Apply", "Log In ›", "Create Profile ›"):
            # Skip navigation/filter lines
            skip_patterns = [
                r'^Jobs by', r'^Engineer', r'^Design', r'^Recruit', r'^Science',
                r'^Product$', r'^Operations', r'^Sales', r'^Marketing', r'^Legal',
                r'^Finance', r'^Work at', r'^Startup Jobs', r'^Internships',
                r'^Upcoming', r'^How it', r'^Find open', r'^And creat',
                r'^Fulltime', r'^Intern', r'^Remote', r'^Contract',
            ]
            is_skip = any(re.match(pat, line) for pat in skip_patterns)

            # Job type/location/category line
            is_metadata = bool(re.match(
                r'^(?:Fulltime|Intern|Contract|Remote)',
                line
            ))

            if not is_skip and not is_metadata and len(line) > 3 and len(line) < 200:
                # This looks like a job title
                title = line.strip()
                # Check next line for metadata (Intern/Fulltime/location)
                job_type = ""
                location = ""
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    if re.match(r'^(?:Fulltime|Intern|Contract)', next_line):
                        job_type = "Intern" if "Intern" in next_line else "Fulltime"
                        location = next_line
                        i += 1  # skip metadata line

                jobs.append({
                    "company": current_company,
                    "title": title,
                    "job_type": job_type,
                    "location": location,
                    "source_url": source_url,
                })

        i += 1

    return jobs


def _find_greenhouse_jobs(company_name: str) -> list[dict]:
    """Try to find a company's Greenhouse job board and extract job URLs."""
    jobs = []

    # Try different slug formats for the company
    for transform in SLUG_TRANSFORMS:
        slug = transform(company_name)
        if not slug:
            continue

        board_url = f"https://boards.greenhouse.io/{slug}"
        try:
            resp = requests.get(
                board_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200 and "greenhouse" in resp.url.lower():
                # Found a valid Greenhouse board!
                logger.info(f"Found Greenhouse board for {company_name}: {board_url}")
                board_jobs = _parse_greenhouse_board(resp.text, resp.url, company_name)
                return board_jobs
        except requests.RequestException:
            continue

    return jobs


def _parse_greenhouse_board(html: str, board_url: str, company: str) -> list[dict]:
    """Parse a Greenhouse job board page for individual job listings."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []

    # Greenhouse boards list jobs as links
    # Common patterns: /jobs/{id}, /embed/job_app?token=xxx
    job_links = soup.find_all("a", href=True)
    for link in job_links:
        href = link["href"]
        full_url = urljoin(board_url, href)

        # Only keep links to individual jobs
        if "/jobs/" not in full_url.lower():
            continue
        if not is_greenhouse_url(full_url):
            continue

        title = sanitize_text(link.get_text())
        if not title or title in ("Apply", "Back", "View"):
            continue

        # Try to get location from sibling elements
        location = ""
        parent = link.parent
        if parent:
            loc_el = parent.find(["span", "div"], class_=re.compile(r"location", re.I))
            if loc_el:
                location = sanitize_text(loc_el.get_text())

        jobs.append({
            "job_url": full_url,
            "company": company,
            "title": title,
            "location": location,
        })

    return jobs
