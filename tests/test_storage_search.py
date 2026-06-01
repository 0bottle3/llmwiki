from pathlib import Path

from team_vault.models import DocKind, IngestDocumentRequest, Sensitivity
from team_vault.search import search_documents
from team_vault.storage import LocalVaultStorage, make_document


def test_search_documents_when_shared_doc_matches(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    document = make_document(
        "alice",
        IngestDocumentRequest(
            path="runbooks/payment-timeout.md",
            title="Payment Timeout Runbook",
            content="결제 timeout 대응은 gateway 로그와 retry queue를 먼저 확인한다.",
            kind=DocKind.NOTE,
            share_team=True,
        ),
    )

    storage.put_document(document)
    response = search_documents(
        storage,
        query="payment timeout",
        limit=5,
        offset=0,
        owner=None,
        include_private=False,
    )

    assert response.count == 1
    assert response.items[0].doc_id == document.doc_id
    assert response.items[0].score > 0


def test_search_documents_when_private_doc_requires_owner(tmp_path: Path) -> None:
    storage = LocalVaultStorage(tmp_path)
    document = make_document(
        "alice",
        IngestDocumentRequest(
            path="conversations/debug.jsonl",
            content="prod secret 조사 대화",
            kind=DocKind.CONVERSATION,
            share_team=False,
        ),
    )

    storage.put_document(document)
    hidden = search_documents(
        storage,
        query="secret",
        limit=5,
        offset=0,
        owner="bob",
        include_private=True,
    )
    visible = search_documents(
        storage,
        query="secret",
        limit=5,
        offset=0,
        owner="alice",
        include_private=True,
    )

    assert document.sensitivity is Sensitivity.PRIVATE
    assert hidden.count == 0
    assert visible.count == 1

