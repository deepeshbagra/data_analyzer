"""GSTIN, PAN, IFSC and document-number keys.

The GSTINs asserted valid here are published examples whose check digits are
independently known, not values produced by the implementation under test. A
checksum test that generates its own expectations proves only that the code
agrees with itself.
"""

from __future__ import annotations

import pytest

from worker.extract.common import IdentifierError
from worker.extract.identifiers import (
    document_number_key,
    gstin_check_digit,
    is_valid_gstin,
    normalize_gstin,
    normalize_ifsc,
    normalize_pan,
    pan_from_gstin,
)

#: Published GSTINs. Structure and check digit both known-good.
VALID_GSTINS = ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4", "09AAACH7409R1ZZ"]


# ---------------------------------------------------------------------------
# GSTIN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gstin", VALID_GSTINS)
def test_valid_gstins_are_accepted_unchanged(gstin: str) -> None:
    assert normalize_gstin(gstin) == gstin
    assert is_valid_gstin(gstin) is True


@pytest.mark.parametrize("gstin", VALID_GSTINS)
def test_check_digit_matches_the_published_value(gstin: str) -> None:
    assert gstin_check_digit(gstin[:14]) == gstin[14]


@pytest.mark.parametrize(
    "raw",
    [
        "27 AAPFU 0939 F1ZV",
        "27aapfu0939f1zv",
        "  27AAPFU0939F1ZV  ",
        "27-AAPFU-0939-F1ZV",
    ],
)
def test_formatting_noise_is_canonicalised_away(raw: str) -> None:
    assert normalize_gstin(raw) == "27AAPFU0939F1ZV"


def test_single_character_error_is_caught_by_the_check_digit() -> None:
    """The point of validating the checksum at all.

    ``F`` -> ``E`` in the PAN section is exactly the kind of slip OCR makes,
    and it produces a structurally perfect GSTIN. Without the check digit this
    would silently become a second vendor.
    """
    with pytest.raises(IdentifierError, match="check digit"):
        normalize_gstin("27AAPEU0939F1ZV")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("27AAPFU0939F1ZW", "check digit"),  # last character wrong
        ("27AAPFU0939F1Z", "15 characters"),  # truncated
        ("27AAPFU0939F1ZVX", "15 characters"),  # a digit doubled
        ("AA27PFU0939F1ZV", "structure"),  # state code not leading
        ("27AAPFU0939F1XV", "structure"),  # the mandatory Z is missing
        ("00AAPFU0939F1ZV", "state code"),  # 00 is not an assigned state
        ("40AAPFU0939F1ZV", "state code"),  # 40 is not an assigned state
        ("", "empty"),
        ("N/A", "empty"),
    ],
)
def test_malformed_gstins_are_rejected(raw: str, reason: str) -> None:
    with pytest.raises(IdentifierError, match=reason):
        normalize_gstin(raw)


def test_ocr_confusions_are_reported_not_repaired() -> None:
    """``O`` for ``0`` is a guess about who the vendor is, so we do not make it.

    Repairing here would mean the check digit ends up validating our own
    correction instead of the document, which is worse than useless: it would
    convert an obviously bad read into a confidently wrong one.
    """
    with pytest.raises(IdentifierError):
        normalize_gstin("27AAPFUO939F1ZV")  # letter O in place of zero


def test_is_valid_gstin_never_raises() -> None:
    assert is_valid_gstin(None) is False
    assert is_valid_gstin("") is False
    assert is_valid_gstin("nonsense") is False
    assert is_valid_gstin("27AAPFU0939F1ZW") is False


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AAPFU0939F", "AAPFU0939F"),
        ("aapfu0939f", "AAPFU0939F"),
        ("AAPFU 0939 F", "AAPFU0939F"),
    ],
)
def test_normalize_pan(raw: str, expected: str) -> None:
    assert normalize_pan(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["AAPFU0939", "AAPF0939F", "AAPFU09399", "AAPFU0939FF", "1APFU0939F", ""],
)
def test_malformed_pans_are_rejected(raw: str) -> None:
    with pytest.raises(IdentifierError):
        normalize_pan(raw)


def test_pan_extracted_from_a_gstin_inherits_its_check_digit() -> None:
    """Prefer this over reading the PAN separately off the same page.

    The embedded PAN is covered by the GSTIN checksum; a PAN read on its own
    has no checksum at all, so a mis-read one is undetectable.
    """
    assert pan_from_gstin("27AAPFU0939F1ZV") == "AAPFU0939F"
    assert pan_from_gstin("29AAGCB7383J1Z4") == "AAGCB7383J"
    assert normalize_pan(pan_from_gstin("27AAPFU0939F1ZV")) == "AAPFU0939F"


def test_pan_is_not_extracted_from_an_invalid_gstin() -> None:
    with pytest.raises(IdentifierError):
        pan_from_gstin("27AAPFU0939F1ZW")


# ---------------------------------------------------------------------------
# IFSC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HDFC0000123", "HDFC0000123"),
        ("hdfc0000123", "HDFC0000123"),
        ("HDFC 0000123", "HDFC0000123"),
        ("SBIN0011513", "SBIN0011513"),
    ],
)
def test_normalize_ifsc(raw: str, expected: str) -> None:
    assert normalize_ifsc(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "HDFCO000123",  # letter O where the reserved zero must be
        "HDFC1000123",  # fifth character is not zero
        "HDF00000123",  # three-letter bank code
        "HDFC000012",  # too short
        "HDFC00001234",  # too long
        "",
    ],
)
def test_malformed_ifsc_is_rejected(raw: str) -> None:
    with pytest.raises(IdentifierError):
        normalize_ifsc(raw)


# ---------------------------------------------------------------------------
# Document number keys
# ---------------------------------------------------------------------------


def test_the_same_invoice_written_three_ways_yields_one_key() -> None:
    """The vendor, the ledger import and the bank narration disagree on
    punctuation. Phase 2 blocks on this key, so they must agree here."""
    keys = {
        document_number_key("INV-2026/001"),
        document_number_key("inv 2026 001"),
        document_number_key("INV/2026-001"),
        document_number_key(" INV2026001 "),
    }
    assert keys == {"INV2026001"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PI/24-25/0042", "PI24250042"),
        ("#1234", "1234"),
        ("", ""),
    ],
)
def test_document_number_key(raw: str, expected: str) -> None:
    assert document_number_key(raw) == expected


def test_document_number_key_collapses_separators_knowingly() -> None:
    """A documented cost, asserted so it cannot change by accident.

    Widening the candidate set is recoverable -- scoring narrows it. Missing a
    real match because of a hyphen is not.
    """
    assert document_number_key("INV-1") == document_number_key("INV1")
