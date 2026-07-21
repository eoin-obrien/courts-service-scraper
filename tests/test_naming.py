import pytest

from courts_scraper.naming import (
    MissingCitationError,
    citation_slug,
    judge_slug,
    pdf_filename,
)


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        ("[2026] IESC 36", "2026_IESC_36"),
        ("[2019] IECA 112", "2019_IECA_112"),
        ("[2020] IEHC 5", "2020_IEHC_5"),
    ],
)
def test_citation_slug(citation, expected):
    assert citation_slug(citation) == expected


@pytest.mark.parametrize("bad", ["", "   ", None, "not a citation", "IESC 36"])
def test_citation_slug_rejects_missing(bad):
    with pytest.raises(MissingCitationError):
        citation_slug(bad)


@pytest.mark.parametrize(
    ("judge", "expected"),
    [
        ("Woulfe J.", "Woulfe-J"),
        ("O'Donnell C.J.", "O-Donnell-C-J"),
        # Accented Irish names transliterate to ASCII, keeping their identity
        # instead of collapsing to a colliding slug.
        ("Ó Caoimh J.", "O-Caoimh-J"),
        ("Ní Raifeartaigh J.", "Ni-Raifeartaigh-J"),
        ("", ""),
        (None, ""),
    ],
)
def test_judge_slug(judge, expected):
    assert judge_slug(judge) == expected


def test_judge_slug_is_length_capped():
    slug = judge_slug("X " * 200)  # pathological over-long value
    assert len(slug) <= 80
    assert not slug.endswith("-")


def test_pdf_filename_combines_citation_and_judge():
    assert pdf_filename("[2026] IESC 36", "Woulfe J.") == "2026_IESC_36_Woulfe-J.pdf"


def test_pdf_filename_without_judge():
    assert pdf_filename("[2026] IESC 36", "") == "2026_IESC_36.pdf"


def test_pdf_filename_collision_suffix():
    taken = {"2026_IESC_36_Woulfe-J.pdf"}
    assert (
        pdf_filename("[2026] IESC 36", "Woulfe J.", taken=taken)
        == "2026_IESC_36_Woulfe-J_2.pdf"
    )


def test_pdf_filename_requires_citation():
    with pytest.raises(MissingCitationError):
        pdf_filename(None, "Woulfe J.")
