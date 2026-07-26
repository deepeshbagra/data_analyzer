"""Ingest: an uploaded artefact becomes a ``source_file`` row.

Nothing here reads a document's *contents* for business meaning -- that is
extraction's job, in ``worker/extract``. Ingest establishes identity (what
bytes are these, have we seen them before) and shape (how many pages, is there
a text layer), which is what decides who extracts it and how.

* ``files``   -- pure functions over bytes. No database, no network.
* ``service`` -- dedupe, storage and the ``source_file`` row.
* ``tasks``   -- the Celery shim over the above.
"""

from __future__ import annotations
