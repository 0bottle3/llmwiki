import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from team_vault.config import Settings
from team_vault.models import DocKind, IngestDocumentRequest, Sensitivity, StoredDocument


class StorageError(RuntimeError):
    pass


class VaultStorage(Protocol):
    def put_document(self, document: StoredDocument) -> str: ...

    def get_document(self, doc_id: str) -> StoredDocument | None: ...

    def list_documents(self) -> Sequence[StoredDocument]: ...


@dataclass(frozen=True, slots=True)
class LocalVaultStorage:
    root: Path

    def put_document(self, document: StoredDocument) -> str:
        key = raw_storage_key(document)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        return key

    def get_document(self, doc_id: str) -> StoredDocument | None:
        for path in self._document_paths():
            document = StoredDocument.model_validate_json(path.read_text(encoding="utf-8"))
            if document.doc_id == doc_id:
                return document
        return None

    def list_documents(self) -> Sequence[StoredDocument]:
        return [
            StoredDocument.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self._document_paths()
        ]

    def _document_paths(self) -> Iterable[Path]:
        raw_root = self.root / "raw"
        if not raw_root.exists():
            return []
        return sorted(raw_root.glob("*/*/*.json"))


@dataclass(frozen=True, slots=True)
class S3VaultStorage:
    bucket: str
    prefix: str
    region: str

    def put_document(self, document: StoredDocument) -> str:
        key = self._key(raw_storage_key(document))
        try:
            boto3.client("s3", region_name=self.region).put_object(
                Bucket=self.bucket,
                Key=key,
                Body=document.model_dump_json(indent=2).encode("utf-8"),
                ContentType="application/json",
                Metadata={
                    "doc-id": document.doc_id,
                    "owner": document.owner,
                    "source-path-sha256": stable_digest(document.source_path),
                },
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"failed to write document to s3://{self.bucket}/{key}") from exc
        return key

    def get_document(self, doc_id: str) -> StoredDocument | None:
        for document in self.list_documents():
            if document.doc_id == doc_id:
                return document
        return None

    def list_documents(self) -> Sequence[StoredDocument]:
        client = boto3.client("s3", region_name=self.region)
        documents: list[StoredDocument] = []
        token: str | None = None
        while True:
            request = {"Bucket": self.bucket, "Prefix": self._key("raw/")}
            if token is not None:
                request["ContinuationToken"] = token
            try:
                response = client.list_objects_v2(**request)
            except (BotoCoreError, ClientError) as exc:
                location = f"s3://{self.bucket}/{self._key('raw/')}"
                raise StorageError(f"failed to list {location}") from exc
            for item in response.get("Contents", []):
                key = str(item["Key"])
                if key.endswith(".json"):
                    documents.append(self._read_key(client, key))
            if not response.get("IsTruncated", False):
                return documents
            token = str(response["NextContinuationToken"])

    def _read_key(self, client, key: str) -> StoredDocument:
        try:
            response = client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].read().decode("utf-8")
        except (BotoCoreError, ClientError, UnicodeDecodeError) as exc:
            raise StorageError(f"failed to read s3://{self.bucket}/{key}") from exc
        return StoredDocument.model_validate_json(body)

    def _key(self, suffix: str) -> str:
        cleaned_prefix = self.prefix.strip("/")
        if not cleaned_prefix:
            return suffix
        return f"{cleaned_prefix}/{suffix}"


def build_storage(settings: Settings) -> VaultStorage:
    match settings.storage_backend:
        case "local":
            return LocalVaultStorage(settings.local_data_dir)
        case "s3":
            if not settings.s3_bucket:
                raise StorageError("TEAM_VAULT_S3_BUCKET is required when storage_backend=s3")
            return S3VaultStorage(settings.s3_bucket, settings.s3_prefix, settings.aws_region)


def make_document(owner: str, payload: IngestDocumentRequest) -> StoredDocument:
    content_sha256 = stable_digest(payload.content)
    source_digest = stable_digest(f"{owner}:{payload.path}:{content_sha256}")[:16]
    sensitivity = Sensitivity.TEAM if payload.share_team else Sensitivity.PRIVATE
    if payload.kind is DocKind.CONVERSATION and not payload.share_team:
        sensitivity = Sensitivity.PRIVATE
    return StoredDocument(
        doc_id=source_digest,
        owner=owner,
        source_path=payload.path,
        title=payload.title or title_from_path(payload.path),
        content=payload.content,
        content_sha256=content_sha256,
        kind=payload.kind,
        sensitivity=sensitivity,
        metadata=payload.metadata,
    )


def raw_storage_key(document: StoredDocument) -> str:
    month = document.created_at.strftime("%Y-%m")
    return f"raw/{document.owner}/{month}/{document.doc_id}.json"


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def title_from_path(path: str) -> str:
    name = Path(path).name
    return name.rsplit(".", maxsplit=1)[0] or "untitled"
