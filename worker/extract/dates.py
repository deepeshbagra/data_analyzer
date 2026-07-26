"""Business dates, parsed day-first with ambiguity reported rather than hidden.

The whole module exists for one string: ``03/04/2026``. India writes day-first
and so does most Indian invoicing software, but US-authored templates in the
same inbox do not. Choosing wrong moves a document across a month boundary,
which quietly corrupts ageing buckets, GST period totals and every
late-payment rule -- and leaves a date that looks entirely reasonable
afterwards.

:func:`parse_date` therefore returns ``ambiguous=True`` whenever both readings
are valid and different. The caller records that on the field's
``field_confidence`` entry, so the review queue can surface exactly the dates a
human needs to confirm instead of all of them.

No third-party date library: ``dateutil`` guesses, and prefers to return
*something*. Here, refusing is a feature.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from worker.extract.common import DateParseError, is_placeholder, normalize_whitespace

#: Business documents outside this window are OCR damage, not history. A 1899
#: invoice date has never once been correct; a two-digit year misread as a
#: century has.
MIN_YEAR = 1900
MAX_YEAR = 2100

#: Two-digit years below this pivot are 20xx, at or above it are 19xx. Fixed,
#: not computed from today: a pivot relative to "now" would make the same
#: document parse to a different year on re-processing, defeating the
#: reproducibility that document.extractor_model_version exists to provide.
YEAR_PIVOT = 68

_MONTHS_IN_YEAR = 12
_ISO_YEAR_DIGITS = 4
_TWO_DIGIT_YEAR_CEILING = 100


@dataclass(frozen=True)
class ParsedDate:
    """A date plus whether the source string could have meant another one.

    Attributes:
        value: the resolved date.
        ambiguous: True when day-first and month-first both yield valid but
            *different* dates. ``15/04/2026`` is not ambiguous (15 cannot be a
            month); ``03/04/2026`` is; ``04/04/2026`` is not (same result).
    """

    value: dt.date
    ambiguous: bool


_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

# Statement exports carry a time the business date does not want.
_TIME_SUFFIX_RE = re.compile(r"[\s,]*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]\.?M\.?)?$", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"(?<=\d)(?:ST|ND|RD|TH)", re.IGNORECASE)

# The backreference on the separator is deliberate: "15/04-2026" is a mangled
# cell, not a date.
_NUMERIC_RE = re.compile(r"(\d{1,4})([/\-.])(\d{1,2})\2(\d{1,4})")
_DAY_MONTH_NAME_RE = re.compile(r"(\d{1,2})[\s\-/.,]+([A-Za-z]{3,9})[\s\-/.,]+(\d{2,4})")
_MONTH_NAME_DAY_RE = re.compile(r"([A-Za-z]{3,9})[\s\-/.,]+(\d{1,2})[\s\-/.,]+(\d{2,4})")


def parse_date(raw: str, *, dayfirst: bool = True) -> ParsedDate:
    """Parse a business date.

    Args:
        raw: the string as extracted.
        dayfirst: how to resolve a genuinely ambiguous numeric date. Defaults
            to True (Indian convention). Pass False only when the caller knows
            the template is month-first -- a per-template setting, never a
            per-document guess.

    Raises:
        DateParseError: on an unrecognised shape, an impossible calendar date,
            or a year outside 1900..2100.
    """
    text = _ORDINAL_RE.sub("", _TIME_SUFFIX_RE.sub("", normalize_whitespace(raw))).strip(" ,")
    if not text:
        raise DateParseError("empty date", raw)

    match = _NUMERIC_RE.fullmatch(text)
    if match:
        return _from_numeric(match, raw, dayfirst=dayfirst)

    match = _DAY_MONTH_NAME_RE.fullmatch(text)
    if match:
        day, month_name, year = match.groups()
        return _build(_expand_year(int(year)), _month(month_name, raw), int(day), raw)

    match = _MONTH_NAME_DAY_RE.fullmatch(text)
    if match:
        month_name, day, year = match.groups()
        return _build(_expand_year(int(year)), _month(month_name, raw), int(day), raw)

    raise DateParseError("unrecognised date format", raw)


def parse_optional_date(raw: str | None) -> ParsedDate | None:
    """``None`` for a blank cell, otherwise :func:`parse_date`."""
    if raw is None or is_placeholder(raw):
        return None
    return parse_date(raw)


def _from_numeric(match: re.Match[str], raw: str, *, dayfirst: bool) -> ParsedDate:
    first_token, _, second_token, year_token = match.groups()
    first, second = int(first_token), int(second_token)

    # A four-digit leading component is ISO, and ISO is never ambiguous.
    if len(first_token) == _ISO_YEAR_DIGITS:
        return _build(first, second, int(year_token), raw)

    year = _expand_year(int(year_token))

    if first > _MONTHS_IN_YEAR and second > _MONTHS_IN_YEAR:
        raise DateParseError("neither component can be a month", raw)
    if first > _MONTHS_IN_YEAR:
        return _build(year, second, first, raw)
    if second > _MONTHS_IN_YEAR:
        # Cannot be day-first, so this is month-first and settled, not guessed.
        return _build(year, first, second, raw)

    day, month = (first, second) if dayfirst else (second, first)
    return _build(year, month, day, raw, ambiguous=first != second)


def _expand_year(year: int) -> int:
    if year >= _TWO_DIGIT_YEAR_CEILING:
        return year
    return 2000 + year if year <= YEAR_PIVOT else 1900 + year


def _month(name: str, raw: str) -> int:
    month = _MONTHS.get(name.lower())
    if month is None:
        raise DateParseError(f"unknown month name {name!r}", raw)
    return month


def _build(year: int, month: int, day: int, raw: str, *, ambiguous: bool = False) -> ParsedDate:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise DateParseError(f"year {year} is outside {MIN_YEAR}..{MAX_YEAR}", raw)
    try:
        value = dt.date(year, month, day)
    except ValueError as exc:
        raise DateParseError(str(exc), raw) from exc
    return ParsedDate(value=value, ambiguous=ambiguous)
