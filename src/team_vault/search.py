import re
from collections.abc import Sequence

from team_vault.models import SearchHit, SearchResponse, Sensitivity, StoredDocument
from team_vault.storage import VaultStorage

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_.-]+")


def search_documents(
    storage: VaultStorage,
    *,
    query: str,
    limit: int,
    offset: int,
    owner: str | None,
    include_private: bool,
) -> SearchResponse:
    tokens = tokenize(query)
    visible = [
        document
        for document in storage.list_documents()
        if can_read_document(document, owner=owner, include_private=include_private)
    ]
    hits = (score_document(document, tokens) for document in visible)
    scored = sorted((hit for hit in hits if hit.score > 0), key=lambda hit: hit.score, reverse=True)
    page = scored[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(scored) else None
    return SearchResponse(
        query=query,
        total_count=len(scored),
        count=len(page),
        limit=limit,
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
        items=page,
    )


def recent_documents(
    storage: VaultStorage,
    *,
    limit: int,
    owner: str | None,
    include_private: bool,
) -> Sequence[SearchHit]:
    visible = [
        document
        for document in storage.list_documents()
        if can_read_document(document, owner=owner, include_private=include_private)
    ]
    recent = sorted(visible, key=lambda document: document.created_at, reverse=True)[:limit]
    return [document_to_hit(document, score=1.0, query_tokens=[]) for document in recent]


def can_read_document(
    document: StoredDocument,
    *,
    owner: str | None,
    include_private: bool,
) -> bool:
    if document.sensitivity is Sensitivity.TEAM:
        return True
    return include_private and owner == document.owner


def score_document(document: StoredDocument, query_tokens: Sequence[str]) -> SearchHit:
    haystack_title = document.title.lower()
    haystack_content = document.content.lower()
    score = 0.0
    for token in query_tokens:
        if token in haystack_title:
            score += 5.0
        if token in haystack_content:
            score += 1.0 + min(haystack_content.count(token), 5) * 0.2
    return document_to_hit(document, score=score, query_tokens=query_tokens)


def document_to_hit(
    document: StoredDocument,
    *,
    score: float,
    query_tokens: Sequence[str],
) -> SearchHit:
    return SearchHit(
        doc_id=document.doc_id,
        title=document.title,
        owner=document.owner,
        source_path=document.source_path,
        kind=document.kind,
        sensitivity=document.sensitivity,
        score=round(score, 3),
        snippet=build_snippet(document.content, query_tokens),
    )


def tokenize(query: str) -> Sequence[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(query) if match.group(0)]


def build_snippet(content: str, query_tokens: Sequence[str]) -> str:
    normalized = content.replace("\n", " ")
    lower = normalized.lower()
    first_index = 0
    for token in query_tokens:
        found = lower.find(token)
        if found >= 0:
            first_index = max(found - 80, 0)
            break
    snippet = normalized[first_index : first_index + 220].strip()
    if len(normalized) > first_index + 220:
        return f"{snippet}..."
    return snippet
