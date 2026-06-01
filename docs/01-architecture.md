# 01. 전체 아키텍처

## 한 줄 요약

> 팀원이 옵시디언 vault를 S3에 올리면 → EKS Pod가 LLM 배치로 자동 가공/임베딩 →
> Qdrant에 색인 → 각자 AI가 MCP로 검색·참조.

## 아키텍처 다이어그램

```
┌─ 입력 (Producers) ──────────────────────────────────┐
│ 팀원 PC                                              │
│   ├─ Obsidian / 데몬 → ALB internal /ingest         │
│   │                    → ingest-api Pod →(IRSA)→ S3 │
│   └─ Obsidian Git plugin → GitHub → CodeBuild → S3  │
│  (팀원 PC에 AWS 자격증명 없음. ALB 회사 IP SG가 게이트,│
│   Pod가 자기 IRSA로 S3에 씀. 업로드는 HTTPS POST,    │
│   MCP 아님 — MCP는 검색 전용)                        │
└──────────────────────┬──────────────────────────────┘
                       ↓ (S3 raw/ 에 적재)
┌─ AWS EKS Cluster ───────────────────────────────────┐
│                                                      │
│  ┌─ processor (CronJob, 주기 실행) ───────┐          │
│  │ • S3 raw/ 스캔 → 바뀐 문서만 추림      │          │
│  │ • Anthropic Batch API 호출            │          │
│  │   - 메타데이터 추출 / 민감정보 마스킹  │          │
│  │   - 요약 / 태그 / 분류                 │          │
│  │ • 임베딩 생성 (Voyage)                 │          │
│  │ • S3 processed/ write + 검색 인덱스 upsert │      │
│  └──────────────┬─────────────────────────┘          │
│                 ↓                                    │
│  ┌─ search-api (Rollout, 2+ replicas) ───┐          │
│  │ • FastAPI: REST 엔드포인트            │          │
│  │ • MCP server (SSE/HTTP transport)     │          │
│  │ • 하이브리드 검색 + 리랭킹            │          │
│  │ • → ALB Ingress (사내 도메인)         │          │
│  └────────────────────────────────────────┘          │
└──────────────────────┬───────────────────────────────┘
                       ↓
┌─ 저장소 (Stateful) ──────────────────────────────────┐
│ S3                                                    │
│   ├─ raw/        원본 마크다운 + 대화 transcript     │
│   ├─ processed/  가공본 (JSON)                       │
│   ├─ embeddings/ 임베딩 (parquet)                    │
│   └─ snapshots/  일일 백업                           │
│                                                       │
│ Qdrant on EKS (벡터 검색; OpenSearch Serverless는 확장경로) │
│   └─ vault collection: 벡터(k-NN) + 키워드 보조      │
└──────────────────────┬───────────────────────────────┘
                       ↓
┌─ 소비 (Consumers) ───────────────────────────────────┐
│ 팀원 Claude Code / Codex CLI / Kiro (Desktop 등)     │
│   └─ MCP client → https://wiki.team.internal/mcp     │
│      (사내망/VPN + 회사 IP 게이트, 인증 서버 없음)   │
└───────────────────────────────────────────────────────┘
```

> **구성**: 상시 Pod = `search-api` + `Qdrant`, 주기 Job = `processor`(CronJob).
> 업로드는 `ingest-api` Pod 경유(ALB internal `/ingest` → IRSA로 S3). zero-touch
> 게이트웨이가 필요 없는 초기엔 본인 PC `aws s3 sync`로 시작해도 됨.
> 실시간 이벤트(ingestor/SQS), KEDA, curator는 제거됨 — 필요해지면 "확장 경로"로.
> CloudFront는 *정적 위키 읽기 전용*(선택)에만 쓰며, 업로드 경로가 아니다.

## 데이터 플로우 (E2E)

### (1) 업로드 (ingest-api Pod 경유)

```
1. 팀원이 노트 작성 → Obsidian / 데몬
2. 데몬이 HTTPS POST → ALB internal /ingest (회사 IP SG 게이트,
   X-Vault-Hostname 헤더로 작성자 식별)
3. ingest-api Pod가 자기 IRSA 권한으로 S3에 씀 (PC엔 AWS 키 없음)
   → s3://team-vault/raw/{member}/{YYYY-MM}/note.md
   (AI 대화 transcript도 같은 경로로 raw/conversations/...)
```

> 업로드는 평범한 HTTPS POST다. MCP는 *검색(읽기)* 에만 쓰이고 업로드와 무관.
> 초기엔 ingest-api 없이 본인 PC `aws s3 sync`로 시작하는 것도 가능(15번 참고).

### (2) 가공 (배치 CronJob)

```
3. processor CronJob이 정해진 주기(기본 매시)로 실행
4. S3 raw/ 스캔 → 지난 실행 이후 바뀐 문서만 선별 (etag/타임스탬프)
5. Anthropic Batch API 요청 생성
   - system prompt는 prompt caching 활성화
   - 50% 비용 할인 + 24시간 SLA (Pod 1개가 전부 제출, Anthropic이 병렬 처리)
6. 배치 완료 후 결과 fetch, 각 문서에 대해:
   - processed/{doc_id}.json 작성
   - 임베딩 생성 (Voyage v3)
   - 검색 인덱스 upsert
   - 실패분은 다음 실행에서 재시도
```

### (3) 검색 (실시간)

```
7. 팀원 AI → MCP search(query)
8. search-api:
   - 쿼리 임베딩 생성
   - 하이브리드 검색 (벡터 + BM25)
   - top-k 결과를 LLM으로 리랭킹 (선택)
   - 결과 반환 (제목/요약/링크/관련도)
9. AI가 답변에 인용
```

## 컴포넌트 책임 분리

| 컴포넌트 | 종류 | 입력 | 출력 | 상태 |
|---------|------|------|------|------|
| ingest-api | Deployment (상시, 선택) | HTTPS POST `/ingest` | S3 raw/ write | Stateless |
| processor | CronJob (주기) | S3 raw/ (주기 스캔) | S3 processed/ + Qdrant upsert | Stateless |
| search-api | Deployment (상시) | HTTP/MCP 요청 | 검색 결과 | Stateless |
| Qdrant | StatefulSet (상시) | upsert/search | 벡터 검색 | **Stateful (PV)** |

업로드는 ALB → ingest-api Pod 경유(IRSA로 S3 write). Qdrant만 상태를 가지며(PV),
나머지 워크로드는 stateless → 상태는 S3/Qdrant에만 → 스케일/롤백 자유.

## 비기능 요구사항 (NFR)

| 항목 | 목표 |
|-----|------|
| 가공 SLA | 신규 문서 → CronJob 주기 + Batch 24h 내 검색 가능 |
| 검색 응답 | p95 < 800ms |
| 가용성 | 99% (단일 리전, AZ 분산) |
| 데이터 보존 | 원본 영구, 가공본 영구, 임베딩 최신만 |
| 보안 | 사내망 only (회사 IP / VPN) |

## 확장 경로 (Phase 2+, 필요해지면 다시 붙인다)

현재는 MVP로 빠졌지만, 요구가 생기면 단계적으로 추가:

- **실시간 반영** (분 단위 필요 시): S3 Event → SQS → `ingestor` Pod + processor를 상시 컨슈머로 (KEDA 0→N)
- **큐레이션** (`curator` CronJob): outdated/중복 판정, Slack 일일 다이제스트
- 정적 HTML 위키 export (Quartz/Astro)
- Slackbot으로 자연어 검색
- 권한 분리 (팀별 인덱스)
- 멀티 리전 DR
