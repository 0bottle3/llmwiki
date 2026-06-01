from typing import Annotated

from fastapi import FastAPI, Header, status

from team_vault.config import Settings
from team_vault.identity import ClientIdentity, OwnerResolver
from team_vault.models import IngestDocumentRequest, IngestDocumentResponse
from team_vault.storage import VaultStorage, build_storage, make_document


def create_app(
    settings: Settings | None = None,
    storage: VaultStorage | None = None,
    owner_resolver: OwnerResolver | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_storage = storage or build_storage(resolved_settings)
    resolver = owner_resolver or OwnerResolver.from_file(
        resolved_settings.hostname_map_path,
        trust_os_user_fallback=resolved_settings.trust_os_user_fallback,
        default_owner=resolved_settings.default_owner,
    )
    app = FastAPI(title="Team Vault Ingest API", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/ingest/document",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestDocumentResponse,
    )
    def ingest_document(
        payload: IngestDocumentRequest,
        x_vault_hostname: Annotated[str | None, Header()] = None,
        x_vault_os_user: Annotated[str | None, Header()] = None,
    ) -> IngestDocumentResponse:
        owner = resolver.resolve(ClientIdentity(x_vault_hostname, x_vault_os_user))
        document = make_document(owner, payload)
        storage_key = resolved_storage.put_document(document)
        return IngestDocumentResponse(
            accepted=True,
            doc_id=document.doc_id,
            owner=document.owner,
            storage_key=storage_key,
            size_bytes=document.size_bytes,
            sensitivity=document.sensitivity,
        )

    @app.post(
        "/ingest/vault",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestDocumentResponse,
    )
    def ingest_vault(
        payload: IngestDocumentRequest,
        x_vault_hostname: Annotated[str | None, Header()] = None,
        x_vault_os_user: Annotated[str | None, Header()] = None,
    ) -> IngestDocumentResponse:
        owner = resolver.resolve(ClientIdentity(x_vault_hostname, x_vault_os_user))
        document = make_document(owner, payload)
        storage_key = resolved_storage.put_document(document)
        return IngestDocumentResponse(
            accepted=True,
            doc_id=document.doc_id,
            owner=document.owner,
            storage_key=storage_key,
            size_bytes=document.size_bytes,
            sensitivity=document.sensitivity,
        )

    return app


def main() -> None:
    import uvicorn

    uvicorn.run("team_vault.ingest_app:create_app", factory=True, host="0.0.0.0", port=8080)

