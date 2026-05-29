# 03. EKS Pod 설계

워크로드별 Pod 명세, 리소스 요청, 스케일 정책.

## 워크로드 목록

| 이름 | 종류 | 복제수 | 트리거 |
|------|-----|-------|--------|
| ingestor | Deployment | 1 | SQS:s3-events |
| processor | Deployment + KEDA | 0~20 | SQS:work-queue |
| search-api | Deployment | 2~4 | HTTP |
| curator | CronJob | 1 (매일 09:00 KST) | 시간 |

전부 **stateless**. 상태는 외부(S3/OpenSearch)에만.

## 네임스페이스

```
namespace: team-vault
labels:
  app.kubernetes.io/part-of: team-vault
```

ResourceQuota / NetworkPolicy로 격리.

## 1) ingestor

### 책임
- SQS:s3-events에서 S3 PutObject/Delete 이벤트 수신
- 변경된 doc_id 산출 (S3 key → hash)
- SQS:work-queue로 가공 작업 enqueue
- 중복 이벤트는 디바운싱 (같은 key 60초 내 다회 수정 → 1회만)

### 리소스
```yaml
resources:
  requests: { cpu: 100m, memory: 256Mi }
  limits:   { cpu: 500m, memory: 512Mi }
replicas: 1
```

### 헬스체크
- `/healthz`: SQS 연결 가능 여부
- `/readyz`: 시작 후 첫 메시지 처리 완료

### IRSA 권한
```
sqs:ReceiveMessage, DeleteMessage  (s3-events)
sqs:SendMessage                     (work-queue)
s3:HeadObject, GetObjectTagging     (raw/*)
```

## 2) processor

### 책임
- SQS:work-queue에서 작업 N개 batch poll
- Anthropic Batch API 요청 빌드 (한 batch = 최대 100k 요청)
- 배치 결과 polling/webhook
- 임베딩 생성 (Voyage API batch)
- S3:processed/ 에 가공본 write
- OpenSearch 인덱스에 upsert
- 실패 시 DLQ로

### 리소스
```yaml
resources:
  requests: { cpu: 500m, memory: 1Gi }
  limits:   { cpu: 2,    memory: 4Gi }
```

### KEDA 스케일

```yaml
triggers:
- type: aws-sqs-queue
  metadata:
    queueURL: <work-queue-url>
    queueLength: "10"        # 메시지 10개당 replica 1개
    awsRegion: ap-northeast-2
  authenticationRef:
    name: keda-trigger-auth-aws

minReplicaCount: 0
maxReplicaCount: 20
pollingInterval: 30
cooldownPeriod: 300
```

idle 시 0으로 떨어져 비용 절감. 큐 차면 자동 확장.

### IRSA 권한
```
sqs:ReceiveMessage, DeleteMessage, ChangeMessageVisibility  (work-queue)
sqs:SendMessage                                              (dlq)
s3:GetObject (raw/*), PutObject (processed/*, embeddings/*)
aoss:APIAccessAll                                            (OpenSearch)
secretsmanager:GetSecretValue                                (LLM keys)
```

### 배치 처리 정책
- 1 사이클: 최대 100개 문서 또는 10MB 토큰
- 배치 제출 후 SQS visibility timeout 연장 (`ChangeMessageVisibility`)
- 배치 완료(평균 1~6시간) 후 DeleteMessage

## 3) search-api

### 책임
- REST: `/search`, `/document/{id}`, `/recent`
- MCP (SSE/HTTP transport) 노출: `mcp.search`, `mcp.get_document`, `mcp.recent_changes`
- 쿼리 임베딩 → OpenSearch 하이브리드 검색
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
  alb.ingress.kubernetes.io/scheme: internal
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
  alb.ingress.kubernetes.io/certificate-arn: <ACM cert>
  alb.ingress.kubernetes.io/auth-type: cognito   # 또는 OIDC
```

### IRSA 권한
```
aoss:APIAccessAll               (OpenSearch read)
s3:GetObject                    (processed/*)
secretsmanager:GetSecretValue   (LLM keys, 리랭킹용)
```

## 4) curator

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
aoss:APIAccessAll
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
OPENSEARCH_ENDPOINT=...aoss.amazonaws.com
S3_BUCKET=team-vault
SQS_WORK_QUEUE_URL=...
SQS_DLQ_URL=...
LLM_PROVIDER=anthropic
EMBED_PROVIDER=voyage
LOG_LEVEL=INFO
OTEL_EXPORTER_OTLP_ENDPOINT=...
```

API 키류는 **External Secrets Operator**로 Secrets Manager에서 주입.

### 관측성
- 모든 Pod: `OTEL_*` 환경변수, FluentBit 사이드카 또는 DaemonSet
- 메트릭: SQS depth, 처리 지연, LLM 호출 수, 토큰 사용량
- 대시보드: CloudWatch + Grafana

### PodDisruptionBudget
```yaml
search-api:  minAvailable: 1
ingestor:    minAvailable: 0  (단일 replica, 잠시 끊겨도 SQS가 버퍼)
processor:   minAvailable: 0  (재시도 가능)
```

### NetworkPolicy
- ingestor/processor: egress to AWS APIs만 허용
- search-api: ingress from ALB만, egress to OpenSearch/S3만
- pod-to-pod 통신 없음 (전부 외부 서비스로)
