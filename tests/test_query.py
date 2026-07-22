import pytest

from courts_scraper.query import (
    Court,
    build_query,
    resolve_court_tokens,
    search_url,
)

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


def test_build_query_multiple_courts_repeats_filter_tokens():
    # The live site unions repeated same-facet tokens joined by AND, so this is
    # the OR of the courts. An explicit "(A OR B)" grouping is NOT understood by
    # the parser and returns the whole unfiltered corpus -- do not reintroduce it.
    query = build_query([Court.SUPREME, Court.HIGH])
    assert query == (
        '" type:Judgment" AND "filter:alfresco_radio.title" '
        'AND "filter:alfresco_Court.Supreme Court" '
        'AND "filter:alfresco_Court.High Court"'
    )
    assert " OR " not in query
    assert "(" not in query


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
        ("district", Court.DISTRICT),
        ("circuit", Court.CIRCUIT),
        ("courts-martial-appeal", Court.COURTS_MARTIAL_APPEAL),
        ("central_criminal", Court.CENTRAL_CRIMINAL),
    ],
)
def test_from_token(token, court):
    assert Court.from_token(token) is court


def test_from_token_rejects_unknown():
    with pytest.raises(ValueError, match="unknown court"):
        Court.from_token("chancery")


def test_all_facets_have_exact_live_spelling():
    # These are the exact "Jurisdiction" facet labels on the live site; the
    # value must match byte-for-byte or the filter silently returns nothing.
    assert {c.value for c in Court} == {
        "Supreme Court",
        "Court of Appeal",
        "High Court",
        "Court of Criminal Appeal",
        "Courts-Martial Appeal Courts",
        "Central Criminal Court",
        "Special Criminal Court",
        "Circuit Court",
        "District Court",
    }


def test_resolve_superior_group_expands_to_three_courts():
    assert resolve_court_tokens(["superior"]) == (
        Court.SUPREME,
        Court.COURT_OF_APPEAL,
        Court.HIGH,
    )


def test_resolve_all_group_is_every_court():
    assert resolve_court_tokens(["all"]) == tuple(Court)


def test_resolve_dedupes_group_and_overlapping_token():
    # "superior" already includes high; the extra token must not duplicate it.
    assert resolve_court_tokens(["superior", "high"]) == (
        Court.SUPREME,
        Court.COURT_OF_APPEAL,
        Court.HIGH,
    )


def test_resolve_mixes_group_and_individual_courts():
    assert resolve_court_tokens(["supreme", "district"]) == (
        Court.SUPREME,
        Court.DISTRICT,
    )


def test_resolve_rejects_unknown_token():
    with pytest.raises(ValueError, match="unknown court"):
        resolve_court_tokens(["supreme", "chancery"])
