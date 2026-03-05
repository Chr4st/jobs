"""Greenhouse scanner — discovers intern roles via two complementary sources:

  1. PRIMARY: community-maintained GitHub repos (vanshb03/Summer2026-Internships, etc.)
     → broad coverage, 500+ companies, live-updated
  2. SUPPLEMENT: greenhouse_sources.yaml company slugs via Greenhouse Boards API
     → ensures curated firms (quant shops, etc.) are never missed

Usage:
    python -m src.main scan-gh
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import requests
import yaml

from src.discovery.web_scraper import scrape_greenhouse_internships
from src.storage.db import (
    create_application,
    get_connection,
    get_job_by_dedup,
    upsert_job,
)
from src.utils.dedupe import make_dedup_key
from src.utils.logging import setup_logging
from src.utils.role_classify import classify_role, get_resume_path

logger = setup_logging("jobbot.greenhouse.scanner")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "greenhouse_sources.yaml"
REQUEST_TIMEOUT = 12

# ── Quality filters ───────────────────────────────────────────────────────────

_PHD_RE = re.compile(
    r"\b(ph\.?d\.?|postdoc|post-doc|phd/ms|ms/phd|doctoral|"
    r"master'?s? degree required|master'?s? degree only|"
    r"graduate researcher)\b",
    re.IGNORECASE,
)

_SENIOR_RE = re.compile(
    r"\b([5-9]\+?\s*years?|[1-9][0-9]\+?\s*years?)\s*(of\s+)?(experience|exp)\b",
    re.IGNORECASE,
)

_EXCLUDED_LOCATIONS_RE = re.compile(
    r"\b(singapore|hong kong|tokyo|japan|china|mainland|india|australia|sydney|"
    r"seoul|korea|brazil|mexico|jakarta|bangkok|taipei|taiwan|mumbai|bangalore|"
    r"hyderabad|dubai|riyadh|tel aviv|israel|cairo|johannesburg|south africa|"
    r"new zealand|malaysia|vietnam|philippines)\b",
    re.IGNORECASE,
)

_NA_EU_RE = re.compile(
    r"\b(us|usa|united states|new york|san francisco|chicago|boston|seattle|"
    r"austin|los angeles|menlo park|palo alto|cambridge|new jersey|connecticut|"
    r"remote|canada|toronto|vancouver|montreal|"
    r"uk|united kingdom|london|amsterdam|berlin|paris|zurich|dublin|stockholm|"
    r"munich|frankfurt|geneva|netherlands|germany|france|switzerland|ireland|"
    r"sweden|denmark|norway|finland|austria|belgium|poland|czech|spain|italy|"
    r"europe|european)\b",
    re.IGNORECASE,
)


def _is_undergrad_eligible(title: str) -> bool:
    return not _PHD_RE.search(title)


def _is_location_eligible(location: str) -> bool:
    if not location or not location.strip():
        return True
    if "remote" in location.lower():
        return True
    if _EXCLUDED_LOCATIONS_RE.search(location):
        return False
    if _NA_EU_RE.search(location):
        return True
    return True  # unknown → include


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _upsert_and_track(
    conn,
    company: str,
    title: str,
    url: str,
    location: str,
    role_family: str,
    slug: str,
    job_id: str,
    summary: dict,
) -> bool:
    """Upsert job + create application if new. Returns True if newly inserted."""
    dedup_key = make_dedup_key(company, title, url)
    existing = get_job_by_dedup(conn, dedup_key)
    if existing:
        return False

    raw_json = {
        "title": title,
        "company": company,
        "location": location,
        "apply_url": url,
        "gh_slug": slug,
        "gh_job_id": job_id,
    }
    upsert_job(conn, dedup_key, url, company, title, role_family, location, 1.0, raw_json)
    create_application(conn, dedup_key, get_resume_path(role_family))
    summary["new_jobs"] += 1
    return True


def _apply_role_filters(job: dict, cfg: dict) -> bool:
    """Return True if the job passes role_filters config."""
    filters = cfg.get("role_filters", {})
    if not filters:
        return True

    role_family = job.get("role_family", "fullstack")
    title       = (job.get("title") or job.get("role_title", "")).lower()

    # Family whitelist
    families = filters.get("families")
    if families and role_family not in families:
        return False

    # Extra required title keywords
    require_kw = [k.lower() for k in filters.get("require_title_keywords", [])]
    if require_kw and not any(k in title for k in require_kw):
        return False

    # Excluded title keywords
    exclude_kw = [k.lower() for k in filters.get("exclude_title_keywords", [])]
    if any(k in title for k in exclude_kw):
        return False

    return True


def _scan_from_web(
    intern_re: re.Pattern,
    eng_kw: list[str],
    conn,
    summary: dict,
    cfg: dict = {},
) -> list[dict]:
    """Primary: scrape community repos for Greenhouse intern links."""
    jobs = scrape_greenhouse_internships(
        intern_re=intern_re,
        eng_keywords=eng_kw,
        phd_re=_PHD_RE,
        location_eligible_fn=_is_location_eligible,
    )

    results = []
    for j in jobs:
        company     = j["company"]
        title       = j["title"]
        url         = j["job_url"]
        location    = j["location"]
        role_family = j["role_family"]
        slug        = j["slug"]
        job_id      = j["job_id"]

        if not _apply_role_filters(j, cfg):
            logger.debug(f"  SKIP [role_filter] {company}: {title}")
            continue

        is_new = _upsert_and_track(conn, company, title, url, location, role_family, slug, job_id, summary)
        summary["discovered"] += 1
        results.append({
            "company": company, "title": title, "url": url,
            "location": location, "role_family": role_family, "is_new": is_new,
            "source": "web",
        })
        logger.info(f"  {'NEW' if is_new else 'SEEN'} [WEB/{company}] {title} ({role_family})")

    return results


def _scan_from_yaml(
    cfg: dict,
    intern_re: re.Pattern,
    eng_kw: list[str],
    conn,
    summary: dict,
    seen_urls: set[str],
) -> list[dict]:
    """Supplement: hit Greenhouse Boards API for curated YAML companies."""
    companies: dict[str, str] = cfg.get("companies", {})
    results = []

    for company, slug in companies.items():
        summary["companies_scanned"] += 1
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            resp = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(f"  {company} ({slug}): HTTP {resp.status_code}")
                continue
            board_jobs = resp.json().get("jobs", [])
        except Exception as e:
            logger.warning(f"  {company} ({slug}): request error: {e}")
            summary["errors"] += 1
            continue

        company_hits = 0
        for j in board_jobs:
            title = j.get("title", "")
            t_lower = title.lower()

            if not intern_re.search(t_lower):
                continue
            if not any(re.search(r"\b" + re.escape(k) + r"\b", t_lower) for k in eng_kw):
                continue

            url = j.get("absolute_url", "")
            if not url or url in seen_urls:
                continue

            location = (
                j.get("location", {}).get("name", "")
                if isinstance(j.get("location"), dict)
                else ""
            )

            if not _is_undergrad_eligible(title):
                logger.debug(f"  SKIP [PhD] [{company}] {title}")
                continue
            if not _is_location_eligible(location):
                logger.debug(f"  SKIP [location='{location}'] [{company}] {title}")
                continue

            role_family = classify_role(title, "") or "fullstack"
            job_id = str(j.get("id", ""))

            is_new = _upsert_and_track(
                conn, company, title, url, location, role_family, slug, job_id, summary
            )
            seen_urls.add(url)
            summary["discovered"] += 1
            company_hits += 1
            results.append({
                "company": company, "title": title, "url": url,
                "location": location, "role_family": role_family, "is_new": is_new,
                "source": "yaml",
            })
            logger.info(f"  {'NEW' if is_new else 'SEEN'} [YAML/{company}] {title} ({role_family})")

        if company_hits:
            summary["companies_with_hits"] += 1

    return results


def scan_greenhouse_boards(
    max_jobs: int = 500,
    db_path: Optional[Path] = None,
    event_callback=None,
) -> dict:
    """Discover Greenhouse intern roles from web scraper (primary) + YAML (supplement).

    Returns:
        {discovered, new_jobs, companies_scanned, companies_with_hits, errors, jobs: [...]}
    """
    cfg = _load_config()
    intern_re = re.compile(
        cfg.get("intern_pattern", r"\b(intern|internship|co-op|co op)\b"),
        re.IGNORECASE,
    )
    eng_kw = [k.lower() for k in cfg.get("engineering_keywords", [])]

    conn = get_connection(db_path)
    summary = {
        "discovered": 0,
        "new_jobs": 0,
        "companies_scanned": 0,
        "companies_with_hits": 0,
        "errors": 0,
        "jobs": [],
    }

    def _emit(msg: dict):
        if event_callback:
            try:
                event_callback(msg)
            except Exception:
                pass

    _emit({"type": "scan", "status": "started"})

    # ── Phase 1: Web scraper (primary) ────────────────────────────────────────
    logger.info("=== Phase 1: Web scraper (community repos) ===")
    web_jobs = _scan_from_web(intern_re, eng_kw, conn, summary, cfg)
    summary["jobs"].extend(web_jobs)

    # Build seen URL set to avoid double-counting in YAML phase
    seen_urls: set[str] = {j["url"] for j in web_jobs}

    # ── Phase 2: YAML supplement ───────────────────────────────────────────────
    if cfg.get("companies"):
        logger.info("=== Phase 2: YAML supplement (curated boards) ===")
        yaml_jobs = _scan_from_yaml(cfg, intern_re, eng_kw, conn, summary, seen_urls)
        summary["jobs"].extend(yaml_jobs)

    conn.close()

    _emit({"type": "scan", "status": "completed", **{k: v for k, v in summary.items() if k != "jobs"}})
    logger.info(
        f"Scan complete: {summary['discovered']} intern roles "
        f"({summary['new_jobs']} new), "
        f"{summary['companies_with_hits']}/{summary['companies_scanned']} YAML companies hit"
    )

    if max_jobs and len(summary["jobs"]) > max_jobs:
        summary["jobs"] = summary["jobs"][:max_jobs]

    return summary
