"""Discover jobs from Greenhouse boards for big tech and quant companies.

Approach:
1. Query Greenhouse JSON API for known company boards
2. Filter for engineering intern roles
3. Sort by updated_at to get newest postings first
"""

import re
from datetime import datetime, timezone
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

logger = setup_logging("jobbot.discover")

# WAAS pages (legacy, kept for reference)
WAAS_URLS = [
    "https://www.workatastartup.com/jobs?jobType=internship",
]

PAGE_TIMEOUT = 45000
REQUEST_TIMEOUT = 10

# Common slug transformations: company name -> greenhouse slug
SLUG_TRANSFORMS = [
    lambda s: s,
    lambda s: s.lower(),
    lambda s: s.replace(" ", ""),
    lambda s: s.replace(" ", "-"),
    lambda s: s.replace(" ", "").lower(),
    lambda s: s.replace(" ", "-").lower(),
    lambda s: re.sub(r'[^a-z0-9]', '', s.lower()),
    lambda s: re.sub(r'[^a-z0-9-]', '', s.lower().replace(" ", "-")),
    lambda s: s.split()[0].lower() if " " in s else s.lower(),
]

# ── Big Tech / Quant / AI company → Greenhouse slug ──────────────────────
BIG_TECH_GREENHOUSE_SLUGS: dict[str, str] = {
    # Big Tech / Growth
    "Stripe": "stripe",
    "Coinbase": "coinbase",
    "Ramp": "ramp",
    "Brex": "brex",
    "Plaid": "plaid",
    "Robinhood": "robinhood",
    "Discord": "discord",
    "Reddit": "reddit",
    "Pinterest": "pinterestcareers",
    "Snap": "snap",
    "Databricks": "databricks",
    "Snowflake": "snowflakecomputing",
    "Palantir": "palantir",
    "Scale AI": "scaleai",
    "Notion": "notion",
    "Figma": "figma",
    "Verkada": "verkada",
    "Cloudflare": "cloudflare",
    "Toast": "toast",
    "Airtable": "airtable",
    "Cockroach Labs": "cockroachlabs",
    "HashiCorp": "hashicorp",
    "Datadog": "datadog",
    "MongoDB": "mongodb",
    "Elastic": "elastic",
    "Confluent": "confluent",
    "Twilio": "twilio",
    "Square": "squareup",
    "Chime": "chime",
    "Affirm": "affirm",
    "SoFi": "sofi",
    "Nuro": "nuro",
    "Waymo": "waymo",
    "Aurora": "aaborainnovation",
    "Anduril": "anduril",
    "Shield AI": "shieldai",
    "SpaceX": "spacex",
    "Rippling": "rippling",
    "Mercury": "mercury",
    "Benchling": "benchling",
    # Quant / Trading
    "Two Sigma": "twosigma",
    "Citadel": "citadel",
    "Jane Street": "janestreet",
    "DE Shaw": "deshaw",
    "HRT": "hudsonrivertrading",
    "Jump Trading": "jumptrading",
    "DRW": "drweng",
    "Virtu Financial": "virtu",
    "IMC Trading": "imc",
    "Optiver": "optiver",
    "SIG": "sigcareers",
    "Akuna Capital": "akunacapital",
    "Five Rings": "fiveringsllc",
    "Tower Research": "towerresearchcapital",
    "Millennium": "millennium",
    "Point72": "point72",
    "Bridgewater": "bridgewater",
    "AQR": "aqr",
    "Voleon": "voleon",
    "Headlands Tech": "headlandstech",
    # AI / ML
    "OpenAI": "openai",
    "Anthropic": "anthropic",
    "Cohere": "cohere",
    "DeepMind": "deepmind",
    "Hugging Face": "huggingface",
    "Weights & Biases": "wandb",
    "Runway": "runwayml",
    "Stability AI": "stabilityai",
    "Character AI": "character",
    "Perplexity": "perplexityai",
    "ElevenLabs": "elevenlabs",
    "Mistral": "mistral",
    "Adept AI": "adeptailabs",
}

# Legacy YC slug overrides (kept for backward compat)
KNOWN_GREENHOUSE_SLUGS: dict[str, str] = {
    "Hive": "hive",
    "Verkada": "verkada",
    "Scale AI": "scaleai",
    "Scale": "scaleai",
    "Retool": "retool",
    "Brex": "brex",
    "Faire": "faire",
    "Ramp": "ramp",
    "Replit": "replit",
    "Loom": "loom",
    "Stripe": "stripe",
    "Airbnb": "airbnb",
    "Coinbase": "coinbase",
    "DoorDash": "doordash",
    "Gusto": "gusto",
    "Instacart": "instacart",
    "Flexport": "flexport",
    "Notion": "notion",
    "Snorkel AI": "snorkelai",
    "Weights & Biases": "wandb",
    "Hugging Face": "huggingface",
    "Pave": "pave",
    "Stytch": "stytch",
    "Ashby": "ashbyhq",
    "Census": "getcensus",
    "Rootly": "rootly",
    "Vanta": "vanta",
    "Whatnot": "whatnot",
    "PostHog": "posthog",
    "Cal.com": "calcom",
    "Mixpanel": "mixpanel",
    "Amplitude": "amplitude",
    "GitLab": "gitlab",
    "Databricks": "databricks",
    "Anyscale": "anyscale",
    "Modal": "modal-labs",
    "Render": "render",
    "Railway": "railway",
    "Supabase": "supabase",
    "PlanetScale": "planetscale",
    "Neon": "neondatabase",
    "Vercel": "vercel",
    "Temporal": "temporal",
    "Fly.io": "fly",
    "Materialize": "materialize",
    "Cohere": "cohere",
    "Anthropic": "anthropic",
    "Deepgram": "deepgram",
    "AssemblyAI": "assemblyai",
    "ElevenLabs": "elevenlabs",
    "Labelbox": "labelbox",
    "Airbyte": "airbyte",
    "dbt Labs": "dbtlabsinc",
    "Fivetran": "fivetran",
    "Monte Carlo": "montecarlodata",
    "Hex": "hex",
    "Observable": "observable",
    "Sourcegraph": "sourcegraph",
    "Linear": "linear",
    "Runway": "runwayml",
    "Luma AI": "lumalabs",
    "Stability AI": "stabilityai",
    "Character.AI": "character",
    "Perplexity": "perplexityai",
    "Glean": "glaboratories",
    "Deel": "deel",
    "Remote": "remotecom",
    "Lattice": "lattice",
    "Rippling": "rippling",
    "Mercury": "mercury",
    "Benchling": "benchling",
}


import re

_INTERN_RE = re.compile(r'\b(intern|internship|co-op)\b', re.IGNORECASE)
_ENG_KW = [
    "engineer", "developer", "software", "swe", "ml", "ai", "data", "quant",
    "trading", "fullstack", "full-stack", "full stack", "backend", "frontend",
    "research", "infrastructure", "platform", "systems", "security",
    "cloud", "mobile", "applied scientist",
]


def discover_greenhouse_direct(max_jobs: int = 40) -> list[dict]:
    """Scan big tech / quant Greenhouse boards for engineering intern roles.

    Queries the Greenhouse JSON API for each company in BIG_TECH_GREENHOUSE_SLUGS,
    filters for intern + engineering roles, and sorts by updated_at (newest first).

    Returns list of dicts:
        - job_url, company, title, location, updated_at, source_url
    """
    found: list[dict] = []

    for company, slug in BIG_TECH_GREENHOUSE_SLUGS.items():
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            resp = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            jobs = resp.json().get("jobs", [])
        except Exception:
            continue

        for j in jobs:
            title = j.get("title", "")
            t_lower = title.lower()
            is_intern = bool(_INTERN_RE.search(t_lower))
            is_eng = any(k in t_lower for k in _ENG_KW)
            if not (is_intern and is_eng):
                continue

            url = j.get("absolute_url", "")
            if not url:
                continue

            location = j.get("location", {}).get("name", "")
            updated = j.get("updated_at", "")

            found.append({
                "job_url": url,
                "company": company,
                "title": title,
                "location": location,
                "updated_at": updated,
                "description": "",
                "source_url": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            })
            logger.info(f"  ✓ {company}: {title} [{location}]")

    # Sort by updated_at descending (newest first)
    def _sort_key(j):
        try:
            return datetime.fromisoformat(j["updated_at"].replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    found.sort(key=_sort_key, reverse=True)

    logger.info(f"=== Found {len(found)} engineering intern roles across {len(BIG_TECH_GREENHOUSE_SLUGS)} companies ===")

    if max_jobs and len(found) > max_jobs:
        found = found[:max_jobs]
        logger.info(f"Trimmed to {max_jobs} newest jobs")

    return found


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
    # Build list of slugs to try: known overrides first, then generated
    slugs_to_try = []

    # Check known overrides first
    if company_name in KNOWN_GREENHOUSE_SLUGS:
        slugs_to_try.append(KNOWN_GREENHOUSE_SLUGS[company_name])

    # Add generated slugs
    seen_slugs = set(slugs_to_try)
    for transform in SLUG_TRANSFORMS:
        slug = transform(company_name)
        if slug and slug not in seen_slugs:
            seen_slugs.add(slug)
            slugs_to_try.append(slug)

    for slug in slugs_to_try:
        # Try JSON API first (faster, more reliable)
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            resp = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("jobs"):
                    logger.info(f"Found Greenhouse board for {company_name} via API: {slug} ({len(data['jobs'])} jobs)")
                    jobs = []
                    for j in data["jobs"]:
                        job_url = j.get("absolute_url", "")
                        title = j.get("title", "")
                        location = j.get("location", {}).get("name", "")
                        if job_url and title:
                            jobs.append({
                                "job_url": job_url,
                                "title": title,
                                "location": location,
                            })
                    return jobs
        except (requests.RequestException, ValueError):
            pass

        # Fallback: try HTML board
        board_url = f"https://boards.greenhouse.io/{slug}"
        try:
            resp = requests.get(
                board_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200 and "greenhouse" in resp.url.lower():
                logger.info(f"Found Greenhouse board for {company_name}: {board_url}")
                board_jobs = _parse_greenhouse_board(resp.text, resp.url, company_name)
                if board_jobs:
                    return board_jobs
        except requests.RequestException:
            continue

    return []


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
