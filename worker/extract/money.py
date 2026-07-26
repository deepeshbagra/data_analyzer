"""Monetary amounts, as they actually appear on Indian business documents.

Money is ``Decimal`` end to end (DECISIONS #7). Nothing in this module accepts
or produces a float, and every returned value is quantised to four decimal
places -- the scale of ``numeric(18, 4)`` -- with ``ROUND_HALF_UP``.

The design rule throughout is **parse the unambiguous, refuse the rest**. Where
two readings of a string are both defensible, the parser raises instead of
picking one, and the field goes to review. A silent wrong amount survives every
downstream check; a raised error does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

from worker.extract.common import MoneyParseError, is_placeholder, normalize_whitespace

#: The scale of numeric(18, 4). Every amount is quantised to this.
MONEY_EXPONENT = Decimal("0.0001")

#: numeric(18, 4) holds 14 integer digits. Anything at or above this would fail
#: at INSERT with a Postgres numeric overflow, so it fails here instead, where
#: the error can name the field and the page.
MAX_MONEY = Decimal(10) ** 14

#: Indian grouping: the lowest group is three digits, every group above it two.
LAST_GROUP_DIGITS = 3
HIGHER_GROUP_DIGITS = 2

_PAISE_PER_RUPEE = 100
#: "()" plus at least one character before a parenthesised negative is credible.
_MIN_PARENTHESISED = 2


class AmountMarker(StrEnum):
    """A ``Dr``/``Cr`` annotation found next to an amount.

    Reported, never applied. DECISIONS #11 keeps magnitude and direction in
    separate columns precisely because statement formats disagree about what a
    sign means; folding the marker into the sign here would bake one bank's
    convention into the number itself.
    """

    DEBIT = "debit"
    CREDIT = "credit"


@dataclass(frozen=True)
class ParsedAmount:
    """An amount plus whatever the document said about it.

    Attributes:
        amount: signed, exact, quantised to four decimal places.
        currency: ISO 4217 code if a symbol or code was present, else None.
            None means "the document did not say", not "INR".
        marker: the Dr/Cr annotation, if any. See :class:`AmountMarker`.
    """

    amount: Decimal
    currency: str | None
    marker: AmountMarker | None


# --- Affix vocabulary -------------------------------------------------------

_CURRENCY_CODES = {
    "₹": "INR",
    "RS": "INR",
    "RS.": "INR",
    "INR": "INR",
    "US$": "USD",
    "USD": "USD",
    "$": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "¥": "JPY",
    "JPY": "JPY",
    "AED": "AED",
    "SGD": "SGD",
}

# Longest-first within each family so "US$" is not eaten by "$".
_CURRENCY_ALT = r"₹|RS\.?|INR|US\$|USD|\$|EUR|€|GBP|£|JPY|¥|AED|SGD"
_CURRENCY_PREFIX_RE = re.compile(rf"^({_CURRENCY_ALT})\s*")
_CURRENCY_SUFFIX_RE = re.compile(rf"\s*({_CURRENCY_ALT})$")

_MARKER_PREFIX_RE = re.compile(r"^(DR|CR)\.?\s*")
_MARKER_SUFFIX_RE = re.compile(r"\s*(DR|CR)\.?$")
_MARKERS = {"DR": AmountMarker.DEBIT, "CR": AmountMarker.CREDIT}

# U+002D hyphen, U+2212 minus, U+2013/U+2014 dashes used as a minus by exporters.
# Written as escapes: the four are visually indistinguishable in source.
_SIGNS = "-+\u2212\u2013\u2014"
_SIGN_PREFIX_RE = re.compile(rf"^([{re.escape(_SIGNS)}])\s*")
_SIGN_SUFFIX_RE = re.compile(rf"\s*([{re.escape(_SIGNS)}])$")

# "/-" and "/=" terminate a rupee figure on most Indian invoices; "ONLY" and a
# leading "RUPEES" come from the same house style.
_NOISE_SUFFIX_RE = re.compile(r"\s*(?:/[-=]|\bONLY)$")
_NOISE_PREFIX_RE = re.compile(r"^(?:RUPEES|RUPEE)\b[:\s]*")

# --- Number shapes ----------------------------------------------------------

_NUMERIC_RE = re.compile(r"[\d.,]+")
#: 12,34,567 -- last group of three, everything above it in twos.
_INDIAN_GROUPS_RE = re.compile(r"\d{1,2}(?:,\d{2})*,\d{3}")
#: 1,234,567 -- uniform groups of three.
_WESTERN_GROUPS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
#: 1.234.567,89 -- dot groups with a comma decimal. The *only* comma-as-decimal
#: form accepted, because it is the only one that cannot also be read as Indian
#: or Western grouping.
_EUROPEAN_RE = re.compile(r"\d{1,3}(?:\.\d{3})+,\d{1,2}")


@dataclass
class _Affixes:
    """Mutable state threaded through the affix-stripping loop."""

    negative: bool = False
    signed: bool = False
    currency: str | None = None
    marker: AmountMarker | None = None


def parse_amount(raw: str) -> ParsedAmount:
    """Parse one amount string into an exact :class:`ParsedAmount`.

    Accepts Indian (``1,23,456.78``), Western (``123,456.78``) and unambiguous
    European (``1.234.567,89``) grouping, an optional currency symbol or code
    on either side, parentheses or a leading/trailing minus for negatives, a
    ``Dr``/``Cr`` marker, and the ``/-`` and ``Only`` suffixes.

    Raises:
        MoneyParseError: on anything else, including digit grouping that
            matches no convention -- which is the signal that OCR damaged the
            figure, and is worth a review rather than a guess.
    """
    text = normalize_whitespace(raw).upper()
    if not text:
        raise MoneyParseError("empty amount", raw)

    affixes = _Affixes()
    text = _strip_affixes(text, affixes).replace(" ", "")

    if not text or not _NUMERIC_RE.fullmatch(text) or not any(c.isdigit() for c in text):
        raise MoneyParseError("not a recognisable amount", raw)

    integer_digits, fraction_digits = _split_number(text, raw)
    try:
        value = Decimal(f"{integer_digits or '0'}.{fraction_digits or '0'}")
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regexes
        raise MoneyParseError("not a recognisable amount", raw) from exc

    if affixes.negative:
        value = -value
    # Explicit rounding rather than relying on the global context set in
    # api.settings: this module is imported by the worker, by tests and
    # eventually by scripts, and the rounding of money must not depend on
    # whether some other import happened first.
    value = value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)

    if abs(value) >= MAX_MONEY:
        raise MoneyParseError("amount does not fit numeric(18, 4)", raw)

    return ParsedAmount(amount=value, currency=affixes.currency, marker=affixes.marker)


def parse_money(raw: str) -> Decimal:
    """The amount alone. Use when currency and Dr/Cr are already known."""
    return parse_amount(raw).amount


def parse_optional_money(raw: str | None) -> Decimal | None:
    """``None`` for a blank cell, otherwise :func:`parse_money`.

    A blank debit column means "no debit", not "a debit of zero". Collapsing
    the two would invent rows the document does not contain.
    """
    if raw is None or is_placeholder(raw):
        return None
    return parse_money(raw)


def _strip_affixes(text: str, state: _Affixes) -> str:
    """Peel currency, sign, markers and noise off both ends, in any order.

    Order genuinely varies -- ``-₹1,234``, ``₹-1,234`` and ``(₹1,234) Dr`` all
    occur -- so this loops until nothing more comes off rather than assuming a
    fixed sequence. Each affix can only be taken once, so the loop terminates.
    """
    for _ in range(8):
        before = text
        text = _strip_parentheses(text, state)
        text = _strip_marker(text, state)
        # Noise before sign, and the order is load-bearing: the "/-" that ends
        # a rupee figure ends in a hyphen, and a sign pass run first would take
        # that hyphen as a minus and turn 1,23,456/- negative.
        text = _strip_noise(text)
        text = _strip_currency(text, state)
        text = _strip_sign(text, state)
        if text == before:
            break
    return text.strip()


def _strip_parentheses(text: str, state: _Affixes) -> str:
    if len(text) > _MIN_PARENTHESISED and text.startswith("(") and text.endswith(")"):
        state.negative = not state.negative
        return text[1:-1].strip()
    return text


def _strip_marker(text: str, state: _Affixes) -> str:
    if state.marker is not None:
        return text
    for pattern in (_MARKER_SUFFIX_RE, _MARKER_PREFIX_RE):
        match = pattern.search(text)
        if match:
            state.marker = _MARKERS[match.group(1)]
            return (text[: match.start()] + text[match.end() :]).strip()
    return text


def _strip_currency(text: str, state: _Affixes) -> str:
    if state.currency is not None:
        return text
    for pattern in (_CURRENCY_PREFIX_RE, _CURRENCY_SUFFIX_RE):
        match = pattern.search(text)
        if match:
            state.currency = _CURRENCY_CODES[match.group(1)]
            return (text[: match.start()] + text[match.end() :]).strip()
    return text


def _strip_sign(text: str, state: _Affixes) -> str:
    if state.signed:
        return text
    for pattern in (_SIGN_PREFIX_RE, _SIGN_SUFFIX_RE):
        match = pattern.search(text)
        if match:
            state.signed = True
            if match.group(1) != "+":
                state.negative = not state.negative
            return (text[: match.start()] + text[match.end() :]).strip()
    return text


def _strip_noise(text: str) -> str:
    text = _NOISE_SUFFIX_RE.sub("", text)
    return _NOISE_PREFIX_RE.sub("", text).strip()


def _split_number(token: str, raw: str) -> tuple[str, str]:
    """Split a grouped numeric token into (integer digits, fraction digits).

    Rejects any grouping that matches neither the Indian nor the Western
    convention. ``1,50`` is the case that motivates this: it is 150 under
    Indian grouping and 1.50 under European, and no amount of context in this
    function can settle which. Refusing sends it to review; guessing would put
    a hundredfold error into a ledger.
    """
    if _EUROPEAN_RE.fullmatch(token):
        integer_part, _, fraction = token.rpartition(",")
        return integer_part.replace(".", ""), fraction

    if token.count(".") > 1:
        raise MoneyParseError("more than one decimal point", raw)
    if "," in token and "." in token and token.rindex(",") > token.rindex("."):
        raise MoneyParseError("ambiguous decimal separator", raw)

    integer_part, _, fraction = token.partition(".")
    if fraction and not fraction.isdigit():
        raise MoneyParseError("non-numeric fraction", raw)

    if "," in integer_part:
        indian = _INDIAN_GROUPS_RE.fullmatch(integer_part)
        western = _WESTERN_GROUPS_RE.fullmatch(integer_part)
        if not indian and not western:
            raise MoneyParseError("digit grouping matches no convention", raw)
    elif integer_part and not integer_part.isdigit():
        raise MoneyParseError("non-numeric integer part", raw)

    return integer_part.replace(",", ""), fraction


# ---------------------------------------------------------------------------
# Percentages
# ---------------------------------------------------------------------------

_PERCENT_RE = re.compile(r"\s*(?:%|PERCENT|PCT)\s*$")


def parse_percentage(raw: str) -> Decimal:
    """Parse a tax or discount rate into ``numeric(9, 4)`` range.

    ``"18%"``, ``"18.00 %"`` and ``"18"`` all yield ``Decimal("18.0000")``.
    Values outside 0..100 are rejected: a GST rate of 101% or -5% is OCR damage
    or a misread column, and letting it through would silently distort every
    tax reconciliation that reads the line.
    """
    text = _PERCENT_RE.sub("", normalize_whitespace(raw).upper())
    if not text:
        raise MoneyParseError("empty percentage", raw)
    value = parse_money(text)
    if not (Decimal(0) <= value <= Decimal(100)):
        raise MoneyParseError("percentage outside 0..100", raw)
    return value


# ---------------------------------------------------------------------------
# Amounts in words
# ---------------------------------------------------------------------------

_WORD_VALUES = {
    "zero": 0,
    "nil": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fourty": 40,  # a common misspelling on hand-filled invoices
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

_HUNDRED_WORDS = frozenset({"hundred", "hundreds"})

#: Indian scale words. Crore and lakh are the reason a Western words parser is
#: not reusable here.
_SCALE_WORDS = {
    "thousand": 1_000,
    "thousands": 1_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}

_IGNORED_WORDS = frozenset({"and", "only", "rupees", "rupee", "rs", "inr", "of", "the"})
_PAISE_WORDS = frozenset({"paise", "paisa", "paises", "paisas"})

_NON_LETTER_RE = re.compile(r"[^a-z\s]")


def parse_amount_in_words(raw: str) -> Decimal:
    """Parse the "Amount in words" line found on every Indian tax invoice.

    This exists to be an *independent* read of the same total. Comparing it
    against the parsed figure catches the failure OCR is most likely to
    produce and least likely to make obvious: a dropped or duplicated digit,
    which leaves a perfectly well-formed number that is off by a factor of ten.

    Handles both orderings of the fractional part -- ``"... and Seventy Eight
    Paise Only"`` and ``"... and Paise Seventy Eight Only"``.

    Raises:
        MoneyParseError: on an unknown word, on scale words that do not
            strictly decrease (``"One Thousand Two Lakh"``), or on a paise
            component of 100 or more.
    """
    text = _NON_LETTER_RE.sub(" ", normalize_whitespace(raw).lower())
    tokens = text.split()
    rupee_tokens, paise_tokens = _split_paise(tokens)

    rupees = _words_to_int(rupee_tokens, raw) if _meaningful(rupee_tokens) else 0
    paise = _words_to_int(paise_tokens, raw) if _meaningful(paise_tokens) else 0

    if not _meaningful(rupee_tokens) and not _meaningful(paise_tokens):
        raise MoneyParseError("no number words found", raw)
    if paise >= _PAISE_PER_RUPEE:
        raise MoneyParseError("paise component is not below 100", raw)

    value = (Decimal(rupees) + Decimal(paise) / _PAISE_PER_RUPEE).quantize(
        MONEY_EXPONENT, rounding=ROUND_HALF_UP
    )
    if value >= MAX_MONEY:
        raise MoneyParseError("amount does not fit numeric(18, 4)", raw)
    return value


def _meaningful(tokens: list[str]) -> bool:
    return any(token not in _IGNORED_WORDS for token in tokens)


def _split_paise(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split the token run at the paise keyword, whichever side the digits sit.

    ``"... Fifty Six and Seventy Eight Paise Only"`` puts the fraction *before*
    the keyword; ``"... Fifty Six and Paise Seventy Eight Only"`` puts it after.
    Both are common, and the difference is not decorative -- reading the words
    before the keyword as part of the rupee total would give 123534 instead of
    123456.78.
    """
    for index, token in enumerate(tokens):
        if token not in _PAISE_WORDS:
            continue
        tail = tokens[index + 1 :]
        if _meaningful(tail):
            return tokens[:index], tail
        head = tokens[:index]
        for back in range(len(head) - 1, -1, -1):
            if head[back] == "and":
                return head[:back], head[back + 1 :]
        return head, []
    return tokens, []


def _words_to_int(tokens: list[str], raw: str) -> int:
    total = 0
    current = 0
    last_scale: int | None = None

    for token in tokens:
        if token in _IGNORED_WORDS:
            continue
        if token in _WORD_VALUES:
            current += _WORD_VALUES[token]
        elif token in _HUNDRED_WORDS:
            current = (current or 1) * 100
        elif token in _SCALE_WORDS:
            scale = _SCALE_WORDS[token]
            # "One Thousand Two Lakh" is not a number anyone writes; it is a
            # transcription error, and reading it as 1000 + 200000 would hide
            # that.
            if last_scale is not None and scale >= last_scale:
                raise MoneyParseError("scale words do not decrease", raw)
            last_scale = scale
            total += (current or 1) * scale
            current = 0
        else:
            raise MoneyParseError(f"unrecognised number word {token!r}", raw)

    return total + current


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_indian(value: Decimal, *, places: int = 2) -> str:
    """Render an amount with Indian digit grouping: ``1,23,456.78``.

    The inverse of :func:`parse_money` for every value the column can hold,
    which is asserted as a property test. That round trip is what lets a
    reviewer trust that the figure on screen is the figure in the row.
    """
    quantised = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if quantised < 0 else ""
    integer_part, _, fraction = f"{abs(quantised):f}".partition(".")

    if len(integer_part) > LAST_GROUP_DIGITS:
        head, tail = integer_part[:-LAST_GROUP_DIGITS], integer_part[-LAST_GROUP_DIGITS:]
        groups: list[str] = []
        while len(head) > HIGHER_GROUP_DIGITS:
            groups.insert(0, head[-HIGHER_GROUP_DIGITS:])
            head = head[:-HIGHER_GROUP_DIGITS]
        if head:
            groups.insert(0, head)
        integer_part = ",".join([*groups, tail])

    return f"{sign}{integer_part}" + (f".{fraction}" if fraction else "")
