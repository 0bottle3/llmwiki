from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import import_module
from typing import Protocol, TypeVar

from fastapi import FastAPI
from starlette.types import ASGIApp

from team_vault.models import ResponseFormat
from team_vault.search import recent_documents, search_documents
from team_vault.storage import VaultStorage

ToolFunc = TypeVar("ToolFunc", bound=Callable[..., str])


class McpSessionManager(Protocol):
    def run(self) -> AbstractAsyncContextManager[None]: ...


class McpServer(Protocol):
    session_manager: McpSessionManager

    def tool(
        self,
        name: str,
        annotations: dict[str, str | bool],
    ) -> Callable[[ToolFunc], ToolFunc]: ...

    def streamable_http_app(self) -> ASGIApp: ...


def build_mcp_server(storage: VaultStorage) -> McpServer:
    fastmcp_module = import_module("mcp.server.fastmcp")
    fast_mcp = fastmcp_module.FastMCP
    mcp = fast_mcp("team_vault_mcp", stateless_http=True, json_response=True)

    @mcp.tool(
        name="team_vault_search",
        annotations={
            "title": "Search Team Vault",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def team_vault_search(
        query: str,
        limit: int = 5,
        response_format: ResponseFormat = ResponseFormat.JSON,
    ) -> str:
        """Search shared Team Vault notes and return ranked results."""

        response = search_documents(
            storage,
            query=query,
            limit=min(max(limit, 1), 20),
            offset=0,
            owner=None,
            include_private=False,
        )
        return format_response(response.model_dump_json(indent=2), response_format)

    @mcp.tool(
        name="team_vault_get_document",
        annotations={
            "title": "Get Team Vault Document",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def team_vault_get_document(doc_id: str) -> str:
        """Read a shared Team Vault document by doc_id."""

        document = storage.get_document(doc_id)
        if document is None or document.sensitivity.value == "private":
            return '{"error":"document not found"}'
        return document.model_dump_json(indent=2)

    @mcp.tool(
        name="team_vault_recent",
        annotations={
            "title": "Recent Team Vault Documents",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def team_vault_recent(limit: int = 10) -> str:
        """List recently ingested shared Team Vault documents."""

        hits = recent_documents(
            storage,
            limit=min(max(limit, 1), 20),
            owner=None,
            include_private=False,
        )
        return "[" + ",".join(hit.model_dump_json() for hit in hits) + "]"

    return mcp


def mcp_lifespan(mcp_server: McpServer) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    return lifespan


def format_response(json_text: str, response_format: ResponseFormat) -> str:
    match response_format:
        case ResponseFormat.JSON:
            return json_text
        case ResponseFormat.MARKDOWN:
            return f"```json\n{json_text}\n```"
