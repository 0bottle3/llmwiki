from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query

from team_vault.config import Settings
from team_vault.identity import ClientIdentity, OwnerResolver
from team_vault.mcp_server import build_mcp_server, mcp_lifespan
from team_vault.models import SearchHit, SearchResponse, StoredDocument
from team_vault.search import recent_documents, search_documents
from team_vault.storage import VaultStorage, build_storage


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
    mcp_server = build_mcp_server(resolved_storage) if resolved_settings.enable_mcp else None
    lifespan = mcp_lifespan(mcp_server) if mcp_server is not None else None
    app = FastAPI(title="Team Vault Search API", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/search", response_model=SearchResponse)
    def search_endpoint(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=50)] = 5,
        offset: Annotated[int, Query(ge=0)] = 0,
        include_private: bool = False,
        x_vault_hostname: Annotated[str | None, Header()] = None,
        x_vault_os_user: Annotated[str | None, Header()] = None,
    ) -> SearchResponse:
        owner = resolver.resolve(ClientIdentity(x_vault_hostname, x_vault_os_user))
        return search_documents(
            resolved_storage,
            query=q,
            limit=limit,
            offset=offset,
            owner=owner,
            include_private=include_private,
        )

    @app.get("/api/document/{doc_id}", response_model=StoredDocument)
    def document_endpoint(
        doc_id: str,
        include_private: bool = False,
        x_vault_hostname: Annotated[str | None, Header()] = None,
        x_vault_os_user: Annotated[str | None, Header()] = None,
    ) -> StoredDocument:
        owner = resolver.resolve(ClientIdentity(x_vault_hostname, x_vault_os_user))
        document = resolved_storage.get_document(doc_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        cannot_read_private = not include_private or document.owner != owner
        if document.sensitivity.value == "private" and cannot_read_private:
            raise HTTPException(status_code=404, detail="document not found")
        return document

    @app.get("/api/recent", response_model=list[SearchHit])
    def recent_endpoint(
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        include_private: bool = False,
        x_vault_hostname: Annotated[str | None, Header()] = None,
        x_vault_os_user: Annotated[str | None, Header()] = None,
    ) -> list[SearchHit]:
        owner = resolver.resolve(ClientIdentity(x_vault_hostname, x_vault_os_user))
        return list(
            recent_documents(
                resolved_storage,
                limit=limit,
                owner=owner,
                include_private=include_private,
            )
        )

    if mcp_server is not None:
        app.mount("/mcp", mcp_server.streamable_http_app())

    return app


def main() -> None:
    import uvicorn

    uvicorn.run("team_vault.search_app:create_app", factory=True, host="0.0.0.0", port=8080)
