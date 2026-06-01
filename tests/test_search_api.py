from pathlib import Path

from fastapi.testclient import TestClient

from team_vault.config import Settings
from team_vault.models import DocKind, IngestDocumentRequest
from team_vault.search_app import create_app
from team_vault.storage import LocalVaultStorage, make_document


def test_search_endpoint_when_shared_doc_matches(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    storage.put_document(
        make_document(
            "alice",
            IngestDocumentRequest(
                path="notes/infra.md",
                title="Infra Overview",
                content="EKS와 S3로 팀 vault를 운영한다.",
                kind=DocKind.NOTE,
                share_team=True,
            ),
        )
    )
    client = TestClient(create_app(settings=Settings(enable_mcp=False), storage=storage))

    response = client.get("/api/search", params={"q": "EKS", "limit": 5})

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 1
    assert body["items"][0]["title"] == "Infra Overview"


def test_document_endpoint_when_private_doc_belongs_to_other_owner(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    document = make_document(
        "alice",
        IngestDocumentRequest(
            path="sessions/private.jsonl",
            content="개인 대화",
            kind=DocKind.CONVERSATION,
            share_team=False,
        ),
    )
    storage.put_document(document)
    client = TestClient(create_app(settings=Settings(enable_mcp=False), storage=storage))

    response = client.get(
        f"/api/document/{document.doc_id}",
        params={"include_private": True},
        headers={"X-Vault-OS-User": "bob"},
    )

    assert response.status_code == 404

