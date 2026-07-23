from pathlib import Path

import pytest

from courts_scraper.naming import (
    MissingCitationError,
    UnsafeFilenameError,
    citation_slug,
    judge_slug,
    pdf_filename,
    safe_output_path,
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


@pytest.mark.parametrize(
    ("citation", "expected"),
    [
        # Older judgments carry a lower-cased court token; the slug normalises it
        # to upper case so they name canonically alongside their peers.
        ("[2015] IEHc 168", "2015_IEHC_168"),
        ("[2009] iehc 537", "2009_IEHC_537"),
        ("[2013] IEHc 453", "2013_IEHC_453"),
        ("[2014] iesc 12", "2014_IESC_12"),
    ],
)
def test_citation_slug_normalises_case(citation, expected):
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


# --- security: path traversal / malicious input ---------------------------

# Judge labels are attacker-influenceable (scraped from a page cell), so a
# malicious value must never survive into a path-bearing filename.
MALICIOUS_JUDGES = [
    "../../.bashrc",
    "../../../etc/passwd",
    "/etc/passwd",
    "foo/bar",
    "a/../../b",
    r"..\..\windows\system32",
    r"C:\Windows\System32",
    "....//....//",
    ".bashrc",
    "..",
    ".",
    "\x00null",
    "x" * 500,
]


@pytest.mark.parametrize("judge", MALICIOUS_JUDGES)
def test_pdf_filename_is_traversal_safe(judge, tmp_path):
    name = pdf_filename("[2026] IESC 36", judge)

    # The result is a single, plain filename component -- no separators,
    # no parent refs, not hidden, capped in length, always a .pdf.
    assert "/" not in name
    assert "\\" not in name
    assert ".." not in name
    assert not name.startswith(".")
    assert name == Path(name).name
    assert name.endswith(".pdf")
    assert len(name) <= 255

    # Joined onto an output directory, it stays a direct child of it.
    assert (tmp_path / name).resolve().parent == tmp_path.resolve()


@pytest.mark.parametrize(
    "citation",
    [
        "[2026] IESC 36/../../../etc/passwd",
        "[2026] IESC 36/../secret",
        "[2026] IESC 36 && rm -rf /",
    ],
)
def test_pdf_filename_drops_trailing_citation_garbage(citation):
    # citation_slug captures only [year] CODE num; anything after is discarded.
    assert pdf_filename(citation, "Woulfe J.") == "2026_IESC_36_Woulfe-J.pdf"


@pytest.mark.parametrize(
    "citation", ["../../etc/passwd", "/etc/passwd", "[bad]", "..", ""]
)
def test_pdf_filename_rejects_non_citation(citation):
    with pytest.raises(MissingCitationError):
        pdf_filename(citation, "Woulfe J.")


def test_safe_output_path_accepts_plain_filename(tmp_path):
    path = safe_output_path(tmp_path, "2026_IESC_36_Woulfe-J.pdf")
    assert path == (tmp_path / "2026_IESC_36_Woulfe-J.pdf").resolve()
    assert path.parent == tmp_path.resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.pdf",
        "../../.bashrc",
        "/etc/passwd",
        "sub/dir.pdf",
        r"..\evil.pdf",
        "..",
        ".",
        "",
        ".hidden",
    ],
)
def test_safe_output_path_rejects_traversal(tmp_path, bad):
    with pytest.raises(UnsafeFilenameError):
        safe_output_path(tmp_path, bad)
