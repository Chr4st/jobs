"""Deterministic match scoring for job candidates."""

import re
from datetime import datetime, timezone
from typing import Optional

from src.utils.role_classify import ROLE_PATTERNS


def compute_match_score(
    title: str,
    description: str,
    role_family: Optional[str],
    location: str = "",
    first_seen: Optional[str] = None,
    preferred_locations: Optional[list[str]] = None,
) -> float:
    """Compute a match score between 0.0 and 1.0.

    Weights:
      - title_match:      0.40 (how well the title matches the role family)
      - keyword_density:   0.30 (relevant keywords in description)
      - location_match:    0.15 (matches preferred locations)
      - recency:           0.15 (newer postings score higher)
    """
    if not role_family:
        return 0.0

    title_score = _score_title_match(title, role_family)
    keyword_score = _score_keyword_density(description, role_family)
    location_score = _score_location(location, preferred_locations or [])
    recency_score = _score_recency(first_seen)

    total = (
        title_score * 0.40
        + keyword_score * 0.30
        + location_score * 0.15
        + recency_score * 0.15
    )
    return round(min(max(total, 0.0), 1.0), 3)


def _score_title_match(title: str, role_family: str) -> float:
    """Score 0-1 based on how strongly the title matches the role family."""
    patterns = ROLE_PATTERNS.get(role_family, {}).get("title_patterns", [])
    for pattern in patterns:
        if pattern.search(title):
            return 1.0
    # Partial match: check if any keyword appears in title
    keywords = ROLE_PATTERNS.get(role_family, {}).get("desc_keywords", [])
    title_lower = title.lower()
    hits = sum(1 for kw in keywords if kw in title_lower)
    if hits > 0:
        return min(hits * 0.3, 0.7)
    return 0.0


def _score_keyword_density(description: str, role_family: str) -> float:
    """Score 0-1 based on relevant keyword density in description."""
    if not description:
        return 0.3  # Neutral if no description

    keywords = ROLE_PATTERNS.get(role_family, {}).get("desc_keywords", [])
    desc_lower = description.lower()
    hits = sum(1 for kw in keywords if kw in desc_lower)
    # Normalize: expect ~3-5 keywords for a strong match
    return min(hits / 5.0, 1.0)


def _score_location(location: str, preferred: list[str]) -> float:
    """Score 0-1 based on location match."""
    if not preferred:
        return 0.7  # No preference = neutral-positive

    location_lower = location.lower()

    if not location_lower or "remote" in location_lower:
        return 0.9  # Remote is generally good

    for pref in preferred:
        if pref.lower() in location_lower:
            return 1.0

    # Partial: same state/country
    for pref in preferred:
        parts = [p.strip().lower() for p in pref.split(",")]
        loc_parts = [p.strip().lower() for p in location.split(",")]
        if any(p in loc_parts for p in parts):
            return 0.6

    return 0.3


def _score_recency(first_seen: Optional[str]) -> float:
    """Score 0-1 based on how recently the job was discovered."""
    if not first_seen:
        return 0.5

    try:
        seen_dt = datetime.fromisoformat(first_seen)
        now = datetime.now(timezone.utc)
        days_old = (now - seen_dt).days
    except (ValueError, TypeError):
        return 0.5

    if days_old <= 1:
        return 1.0
    elif days_old <= 3:
        return 0.9
    elif days_old <= 7:
        return 0.7
    elif days_old <= 14:
        return 0.5
    elif days_old <= 30:
        return 0.3
    else:
        return 0.1
