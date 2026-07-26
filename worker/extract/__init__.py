"""Document extraction: raw files in, structured rows out.

This package is the *only* place in the system permitted to call an LLM, and
even here only ``vlm.py`` may do so (architecture principle 2). Everything in
this module tree that touches a number is deterministic and unit-tested,
because a plausible-looking wrong amount is the most expensive failure the
platform can produce.

Layout, as it fills in across Phase 1:

* ``common``      -- the error hierarchy and text normalisation shared below.
* ``money``       -- amounts, percentages, amounts in words.
* ``dates``       -- business dates, day-first with explicit ambiguity.
* ``identifiers`` -- GSTIN, PAN, IFSC and document-number comparison keys.
"""

from __future__ import annotations
