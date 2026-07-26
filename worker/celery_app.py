"""Celery application: configuration, plus the list of modules holding tasks.

Queue topology note: Phase 4 adds per-tenant queues with fair-share rate
limiting so one 500-file upload cannot starve other tenants. The task routing
hook is left in place here so that change is configuration rather than a
rewrite.
"""

from __future__ import annotations

from celery import Celery

from api.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "analyzer",
    broker=str(settings.celery_broker_url),
    backend=str(settings.celery_result_backend),
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    # Long OCR/VLM calls: ack after completion so a killed worker's work is
    # redelivered rather than lost.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Fail loudly instead of retrying forever on a poison document.
    task_default_retry_delay=30,
    task_max_retries=3,
    task_default_queue="default",
    # Explicit rather than autodiscover: autodiscovery imports by convention,
    # so a task module that fails to import is silently absent and its tasks
    # queue forever with nothing consuming them. A bad import here is a
    # startup crash instead.
    include=["worker.ingest.tasks"],
)
