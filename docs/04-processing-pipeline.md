# 04. 가공 파이프라인

LLM 무한 사용 가능 + LangChain 없이 직접 호출하는 배치 가공 설계.

## 설계 원칙

1. **LangChain 안 씀** — 추상화 오버헤드 제거, 디버깅 단순화
2. **Anthropic Batch API 우선** — 50% 할인 + 24시간 SLA, 실시간성 불필요
3. **Prompt Caching 필수** — 시스템 프롬프트 90% 절감
4. **JSON 강제 출력** — `tool_use` 또는 `response_format` 활용
5. **실패는 DLQ로** — 절대 사일런트 드롭 금지

## 파이프라인 단계

```
[SQS: work-queue]
      ↓
1. Fetch (S3에서 원본 fetch)
      ↓
2. Pre-process (텍스트 정규화, 길이 체크)
      ↓
3. LLM Batch (메타 추출 + 마스킹 + 요약 + 분류)
      ↓
4. Embed (Voyage batch)
      ↓
5. Persist (S3:processed + OpenSearch upsert)
      ↓
6. Post (중복 감지, 링크 자동 생성)
      ↓
[완료: SQS DeleteMessage]
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

**SQS visibility timeout 관리**: 배치 제출 후 메시지의 가시성을 24시간으로 연장
(`ChangeMessageVisibility`).

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

### OpenSearch (upsert)

```python
def index_doc(doc: RawDoc, meta: Metadata, embedding: list[float]):
    opensearch.index(
        index="vault",
        id=doc.id,
        body={
            "title": meta.title,
            "summary": meta.summary,
            "tags": meta.tags,
            "type": meta.type,
            "quality": meta.quality,
            "language": meta.language,
            "redacted_content": meta.redacted_content,
            "embedding": embedding,
            "s3_key": doc.s3_key,
            "indexed_at": datetime.utcnow().isoformat(),
        },
        refresh=False,
    )
```

### OpenSearch 인덱스 매핑

```json
{
  "settings": {
    "index.knn": true
  },
  "mappings": {
    "properties": {
      "title":             {"type": "text", "analyzer": "nori"},
      "summary":           {"type": "text", "analyzer": "nori"},
      "redacted_content":  {"type": "text", "analyzer": "nori"},
      "tags":              {"type": "keyword"},
      "type":              {"type": "keyword"},
      "quality":           {"type": "float"},
      "language":          {"type": "keyword"},
      "embedding": {
        "type": "knn_vector",
        "dimension": 1024,
        "method": {
          "name": "hnsw",
          "engine": "lucene",
          "space_type": "cosinesimil"
        }
      },
      "s3_key":     {"type": "keyword"},
      "indexed_at": {"type": "date"}
    }
  }
}
```

`nori`는 한국어 형태소 분석기 (OpenSearch 플러그인).

## 5) Post — 중복 감지

```python
def detect_duplicates(doc_id: str, embedding: list[float], threshold=0.95):
    hits = opensearch.knn_search(index="vault", query={
        "knn": {"embedding": {"vector": embedding, "k": 5}}
    })
    duplicates = [
        h for h in hits['hits']['hits']
        if h['_id'] != doc_id and h['_score'] > threshold
    ]
    if duplicates:
        write_merge_proposal(doc_id, duplicates)
```

`merge_proposal`은 별도 S3 경로(`proposals/`)에 쌓고 curator가 슬랙으로 보고.

## 6) 검색 (search-api에서)

```python
def hybrid_search(query: str, top_k=10, filters: dict | None = None):
    q_emb = embed_query(query)
    body = {
        "size": top_k,
        "query": {
            "bool": {
                "should": [
                    {"knn": {"embedding": {"vector": q_emb, "k": top_k}}},
                    {"multi_match": {
                        "query": query,
                        "fields": ["title^3", "summary^2", "redacted_content"],
                        "type": "best_fields",
                    }},
                ],
                "filter": build_filters(filters),
            }
        },
    }
    return opensearch.search(index="vault", body=body)
```

벡터 + BM25 결과를 OpenSearch의 RRF 또는 클라이언트 측에서 weighted sum.

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
| S3 fetch 실패 | 5회 재시도(지수 백오프) → DLQ |
| LLM 일시 오류 | Anthropic SDK 자동 재시도 |
| LLM 응답 스키마 위반 | tool_use 강제로 발생 어려움. 발생 시 DLQ |
| 임베딩 실패 | 3회 재시도 → DLQ |
| OpenSearch 일시 오류 | 5회 재시도 → DLQ |
| DLQ 도착 | curator가 다음날 슬랙 알림 |

## 비용/성능 메트릭 (필수)

- `processor_batch_size`            (히스토그램)
- `processor_llm_tokens_in/out`     (카운터)
- `processor_embed_tokens`          (카운터)
- `processor_batch_latency_seconds` (히스토그램)
- `processor_failures_total`        (카운터, by reason)
- `opensearch_index_latency_ms`     (히스토그램)

월말에 토큰 사용량 × 단가 자동 산정 → `08-cost-estimate.md` 모델에 피드백.
