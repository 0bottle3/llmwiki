# 01. 전체 아키텍처

## 한 줄 요약

> 팀원이 옵시디언 vault를 S3에 올리면 → EKS Pod가 LLM 배치로 자동 가공/임베딩 →
> OpenSearch에 색인 → 각자 AI가 MCP로 검색·참조.

## 아키텍처 다이어그램

```
┌─ 입력 (Producers) ──────────────────────────────────┐
│ 팀원 Obsidian                                        │
│   ├─ remotely-save plugin → S3 직접 sync            │
│   └─ Obsidian Git plugin → GitHub → CodeBuild → S3  │
└──────────────────────┬──────────────────────────────┘
                       ↓ (S3 PutObject Event)
┌─ AWS EKS Cluster ───────────────────────────────────┐
│                                                      │
│  ┌─ ingestor (Deployment, 1 replica) ────┐          │
│  │ • S3 Event Notification 수신 (SQS)    │          │
│  │ • 변경된 doc_id를 work-queue로 push    │          │
│  └──────────────┬─────────────────────────┘          │
│                 ↓                                    │
│            [ SQS: work-queue ]                       │
│                 ↓                                    │
│  ┌─ processor (KEDA-scaled, 0~N replicas) ┐         │
│  │ • SQS poll → 배치로 모음               │          │
│  │ • Anthropic Batch API 호출            │          │
│  │   - 메타데이터 추출                    │          │
│  │   - 민감정보 마스킹                    │          │
│  │   - 요약/태그/분류                     │          │
│  │ • 임베딩 생성 (Voyage/Cohere)          │          │
│  │ • S3에 가공본 write                    │          │
│  │ • OpenSearch에 upsert                  │          │
│  └──────────────┬─────────────────────────┘          │
│                 ↓                                    │
│  ┌─ search-api (Deployment, 2+ replicas) ┐          │
│  │ • FastAPI: REST 엔드포인트            │          │
│  │ • MCP server (SSE/HTTP transport)     │          │
│  │ • 하이브리드 검색 + 리랭킹            │          │
│  │ • → ALB Ingress (사내 도메인)         │          │
│  └────────────────────────────────────────┘          │
│                                                      │
│  ┌─ curator (CronJob, 매일 09:00 KST) ──┐           │
│  │ • outdated/중복/저품질 문서 마킹     │           │
│  │ • 일일 다이제스트 → Slack            │           │
│  └────────────────────────────────────────┘          │
└──────────────────────┬───────────────────────────────┘
                       ↓
┌─ 저장소 (Stateful) ──────────────────────────────────┐
│ S3                                                    │
│   ├─ raw/        원본 마크다운                       │
│   ├─ processed/  가공본 (JSON)                       │
│   ├─ embeddings/ 임베딩 (parquet)                    │
│   └─ snapshots/  일일 백업                           │
│                                                       │
│ OpenSearch Serverless                                 │
│   └─ vault index: 벡터(k-NN) + BM25 하이브리드       │
│                                                       │
│ DynamoDB (선택)                                       │
│   └─ doc 메타데이터 핫 인덱스                        │
└──────────────────────┬───────────────────────────────┘
                       ↓
┌─ 소비 (Consumers) ───────────────────────────────────┐
│ 팀원 Claude Desktop / Claude Code / Cursor           │
│   └─ MCP client → https://wiki.team.internal/mcp     │
│      (Cognito JWT 또는 mTLS)                         │
└───────────────────────────────────────────────────────┘
```

## 데이터 플로우 (E2E)

### (1) 업로드 → 가공 트리거

```
1. 팀원이 노트 작성 → Obsidian
2. remotely-save → s3://team-vault/raw/{member}/{YYYY-MM}/note.md
3. S3 Event Notification → SQS:s3-events
4. ingestor가 SQS:s3-events 컨슘
   - doc_id 생성 (S3 key hash)
   - 변경 종류 판정 (생성/수정/삭제)
   - SQS:work-queue로 작업 enqueue
```

### (2) 가공 (배치)

```
5. processor가 SQS:work-queue를 N개씩 batch pull
6. Anthropic Batch API 요청 생성
   - system prompt는 prompt caching 활성화
   - 50% 비용 할인 + 24시간 SLA
7. 배치 완료 후 결과 fetch
8. 각 문서에 대해:
   - processed/{doc_id}.json 작성
   - 임베딩 생성 (Voyage v3 등)
   - OpenSearch upsert
   - 중복 감지 시 merge-proposal 생성
```

### (3) 검색 (실시간)

```
9. 팀원 AI → MCP search(query)
10. search-api:
    - 쿼리 임베딩 생성
    - OpenSearch 하이브리드 검색 (벡터 + BM25)
    - top-k 결과를 LLM으로 리랭킹 (선택)
    - 결과 반환 (제목/요약/링크/관련도)
11. AI가 답변에 인용
```

### (4) 큐레이션 (배치)

```
12. curator CronJob 매일 09:00 KST 실행
    - 30일 이상 미수정 + low-confidence → outdated 마킹
    - 임베딩 유사도 0.95+ → 중복 후보
    - 슬랙으로 다이제스트 발송
13. 사람이 검토 후 PR 머지 or 무시
```

## 컴포넌트 책임 분리

| 컴포넌트 | 입력 | 출력 | 상태 |
|---------|------|------|------|
| ingestor | S3 Event | SQS:work-queue 메시지 | Stateless |
| processor | SQS:work-queue | S3:processed + OpenSearch | Stateless |
| search-api | HTTP/MCP 요청 | 검색 결과 | Stateless |
| curator | 시간 트리거 | Slack + S3 마킹 | Stateless |

**모든 워크로드 stateless** → 상태는 S3/OpenSearch에만 존재 → 스케일/롤백 자유.

## 비기능 요구사항 (NFR)

| 항목 | 목표 |
|-----|------|
| 가공 SLA | 신규 문서 → 24시간 내 검색 가능 |
| 검색 응답 | p95 < 800ms |
| 가용성 | 99% (단일 리전, AZ 분산) |
| 데이터 보존 | 원본 영구, 가공본 영구, 임베딩 최신만 |
| 보안 | 사내망 only (Tailscale/VPN) |

## 확장 포인트 (Phase 2+)

- 정적 HTML 위키 export (Quartz/Astro)
- Slackbot으로 자연어 검색
- 자동 PR 리뷰 (가공 품질 검증)
- 권한 분리 (팀별 인덱스)
- 멀티 리전 DR
