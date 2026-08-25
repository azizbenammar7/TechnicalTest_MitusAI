"""Authentication strategy for Azure Blob, kept separate from storage behaviour.

Two strategies implement the same small surface:

* ``ConnectionStringCredential`` -- development/emulator (Azurite). SAS tokens are
  signed with the shared account key that the connection string carries. Never
  used against a real production account.
* ``ManagedIdentityCredential`` -- production. The client authenticates with
  ``DefaultAzureCredential`` (Managed Identity in Azure) and SAS tokens are
  signed with a short-lived *user delegation key*, so no account key ever exists
  in the process.

Domain and application code never see any of this; only the Blob adapter does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)


class BlobCredentialStrategy(Protocol):
    account_name: str
    container: str

    def service_client(self) -> BlobServiceClient: ...

    def blob_write_sas(self, blob_name: str, *, expiry: datetime) -> str: ...

    def blob_read_sas(self, blob_name: str, *, expiry: datetime) -> str: ...


@dataclass(frozen=True, slots=True)
class ConnectionStringCredential:
    """Development strategy backed by a connection string (Azurite or a dev key)."""

    connection_string: str
    container: str
    account_name: str
    account_key: str

    def service_client(self) -> BlobServiceClient:
        return BlobServiceClient.from_connection_string(self.connection_string)

    def blob_write_sas(self, blob_name: str, *, expiry: datetime) -> str:
        # Write-only, single-blob, short-lived. No read/list/delete permission.
        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=self.account_key,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
            start=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

    def blob_read_sas(self, blob_name: str, *, expiry: datetime) -> str:
        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=self.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            start=datetime.now(timezone.utc) - timedelta(minutes=5),
        )


@dataclass(frozen=True, slots=True)
class ManagedIdentityCredential:
    """Production strategy using DefaultAzureCredential + user delegation SAS."""

    account_url: str
    container: str
    account_name: str

    def service_client(self) -> BlobServiceClient:
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(self.account_url, credential=DefaultAzureCredential())

    def blob_write_sas(self, blob_name: str, *, expiry: datetime) -> str:
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        client = self.service_client()
        delegation_key = client.get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_name,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
            start=start,
        )

    def blob_read_sas(self, blob_name: str, *, expiry: datetime) -> str:
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        client = self.service_client()
        delegation_key = client.get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=blob_name,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            start=start,
        )


def _parse_connection_string(connection_string: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for segment in connection_string.split(";"):
        if "=" in segment:
            key, _, value = segment.partition("=")
            parts[key.strip()] = value.strip()
    return parts


def credential_from_environment(container: str) -> BlobCredentialStrategy:
    """Build the credential strategy from environment, never from source constants.

    Selection is explicit and fails fast; it never silently downgrades.
    """
    connection_string = os.getenv("FOOTBALLAI_BLOB_CONNECTION_STRING", "").strip()
    account_url = os.getenv("FOOTBALLAI_BLOB_ACCOUNT_URL", "").strip()
    if connection_string:
        fields = _parse_connection_string(connection_string)
        account_name = fields.get("AccountName")
        account_key = fields.get("AccountKey")
        if not account_name or not account_key:
            raise ValueError(
                "FOOTBALLAI_BLOB_CONNECTION_STRING must include AccountName and AccountKey"
            )
        return ConnectionStringCredential(
            connection_string=connection_string,
            container=container,
            account_name=account_name,
            account_key=account_key,
        )
    if account_url:
        # https://<account>.blob.core.windows.net
        account_name = account_url.split("//", 1)[-1].split(".", 1)[0]
        if not account_name:
            raise ValueError("FOOTBALLAI_BLOB_ACCOUNT_URL is malformed")
        return ManagedIdentityCredential(
            account_url=account_url, container=container, account_name=account_name
        )
    raise ValueError(
        "Azure Blob backend requires FOOTBALLAI_BLOB_CONNECTION_STRING (dev) or "
        "FOOTBALLAI_BLOB_ACCOUNT_URL (managed identity)"
    )
