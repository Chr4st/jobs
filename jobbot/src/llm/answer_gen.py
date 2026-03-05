"""LLM-powered answer generator for open-ended application questions.

Uses the local Ollama model (OLLAMA_MODEL env var, default: mistral) to generate
tailored answers for questions that can't be resolved by the profile or heuristic map.

Only called for required text/textarea fields with empty labels or genuinely
open-ended prompts (e.g. "Why are you interested in X?").
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import ollama

from src.utils.logging import setup_logging

logger = setup_logging("jobbot.llm.answer_gen")

_MODEL = os.environ.get("OLLAMA_MODEL", "mistral")
_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "llm_answer_cache.json"

# Minimum word count for answers to open-ended questions
_MIN_WORDS = 30
_MAX_WORDS = 120

# Role-family → area of interest defaults (used when question asks for an area)
_ROLE_AREA_DEFAULTS = {
    "quant":     "Quantitative Research / Systems",
    "ml":        "Machine Learning / AI Infrastructure",
    "fullstack": "Backend Systems / Infrastructure",
    "founding":  "Full-Stack / Infrastructure",
}


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _cache_key(question: str, company: str, role_title: str) -> str:
    raw = f"{question}|{company}|{role_title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generate_answer(
    question: str,
    profile: dict,
    company: str = "",
    role_title: str = "",
    role_family: str = "fullstack",
    max_words: int = _MAX_WORDS,
) -> Optional[str]:
    """Generate a tailored answer for an open-ended application question.

    Returns a string answer, or None if generation fails.
    Answers are cached to disk keyed on (question, company, role_title).
    """
    # Short-circuit for known deterministic questions (no cache needed)
    quick = _quick_answer(question, profile, role_family)
    if quick is not None:
        return quick

    # Check cache first
    cache = _load_cache()
    key = _cache_key(question, company, role_title)
    if key in cache:
        logger.debug(f"LLM cache hit: {question[:50]}")
        return cache[key]

    # Build context from profile
    name        = f"{profile.get('first_name', '')} {profile.get('last_name', '')}"
    university  = profile.get("university", "University of Michigan")
    major       = profile.get("major", "Computer Science")
    gpa         = profile.get("gpa", "")
    why_snippet = profile.get("why_snippets", {}).get(role_family, "")

    prompt = f"""You are helping {name}, a student at {university} majoring in {major}{f' (GPA {gpa})' if gpa else ''}, fill out a job application.

Company: {company}
Role: {role_title}
Role Family: {role_family}

Candidate background highlights:
{why_snippet}

Write a concise, professional answer (max {max_words} words) to this application question:
"{question}"

Rules:
- Be specific and authentic to the candidate's background above
- Do NOT use filler phrases like "I am passionate about..."
- Do NOT use bullet points — write in plain prose
- Do NOT start with "I" — vary the sentence structure
- Keep it under {max_words} words
- Return ONLY the answer text, nothing else"""

    try:
        response = ollama.chat(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_predict": 200},
        )
        answer = response["message"]["content"].strip()
        # Strip any meta-commentary the model might add
        answer = _clean_llm_output(answer)
        if not answer:
            return None

        # Cache and return
        cache[key] = answer
        _save_cache(cache)
        logger.info(f"LLM answer generated for: {question[:60]}")
        return answer

    except Exception as e:
        logger.warning(f"Ollama answer generation failed: {e}")
        return None


def _quick_answer(question: str, profile: dict, role_family: str) -> Optional[str]:
    """Deterministic answers for common questions that don't need LLM."""
    ql = question.lower()

    # Area of interest / department (checked before yes/no patterns)
    if any(k in ql for k in ("area of interest", "department", "which team", "which area",
                              "interest in software", "area of focus", "most interested in",
                              "primary interest", "role interest")):
        return _ROLE_AREA_DEFAULTS.get(role_family, "Backend Systems / Infrastructure")

    # How did you hear (flexible matching)
    if any(k in ql for k in ("how did you hear", "how did you first hear",
                              "how did you learn", "where did you hear",
                              "first hear about", "learn about us",
                              "where did you learn", "how did you find out")):
        return "Online job board"

    # GPA scale
    if "grading scale" in ql or "gpa scale" in ql:
        return profile.get("gpa_scale", "4.0")

    # Overall GPA
    if "overall gpa" in ql or "cumulative gpa" in ql:
        return profile.get("gpa", "3.9")

    # Outstanding offers
    if "outstanding offer" in ql or "competing offer" in ql or "other offer" in ql:
        return "No"

    # Further education
    if "further education" in ql or "pursuing graduate" in ql or "graduate school" in ql:
        return "No"

    # Enrollment
    if "enrolled" in ql and ("university" in ql or "program" in ql):
        return "Yes"

    # Graduation date
    if "graduation" in ql and any(k in ql for k in ("year", "date", "when")):
        return profile.get("graduation_date", profile.get("graduation_year", "May 2029"))

    return None


def _clean_llm_output(text: str) -> str:
    """Remove LLM meta-commentary, quotes, markdown, etc."""
    # Remove leading/trailing quotes
    text = text.strip('"\'')
    # Remove markdown bold/italic
    import re
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    # Remove "Answer:" or "Response:" prefix
    text = re.sub(r"^(answer|response|here('?s| is (a|my| the)))[:\s]+", "", text, flags=re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def can_generate(question: str) -> bool:
    """Return True if this question is a good candidate for LLM generation.

    Filters out yes/no questions, EEO questions, and questions that should be
    answered by profile/heuristic instead of LLM.
    """
    ql = question.lower()
    # Skip yes/no pattern questions
    yesno_keywords = [
        "do you", "are you", "will you", "have you", "would you",
        "is this", "can you", "did you",
    ]
    # But keep "why" / "describe" / "how would" type questions
    open_keywords = [
        "why", "describe", "explain", "what makes", "tell us",
        "how would", "what is your", "elaborate", "share",
        "what experience", "what skills",
    ]
    if any(k in ql for k in open_keywords):
        return True
    if any(k in ql for k in yesno_keywords):
        return False
    # Default: if question is long, it's probably open-ended
    return len(question) > 60
