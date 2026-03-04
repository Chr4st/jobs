"""Classify job titles/descriptions into one of 4 role families."""

import re
from typing import Optional


ROLE_PATTERNS = {
    "founding": {
        "title_patterns": [
            re.compile(r"founding\s+\w*\s*engineer", re.IGNORECASE),
            re.compile(r"founding\s+(?:member|employee)", re.IGNORECASE),
            re.compile(r"first\s+engineer", re.IGNORECASE),
            re.compile(r"engineer\s*#?\s*1", re.IGNORECASE),
            re.compile(r"early[\s-]+stage\s+engineer", re.IGNORECASE),
        ],
        "desc_keywords": [
            "founding", "first engineer", "employee #1", "early stage",
            "0-to-1", "zero to one", "greenfield",
        ],
    },
    "quant": {
        "title_patterns": [
            re.compile(r"quant", re.IGNORECASE),
            re.compile(r"trading", re.IGNORECASE),
            re.compile(r"systematic", re.IGNORECASE),
        ],
        "desc_keywords": [
            "quantitative", "trading", "algorithmic", "systematic",
            "alpha", "backtesting", "market making", "signal",
        ],
    },
    "ml": {
        "title_patterns": [
            re.compile(r"(?:ml|ai|machine\s+learning|deep\s+learning)", re.IGNORECASE),
            re.compile(r"data\s+scientist", re.IGNORECASE),
            re.compile(r"(?:nlp|computer\s+vision|cv)\s+engineer", re.IGNORECASE),
        ],
        "desc_keywords": [
            "machine learning", "deep learning", "neural network", "pytorch",
            "tensorflow", "nlp", "computer vision", "llm", "transformer",
            "model training", "ml infrastructure", "ai/ml", "ml/ai",
        ],
    },
    "fullstack": {
        "title_patterns": [
            re.compile(r"full[\s-]?stack", re.IGNORECASE),
            re.compile(r"software\s+engineer", re.IGNORECASE),
            re.compile(r"\bswe\b", re.IGNORECASE),
            re.compile(r"web\s+(?:developer|engineer)", re.IGNORECASE),
            re.compile(r"backend\s+engineer", re.IGNORECASE),
            re.compile(r"frontend\s+engineer", re.IGNORECASE),
        ],
        "desc_keywords": [
            "full stack", "fullstack", "full-stack", "react", "node",
            "frontend", "backend", "api", "web application", "typescript",
            "javascript", "django", "flask", "rails", "next.js",
        ],
    },
}

# Priority order: more specific roles first
CLASSIFICATION_ORDER = ["founding", "quant", "ml", "fullstack"]


def classify_role(title: str, description: str = "") -> Optional[str]:
    """Classify a job into a role family based on title and description.

    Returns one of: 'founding', 'fullstack', 'ml', 'quant', or None.
    Priority: founding > quant > ml > fullstack (more specific first).
    """
    title_lower = title.lower()
    desc_lower = description.lower()

    scores = {}

    for family in CLASSIFICATION_ORDER:
        patterns = ROLE_PATTERNS[family]
        score = 0.0

        # Title pattern match (strong signal)
        for pattern in patterns["title_patterns"]:
            if pattern.search(title):
                score += 3.0
                break

        # Description keyword matches
        keyword_hits = sum(
            1 for kw in patterns["desc_keywords"]
            if kw in desc_lower
        )
        score += min(keyword_hits * 0.5, 2.0)

        # Title keyword presence (weaker signal)
        title_keyword_hits = sum(
            1 for kw in patterns["desc_keywords"]
            if kw in title_lower
        )
        score += min(title_keyword_hits * 1.0, 2.0)

        scores[family] = score

    # Filter out families with very low scores
    viable = {k: v for k, v in scores.items() if v >= 1.0}

    if not viable:
        return None

    # Return highest scoring family, respecting priority order for ties
    max_score = max(viable.values())
    for family in CLASSIFICATION_ORDER:
        if viable.get(family, 0) == max_score:
            return family

    return None


def get_resume_path(role_family: str) -> str:
    """Get the resume filename for a role family."""
    mapping = {
        "founding": "resumes/founding.pdf",
        "fullstack": "resumes/fullstack.pdf",
        "ml": "resumes/ml.pdf",
        "quant": "resumes/quant.pdf",
    }
    return mapping.get(role_family, "resumes/fullstack.pdf")
