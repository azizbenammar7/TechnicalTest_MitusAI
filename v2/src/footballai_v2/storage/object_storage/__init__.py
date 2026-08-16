"""Provider-neutral object-storage adapters (data plane).

The object-storage plane owns large bytes -- uploaded videos and published
artifacts -- keyed by an opaque, run-scoped object reference. It never holds the
analysis-run manifest; that is the PostgreSQL/local control plane's job.
"""

from footballai_v2.storage.object_storage.keys import (
    artifact_object_key,
    input_prefix,
    run_prefix,
)
from footballai_v2.storage.object_storage.memory import InMemoryObjectStorage

__all__ = [
    "InMemoryObjectStorage",
    "artifact_object_key",
    "input_prefix",
    "run_prefix",
]
