"""Date parsing for Irish-formatted dates.

The site presents dates in two forms, and the day always comes before the
month (Irish/British convention), so we must parse explicitly rather than let a
generic parser guess -- ``02/07/2026`` is 2 July, never 7 February.

* Search results table: ``dd/mm/yyyy`` (e.g. ``02/07/2026``).
* Judgment view page:   ``DD Month YYYY`` (e.g. ``02 July 2026``).

Month names are matched against an explicit English table rather than
``strptime("%B")`` so parsing does not depend on the process's ``LC_TIME``
locale -- under a non-English locale ``%B`` would silently fail to match
"July" and the date would be dropped. All parsers normalise to ISO 8601
(``YYYY-MM-DD``) for storage and export.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}

# "2 July 2026" / "02 July 2026" -- day, month name, four-digit year.
_LONG_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")


def parse_irish_date(value: str | None) -> str | None:
    """Parse an Irish-formatted date string into an ISO 8601 date.

    Args:
        value: A date such as ``"02/07/2026"`` or ``"02 July 2026"``. Empty or
            ``None`` input yields ``None`` (a legitimately absent date).

    Returns:
        The date as ``"YYYY-MM-DD"``, or ``None`` if the input was empty.

    Raises:
        ValueError: If a non-empty value matches none of the known formats.
            Callers may catch this to record the raw value instead of aborting.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    # Numeric day-first form (locale-independent by construction).
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError:
        pass

    # Long form with an English month name, matched without relying on locale.
    match = _LONG_DATE_RE.match(text)
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month is not None:
            try:
                return date(int(year), month, int(day)).isoformat()
            except ValueError:
                pass  # e.g. 31 February -> fall through to the error below

    raise ValueError(f"unrecognised Irish date format: {value!r}")
