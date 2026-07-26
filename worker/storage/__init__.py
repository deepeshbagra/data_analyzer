"""Object storage, behind one interface.

Architecture principle 5 in its second instance: ``base`` defines the
``ObjectStore`` protocol and the key layout, ``s3`` is the only module in the
repository permitted to import ``boto3``. Swapping MinIO for S3, or S3 for
anything else, is a new adapter and a settings change.
"""

from __future__ import annotations
