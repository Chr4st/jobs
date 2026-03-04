"""Input sanitization and validation utilities."""

import re
from html import unescape
from typing import Optional


def sanitize_text(text: str) -> str:
    """Clean text: strip HTML, normalize whitespace."""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode HTML entities
    text = unescape(text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def sanitize_url(url: str) -> str:
    """Normalize a URL: strip tracking params, ensure https."""
    url = url.strip()
    # Remove common tracking params
    url = re.sub(r'[?&](utm_\w+|ref|source|gh_src)=[^&]*', '', url)
    # Clean up leftover ? or &
    url = re.sub(r'\?&', '?', url)
    url = re.sub(r'\?$', '', url)
    # Ensure https
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    return url


def extract_company_from_greenhouse_url(url: str) -> Optional[str]:
    """Extract company slug from Greenhouse URL.

    Examples:
        https://boards.greenhouse.io/acmecorp/jobs/12345 -> acmecorp
        https://jobs.greenhouse.io/acmecorp -> acmecorp
    """
    patterns = [
        re.compile(r'boards\.greenhouse\.io/(\w+)', re.IGNORECASE),
        re.compile(r'jobs\.greenhouse\.io/(\w+)', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def is_greenhouse_url(url: str) -> bool:
    """Check if a URL is a Greenhouse job posting."""
    return bool(re.search(r'greenhouse\.io', url, re.IGNORECASE))


def normalize_greenhouse_url(url: str) -> str:
    """Normalize a Greenhouse URL to a canonical form."""
    url = sanitize_url(url)
    # Remove hash fragments
    url = re.sub(r'#.*$', '', url)
    return url


def safe_filename(text: str, max_len: int = 80) -> str:
    """Generate a filesystem-safe filename from text."""
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'\s+', '_', text).strip('_')
    return text[:max_len]


def validate_email(email: str) -> bool:
    """Basic email validation."""
    return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email))


def validate_phone(phone: str) -> bool:
    """Basic phone number validation (allows various formats)."""
    cleaned = re.sub(r'[\s\-\(\)\.]+', '', phone)
    return bool(re.match(r'^\+?\d{10,15}$', cleaned))
