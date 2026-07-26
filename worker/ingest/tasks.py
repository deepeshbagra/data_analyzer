"""Celery entry points for ingest.

Deliberately thin. Every task here is a shim that parses its arguments and
calls a plain function in :mod:`worker.ingest.service`, so the logic is
testable by calling that function directly -- no broker, no worker, no
``apply_async`` in a test.

Task arguments are strings rather than ``UUID`` objects because the serializer
is JSON (see ``worker/celery_app.py``), and a task signature that only works
under pickle is a task signature that breaks the day someone tightens the
serializer.
"""

from __future__ import annotations

import uuid

from worker.celery_app import celery_app
from worker.ingest.service import probe_file
from worker.storage.base import StorageError


@celery_app.task(
    name="ingest.probe_source_file",
    # Only storage failures are retried. An encrypted or damaged PDF fails the
    # same way every time, and retrying it three times just delays telling the
    # user something they need to act on.
    autoretry_for=(StorageError,),
    retry_backoff=True,
    max_retries=3,
)
def probe_source_file(tenant_id: str, source_file_id: str) -> bool:
    """Fill in ``page_count`` and ``has_text_layer`` for one uploaded file."""
    return probe_file(uuid.UUID(tenant_id), uuid.UUID(source_file_id))
