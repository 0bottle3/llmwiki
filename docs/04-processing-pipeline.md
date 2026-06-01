# 04. 가공 파이프라인

LLM 무한 사용 가능 + LangChain 없이 직접 호출하는 배치 가공 설계.

## 설계 원칙

1. **LangChain 안 씀** — 추상화 오버헤드 제거, 디버깅 단순화
2. **Anthropic Batch API 우선** — 50% 할인 + 24시간 SLA, 실시간성 불필요
3. **Prompt Caching 필수** — 시스템 프롬프트 90% 절감
4. **JSON 강제 출력** — `tool_use` 또는 `response_format` 활용
5. **실패는 다음 CronJob에서 재시도** — 절대 사일런트 드롭 금지, processed/ etag로 멱등 판정

## 파이프라인 단계

```
[CronJob: S3 raw/ 주기 스캔]
      ↓
1. Fetch (S3에서 원본 fetch, etag로 변경 감지)
      ↓
2. Pre-process (텍스트 정규화, 길이 체크)
      ↓
3. LLM Batch (메타 추출 + 마스킹 + 요약 + 분류)
      ↓
4. Embed (Voyage batch)
      ↓
5. Persist (S3:processed + Qdrant upsert)
      ↓
6. Post (중복 감지, 링크 자동 생성)
      ↓
[완료: processed/ etag 기록]
```

## 1) Fetch & Pre-process

```python
def fetch_doc(s3_key: str) -> RawDoc:
    obj = s3.get_object(Bucket=BUCKET_RAW, Key=s3_key)
    content = obj['Body'].read().decode('utf-8')
    return RawDoc(
        id=hashlib.sha256(s3_key.encode()).hexdigest()[:16],
        s3_key=s3_key,
        content=content,
        size=len(content),
        last_modified=obj['LastModified'],
        etag=obj['ETag'],
    )

def preprocess(doc: RawDoc) -> RawDoc:
    # 너무 짧으면 skip
    if doc.size < 100:
        raise SkipDoc("too short")
    # 너무 크면 청크 분할 (토큰 한계)
    if doc.size > 100_000:
        doc.chunks = split_by_heading(doc.content, max_tokens=20_000)
    # 프론트매터 추출 (있으면)
    doc.existing_frontmatter = extract_frontmatter(doc.content)
    return doc
```

## 2) LLM 호출 — Anthropic Batch API

### 프롬프트 구조

```python
SYSTEM_PROMPT = """
당신은 팀 위키 큐레이터입니다. 주어진 마크다운 문서를 분석하여
구조화된 JSON으로 반환합니다.

규칙:
- 민감정보(API key, 비밀번호, 사람 이름의 부정적 평가 등)는 [REDACTED]로 마스킹
- 요약은 200자 이내, 검색 쿼리 매칭 잘 되도록 키워드 포함
- 한국어 문서는 한국어로, 영어 문서는 영어로 메타데이터 작성
- type은 다음 중 하나: incident | runbook | knowledge | decision | daily | other
- quality 0.0~1.0: 재현성, 명확성, 완결성 종합
"""

USER_PROMPT_TEMPLATE = """
다음 문서를 분석하여 `submit_metadata` 도구를 호출하세요.

<document path="{s3_key}" author="{author}" last_modified="{last_modified}">
{content}
</document>
"""

METADATA_TOOL = {
    "name": "submit_metadata",
    "description": "문서의 구조화된 메타데이터를 제출합니다.",
    "input_schema": {
        "type": "object",
        "required": ["title", "summary", "tags", "type", "quality",
                     "has_secrets", "redacted_content", "related_topics"],
        "properties": {
            "title":             {"type": "string"},
            "summary":           {"type": "string", "maxLength": 200},
            "tags":              {"type": "array", "items": {"type": "string"}},
            "type":              {"type": "string",
                                  "enum": ["incident", "runbook", "knowledge",
                                           "decision", "daily", "other"]},
            "quality":           {"type": "number", "minimum": 0, "maximum": 1},
            "has_secrets":       {"type": "boolean"},
            "redacted_content":  {"type": "string"},
            "related_topics":    {"type": "array", "items": {"type": "string"}},
            "language":          {"type": "string", "enum": ["ko", "en", "mixed"]},
        },
    },
}
```

### Batch 요청

```python
def submit_batch(docs: list[RawDoc]) -> str:
    requests = []
    for doc in docs:
        requests.append({
            "custom_id": doc.id,
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4096,
                "system": [{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # ← prompt caching
                }],
                "tools": [METADATA_TOOL],
                "tool_choice": {"type": "tool", "name": "submit_metadata"},
                "messages": [{
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        s3_key=doc.s3_key,
                        author=infer_author(doc.s3_key),
                        last_modified=doc.last_modified.isoformat(),
                        content=doc.content[:50_000],  # 컨텍스트 한계 안전 마진
                    ),
                }],
            },
        })

    batch = anthropic_client.messages.batches.create(requests=requests)
    return batch.id
```

### Batch 완료 대기

```python
def wait_for_batch(batch_id: str, timeout_sec=24*3600) -> Iterator[Result]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        batch = anthropic_client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            for line in anthropic_client.messages.batches.results(batch_id):
                yield line
            return
        time.sleep(60)
    raise TimeoutError(f"batch {batch_id} not finished in {timeout_sec}s")
```

## 3) Embed — Voyage Batch

```python
import voyageai

vo = voyageai.Client()

def embed_batch(texts: list[str]) -> list[list[float]]:
    # Voyage는 batch당 최대 128개
    out = []
    for chunk in batched(texts, 128):
        resp = vo.embed(
            texts=chunk,
            model="voyage-3",
            input_type="document",
        )
        out.extend(resp.embeddings)
    return out
```

쿼리 임베딩은 `input_type="query"` (asymmetric).

> **[M7] 임베딩 차원 고정 주의**: 임베딩 차원(1024)은 Qdrant collection 생성 시 고정된다.
> 임베딩 모델을 교체할 경우(특히 차원이 다른 모델로 변경 시) collection 재생성 + 전체 재인덱싱이 필요하다.
> Voyage-3 기준 1024차원; 모델 변경 전 차원 호환 여부를 반드시 확인할 것.

## 4) Persist

### S3 (processed)

```python
def write_processed(doc: RawDoc, meta: Metadata):
    payload = {
        "doc_id": doc.id,
        "source": {
            "s3_key": doc.s3_key,
            "etag": doc.etag,
            "last_modified": doc.last_modified.isoformat(),
        },
        "frontmatter": {
            "title": meta.title,
            "type": meta.type,
            "tags": meta.tags,
            "quality": meta.quality,
            "language": meta.language,
            "summary": meta.summary,
            "related_topics": meta.related_topics,
        },
        "redacted_content": meta.redacted_content,
        "processed_at": datetime.utcnow().isoformat(),
        "model": "claude-haiku-4-5-20251001",
        "schema_version": 1,
    }
    s3.put_object(
        Bucket=BUCKET,
        Key=f"processed/{doc.id}.json",
        Body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        ContentType="application/json",
        Metadata={"doc-id": doc.id, "etag": doc.etag},
    )
```

### Qdrant (upsert)

```python
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

qdrant = QdrantClient(host="qdrant", port=6333)

# 컬렉션 초기화 (최초 1회)
qdrant.recreate_collection(
    collection_name="vault",
    vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
)

def index_doc(doc: RawDoc, meta: Metadata, embedding: list[float]):
    qdrant.upsert(
        collection_name="vault",
        points=[PointStruct(
            id=doc.id,
            vector=embedding,
            payload={
                "title": meta.title,
                "summary": meta.summary,
                "tags": meta.tags,
                "type": meta.type,
                "quality": meta.quality,
                "language": meta.language,
                "redacted_content": meta.redacted_content,
                "s3_key": doc.s3_key,
                "indexed_at": datetime.utcnow().isoformat(),
            },
        )],
    )
```

### Qdrant 컬렉션 스펙

| 필드 | 종류 | 비고 |
|------|------|------|
| vector | dense, size=1024, Cosine | Voyage-3 임베딩 |
| payload.title | keyword / text | 페이로드 필터용 |
| payload.summary | text | 페이로드 필터용 |
| payload.tags | keyword[] | 다중값 필터 |
| payload.type | keyword | incident 등 |
| payload.quality | float | 범위 필터 |
| payload.language | keyword | ko / en / mixed |
| payload.redacted_content | text | 전문 저장 |
| payload.s3_key | keyword | 원본 참조 |
| payload.indexed_at | datetime | 최신순 정렬 |

dense vector 검색이 기본; 키워드/BM25는 payload 필터 또는 Qdrant sparse vector로 보조.

## 5) Post — 중복 감지

```python
def detect_duplicates(doc_id: str, embedding: list[float], threshold=0.95):
    hits = qdrant.search(
        collection_name="vault",
        query_vector=embedding,
        limit=5,
        with_payload=True,
    )
    duplicates = [
        h for h in hits
        if str(h.id) != doc_id and h.score > threshold
    ]
    if duplicates:
        write_merge_proposal(doc_id, duplicates)
```

`merge_proposal`은 별도 S3 경로(`proposals/`)에 쌓고 curator가 슬랙으로 보고.

## 6) 검색 (search-api에서)

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue, SearchRequest

def hybrid_search(query: str, top_k=10, filters: dict | None = None):
    q_emb = embed_query(query)

    # 페이로드 필터 변환 (예: type, language, quality 범위)
    qdrant_filter = build_qdrant_filter(filters) if filters else None

    results = qdrant.search(
        collection_name="vault",
        query_vector=q_emb,
        query_filter=qdrant_filter,
        limit=top_k,
        with_payload=True,
    )
    return results
```

dense vector 유사도 검색이 기본; 키워드 필터링은 `query_filter`의 payload 조건으로 보조.
BM25 수준의 전문 키워드 검색이 필요한 경우 Qdrant sparse vector(보조) 또는 Phase 2+ 확장경로(OpenSearch) 적용.

### 선택: Claude로 리랭킹

```python
def rerank(query: str, hits: list[dict], top_n=5):
    # Claude Haiku한테 top-20 던지고 "관련도 정렬" 시킴
    # 비용 미미, 품질 ↑
    ...
```

## 실패/재시도 정책

| 실패 종류 | 처리 |
|----------|------|
| S3 fetch 실패 | 5회 재시도(지수 백오프) → 다음 CronJob 실행에서 재시도 (processed/ etag로 멱등 판정) |
| LLM 일시 오류 | Anthropic SDK 자동 재시도 |
| LLM 응답 스키마 위반 | tool_use 강제로 발생 어려움. 발생 시 다음 CronJob 실행에서 재시도 |
| 임베딩 실패 | 3회 재시도 → 다음 CronJob 실행에서 재시도 |
| Qdrant 일시 오류 | 5회 재시도 → 다음 CronJob 실행에서 재시도 |
| 반복 실패 | curator가 다음날 슬랙 알림 (처리 실패 카운터 기준) |

## 비용/성능 메트릭 (필수)

- `processor_batch_size`            (히스토그램)
- `processor_llm_tokens_in/out`     (카운터)
- `processor_embed_tokens`          (카운터)
- `processor_batch_latency_seconds` (히스토그램)
- `processor_failures_total`        (카운터, by reason)
- `qdrant_upsert_latency_ms`         (히스토그램)

월말에 토큰 사용량 × 단가 자동 산정 → `08-cost-estimate.md` 모델에 피드백.
