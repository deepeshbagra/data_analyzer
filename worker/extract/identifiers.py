"""GSTIN, PAN, IFSC and document-number comparison keys.

These are the fields that make a vendor identifiable, so they are also the
fields where an OCR slip is most expensive: a GSTIN with one wrong character
creates a second vendor that looks like a first-time supplier, splits the
purchase history, and defeats the duplicate-invoice rules that depend on
grouping by party.

The GSTIN check digit is what makes that detectable. It is a real checksum, so
a single mis-read character fails it with probability 35/36 -- the closest
thing to free validation this pipeline gets.
"""

from __future__ import annotations

import re

from worker.extract.common import IdentifierError, is_placeholder, normalize_whitespace

#: 2-digit state code, 10-char PAN, 1-char entity number, "Z", 1 check char.
_GSTIN_RE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]")
#: 5 letters, 4 digits, 1 letter. No checksum exists for PAN.
_PAN_RE = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
#: 4-letter bank code, a mandatory "0", then a 6-character branch code.
_IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")

#: The GSTIN check-digit alphabet, in value order. Position in this string *is*
#: the character's numeric value.
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_BASE = len(_GSTIN_ALPHABET)

#: State code (2) + PAN (10) + entity number (1) + "Z" (1) + check digit (1).
GSTIN_LENGTH = 15

#: State codes 01..38 are assigned; 97 is "Other Territory" and 99 is Centre
#: Jurisdiction. Anything else is a mis-read.
_VALID_STATE_CODES = frozenset(f"{code:02d}" for code in range(1, 39)) | {"97", "99"}


def gstin_check_digit(first_fourteen: str) -> str:
    """Compute the 15th character of a GSTIN from the first fourteen.

    The published algorithm: each character's value is its index in the
    base-36 alphabet, multiplied by 1 or 2 alternating from position 0. Each
    product's quotient and remainder modulo 36 are summed, and the check digit
    is the character whose value completes that sum to a multiple of 36.
    """
    total = 0
    for index, char in enumerate(first_fourteen):
        value = _GSTIN_ALPHABET.index(char)
        product = value * (1 if index % 2 == 0 else 2)
        total += product // _GSTIN_BASE + product % _GSTIN_BASE
    return _GSTIN_ALPHABET[(_GSTIN_BASE - total % _GSTIN_BASE) % _GSTIN_BASE]


def normalize_gstin(raw: str) -> str:
    """Canonicalise and fully validate a GSTIN.

    Strips spaces and punctuation, uppercases, then checks the structural
    pattern, the state code and the check digit.

    Deliberately does *not* attempt OCR repair (``O`` for ``0``, ``I`` for
    ``1``, ``S`` for ``5``). A repaired GSTIN is a guess about who the
    counterparty is, and the check digit would then be validating our own
    correction rather than the document. Failing sends the field to a reviewer
    who can look at the page.

    Raises:
        IdentifierError: on wrong length, bad structure, an unassigned state
            code, or a check-digit mismatch.
    """
    text = _canonical(raw)
    if len(text) != GSTIN_LENGTH:
        raise IdentifierError(f"GSTIN must be {GSTIN_LENGTH} characters, got {len(text)}", raw)
    if not _GSTIN_RE.fullmatch(text):
        raise IdentifierError("GSTIN does not match the required structure", raw)
    if text[:2] not in _VALID_STATE_CODES:
        raise IdentifierError(f"unassigned GST state code {text[:2]!r}", raw)
    expected = gstin_check_digit(text[:14])
    if text[14] != expected:
        raise IdentifierError(f"GSTIN check digit is {text[14]!r}, expected {expected!r}", raw)
    return text


def is_valid_gstin(raw: str | None) -> bool:
    """Non-raising form, for scoring and blocking rather than extraction."""
    if raw is None:
        return False
    try:
        normalize_gstin(raw)
    except IdentifierError:
        return False
    return True


def normalize_pan(raw: str) -> str:
    """Canonicalise and structurally validate a PAN.

    There is no check digit, so this can only reject the malformed. A
    structurally valid but wrong PAN is exactly why the GSTIN embedded form is
    preferred wherever both appear -- see :func:`pan_from_gstin`.
    """
    text = _canonical(raw)
    if not _PAN_RE.fullmatch(text):
        raise IdentifierError("PAN does not match the required structure", raw)
    return text


def pan_from_gstin(raw: str) -> str:
    """Extract the PAN embedded at positions 3..12 of a validated GSTIN.

    Worth doing wherever a GSTIN is present: the PAN inherits the GSTIN's check
    digit protection, so it is strictly more trustworthy than a PAN read
    separately off the same page.
    """
    return normalize_gstin(raw)[2:12]


def normalize_ifsc(raw: str) -> str:
    """Canonicalise and structurally validate an IFSC.

    The fifth character is always ``0`` -- reserved by RBI -- which catches the
    most common OCR confusion in this field, ``O`` read for zero.
    """
    text = _canonical(raw)
    if not _IFSC_RE.fullmatch(text):
        raise IdentifierError("IFSC does not match the required structure", raw)
    return text


def document_number_key(raw: str) -> str:
    """Reduce a document number to a key two documents can be compared on.

    ``INV-2026/001``, ``inv 2026 001`` and ``INV/2026-001`` are the same
    invoice referred to three ways -- by the vendor, by the ledger import and
    by a bank narration. Phase 2 blocks on this key; the original string stays
    in ``document.number`` because that is what the reviewer must see.

    Note the cost, taken knowingly: this maps ``INV-1`` and ``INV1`` to the same
    key. Collisions here widen the candidate set for matching, which scoring
    then narrows. The opposite error -- missing a real match on punctuation --
    cannot be recovered downstream.
    """
    return _NON_ALNUM_RE.sub("", normalize_whitespace(raw).upper())


def _canonical(raw: str) -> str:
    if is_placeholder(raw):
        raise IdentifierError("empty identifier", raw)
    return _NON_ALNUM_RE.sub("", normalize_whitespace(raw).upper())
