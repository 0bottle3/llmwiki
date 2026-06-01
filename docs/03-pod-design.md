# 03. EKS Pod 설계

워크로드별 Pod 명세, 리소스 요청, 스케일 정책.

## 워크로드 목록

| 이름 | 종류 | 복제수 | 트리거 |
|------|-----|-------|--------|
| processor | CronJob | 1 (기본 매시) | 시간 (주기 스캔) |
| search-api | Deployment | 2~4 | HTTP |
| curator | CronJob | 1 (매일 09:00 KST) | 시간 |

전부 **stateless**. 상태는 외부(S3 / Qdrant)에만.

> **아키텍처 기준(01번과 일치)**: 가공은 `processor` **CronJob**이 S3 raw/를
> 주기 스캔해서 바뀐 문서만 가공한다. 실시간 이벤트 경로(ingestor / SQS /
> KEDA)는 MVP에서 **제거**했다 — 필요해지면 맨 아래 "확장 경로"로 다시 붙인다.
> 벡터 저장소는 **Qdrant on EKS**(파드)이며, OpenSearch Serverless는 확장 경로.

## 네임스페이스

```
namespace: team-vault
labels:
  app.kubernetes.io/part-of: team-vault
```

ResourceQuota / NetworkPolicy로 격리.

## 1) processor (CronJob)

### 책임
- 정해진 주기(기본 매시)로 실행
- S3 raw/ 스캔 → 지난 실행 이후 바뀐 문서만 선별 (etag/타임스탬프 비교)
- Anthropic Batch API 요청 빌드 (한 batch = 최대 100k 요청)
- 배치 결과 polling
- 임베딩 생성 (Voyage API batch)
- S3:processed/ 에 가공본 write
- Qdrant collection에 upsert
- **삭제 동기화**: raw/에서 사라진 문서의 doc_id는 Qdrant/processed에서도 제거
  (M2 — doc_id = sha256(s3_key)이므로 파일명/경로 변경 시 옛 doc_id가 orphan으로
  남는다. 매 실행 끝에 "현재 raw/ 목록에 없는 doc_id" 청소)
- 실패분은 다음 실행에서 재시도 (워크 상태는 processed/ etag로 멱등 판정)

### 종류 / 스케줄
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: processor
spec:
  schedule: "0 * * * *"        # 매시 정각 (조정 가능)
  concurrencyPolicy: Forbid     # 이전 실행이 안 끝났으면 건너뜀
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  backoffLimit: 2
```

### 리소스
```yaml
resources:
  requests: { cpu: 500m, memory: 1Gi }
  limits:   { cpu: 2,    memory: 4Gi }
```

CronJob이라 idle 시 파드 0 (스케줄 시에만 기동) → KEDA 없이도 비용 절감.

### IRSA 권한
```
s3:ListBucket, GetObject (raw/*)
s3:PutObject, GetObject, DeleteObject (processed/*, embeddings/*, proposals/*)
secretsmanager:GetSecretValue            (LLM keys)
```

Qdrant는 클러스터 내부 서비스(파드)라 AWS IAM 권한 불필요 — K8s NetworkPolicy로 접근 제어.

### 배치 처리 정책
- 1 실행 사이클: 바뀐 문서 전부를 한 Batch로 제출 (최대 100개 또는 10MB 토큰 단위로 분할)
- Batch 제출 후 같은 CronJob 실행 안에서 완료까지 polling (평균 1~6시간, 최대 24h SLA)
- 한 실행이 다음 스케줄까지 안 끝나면 `concurrencyPolicy: Forbid`로 중복 방지
- **증분이 급한 콘텐츠(transcript 등)는 Batch가 아니라 일반 API 권장** (D6 참고)

## 2) search-api

### 책임
- REST: `/search`, `/document/{id}`, `/recent`
- MCP (SSE/HTTP transport) 노출: `mcp.search`, `mcp.get_document`, `mcp.recent_changes`
- 쿼리 임베딩 → Qdrant 벡터 검색 (+ 키워드 보조)
- (선택) Claude Haiku로 top-k 리랭킹

### 리소스
```yaml
resources:
  requests: { cpu: 250m, memory: 512Mi }
  limits:   { cpu: 1,    memory: 1Gi }
replicas: 2
```

### HPA
```yaml
metrics:
- type: Resource
  resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
minReplicas: 2
maxReplicas: 8
```

### Service & Ingress
```yaml
# Service
type: ClusterIP
ports:
  - name: http,  port: 8080
  - name: mcp,   port: 8081   # SSE/HTTP transport

# Ingress (ALB Internal)
host: wiki.team.internal
paths:
  - /api/*  → search-api:8080
  - /mcp/*  → search-api:8081
annotations:
  alb.ingress.kubernetes.io/scheme: internal                 # 외부 노출 없음
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
  alb.ingress.kubernetes.io/certificate-arn: <ACM cert>
  alb.ingress.kubernetes.io/security-groups: <회사 IP SG>    # 인증 대신 네트워크 게이트
```

### IRSA 권한
```
s3:GetObject                    (processed/*)
secretsmanager:GetSecretValue   (LLM keys, 리랭킹용)
```

Qdrant 접근은 클러스터 내부 서비스 호출 (IAM 불필요).

## 3) curator

### 책임
- 매일 09:00 KST 실행
- 전체 인덱스 스캔
- outdated/중복/저품질 판정
- Slack incoming-webhook으로 다이제스트 발송
- 자동 마킹(태그 추가) — 삭제는 안 함

### 리소스
```yaml
resources:
  requests: { cpu: 250m, memory: 512Mi }
  limits:   { cpu: 1,    memory: 2Gi }
```

### CronJob
```yaml
schedule: "0 0 * * *"     # UTC 00:00 = KST 09:00
concurrencyPolicy: Forbid
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 3
backoffLimit: 1
```

### IRSA 권한
```
s3:GetObject, PutObject (processed/*)
secretsmanager:GetSecretValue
```

## 공통 사항

### 이미지
- 베이스: `python:3.12-slim` 또는 `distroless/python3`
- 멀티 스테이지 빌드
- `non-root` user, read-only root filesystem
- 이미지 스캔: ECR Image Scanning + Trivy

### 환경 변수 (모두 공통)
```
AWS_REGION=ap-northeast-2
QDRANT_ENDPOINT=http://qdrant.team-vault.svc:6333
S3_BUCKET=team-vault
LLM_PROVIDER=anthropic
EMBED_PROVIDER=voyage
LOG_LEVEL=INFO
OTEL_EXPORTER_OTLP_ENDPOINT=...
```

API 키류는 **External Secrets Operator**로 Secrets Manager에서 주입.

### 관측성
- 모든 Pod: `OTEL_*` 환경변수, FluentBit 사이드카 또는 DaemonSet
- 메트릭: CronJob 실행 시간/성공률, 처리 지연, LLM 호출 수, 토큰 사용량, Qdrant upsert 지연
- 대시보드: CloudWatch + Grafana

### PodDisruptionBudget
```yaml
search-api:  minAvailable: 1
# processor/curator는 CronJob → PDB 불필요 (다음 스케줄에 재시도)
```

### NetworkPolicy
- processor/curator: egress to AWS APIs(S3/SM) + 외부 LLM API + Qdrant 서비스만 허용
- search-api: ingress from ALB만, egress to Qdrant/S3만
- Qdrant: ingress from processor/search-api/curator만 (클러스터 내부)
- 그 외 pod-to-pod 통신 차단
