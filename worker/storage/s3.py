"""The S3-compatible adapter. The only module that imports ``boto3``.

Talks to MinIO locally and to S3 (or any S3-compatible store) elsewhere; the
difference is entirely in settings. Every ``botocore`` exception is translated
to :class:`StorageError` at this boundary, so no caller ever handles a
provider-shaped error and no caller ever needs to import botocore to do it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from api.settings import get_settings
from worker.storage.base import ObjectStore, StorageError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3ObjectStore:
    """``ObjectStore`` over an S3-compatible endpoint."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put(
        self,
        key: str,
        stream: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=stream,
                ContentType=content_type,
                ContentLength=content_length,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"could not store {key!r}") from exc

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: Any = response["Body"]
            data: bytes = body.read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"could not read {key!r}") from exc
        return data

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in _NOT_FOUND_CODES:
                return False
            raise StorageError(f"could not stat {key!r}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"could not stat {key!r}") from exc
        return True

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"could not delete {key!r}") from exc

    def presigned_get_url(self, key: str, *, expires_in: int) -> str:
        try:
            url: str = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"could not sign {key!r}") from exc
        return url


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Process-wide store, built from settings.

    Path-style addressing is forced for MinIO: virtual-host addressing needs a
    wildcard DNS entry per bucket, which a container on a compose network does
    not have.
    """
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        config=Config(
            s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    return S3ObjectStore(client, settings.s3_bucket)
