"""Date parsing. Written before ``worker/extract/dates.py`` existed.

The hard case is not exotic formats, it is ``03/04/2026``. India writes
day-first, most invoicing software exports day-first, and a handful of
US-authored templates do not. Guessing wrong moves a document across a month
boundary, which silently corrupts ageing buckets, GST period totals and the
late-payment rules -- all of which look perfectly plausible afterwards.

So the parser reports ambiguity rather than hiding it, and the caller records
that on the field's confidence entry.
"""

from __future__ import annotations

import datetime as dt

import pytest

from worker.extract.common import DateParseError
from worker.extract.dates import parse_date, parse_optional_date

# ---------------------------------------------------------------------------
# Formats that carry no ambiguity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ISO, and the four-digit leading year that identifies it.
        ("2026-04-15", dt.date(2026, 4, 15)),
        ("2026/04/15", dt.date(2026, 4, 15)),
        # Day > 12 settles the order regardless of convention.
        ("15/04/2026", dt.date(2026, 4, 15)),
        ("15-04-2026", dt.date(2026, 4, 15)),
        ("15.04.2026", dt.date(2026, 4, 15)),
        ("15/4/2026", dt.date(2026, 4, 15)),
        # Month name, either order.
        ("15 Apr 2026", dt.date(2026, 4, 15)),
        ("15 April 2026", dt.date(2026, 4, 15)),
        ("15-Apr-2026", dt.date(2026, 4, 15)),
        ("15/Apr/2026", dt.date(2026, 4, 15)),
        ("15th April 2026", dt.date(2026, 4, 15)),
        ("15th April, 2026", dt.date(2026, 4, 15)),
        ("Apr 15, 2026", dt.date(2026, 4, 15)),
        ("April 15 2026", dt.date(2026, 4, 15)),
        ("1st Sept 2026", dt.date(2026, 9, 1)),
        # Case and stray whitespace are noise.
        ("  15  APRIL   2026 ", dt.date(2026, 4, 15)),
        ("15 apr 2026", dt.date(2026, 4, 15)),
        # Bank statements carry a time the business date does not want.
        ("15/04/2026 10:30", dt.date(2026, 4, 15)),
        ("15/04/2026 10:30:45 AM", dt.date(2026, 4, 15)),
    ],
)
def test_unambiguous_formats(raw: str, expected: dt.date) -> None:
    parsed = parse_date(raw)
    assert parsed.value == expected
    assert parsed.ambiguous is False


# ---------------------------------------------------------------------------
# The 03/04 problem
# ---------------------------------------------------------------------------


def test_day_first_is_the_default_and_ambiguity_is_reported() -> None:
    parsed = parse_date("03/04/2026")
    assert parsed.value == dt.date(2026, 4, 3)
    assert parsed.ambiguous is True


def test_month_first_can_be_requested_for_a_known_us_template() -> None:
    parsed = parse_date("03/04/2026", dayfirst=False)
    assert parsed.value == dt.date(2026, 3, 4)
    assert parsed.ambiguous is True


def test_impossible_month_falls_back_to_the_other_order() -> None:
    """``04/13/2026`` cannot be day-first, so it is not ambiguous -- it is US."""
    parsed = parse_date("04/13/2026")
    assert parsed.value == dt.date(2026, 4, 13)
    assert parsed.ambiguous is False


def test_equal_components_are_not_ambiguous() -> None:
    """``04/04/2026`` is the same date under either reading."""
    parsed = parse_date("04/04/2026")
    assert parsed.value == dt.date(2026, 4, 4)
    assert parsed.ambiguous is False


def test_a_named_month_removes_the_ambiguity_entirely() -> None:
    assert parse_date("03 April 2026").value == dt.date(2026, 4, 3)
    assert parse_date("03 April 2026").ambiguous is False


# ---------------------------------------------------------------------------
# Two-digit years
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("15/04/26", dt.date(2026, 4, 15)),
        ("15-Apr-26", dt.date(2026, 4, 15)),
        ("15/04/99", dt.date(1999, 4, 15)),
        ("15/04/68", dt.date(2068, 4, 15)),
        ("15/04/69", dt.date(1969, 4, 15)),
    ],
)
def test_two_digit_year_pivot_is_fixed_not_relative(raw: str, expected: dt.date) -> None:
    """The pivot is 68/69, and it is a constant.

    A pivot computed from "today" would make the same document parse to a
    different year depending on when it was re-processed, which breaks the
    reproducibility the extractor columns exist to provide.
    """
    assert parse_date(raw).value == expected


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "31/02/2026",  # not a real day
        "32/01/2026",
        "00/01/2026",
        "15/00/2026",
        "13/13/2026",  # neither component can be a month
        "April 2026",  # no day
        "2026",
        "20260415",  # digit soup: could be anything
        "15/04/2026/01",
        "15 Bananas 2026",
        "15/04/1799",  # outside the plausible window
        "15/04/2201",
        "",
        "   ",
        "N/A",
    ],
)
def test_unparseable_dates_raise(raw: str) -> None:
    with pytest.raises(DateParseError):
        parse_date(raw)


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "N/A", "NA", "Nil", "None"])
def test_placeholders_are_absence(raw: str | None) -> None:
    assert parse_optional_date(raw) is None


def test_optional_date_still_parses_real_values() -> None:
    parsed = parse_optional_date("15/04/2026")
    assert parsed is not None
    assert parsed.value == dt.date(2026, 4, 15)


def test_error_message_carries_the_raw_value() -> None:
    """The review UI shows the reviewer what the extractor actually saw."""
    with pytest.raises(DateParseError) as exc:
        parse_date("31/02/2026")
    assert "31/02/2026" in str(exc.value)
