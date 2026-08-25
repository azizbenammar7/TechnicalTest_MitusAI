"""Run the ObjectStorage contract against the in-memory and Azure Blob adapters.

The in-memory adapter always runs (deterministic, no emulator). The Azure Blob
adapter runs only when ``FOOTBALLAI_TEST_BLOB_CONNECTION_STRING`` points at an
Azurite emulator, so the fast suite never needs Azure or Azurite.
"""

from __future__ import annotations

import os
import uuid

import pytest

from footballai_v2.storage.object_storage import InMemoryObjectStorage
from contracts.object_storage_contract import ObjectStorageContract


@pytest.fixture
def run_id() -> str:
    return str(uuid.uuid4())


class TestInMemoryObjectStorage(ObjectStorageContract):
    @pytest.fixture
    def storage(self):
        return InMemoryObjectStorage()

    def perform_upload(self, storage, run_id, grant, content):
        storage.put_input(
            run_id,
            content,
            extension=".mp4",
            content_type=grant.required_content_type,
        )


_BLOB_CONNECTION = os.getenv("FOOTBALLAI_TEST_BLOB_CONNECTION_STRING")


@pytest.mark.skipif(
    not _BLOB_CONNECTION,
    reason="set FOOTBALLAI_TEST_BLOB_CONNECTION_STRING (Azurite) to run Blob tests",
)
class TestAzureBlobObjectStorage(ObjectStorageContract):
    @pytest.fixture(scope="class")
    def _credential(self):
        from footballai_v2.storage.object_storage.credentials import ConnectionStringCredential, _parse_connection_string

        fields = _parse_connection_string(_BLOB_CONNECTION)
        return ConnectionStringCredential(
            connection_string=_BLOB_CONNECTION,
            container="footballai-test",
            account_name=fields["AccountName"],
            account_key=fields["AccountKey"],
        )

    @pytest.fixture
    def storage(self, _credential):
        from footballai_v2.storage.object_storage.azure_blob import AzureBlobObjectStorage

        return AzureBlobObjectStorage(_credential)

    def perform_upload(self, storage, run_id, grant, content):
        import httpx

        response = httpx.put(grant.url, content=content, headers=dict(grant.headers))
        response.raise_for_status()
