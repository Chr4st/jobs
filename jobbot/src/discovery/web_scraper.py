"""Dynamic internship discovery by scraping community-maintained GitHub README tables.

Sources (2026/2027 cycle only):
  - SimplifyJobs/Summer2026-Internships  (HTML <table> format, 200+ GH links)
  - vanshb03/Summer2026-Internships      (markdown pipe format, 30+ GH links)

Supports two README formats automatically:
  - Markdown pipe tables: | Company | Role | ... |
  - HTML tables:          <table><tr><td>...</td></tr></table>

Filters applied per-role:
  - 🔒 closed listings
  - 🇺🇸 US-citizen/clearance-only roles  (Christ is not a US citizen)
  - 🛂 no-sponsorship roles              (Christ requires sponsorship)
  - 🎓 advanced-degree-required roles    (undergrad applicant)
  - PhD regex filter (from scanner)
  - NA/EU location filter (from scanner)
"""
from __future__ import annotations

import re
from typing import Callable, Iterator

import requests

from src.utils.logging import setup_logging
from src.utils.role_classify import classify_role

logger = setup_logging("jobbot.discovery.web_scraper")

REQUEST_TIMEOUT = 20

# 2026/2027 cycle sources only — no archived or past-cycle repos
_SOURCES = [
    {
        "name": "SimplifyJobs/Summer2026-Internships",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
        "format": "html",
    },
    {
        "name": "vanshb03/Summer2026-Internships",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/main/README.md",
        "format": "markdown",
    },
]

# Matches both Greenhouse URL formats
_GH_URL_RE = re.compile(
    r"https?://(?:job-boards|boards)\.greenhouse\.io/([^/\"'\s?&]+)/jobs/(\d+)",
    re.IGNORECASE,
)

# ── Exclusion markers ─────────────────────────────────────────────────────────

# Closed/removed listings
_CLOSED_RE = re.compile(r"~~|🔒|\[closed\]|no longer accepting", re.IGNORECASE)

# US-citizen / security clearance only — Christ is not a US citizen
_US_ONLY_RE = re.compile(
    r"🇺🇸|us\s+citizens?\s+only|clearance\s+required|security\s+clearance|"
    r"must\s+be\s+a\s+us\s+citizen|itar|secret\s+clearance|top\s+secret",
    re.IGNORECASE,
)

# No sponsorship offered — Christ requires visa sponsorship
# 🛂 is the Simplify convention for "does NOT offer sponsorship"
_NO_SPONSORSHIP_RE = re.compile(r"🛂|does not offer sponsorship|no sponsorship", re.IGNORECASE)

# Advanced degree required — undergrad applicant
_ADVANCED_DEGREE_RE = re.compile(r"🎓|advanced degree|master'?s? required|phd required", re.IGNORECASE)

# ── Text utilities ─────────────────────────────────────────────────────────────

def _strip(text: str) -> str:
    """Remove HTML tags, markdown links, bold/italic markers, and whitespace."""
    text = re.sub(r"<[^>]+>", "", text)                   # HTML tags
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url)
    text = re.sub(r"[*_`~]+", "", text)                    # formatting chars
    return text.strip()


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href=["\']([^"\']+)["\']', html)


def _is_excluded(role_text: str, link_text: str = "") -> bool:
    """Return True if the role should be skipped based on emoji/text markers."""
    combined = role_text + " " + link_text
    return (
        bool(_CLOSED_RE.search(combined))
        or bool(_US_ONLY_RE.search(combined))
        or bool(_NO_SPONSORSHIP_RE.search(combined))
        or bool(_ADVANCED_DEGREE_RE.search(combined))
    )


# ── Markdown pipe table parser ────────────────────────────────────────────────

_PIPE_ROW_RE = re.compile(r"^\|(.+)\|$")
_SEP_RE      = re.compile(r"^[\s|:\-]+$")
_CONT_RE     = re.compile(r"^[↳⬆⬇→]$|^\s*$")


def _parse_markdown(content: str, source_name: str) -> Iterator[dict]:
    """Parse markdown pipe tables."""
    cols = {"company": 0, "role": 1, "location": 2, "link": 3}
    in_table = header_done = False
    last_company = ""

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                in_table = header_done = False
            continue

        m = _PIPE_ROW_RE.match(stripped)
        if not m:
            continue

        cells = [c.strip() for c in m.group(1).split("|")]

        if all(_SEP_RE.match(c) for c in cells if c):
            header_done = in_table = True
            continue

        if not header_done:
            if any("company" in c.lower() for c in cells):
                for i, c in enumerate(cells):
                    cl = c.lower()
                    if "company" in cl:                     cols["company"] = i
                    elif any(k in cl for k in ("role", "position", "title")): cols["role"] = i
                    elif "location" in cl:                  cols["location"] = i
                    elif any(k in cl for k in ("link", "application", "apply")): cols["link"] = i
            continue

        if not in_table or len(cells) <= max(cols.values()):
            continue

        raw_company = cells[cols["company"]]
        raw_role    = cells[cols["role"]]
        raw_loc     = cells[cols["location"]]
        raw_link    = cells[cols["link"]]

        company_text = _strip(raw_company)
        if _CONT_RE.match(company_text) or not company_text:
            company = last_company
        else:
            company = company_text
            last_company = company

        if not company or _is_excluded(raw_role, raw_link):
            continue

        title    = _strip(raw_role)
        location = _strip(raw_loc)

        for href in _hrefs(raw_link):
            gh = _GH_URL_RE.search(href)
            if not gh:
                continue
            yield {
                "company": company, "title": title, "location": location,
                "job_url": href,
                "clean_url": f"https://job-boards.greenhouse.io/{gh.group(1)}/jobs/{gh.group(2)}",
                "slug": gh.group(1), "job_id": gh.group(2), "source": source_name,
            }


# ── HTML table parser ─────────────────────────────────────────────────────────

# Match a full <tr>...</tr> block (may span multiple lines)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
# Match individual <td>...</td> cells
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
# Match <th>...</th> header cells
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.DOTALL | re.IGNORECASE)


def _parse_html(content: str, source_name: str) -> Iterator[dict]:
    """Parse HTML <table> format (SimplifyJobs style)."""
    col_idx = {"company": 0, "role": 1, "location": 2, "link": 3}
    header_parsed = False
    last_company = ""

    for tr_match in _TR_RE.finditer(content):
        tr = tr_match.group(1)

        # Header row
        ths = _TH_RE.findall(tr)
        if ths and not header_parsed:
            for i, th in enumerate(ths):
                tl = _strip(th).lower()
                if "company" in tl:                          col_idx["company"] = i
                elif any(k in tl for k in ("role", "position", "title")): col_idx["role"] = i
                elif "location" in tl:                       col_idx["location"] = i
                elif any(k in tl for k in ("application", "link", "apply")): col_idx["link"] = i
            header_parsed = True
            continue

        tds = _TD_RE.findall(tr)
        if not tds or len(tds) <= max(col_idx.values()):
            continue

        raw_company = tds[col_idx["company"]]
        raw_role    = tds[col_idx["role"]]
        raw_loc     = tds[col_idx["location"]]
        raw_link    = tds[col_idx["link"]]

        if _is_excluded(raw_role, raw_link):
            continue

        # ↳ continuation rows reuse the previous company (same as markdown format)
        company_text = _strip(raw_company)
        if _CONT_RE.match(company_text) or not company_text:
            company = last_company
        else:
            company = company_text
            last_company = company

        title    = _strip(raw_role)
        location = _strip(raw_loc)

        if not company or not title:
            continue

        for href in _hrefs(raw_link):
            gh = _GH_URL_RE.search(href)
            if not gh:
                continue
            yield {
                "company": company, "title": title, "location": location,
                "job_url": href,
                "clean_url": f"https://job-boards.greenhouse.io/{gh.group(1)}/jobs/{gh.group(2)}",
                "slug": gh.group(1), "job_id": gh.group(2), "source": source_name,
            }


# ── Public API ────────────────────────────────────────────────────────────────

def _parse_readme(content: str, source: dict) -> Iterator[dict]:
    fmt = source.get("format", "markdown")
    if fmt == "html":
        yield from _parse_html(content, source["name"])
    else:
        yield from _parse_markdown(content, source["name"])


def scrape_greenhouse_internships(
    extra_sources: list[dict] | None = None,
    intern_re: re.Pattern | None = None,
    eng_keywords: list[str] | None = None,
    phd_re: re.Pattern | None = None,
    location_eligible_fn: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Fetch and parse 2026-cycle internship repos to discover Greenhouse roles.

    Exclusion filters applied at parse time (before caller filters):
      - 🔒 closed, 🇺🇸 US-only, 🛂 no-sponsorship, 🎓 advanced-degree

    Args:
        extra_sources: additional {"name", "url", "format"} source dicts
        intern_re: compiled pattern — role title must match to be included
        eng_keywords: lowercase strings — title must contain at least one
        phd_re: compiled pattern — roles matching this are excluded
        location_eligible_fn: callable(location) -> bool

    Returns:
        List of dicts: {company, title, location, job_url, clean_url, slug, job_id, role_family, source}
    """
    sources = list(_SOURCES) + (extra_sources or [])
    seen: set[str] = set()
    results: list[dict] = []

    for src in sources:
        logger.info(f"Scraping {src['name']} ({src.get('format', 'markdown')}) ...")
        try:
            resp = requests.get(
                src["url"],
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(f"  {src['name']}: HTTP {resp.status_code}")
                continue
            content = resp.text
        except Exception as e:
            logger.warning(f"  {src['name']}: request error: {e}")
            continue

        hits = 0
        for job in _parse_readme(content, src):
            if job["clean_url"] in seen:
                continue
            seen.add(job["clean_url"])

            title    = job["title"]
            location = job["location"]

            if intern_re and not intern_re.search(title):
                logger.debug(f"  SKIP [not intern] {job['company']}: {title}")
                continue
            if eng_keywords:
                tl = title.lower()
                if not any(re.search(r"\b" + re.escape(k) + r"\b", tl) for k in eng_keywords):
                    logger.debug(f"  SKIP [not eng] {job['company']}: {title}")
                    continue
            if phd_re and phd_re.search(title):
                logger.debug(f"  SKIP [PhD] {job['company']}: {title}")
                continue
            if location_eligible_fn and not location_eligible_fn(location):
                logger.debug(f"  SKIP [location '{location}'] {job['company']}: {title}")
                continue

            job["role_family"] = classify_role(title, "") or "fullstack"
            results.append(job)
            hits += 1

        logger.info(f"  → {hits} qualifying roles")

    logger.info(f"Web scraper total: {len(results)} roles from {len(sources)} sources")
    return results
