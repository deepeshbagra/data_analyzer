"""Money parsing. Written before ``worker/extract/money.py`` existed.

A wrong amount is the worst failure this system has: it is silent, it is
plausible, and it propagates into every KPI and every reconciliation downstream.
So the contract asserted here is deliberately narrow -- parse what is
unambiguous, raise on everything else -- and the raise cases get as much
coverage as the success cases.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from worker.extract.common import MoneyParseError
from worker.extract.money import (
    AmountMarker,
    format_indian,
    parse_amount,
    parse_amount_in_words,
    parse_money,
    parse_optional_money,
    parse_percentage,
)

# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Indian grouping: last three digits, then twos.
        ("1,23,456.78", "123456.78"),
        ("12,34,567", "1234567"),
        ("1,00,000", "100000"),
        ("99,99,99,999.99", "999999999.99"),
        # Western grouping, which appears on software-generated invoices.
        ("123,456.78", "123456.78"),
        ("1,234", "1234"),
        ("1,234,567.89", "1234567.89"),
        # Ungrouped.
        ("123456.78", "123456.78"),
        ("0", "0"),
        ("0.00", "0"),
        (".50", "0.50"),
        # European: dot groups, comma decimal. Only the unambiguous form.
        ("1.234.567,89", "1234567.89"),
        ("1.234,56", "1234.56"),
    ],
)
def test_grouping_conventions(raw: str, expected: str) -> None:
    assert parse_money(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "1,2345",  # neither Indian nor Western
        "12,34,5678",  # last group is four digits
        "1,23,45",  # last group is two digits
        "1,2,3",
        "1,50",  # would be 150 (Indian) or 1.50 (European) -- refuse to guess
        "12,50",
        "1.234.567",  # dot-grouped with no decimal: could be 1234567 or 1.234567
    ],
)
def test_malformed_or_ambiguous_grouping_raises(raw: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money(raw)


# ---------------------------------------------------------------------------
# Currency prefixes, suffixes and Indian invoice noise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "currency"),
    [
        ("₹ 1,23,456.78", "123456.78", "INR"),
        ("₹1,23,456.78", "123456.78", "INR"),
        ("Rs. 1,23,456.78", "123456.78", "INR"),
        ("Rs.1,23,456.78", "123456.78", "INR"),
        ("RS 1,234", "1234", "INR"),
        ("INR 1,23,456.78", "123456.78", "INR"),
        ("₨ 1,234", "1234", "INR"),  # NFKC folds the old rupee sign to "Rs"
        ("$1,234.50", "1234.50", "USD"),
        ("USD 1,234.50", "1234.50", "USD"),
        ("1,234.50 USD", "1234.50", "USD"),
        ("€1.234,56", "1234.56", "EUR"),
        ("1,234", "1234", None),  # no symbol -> no claim about currency
    ],
)
def test_currency_tokens(raw: str, expected: str, currency: str | None) -> None:
    parsed = parse_amount(raw)
    assert parsed.amount == Decimal(expected)
    assert parsed.currency == currency


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rs. 1,23,456/-", "123456"),
        ("1,23,456/-", "123456"),
        ("Rs 5,000 only", "5000"),
        ("Rupees 5,000 Only", "5000"),
        ("1 23 456.78", "123456.78"),  # OCR turned the commas into spaces
        ("\u20b9\u00a01,23,456.78", "123456.78"),  # non-breaking space
        ("\u20b9\u202f1,23,456.78", "123456.78"),  # narrow no-break space
        ("\uff11\uff12\uff13", "123"),  # full-width digits, folded by NFKC
    ],
)
def test_indian_invoice_noise_is_stripped(raw: str, expected: str) -> None:
    assert parse_money(raw) == Decimal(expected)


# ---------------------------------------------------------------------------
# Sign and direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(1,234.00)", "-1234.00"),
        ("(₹1,234.00)", "-1234.00"),
        ("₹(1,234.00)", "-1234.00"),
        ("-1,234", "-1234"),
        ("- 1,234", "-1234"),
        ("\u22121,234", "-1234"),  # unicode minus
        ("\u20131,234", "-1234"),  # en dash used as a minus
        ("-₹1,234", "-1234"),
        ("₹-1,234", "-1234"),
        ("1,234-", "-1234"),  # trailing sign, seen on some statement exports
        ("+1,234", "1234"),
    ],
)
def test_negatives(raw: str, expected: str) -> None:
    assert parse_money(raw) == Decimal(expected)


@pytest.mark.parametrize(
    ("raw", "marker"),
    [
        ("1,234.00 Cr", AmountMarker.CREDIT),
        ("1,234.00 CR", AmountMarker.CREDIT),
        ("1,234.00Cr.", AmountMarker.CREDIT),
        ("1,234.00 Dr", AmountMarker.DEBIT),
        ("Dr. 1,234.00", AmountMarker.DEBIT),
        ("1,234.00", None),
    ],
)
def test_dr_cr_markers_do_not_become_a_sign(raw: str, marker: AmountMarker | None) -> None:
    """DECISIONS #11: magnitude and direction stay separate, always.

    Folding Cr/Dr into the sign here would put the statement's own convention
    into the number, and statement conventions disagree with each other. The
    marker is reported; deciding what it means is the bank-statement parser's
    job, which knows which column it came from.
    """
    parsed = parse_amount(raw)
    assert parsed.amount == Decimal("1234.00")
    assert parsed.amount > 0
    assert parsed.marker == marker


# ---------------------------------------------------------------------------
# Scale, precision and refusal
# ---------------------------------------------------------------------------


def test_quantised_to_four_places_half_up() -> None:
    """numeric(18, 4) is the storage scale, and DECISIONS #7 fixes the rounding."""
    assert parse_money("1,234.56789") == Decimal("1234.5679")
    assert parse_money("1.00005") == Decimal("1.0001")  # half rounds up, not to even
    assert parse_money("1.00015") == Decimal("1.0002")  # ROUND_HALF_EVEN would give .0001
    assert parse_money("100").as_tuple().exponent == -4


def test_amount_too_large_for_the_column_raises() -> None:
    """Better a parse failure than a Postgres numeric overflow at INSERT."""
    assert parse_money("99999999999999.9999") == Decimal("99999999999999.9999")
    with pytest.raises(MoneyParseError):
        parse_money("100000000000000")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "N/A", "-", "abc", "Rs.", "1.2.3", "1,234.5.6", "12/34", "#REF!"],
)
def test_unparseable_input_raises(raw: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money(raw)


@pytest.mark.parametrize("raw", [None, "", "  ", "-", "—", "N/A", "NA", "Nil", "None"])
def test_placeholders_are_absence_not_zero(raw: str | None) -> None:
    """An empty debit column means "no debit", not "a debit of zero".

    Collapsing the two would create a zero-amount row that the matcher then has
    to special-case, and a zero that came from a blank cell is indistinguishable
    from a zero the bank actually printed.
    """
    assert parse_optional_money(raw) is None


def test_optional_money_still_parses_real_values() -> None:
    assert parse_optional_money("Rs. 1,23,456.78") == Decimal("123456.78")
    assert parse_optional_money("0") == Decimal("0")


# ---------------------------------------------------------------------------
# Amount in words -- the cross-check field on every Indian invoice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six Only", "123456"),
        (
            "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six "
            "and Seventy Eight Paise Only",
            "123456.78",
        ),
        (
            "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six "
            "and Paise Seventy Eight Only",
            "123456.78",
        ),
        ("INR Twelve Crore Thirty Four Lakh Fifty Six Thousand Seven Hundred", "123456700"),
        ("Rupees Five Lac Only", "500000"),
        ("Rupees Twenty Only", "20"),
        ("Rupees Zero Only", "0"),
        ("rupees one thousand five hundred", "1500"),
        ("Paise Fifty Only", "0.50"),
    ],
)
def test_amount_in_words(raw: str, expected: str) -> None:
    assert parse_amount_in_words(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "Rupees Only",
        "Rupees Twelve Bananas Only",
        "Rupees One Thousand Two Lakh Only",  # scales must decrease
        "",
        "Rupees One Hundred and Hundred Twenty Paise Only",  # paise >= 100
    ],
)
def test_amount_in_words_rejects_nonsense(raw: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_amount_in_words(raw)


def test_words_agree_with_figures() -> None:
    """The reason this function exists: an independent read of the same total."""
    assert parse_amount_in_words(
        "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six and Seventy Eight Paise Only"
    ) == parse_money("Rs. 1,23,456.78")


# ---------------------------------------------------------------------------
# Percentages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("18%", "18"), ("18.00 %", "18"), ("18", "18"), ("0%", "0"), ("2.5 percent", "2.5")],
)
def test_parse_percentage(raw: str, expected: str) -> None:
    assert parse_percentage(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["-5%", "101%", "abc", ""])
def test_parse_percentage_rejects_out_of_range(raw: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_percentage(raw)


# ---------------------------------------------------------------------------
# Formatting, and the round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123456.78", "1,23,456.78"),
        ("1234567.89", "12,34,567.89"),
        ("999.5", "999.50"),
        ("0", "0.00"),
        ("-123456.78", "-1,23,456.78"),
        ("100000", "1,00,000.00"),
    ],
)
def test_format_indian(value: str, expected: str) -> None:
    assert format_indian(Decimal(value)) == expected


_MONEY = st.decimals(
    min_value=Decimal("-99999999999999.9999"),
    max_value=Decimal("99999999999999.9999"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)


@given(_MONEY)
def test_format_then_parse_is_identity(value: Decimal) -> None:
    """Anything we render, we must be able to read back exactly.

    This is the property that keeps the review screen honest: the figure a
    reviewer sees and the figure stored in the row are the same number.
    """
    assert parse_money(format_indian(value, places=4)) == value
