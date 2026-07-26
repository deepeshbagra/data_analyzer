"""File identity and profiling. No database, no object store, no network.

Two properties here are load-bearing for everything downstream:

* **The file's type is decided by its bytes, never by what the client said.**
  The mime is what routes a document to the text extractor or the VLM, so a
  client-controlled value would let an uploader choose which code path runs on
  their file.
* **``has_text_layer`` decides which extractor sees the document at all.** Get
  it wrong in one direction and a scanned invoice goes to a text parser that
  returns nothing; wrong in the other and every page pays for a VLM call.
"""

from __future__ import annotations

import hashlib
import io

import pytest

from tests.conftest import PNG_BYTES, build_pdf
from worker.ingest.files import (
    MIME_JPEG,
    MIME_PDF,
    MIME_PNG,
    MIME_TIFF,
    EmptyFileError,
    EncryptedFileError,
    FileTooLargeError,
    UnreadableFileError,
    UnsupportedFileTypeError,
    identify,
    profile,
    sniff_mime,
)

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Type sniffing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"%PDF-1.7\n%...", MIME_PDF),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", MIME_PNG),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", MIME_JPEG),
        (b"II*\x00\x08\x00\x00\x00", MIME_TIFF),  # little-endian TIFF
        (b"MM\x00*\x00\x00\x00\x08", MIME_TIFF),  # big-endian TIFF
    ],
)
def test_sniff_recognises_supported_types(header: bytes, expected: str) -> None:
    assert sniff_mime(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"PK\x03\x04",  # zip, i.e. xlsx/docx -- not supported in Phase 1
        b"Date,Narration,Debit,Credit\n",  # csv statement -- Phase 1e
        b"<html><body>",
        b"\x00\x00\x00\x00",
        b"%PD",  # truncated magic
    ],
)
def test_sniff_returns_none_for_anything_else(header: bytes) -> None:
    assert sniff_mime(header) is None


def test_type_comes_from_the_bytes_not_the_extension_or_declared_type() -> None:
    """The security property. A PNG named ``invoice.pdf`` is a PNG.

    ``identify`` takes no filename and no declared content type, which is the
    enforcement mechanism: there is no parameter through which a client could
    influence the answer.
    """
    identity = identify(io.BytesIO(PNG_BYTES), max_bytes=MB)
    assert identity.mime == MIME_PNG


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_identity_is_the_sha256_of_the_exact_bytes() -> None:
    data = build_pdf(1, text="Tax Invoice")
    identity = identify(io.BytesIO(data), max_bytes=MB)
    assert identity.sha256 == hashlib.sha256(data).hexdigest()
    assert identity.byte_size == len(data)
    assert identity.mime == MIME_PDF


def test_identical_bytes_hash_identically_and_differing_bytes_do_not() -> None:
    """The whole dedupe story rests on this, so it is asserted rather than assumed."""
    one = identify(io.BytesIO(build_pdf(1, text="Invoice A")), max_bytes=MB)
    same = identify(io.BytesIO(build_pdf(1, text="Invoice A")), max_bytes=MB)
    other = identify(io.BytesIO(build_pdf(1, text="Invoice B")), max_bytes=MB)
    assert one.sha256 == same.sha256
    assert one.sha256 != other.sha256


def test_the_stream_is_rewound_so_the_caller_can_still_upload_it() -> None:
    """``identify`` consumes the stream to hash it; the caller then stores it.

    Leaving the cursor at EOF would upload a zero-byte object, and the row
    would still record the correct size -- a corruption that looks fine in the
    database and is only visible when someone opens the file.
    """
    data = build_pdf(1)
    stream = io.BytesIO(data)
    identify(stream, max_bytes=MB)
    assert stream.tell() == 0
    assert stream.read() == data


def test_empty_file_is_rejected() -> None:
    with pytest.raises(EmptyFileError):
        identify(io.BytesIO(b""), max_bytes=MB)


def test_oversize_file_is_rejected_without_being_read_whole() -> None:
    with pytest.raises(FileTooLargeError):
        identify(io.BytesIO(b"%PDF-1.7\n" + b"x" * 5000), max_bytes=1024)


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        identify(io.BytesIO(b"PK\x03\x04" + b"\x00" * 100), max_bytes=MB)


def test_a_file_at_exactly_the_limit_is_accepted() -> None:
    """Off-by-one on a size limit rejects legitimate uploads, so pin the boundary."""
    data = build_pdf(1)
    identity = identify(io.BytesIO(data), max_bytes=len(data))
    assert identity.byte_size == len(data)


# ---------------------------------------------------------------------------
# Profiling: page count and text layer
# ---------------------------------------------------------------------------


def test_text_pdf_is_recognised_as_having_a_text_layer() -> None:
    result = profile(build_pdf(3, text="Tax Invoice INV-2026-001 Total Rs 1,23,456.78"), MIME_PDF)
    assert result.page_count == 3
    assert result.has_text_layer is True
    assert result.pages_with_text == 3


def test_scanned_pdf_is_recognised_as_having_none() -> None:
    result = profile(build_pdf(3), MIME_PDF)
    assert result.page_count == 3
    assert result.has_text_layer is False
    assert result.pages_with_text == 0


def test_a_stray_page_number_does_not_make_a_scan_a_text_document() -> None:
    """Scanned pages routinely carry a tiny text object -- a folio number, a
    stamp, a fax header. A threshold of "any text at all" would call every scan
    a text PDF and send it to a parser that returns nothing."""
    result = profile(build_pdf(4, text="7"), MIME_PDF)
    assert result.has_text_layer is False


def test_page_count_is_exact_even_when_sampling_is_not() -> None:
    """Only a bounded sample of pages is read for text -- a 500-page statement
    must not cost 500 extractions -- but the page count itself is never
    sampled, because ``document.page_range`` is validated against it."""
    result = profile(build_pdf(25, text="Statement of account for April 2026"), MIME_PDF)
    assert result.page_count == 25
    assert result.pages_sampled < 25
    assert result.has_text_layer is True


def test_images_are_one_page_with_no_text_layer() -> None:
    result = profile(PNG_BYTES, MIME_PNG)
    assert result.page_count == 1
    assert result.has_text_layer is False


def test_encrypted_pdf_is_reported_specifically() -> None:
    """Password-protected statements are routine in India -- banks mail them
    locked to PAN plus date of birth. The specific error exists so the reason
    reaches a human instead of surfacing as a generic parse failure."""
    with pytest.raises(EncryptedFileError):
        profile(build_pdf(1, text="Statement", password="secret"), MIME_PDF)


def test_corrupt_pdf_is_reported_as_unreadable() -> None:
    with pytest.raises(UnreadableFileError):
        profile(b"%PDF-1.7\nnot actually a pdf", MIME_PDF)


def test_profiling_an_unsupported_type_raises() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        profile(b"PK\x03\x04", "application/zip")
