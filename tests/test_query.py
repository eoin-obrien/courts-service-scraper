import pytest

from courts_scraper.query import Court, build_query, search_url

BASE = "https://ww2.courts.ie"

# The exact encoded path the live site uses for a Supreme Court judgment search.
_SUPREME_ENCODED = (
    "%22%20type%3AJudgment%22%20AND%20%22filter%3Aalfresco_radio.title%22"
    "%20AND%20%22filter%3Aalfresco_Court.Supreme%20Court%22"
)


def test_build_query_single_court():
    assert build_query([Court.SUPREME]) == (
        '" type:Judgment" AND "filter:alfresco_radio.title" '
        'AND "filter:alfresco_Court.Supreme Court"'
    )


def test_build_query_multiple_courts_uses_or():
    query = build_query([Court.SUPREME, Court.HIGH])
    assert (
        '("filter:alfresco_Court.Supreme Court" OR "filter:alfresco_Court.High Court")'
    ) in query


def test_build_query_requires_a_court():
    with pytest.raises(ValueError, match="at least one court"):
        build_query([])


def test_search_url_matches_live_encoding():
    url = search_url(BASE, build_query([Court.SUPREME]), page=0)
    assert url == f"{BASE}/search/Judgments/{_SUPREME_ENCODED}?page=0"


def test_search_url_paginates():
    url = search_url(BASE, build_query([Court.SUPREME]), page=7)
    assert url.endswith("?page=7")


@pytest.mark.parametrize(
    ("token", "court"),
    [
        ("supreme", Court.SUPREME),
        ("Supreme", Court.SUPREME),
        ("court_of_appeal", Court.COURT_OF_APPEAL),
        ("court-of-appeal", Court.COURT_OF_APPEAL),
        ("high", Court.HIGH),
    ],
)
def test_from_token(token, court):
    assert Court.from_token(token) is court


def test_from_token_rejects_unknown():
    with pytest.raises(ValueError, match="unknown court"):
        Court.from_token("district")
