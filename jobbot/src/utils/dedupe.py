"""Deduplication key generation for jobs."""

import hashlib
import re


def normalize(text: str) -> str:
    """Normalize text for consistent dedup key generation."""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s/:.@-]', '', text)
    return text


def make_dedup_key(company: str, role_title: str, job_url: str) -> str:
    """Generate a SHA-256 dedup key from company|role_title|job_url."""
    parts = "|".join([
        normalize(company),
        normalize(role_title),
        normalize(job_url),
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()
