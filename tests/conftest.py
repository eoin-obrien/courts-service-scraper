"""Shared test fixtures: access to saved HTML and a common base URL."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://ww2.courts.ie"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def search_html() -> str:
    return _load("search_page.html")


@pytest.fixture
def view_html() -> str:
    return _load("view_page.html")


@pytest.fixture
def view_no_citation_html() -> str:
    return _load("view_no_citation.html")


@pytest.fixture
def base_url() -> str:
    return BASE_URL
