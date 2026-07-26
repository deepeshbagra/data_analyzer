"""Shared vocabulary for the normalisation layer: errors and text cleanup.

Every normaliser in this package follows one rule: return an exact, canonical
value or raise. There is no "best guess" return path, because a best guess is
indistinguishable from a correct answer once it is a row in the database, and
principle 3 requires every number to be traceable and defensible.

Raising is not a dead end. A ``NormalizationError`` is how a field gets routed
to human review, and it carries the raw input so the reviewer sees exactly what
the extractor saw.
"""

from __future__ import annotations

import re
import unicodedata

#: Strings that mean "this field was blank", as opposed to zero or a real
#: value. Bank statements and spreadsheet exports use all of these for an empty
#: cell. Compared after uppercasing and whitespace collapse.
PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "\u2013",  # en dash
        "\u2014",  # em dash
        ".",
        "N/A",
        "NA",
        "N.A.",
        "NIL",
        "NONE",
        "NULL",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


class NormalizationError(ValueError):
    """A raw string could not be turned into a canonical typed value.

    Args:
        message: what was wrong, in reviewer-readable terms.
        raw: the input exactly as received, preserved on the exception so the
            review UI can show it beside the highlighted region of the page.
    """

    def __init__(self, message: str, raw: object) -> None:
        super().__init__(f"{message}: {raw!r}")
        self.raw = raw


class MoneyParseError(NormalizationError):
    """An amount, percentage or amount-in-words could not be parsed exactly."""


class DateParseError(NormalizationError):
    """A business date could not be parsed, or is outside the plausible window."""


class IdentifierError(NormalizationError):
    """A GSTIN, PAN or IFSC is malformed or fails its checksum."""


def normalize_whitespace(raw: str) -> str:
    """NFKC-normalise, collapse whitespace runs to one space, and strip.

    NFKC is doing more work here than it appears. It folds the characters that
    real documents are full of and that naive parsers trip over: non-breaking
    and narrow no-break spaces (to a plain space), full-width digits (to ASCII),
    and the legacy rupee sign ``₨`` (to ``Rs``). Doing it once, here, means
    no downstream regex has to know about any of them.
    """
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()


def is_placeholder(raw: str | None) -> bool:
    """True when the value means "blank", which is not the same as zero."""
    if raw is None:
        return True
    return normalize_whitespace(raw).upper() in PLACEHOLDERS
