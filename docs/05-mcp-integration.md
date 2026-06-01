# 05. MCP 통합

팀원 AI(Claude/Cursor 등)가 팀 vault를 참조하는 방식. **이게 시스템의 진짜 가치**.

## 전제

- MCP (Model Context Protocol)는 2025년 사실상 표준
- Claude Desktop / Claude Code / Cursor / Cline 모두 지원
- HTTP/SSE transport로 원격 MCP 가능 (stdio는 로컬 only)

## 노출 방식 선택

| 방식 | 장점 | 단점 | 적합한 상황 |
|------|------|------|------------|
| **HTTP/SSE 원격 MCP** | 중앙 관리, 즉시 반영 | 인증 설계 필요 | **사내망 기본** |
| Local proxy + Tailscale | 보안 강함 | 팀원 셋업 추가 | 외부망 사용자 |
| REST + 시스템 프롬프트 | 단순 | AI가 알아서 호출 안 함 | MCP 미지원 도구 |

**추천: HTTP/SSE 원격 MCP (Tailscale 또는 사내 VPN 뒤)**

## 노출 엔드포인트

```
https://wiki.team.internal/mcp           (MCP SSE/HTTP)
https://wiki.team.internal/api/search    (REST, 디버그/외부 도구용)
https://wiki.team.internal/api/document  (REST)
https://wiki.team.internal/api/recent    (REST)
https://wiki.team.internal/healthz       (health)
```

## MCP 도구 정의 (search-api 안)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("team-vault")

\[MASKED_EMAIL]()
def search(query: str, top_k: int = 5, doc_type: str | None = None) -> list[dict]:
    """
    팀 위키에서 검색합니다.

    Args:
        query: 검색어 (자연어 OK)
        top_k: 반환할 결과 수 (기본 5, 최대 20)
        doc_type: 필터 (incident | runbook | knowledge | decision | daily)

    Returns:
        [{ doc_id, title, summary, tags, type, quality, score, s3_key }]
    """
    filters = {"type": doc_type} if doc_type else None
    hits = hybrid_search(query, top_k=min(top_k, 20), filters=filters)
    return [
        {
            "doc_id": h["_id"],
            "title": h["_source"]["title"],
            "summary": h["_source"]["summary"],
            "tags": h["_source"]["tags"],
            "type": h["_source"]["type"],
            "quality": h["_source"]["quality"],
            "score": h["_score"],
            "s3_key": h["_source"]["s3_key"],
        }
        for h in hits["hits"]["hits"]
    ]

\[MASKED_EMAIL]()
def get_document(doc_id: str) -> dict:
    """
    문서 전체 내용을 가져옵니다. search로 doc_id를 먼저 얻으세요.

    Args:
        doc_id: search 결과의 doc_id

    Returns:
        { doc_id, title, content, frontmatter, source }
    """
    payload = read_processed(doc_id)
    return {
        "doc_id": doc_id,
        "title": payload["frontmatter"]["title"],
        "content": payload["redacted_content"],
        "frontmatter": payload["frontmatter"],
        "source": payload["source"],
    }

\[MASKED_EMAIL]()
def recent_changes(days: int = 7, limit: int = 20) -> list[dict]:
    """
    최근 변경된 문서를 반환합니다. "어제 회의록", "최근 장애" 등의 질문에 사용.

    Args:
        days: 며칠 이내 (기본 7)
        limit: 최대 결과 수 (기본 20)
    """
    return query_recent(days=days, limit=limit)

\[MASKED_EMAIL]()
def related_documents(doc_id: str, top_k: int = 5) -> list[dict]:
    """
    임베딩 유사도 기반으로 관련 문서를 찾습니다.
    """
    emb = get_doc_embedding(doc_id)
    return knn_search(emb, top_k=top_k, exclude_ids=[doc_id])

\[MASKED_EMAIL]()
def vault_glossary() -> str:
    """
    팀 용어집과 위키 사용 가이드를 반환합니다.
    AI가 컨텍스트 부족할 때 가장 먼저 호출하면 좋습니다.
    """
    return read_glossary()
```

### 리소스 (선택적)

```python
\[MASKED_EMAIL]("vault://meta/glossary")
def glossary_resource() -> str:
    return read_glossary()

\[MASKED_EMAIL]("vault://meta/owners")
def owners_resource() -> str:
    return json.dumps(read_owners_map())
```

## 검색 가시성 — transcript는 owner 필터 (C3/M1)

`search`/`get_document`/`recent_changes`는 호출자의 호스트네임으로 식별된 `user`를
받아 **`sensitivity: private` 문서를 요청자 본인 것만** 반환한다. 사람이 작성한
공개 문서는 전원 검색 가능하지만, AI 대화 transcript는 기본 비공개이고 공유 표식
(`share: team`)이 있는 것만 공용으로 노출된다. (`07-§3`, `11` 프라이버시 정책)

```python
def visible_filter(requester: str) -> dict:
    # private은 owner 본인만, 그 외(internal/public)는 전원
    return {"should": [
        {"sensitivity": ["internal", "public"]},
        {"sensitivity": "private", "owner": requester},
    ]}
```

## 인증 — 안 함 (네트워크 게이트로 대체)

이 시스템은 **사내 전용**이다. 인증 서버(Cognito)나 토큰을 두지 않고,
*네트워크에 도달할 수 있느냐*로 신뢰를 판단한다. (`14`, `15` 참고)

```
[엔드포인트 도달 가능] = [회사 IP/VPN 안에 있음] = 신뢰
        ↑
  ALB scheme: internal  (인터넷에 안 보임, DNS도 사내만)
  + SG 회사 IP 화이트리스트  (TCP 레벨 차단)
```

- **로그인 없음 / 토큰 없음 / 헤더 인증 없음** → 팀원 셋업 0
- 작성자 *식별*만 필요 (감사 로그용) → 호스트네임 헤더 (`15-zero-touch-onboarding.md`)
- search-api는 인증 미들웨어 대신 `X-Vault-Hostname` 등 식별 헤더만 로깅

> 왜 Cognito/토큰을 안 쓰나: 이미 ALB internal + 회사 IP SG가 외부를 100% 차단한다.
> 그 위에 토큰을 얹으면 발급/로테이션/분실 대응 운영만 늘고 보안 이득은 거의 없다.
> 위협 모델이 올라가면(내부자 위장, 외부 협력사) `15`의 강화 경로(머신 ID → mTLS → 토큰)로.

### (강화용, 지금은 미적용) mTLS

MDM으로 회사 디바이스에 클라이언트 인증서를 깔 수 있게 되면,
ALB 앞단에서 mTLS 종료하고 search-api는 CN 헤더만 확인. 사내 위장까지 차단.

## 팀원 클라이언트 설정

토큰/헤더가 없어 설정이 URL 한 줄로 끝난다.

### Claude Desktop

```json
// ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)
{
  "mcpServers": {
    "team-vault": {
      "url": "https://wiki.team.internal/mcp"
    }
  }
}
```

### Claude Code

```jsonc
// ~/.claude/settings.json
{
  "mcpServers": {
    "team-vault": {
      "type": "http",
      "url": "https://wiki.team.internal/mcp"
    }
  }
}
```

### Cursor

```jsonc
// ~/.cursor/mcp.json
{
  "mcpServers": {
    "team-vault": {
      "url": "https://wiki.team.internal/mcp"
    }
  }
}
```

### MCP 미지원 도구용 fallback

REST 엔드포인트를 시스템 프롬프트에 명시 (인증 헤더 불필요, 사내망에서만 도달):

```
필요할 때 다음 API를 호출:
- GET https://wiki.team.internal/api/search?q=<query>
- GET https://wiki.team.internal/api/document/<doc_id>
```

## 사용 시나리오

### 시나리오 1: 장애 대응

```
[Alice가 결제 timeout 디버깅 중]
Alice → Claude: "결제 timeout 어떻게 봐야하지?"
Claude → mcp.search("결제 timeout 대응") → top 3 문서
Claude → mcp.get_document("...") → 상세 본문
Claude → Alice에게 "Bob이 작성한 runbook이 있어요. 우선 X 로그를 보세요..."
```

### 시나리오 2: 온보딩

```
[신규 입사자 Carol]
Carol → Claude: "우리팀 인프라 어떻게 생겼어?"
Claude → mcp.vault_glossary() → 용어집 컨텍스트
Claude → mcp.search("인프라 개요", doc_type="knowledge")
Claude → Carol에게 정리된 답변 + 추가 학습 링크
```

### 시나리오 3: 의사결정 추적

```
[PM이 과거 결정 확인]
PM → Claude: "왜 우리가 Redis 대신 DynamoDB 골랐지?"
Claude → mcp.search("Redis vs DynamoDB", doc_type="decision")
Claude → 해당 ADR + 관련 논의 인용
```

## 시스템 프롬프트 권장 사항

팀에 표준 시스템 프롬프트 배포:

```
# Team Vault 사용 규칙

당신은 우리팀 AI 어시스턴트입니다. 다음 규칙을 따르세요:

1. 팀 내부 지식이 필요한 질문은 항상 `team-vault.search`를 먼저 호출.
2. 첫 검색이 부족하면 키워드 바꿔서 2~3회 재검색.
3. 검색 결과의 `quality` 값을 신뢰도 가중치로 사용.
   - quality < 0.5: "draft" 라고 명시
   - quality > 0.8: 신뢰 가능한 출처
4. 검색해도 못 찾으면 "팀 vault에 관련 문서 없음" 명시. 추측 금지.
5. 답변 끝에 참조한 doc_id 또는 s3_key를 footnote로.
6. 민감정보가 답변에 포함되면 자동으로 마스킹 (이미 vault에서 마스킹되어 있음).
```

## 모니터링

검색 품질은 *사용 로그*가 정답:

- `mcp_tool_calls_total` (by tool_name, by user)
- `mcp_search_zero_hits_total` — 검색 결과 없음 (커버리지 부족 신호)
- `mcp_search_low_score_total` — top1 score < 0.5 (가공 품질 문제)
- `mcp_get_document_total` (어떤 문서가 자주 인용되는지 → 그게 핵심 자산)

curator가 이 로그를 분석해서 다이제스트에 포함.

## 진화 경로

- **Phase 1**: search + get_document만
- **Phase 2**: recent_changes, related_documents 추가
- **Phase 3**: write 도구 추가 (AI가 직접 문서 초안 작성 → 사람 승인 후 머지)
- **Phase 4**: 다중 인덱스 (팀별 격리)
