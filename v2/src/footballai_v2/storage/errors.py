"""Provider-neutral storage failures exposed to application boundaries."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for sanitized storage failures."""

    code = "storage_error"


class StorageNotFoundError(StorageError, FileNotFoundError):
    code = "object_not_found"


class StorageConflictError(StorageError, FileExistsError):
    code = "object_conflict"


class StorageIntegrityError(StorageError, ValueError):
    code = "integrity_failure"


class InvalidStorageObjectError(StorageError, ValueError):
    code = "invalid_object"


class StorageProviderUnavailableError(StorageError):
    code = "storage_unavailable"
