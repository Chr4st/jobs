"""Tests for deduplication key generation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.utils.dedupe import make_dedup_key, normalize


class TestNormalize:
    def test_lowercase(self):
        assert normalize("Hello World") == "hello world"

    def test_strip_whitespace(self):
        assert normalize("  hello  ") == "hello"

    def test_collapse_whitespace(self):
        assert normalize("hello   world") == "hello world"

    def test_remove_special_chars(self):
        assert normalize("hello! @world#") == "hello @world"

    def test_preserve_url_chars(self):
        result = normalize("https://boards.greenhouse.io/acme/jobs/123")
        assert "https://boards.greenhouse.io/acme/jobs/123" == result


class TestMakeDedupKey:
    def test_deterministic(self):
        key1 = make_dedup_key("Acme", "Software Engineer", "https://example.com/job/1")
        key2 = make_dedup_key("Acme", "Software Engineer", "https://example.com/job/1")
        assert key1 == key2

    def test_case_insensitive(self):
        key1 = make_dedup_key("ACME", "Software Engineer", "https://example.com/job/1")
        key2 = make_dedup_key("acme", "Software Engineer", "https://example.com/job/1")
        assert key1 == key2

    def test_whitespace_insensitive(self):
        key1 = make_dedup_key("Acme Corp", "Software  Engineer", "https://example.com/job/1")
        key2 = make_dedup_key("Acme Corp", "Software Engineer", "https://example.com/job/1")
        assert key1 == key2

    def test_different_companies_differ(self):
        key1 = make_dedup_key("Acme", "SWE", "https://example.com/1")
        key2 = make_dedup_key("Beta", "SWE", "https://example.com/1")
        assert key1 != key2

    def test_different_roles_differ(self):
        key1 = make_dedup_key("Acme", "SWE", "https://example.com/1")
        key2 = make_dedup_key("Acme", "ML Engineer", "https://example.com/1")
        assert key1 != key2

    def test_different_urls_differ(self):
        key1 = make_dedup_key("Acme", "SWE", "https://example.com/1")
        key2 = make_dedup_key("Acme", "SWE", "https://example.com/2")
        assert key1 != key2

    def test_is_sha256_hex(self):
        key = make_dedup_key("Acme", "SWE", "https://example.com/1")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_empty_fields(self):
        # Should still produce a valid key
        key = make_dedup_key("", "", "")
        assert len(key) == 64
