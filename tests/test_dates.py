import pytest

from courts_scraper.dates import parse_irish_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02/07/2026", "2026-07-02"),
        ("02 July 2026", "2026-07-02"),
        ("31/12/1999", "1999-12-31"),
        ("1 January 2001", "2001-01-01"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_parse_irish_date(raw, expected):
    assert parse_irish_date(raw) == expected


def test_day_first_not_month_first():
    # 02/07 is 2 July (day-first), never 7 February.
    assert parse_irish_date("02/07/2026") == "2026-07-02"


def test_unrecognised_format_raises():
    with pytest.raises(ValueError, match="unrecognised"):
        parse_irish_date("July 2nd, 2026")


def test_invalid_month_name_raises():
    with pytest.raises(ValueError, match="unrecognised"):
        parse_irish_date("02 Jullyy 2026")


def test_impossible_calendar_date_raises():
    with pytest.raises(ValueError, match="unrecognised"):
        parse_irish_date("31 February 2026")


def test_month_names_parsed_without_locale(monkeypatch):
    # Force a non-English LC_TIME to prove parsing does not depend on locale.
    import locale

    try:
        locale.setlocale(locale.LC_TIME, "fr_FR.UTF-8")
    except locale.Error:
        pytest.skip("fr_FR.UTF-8 locale not available")
    try:
        assert parse_irish_date("02 July 2026") == "2026-07-02"
    finally:
        locale.setlocale(locale.LC_TIME, "C")
