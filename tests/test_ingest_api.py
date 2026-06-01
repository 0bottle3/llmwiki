from pathlib import Path

from fastapi.testclient import TestClient

from team_vault.identity import OwnerResolver
from team_vault.ingest_app import create_app
from team_vault.storage import LocalVaultStorage


def test_ingest_document_when_hostname_is_known(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    resolver = OwnerResolver({"alice-mbp": "alice"}, True, "unknown")
    client = TestClient(create_app(storage=storage, owner_resolver=resolver))

    response = client.post(
        "/ingest/document",
        headers={"X-Vault-Hostname": "alice-mbp", "X-Vault-OS-User": "ignored"},
        json={
            "path": "notes/payment.md",
            "title": "Payment",
            "content": "결제 장애 대응 문서",
            "kind": "note",
            "share_team": True,
        },
    )

    body = response.json()
    assert response.status_code == 202
    assert body["accepted"] is True
    assert body["owner"] == "alice"
    assert storage.get_document(body["doc_id"]) is not None


def test_ingest_document_when_conversation_is_private(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    client = TestClient(create_app(storage=storage))

    response = client.post(
        "/ingest/document",
        headers={"X-Vault-OS-User": "bob"},
        json={
            "path": "sessions/codex.jsonl",
            "content": "개인 디버깅 대화",
            "kind": "conversation",
            "share_team": False,
        },
    )

    body = response.json()
    assert response.status_code == 202
    assert body["owner"] == "bob"
    assert body["sensitivity"] == "private"

