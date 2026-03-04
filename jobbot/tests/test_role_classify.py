"""Tests for role classification."""

import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.utils.role_classify import classify_role, get_resume_path


class TestClassifyRole:
    def test_founding_engineer_title(self):
        assert classify_role("Founding Engineer") == "founding"

    def test_founding_first_engineer(self):
        assert classify_role("First Engineer") == "founding"

    def test_founding_from_description(self):
        result = classify_role(
            "Engineer",
            "We're looking for a founding engineer to build our product from zero to one."
        )
        assert result == "founding"

    def test_fullstack_title(self):
        assert classify_role("Full Stack Engineer") == "fullstack"

    def test_fullstack_hyphenated(self):
        assert classify_role("Full-Stack Developer") == "fullstack"

    def test_swe_generic(self):
        assert classify_role("Software Engineer") == "fullstack"

    def test_swe_with_react(self):
        result = classify_role(
            "Software Engineer",
            "Build features with React, Node.js, and TypeScript."
        )
        assert result == "fullstack"

    def test_ml_engineer_title(self):
        assert classify_role("ML Engineer") == "ml"

    def test_ai_ml_title(self):
        assert classify_role("AI/ML Engineer") == "ml"

    def test_ml_from_description(self):
        result = classify_role(
            "Engineer",
            "Work on machine learning models, PyTorch, deep learning infrastructure."
        )
        assert result == "ml"

    def test_data_scientist(self):
        assert classify_role("Data Scientist") == "ml"

    def test_quant_trading(self):
        assert classify_role("Quantitative Trading Engineer") == "quant"

    def test_quant_researcher(self):
        assert classify_role("Quantitative Researcher") == "quant"

    def test_quant_from_description(self):
        result = classify_role(
            "Engineer",
            "Build algorithmic trading systems and backtesting frameworks."
        )
        assert result == "quant"

    def test_unrelated_role_returns_none(self):
        assert classify_role("Marketing Manager") is None

    def test_unrelated_role_with_unrelated_desc(self):
        assert classify_role(
            "Content Writer",
            "Write blog posts about product launches."
        ) is None

    def test_founding_beats_fullstack(self):
        """Founding is more specific and should win over fullstack."""
        result = classify_role(
            "Founding Software Engineer",
            "Build our product from scratch as the first engineer."
        )
        assert result == "founding"

    def test_ml_beats_fullstack(self):
        """ML is more specific and should win over generic SWE."""
        result = classify_role(
            "Software Engineer - Machine Learning",
            "Work on deep learning models and ML infrastructure."
        )
        assert result == "ml"

    def test_empty_title(self):
        assert classify_role("") is None

    def test_case_insensitive(self):
        assert classify_role("FOUNDING ENGINEER") == "founding"
        assert classify_role("full stack engineer") == "fullstack"
        assert classify_role("ML ENGINEER") == "ml"


class TestGetResumePath:
    def test_founding(self):
        assert get_resume_path("founding") == "resumes/founding.pdf"

    def test_fullstack(self):
        assert get_resume_path("fullstack") == "resumes/fullstack.pdf"

    def test_ml(self):
        assert get_resume_path("ml") == "resumes/ml.pdf"

    def test_quant(self):
        assert get_resume_path("quant") == "resumes/quant.pdf"

    def test_unknown_defaults_to_fullstack(self):
        assert get_resume_path("unknown") == "resumes/fullstack.pdf"
