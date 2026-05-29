# 09. 구현 로드맵 (4주 MVP)

4주 안에 본인 + 시범 팀원 2~3명이 실제 사용 가능한 상태를 목표.

## 마일스톤 요약

| 주차 | 목표 | 산출물 |
|------|------|--------|
| 1 | 인프라 골격 | Terraform/CDK, EKS namespace, S3, SQS, OpenSearch/Qdrant |
| 2 | 가공 파이프라인 | ingestor/processor 동작, E2E 1개 문서 가공 성공 |
| 3 | 검색 + MCP | search-api + MCP 노출, 본인 Claude 연결 |
| 4 | 팀 베타 | 팀원 2~3명, curator, 슬랙 다이제스트, 회고 |

## Week 1 — 인프라 골격

### 목표
"빈 통"을 다 만들기. 안에 들어가는 건 다음 주.

### 작업

- [ ] **AWS 리소스 (Terraform/CDK)**
  - [ ] S3 버킷 `team-vault` (버저닝/암호화/Public Block)
  - [ ] KMS 키 `alias/team-vault`
  - [ ] SQS 2개: `s3-events`, `work-queue` + DLQ
  - [ ] S3 → SQS Event Notification
  - [ ] OpenSearch Serverless collection `team-vault` (또는 Qdrant Helm 준비)
  - [ ] VPC Endpoint: S3, OpenSearch, Secrets Manager, ECR
  - [ ] Secrets Manager 시크릿 placeholder (값은 수동 입력)
  - [ ] ALB Ingress용 ACM 인증서
  - [ ] Route53 `wiki.team.internal`

- [ ] **EKS 셋업**
  - [ ] namespace `team-vault`
  - [ ] ResourceQuota / LimitRange
  - [ ] External Secrets Operator 설치
  - [ ] KEDA 설치
  - [ ] ALB Controller 확인
  - [ ] IRSA Role 4종 (ingestor / processor / search-api / curator)

- [ ] **저장소**
  - [ ] GitHub repo `team-vault` (코드)
  - [ ] GitHub repo `team-vault-content` (vault 원본, 옵션)
  - [ ] ECR repo 4종

### 완료 기준
- [ ] `terraform apply` 깨끗하게 통과
- [ ] EKS에서 `kubectl get serviceaccounts -n team-vault` 시 4개 IRSA 표시
- [ ] S3에 테스트 파일 업로드 → SQS:s3-events에 메시지 적재 확인

## Week 2 — 가공 파이프라인

### 목표
"노트 1개 업로드 → 1시간 안에 OpenSearch에 색인" E2E 성공.

### 작업

- [ ] **공통 코드 (`code-skeleton/`)**
  - [ ] Python 패키지 구조 (`shared/` 라이브러리)
  - [ ] S3 클라이언트, SQS 클라이언트, OpenSearch 클라이언트 래퍼
  - [ ] 데이터 모델 (Pydantic)
  - [ ] OTel/메트릭 셋업
  - [ ] 시크릿 로딩 (External Secrets에서 마운트된 파일)

- [ ] **ingestor**
  - [ ] SQS:s3-events 컨슘
  - [ ] doc_id 산출, 디바운스 (60초)
  - [ ] SQS:work-queue로 enqueue
  - [ ] Helm chart + Deployment
  - [ ] Dockerfile + GitHub Actions 빌드/푸시

- [ ] **processor**
  - [ ] SQS:work-queue 폴링
  - [ ] Anthropic Batch API 통합
    - [ ] System prompt + Prompt Caching
    - [ ] Tool use 강제 (submit_metadata)
    - [ ] Batch 제출 → 결과 fetch
  - [ ] Voyage 임베딩 배치
  - [ ] S3:processed/ 작성
  - [ ] OpenSearch upsert
  - [ ] Secret redaction (regex 이중 안전망)
  - [ ] DLQ 처리
  - [ ] KEDA ScaledObject

- [ ] **OpenSearch 인덱스**
  - [ ] 인덱스 매핑 적용 (nori 분석기, knn_vector 1024)
  - [ ] 초기 alias `vault` 설정

- [ ] **테스트**
  - [ ] 단위 테스트 (가공 함수, redaction, 스키마)
  - [ ] E2E: 마크다운 1개 → 가공 → OpenSearch 조회 성공

### 완료 기준
- [ ] 마크다운 업로드 후 6시간 이내 OpenSearch에서 doc_id 조회 가능
- [ ] processor 실패 시 DLQ에 메시지 적재
- [ ] CloudWatch에 메트릭/로그 표시

## Week 3 — 검색 + MCP

### 목표
본인 Claude Desktop에서 `team-vault.search()` 호출이 동작.

### 작업

- [ ] **search-api**
  - [ ] FastAPI 골격
  - [ ] `/api/search`, `/api/document/{id}`, `/api/recent`
  - [ ] 하이브리드 검색 (벡터 + BM25)
  - [ ] (선택) Claude Haiku 리랭킹
  - [ ] 인증 미들웨어 (정적 토큰 MVP)
  - [ ] OpenAPI 스펙 생성

- [ ] **MCP 서버**
  - [ ] FastMCP 통합
  - [ ] 도구: `search`, `get_document`, `recent_changes`,
        `related_documents`, `vault_glossary`
  - [ ] HTTP/SSE transport, 포트 8081
  - [ ] 인증 (Bearer token)

- [ ] **노출**
  - [ ] ALB Ingress 작성
  - [ ] Route53 A 레코드
  - [ ] HTTPS 확인 (`https://wiki.team.internal/healthz`)
  - [ ] PodDisruptionBudget, HPA

- [ ] **클라이언트 셋업 가이드**
  - [ ] Claude Desktop 설정 스니펫
  - [ ] Claude Code 설정 스니펫
  - [ ] Cursor 설정 스니펫
  - [ ] 토큰 발급 절차

- [ ] **본인 dogfood**
  - [ ] 본인 vault에 메모 50개+ 가공
  - [ ] Claude로 검색 시도 → 품질 평가
  - [ ] 시스템 프롬프트 튜닝

### 완료 기준
- [ ] Claude Desktop에서 `mcp.search("결제")` → 결과 5개 반환
- [ ] 응답 p95 < 1초
- [ ] 본인이 1주일 매일 사용해도 불편 없음

## Week 4 — 팀 베타

### 목표
시범 팀원 2~3명 합류, 다이제스트 자동화, 회고.

### 작업

- [ ] **curator (CronJob)**
  - [ ] outdated 판정 로직 (30일 + low quality)
  - [ ] 중복 후보 산출 (knn 유사도 > 0.95)
  - [ ] Slack incoming webhook으로 다이제스트 발송
  - [ ] 비용/사용량 요약 포함

- [ ] **팀원 온보딩**
  - [ ] 셋업 가이드 문서 (Obsidian + remotely-save 또는 git plugin)
  - [ ] 토큰 발급
  - [ ] 시범 사용자 2~3명 선정 + 페어 셋업
  - [ ] 첫 주 매일 5분 슬랙 채널로 피드백 수집

- [ ] **운영**
  - [ ] Runbook 작성:
    - DLQ 처리 절차
    - OpenSearch 재인덱싱 절차
    - 시크릿 로테이션
    - Pod 롤백
  - [ ] 백업 검증: snapshots/ 한 번 복구 시도
  - [ ] 모니터링 대시보드 (CloudWatch / Grafana)

- [ ] **회고**
  - [ ] 주말 회고: 잘된 점/문제점/다음 4주 계획
  - [ ] 비용 실측 vs 예상 비교
  - [ ] 검색 zero-hit / low-score 분석 → 컨텐츠 갭 파악

### 완료 기준
- [ ] 3명 팀원이 각자 AI에서 vault 검색 사용 중
- [ ] 일일 다이제스트가 슬랙에 정상 도착
- [ ] 1주일 운영 중 P1 장애 0건
- [ ] 회고 문서가 vault 자체에 저장됨 (메타 시연)

## 이후 계획 (Phase 2+)

- 정적 HTML export (Quartz/Astro)
- AI 기반 자동 문서 초안 작성 (write 도구)
- 팀별 인덱스 분리 + 권한 매트릭스
- Cognito 정식 도입
- DR 구성 (CRR)
- Slackbot으로 자연어 검색

## 리스크 / 미리 대비할 것

| 리스크 | 대비책 |
|--------|--------|
| Anthropic Batch 24h SLA 미달 | DLQ 처리 + 실시간 모드 fallback |
| OpenSearch Serverless 비용 폭증 | 초기 alarm + Qdrant 마이그레이션 플랜 보유 |
| 팀원이 안 씀 | 본인이 dogfood 1주 먼저 → 가치 증명 후 합류 권유 |
| 시크릿 유출 사고 | regex 이중 마스킹 + 인덱스 재생성 절차 사전 작성 |
| Prompt injection | tool_use 강제 + 시스템 프롬프트 가드 |

## 1인 작업 시간 예상

- Week 1: 풀타임 3일 (인프라 코드 + EKS 셋업)
- Week 2: 풀타임 5일 (가공 파이프라인이 가장 큼)
- Week 3: 풀타임 3일 + dogfood
- Week 4: 풀타임 2일 + 운영
- **총 ~13 인-일** (집중하면 3주 압축 가능)
