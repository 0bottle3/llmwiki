# 02. AWS 서비스 매핑

각 책임에 어떤 AWS 서비스를 쓰는지, **왜** 그걸 골랐는지.

## 서비스 매핑 표

| 책임 | 서비스 | 대안 | 선택 이유 |
|------|--------|------|----------|
| 원본/가공본 저장 | **S3 Standard** | EFS | 마크다운은 객체 스토리지가 정답. Athena 쿼리 가능 |
| 이벤트 트리거 | **S3 Event → SQS** | EventBridge | SQS가 재시도/DLQ 깔끔. EventBridge는 fan-out이 필요할 때만 |
| 메시지 큐 | **SQS Standard** | SQS FIFO | 순서 무관, throughput 우선 |
| 컴퓨트 | **EKS Pod** | Lambda | Lambda 15분 한계 + Cold start. EKS 이미 있음 |
| 스케일링 | **KEDA** | HPA | SQS 큐 길이 기반 0→N 스케일 |
| 벡터 + 텍스트 | **OpenSearch Serverless** | Qdrant on EKS, Pinecone | k-NN + BM25 한 번에. AWS 통합 |
| 메타 인덱스 | **DynamoDB** (선택) | RDS | doc_id lookup 빠름. 없어도 OpenSearch로 가능 |
| 비밀 관리 | **Secrets Manager** | SSM Parameter Store | 자동 로테이션 |
| Pod IAM | **IRSA** | Node IAM | Pod별 최소 권한 |
| 인증 | **네트워크 게이트 + 호스트네임 (zero-touch)** | Cognito, 정적 토큰, mTLS | 사내 전용 → IP/VPN 게이트로 충분. 인증 서버 미운영 |
| 외부 노출 | **ALB Ingress** | NLB, CloudFront | L7 라우팅 + WAF |
| 시크릿 주입 | **External Secrets Operator** | CSI Driver | GitOps 친화 |
| 모니터링 | **CloudWatch + X-Ray** | Datadog | AWS 네이티브, 이미 있음 |
| 로깅 | **CloudWatch Logs + FluentBit** | Loki | EKS 기본 |
| 배포 | **ArgoCD / Helm** | kubectl | GitOps |
| 이미지 | **ECR** | Docker Hub | VPC endpoint, IAM |

## 핵심 결정: OpenSearch Serverless vs Qdrant

가장 큰 비용 결정 포인트.

### OpenSearch Serverless

**장점**
- 벡터(k-NN) + BM25 + 필터 한 인덱스에서 동시 처리
- IAM/VPC Endpoint 네이티브 통합
- 운영 부담 0 (관리형)
- AOSS(Amazon OpenSearch Serverless) collection 1개로 시작 가능

**단점**
- 최소 비용 ~$170/월 (2 OCU)
- 콜드 스타트 없지만 idle 시에도 과금
- 인덱스 매핑 변경 제약

### Qdrant on EKS

**장점**
- EKS 노드 비용에 포함 → 추가 비용 $0
- 풍부한 필터/페이로드 인덱싱
- 오픈소스, 락인 없음

**단점**
- 운영 책임 (백업/업그레이드/볼륨)
- BM25 별도 필요 (또는 Qdrant 자체 sparse vector)
- HA 구성 시 노드 수 증가

### 권장

- **MVP/소규모(~10명)**: Qdrant on EKS (비용 절감)
- **운영/대규모/관리 단순화**: OpenSearch Serverless

## 임베딩 모델 선택

| 모델 | 차원 | 비용 | 비고 |
|------|-----|------|------|
| **Voyage-3** | 1024 | $0.06/1M tok | 한국어 양호, 추천 |
| Voyage-3-large | 1024 | $0.18/1M tok | 정확도 ↑ |
| Cohere embed-v3 | 1024 | $0.10/1M tok | 다국어 강함 |
| OpenAI text-embedding-3-large | 3072 | $0.13/1M tok | 차원 큼, 저장 비용 ↑ |
| Anthropic | - | - | 자체 임베딩 미제공 → Voyage 공식 추천 |

**추천**: Voyage-3 (Anthropic 공식 파트너, 한국어 OK, 가성비)

## 가공 LLM

**Anthropic Claude (Batch API)**

- `claude-haiku-4-5-20251001` → 가공/요약/분류 (저비용)
- `claude-sonnet-4-6` → 품질 점수/리랭킹
- `claude-opus-4-8` → 큐레이션/병합 판정 (드물게)
- Batch API: 50% 할인, 24시간 SLA
- Prompt caching: 시스템 프롬프트 90% 절감

## 네트워크 토폴로지

```
[팀원 PC]
   ↓ HTTPS (Tailscale or VPN)
[Route53: wiki.team.internal]
   ↓
[ALB] (Internal scheme)
   ↓
[EKS Ingress Controller → search-api Service]
   ↓
[VPC Endpoints]
   ├─ S3 Gateway
   ├─ OpenSearch Interface
   ├─ Secrets Manager Interface
   └─ ECR Interface
```

전부 사내 VPC 안에서 종결. 외부 노출 없음.

## 리전/AZ

- **리전**: `ap-northeast-2` (서울)
- **AZ**: 최소 2개 (a, c) — Pod replica 분산
- **DR**: Phase 2+, 다른 리전으로 S3 CRR + 임베딩 재인덱싱

## 비용 영향 큰 항목 (요약)

1. **OpenSearch Serverless** (~$170/월)
2. **EKS 노드** (이미 운영 중이면 한계 비용 ~$30~60)
3. **LLM API** (배치 + 캐싱으로 $30~100)
4. **S3** (소량, $5 이하)
5. **데이터 전송** (사내망이면 거의 0)

자세한 산정은 `08-cost-estimate.md` 참고.
