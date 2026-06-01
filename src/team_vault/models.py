from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class DocKind(StrEnum):
    NOTE = "note"
    CONVERSATION = "conversation"


class Sensitivity(StrEnum):
    TEAM = "team"
    PRIVATE = "private"


class ResponseFormat(StrEnum):
    JSON = "json"
    MARKDOWN = "markdown"


class IngestDocumentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=2_000_000)
    kind: DocKind = DocKind.NOTE
    title: str | None = Field(default=None, max_length=200)
    share_team: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)


class StoredDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    owner: str
    source_path: str
    title: str
    content: str
    content_sha256: str
    kind: DocKind
    sensitivity: Sensitivity
    metadata: dict[str, str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


class IngestDocumentResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    doc_id: str
    owner: str
    storage_key: str
    size_bytes: int
    sensitivity: Sensitivity


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    title: str
    owner: str
    source_path: str
    kind: DocKind
    sensitivity: Sensitivity
    score: float
    snippet: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    total_count: int
    count: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    items: list[SearchHit]
