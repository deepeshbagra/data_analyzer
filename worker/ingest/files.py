"""What a file *is*: its identity, its type, and its shape.

Pure functions over bytes. No database, no object store, no network, so every
rule in here is unit-testable without infrastructure.

Two decisions made in this module propagate through the whole pipeline:

* **The mime comes from the bytes.** Not from the filename, not from the
  client's ``Content-Type``. The mime picks the extraction path, so a
  client-controlled mime would let an uploader pick which code runs on their
  file. There is no parameter here through which a caller could pass one.
* **``has_text_layer`` picks the extractor.** True routes to the deterministic
  text path; False routes to OCR/VLM. Wrong in one direction produces empty
  extractions from a scan; wrong in the other spends a model call on every page
  of a document whose text was sitting there in the file.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MIME_PDF = "application/pdf"
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
MIME_TIFF = "image/tiff"

IMAGE_MIMES = frozenset({MIME_PNG, MIME_JPEG, MIME_TIFF})
SUPPORTED_MIMES = frozenset({MIME_PDF}) | IMAGE_MIMES

#: Leading bytes that identify a format. Ordered longest-first so a shorter
#: prefix cannot shadow a longer one.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", MIME_PNG),
    (b"%PDF-", MIME_PDF),
    (b"\xff\xd8\xff", MIME_JPEG),
    (b"II*\x00", MIME_TIFF),
    (b"MM\x00*", MIME_TIFF),
)

#: Enough for every signature above, with room to spare.
SNIFF_BYTES = 16
_CHUNK_SIZE = 1024 * 1024

#: A page counts as having text at 20+ non-whitespace characters. Not 1:
#: scanned pages routinely carry a stray text object -- a folio number, a
#: date stamp, a fax header -- and a threshold of "any text at all" would
#: classify every scan as a text document and send it to a parser that then
#: returns nothing.
MIN_CHARS_FOR_TEXT_PAGE = 20

#: Half the sampled pages must carry text. Mixed documents are common (a text
#: invoice with a scanned annexure), and the majority decides which extractor
#: is right for the document as a whole.
TEXT_LAYER_PAGE_FRACTION = 0.5

#: Text extraction is the expensive part of profiling, so it reads a bounded,
#: evenly spaced sample. A 500-page bank statement must not cost 500
#: extractions to answer one boolean.
MAX_PAGES_SAMPLED = 10


class IngestError(ValueError):
    """A file cannot be ingested. The message is shown to the uploader."""


class EmptyFileError(IngestError):
    """Zero bytes."""


class FileTooLargeError(IngestError):
    """Larger than the configured limit."""


class UnsupportedFileTypeError(IngestError):
    """The bytes are not a format this phase handles."""


class UnreadableFileError(IngestError):
    """Right format, damaged content."""


class EncryptedFileError(UnreadableFileError):
    """Password-protected.

    Its own class because this is routine rather than exceptional in India:
    banks mail statements locked to PAN plus date of birth. The uploader can
    fix it in seconds if told what is wrong, and cannot if it surfaces as a
    generic parse failure. Accepting a password is Phase 1e.
    """


@dataclass(frozen=True)
class FileIdentity:
    """What uniquely identifies an uploaded artefact.

    ``sha256`` is the dedupe key (unique per tenant on ``source_file``) and the
    content-addressed storage key. ``mime`` is sniffed, never declared.
    """

    sha256: str
    byte_size: int
    mime: str


@dataclass(frozen=True)
class FileProfile:
    """The file's shape, as far as ingest can tell without extracting it.

    ``pages_sampled`` and ``pages_with_text`` are kept rather than discarded so
    a surprising ``has_text_layer`` can be explained after the fact without
    re-running anything.
    """

    page_count: int
    has_text_layer: bool
    pages_sampled: int
    pages_with_text: int


def sniff_mime(header: bytes) -> str | None:
    """The mime implied by a file's leading bytes, or None if unrecognised."""
    for magic, mime in _MAGIC:
        if header.startswith(magic):
            return mime
    return None


def identify(stream: BinaryIO, *, max_bytes: int) -> FileIdentity:
    """Hash, measure and type a stream, then rewind it.

    The stream is read once, in chunks, so a large upload is never held in
    memory in full. It is rewound before returning because the caller's next
    move is to hand the same stream to the object store -- leaving the cursor
    at EOF would store a zero-byte object while the row recorded the true size,
    which looks correct in the database and is only visible when a human opens
    the file.

    Args:
        stream: positioned at the start of the file.
        max_bytes: inclusive size limit.

    Raises:
        EmptyFileError, FileTooLargeError, UnsupportedFileTypeError.
    """
    digest = hashlib.sha256()
    byte_size = 0
    header = b""

    while chunk := stream.read(_CHUNK_SIZE):
        if not header:
            header = chunk[:SNIFF_BYTES]
        byte_size += len(chunk)
        # Checked inside the loop, so an oversized upload stops being read as
        # soon as it crosses the limit rather than after it is all in memory.
        if byte_size > max_bytes:
            raise FileTooLargeError(f"file exceeds the {max_bytes} byte limit")
        digest.update(chunk)

    stream.seek(0)

    if byte_size == 0:
        raise EmptyFileError("file is empty")

    mime = sniff_mime(header)
    if mime is None:
        raise UnsupportedFileTypeError(
            "unsupported file type; this phase accepts PDF, PNG, JPEG and TIFF"
        )

    return FileIdentity(sha256=digest.hexdigest(), byte_size=byte_size, mime=mime)


def profile(data: bytes, mime: str) -> FileProfile:
    """Page count and text-layer detection.

    Raises:
        UnsupportedFileTypeError: for a mime this phase does not profile.
        EncryptedFileError, UnreadableFileError: for a PDF that cannot be read.
    """
    if mime == MIME_PDF:
        return _profile_pdf(data)
    if mime in IMAGE_MIMES:
        # A photographed or scanned invoice: one page, and any text in it is
        # pixels, so the VLM path is the only one that can read it.
        return FileProfile(page_count=1, has_text_layer=False, pages_sampled=1, pages_with_text=0)
    raise UnsupportedFileTypeError(f"cannot profile {mime!r}")


def _profile_pdf(data: bytes) -> FileProfile:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise EncryptedFileError("the PDF is password-protected; upload an unlocked copy")
        page_count = len(reader.pages)
    except EncryptedFileError:
        raise
    except (PyPdfError, ValueError, OSError) as exc:
        raise UnreadableFileError(f"the PDF could not be read: {exc}") from exc

    if page_count == 0:
        raise UnreadableFileError("the PDF contains no pages")

    indexes = _sample_indexes(page_count, MAX_PAGES_SAMPLED)
    pages_with_text = 0
    for index in indexes:
        if len(_page_text(reader, index).strip()) >= MIN_CHARS_FOR_TEXT_PAGE:
            pages_with_text += 1

    # Ceiling, so a single sampled page must itself carry text rather than
    # rounding its way to True.
    needed = math.ceil(len(indexes) * TEXT_LAYER_PAGE_FRACTION)
    return FileProfile(
        page_count=page_count,
        has_text_layer=pages_with_text >= needed,
        pages_sampled=len(indexes),
        pages_with_text=pages_with_text,
    )


def _page_text(reader: PdfReader, index: int) -> str:
    """Text of one page, or empty if that page alone is unextractable.

    Broad on purpose: pypdf raises a wide and undocumented set of exceptions on
    malformed content streams, and one bad page in a 200-page statement should
    count as a page without text, not fail the whole document.
    """
    try:
        return reader.pages[index].extract_text() or ""
    except Exception:  # see docstring: one bad page is not a bad document
        return ""


def _sample_indexes(page_count: int, limit: int) -> list[int]:
    """Up to ``limit`` evenly spaced page indexes, always including both ends.

    Both ends matter: covering letters and annexures live at the edges, and a
    sample taken only from the front would call a scanned statement with a
    typed cover page a text document.
    """
    if page_count <= limit:
        return list(range(page_count))
    step = (page_count - 1) / (limit - 1)
    return sorted({round(i * step) for i in range(limit)})
