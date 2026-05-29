# team-vault-gitops

ArgoCD로 배포하는 Team Vault Helm chart 모음.

## 디렉토리

```
team-vault-gitops/
├── namespace.yaml              # team-vault 네임스페이스, ResourceQuota, LimitRange
├── networkpolicy.yaml          # default-deny + 명시 허용
├── ingest-api/                 # 팀원 PC → S3 게이트웨이 (rollout, blue/green)
├── search-api/                 # REST + MCP (rollout, blue/green)
├── ingestor/                   # S3 이벤트 → work queue (deployment, 단일)
├── processor/                  # LLM 배치 가공 (deployment, KEDA 0→N)
└── curator/                    # 일일 다이제스트 (cronjob)
```

## 차트별 책임

| 차트 | 종류 | 트리거 | 외부 노출 |
|-----|------|--------|---------|
| ingest-api | Rollout (blue/green) | HTTPS POST | ALB internal `/ingest` |
| search-api | Rollout (blue/green) | HTTPS GET / MCP | ALB internal `/api`, `/mcp` |
| ingestor | Deployment (단일) | SQS:s3-events | metrics only |
| processor | Deployment + KEDA | SQS:work-queue | metrics only |
| curator | CronJob (매일 09:00 KST) | 시간 | metrics only |

## ArgoCD Application 예시

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: team-vault-ingest-api-prod
  namespace: argocd
spec:
  project: team-vault
  source:
    repoURL: [MASKED_EMAIL]:YOUR_ORG/team-vault-gitops.git
    targetRevision: main
    path: ingest-api
    helm:
      valueFiles:
        - values.yaml
        - values-prod.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: team-vault
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```

각 차트(`ingest-api`, `search-api`, `ingestor`, `processor`, `curator`)별 + 환경(`dev`, `prod`)별로 Application 생성.

## 사전 요구사항

- EKS 클러스터 (`Position: workload` 라벨 노드 풀)
- ArgoCD + Argo Rollouts 컨트롤러
- KEDA (processor용)
- External Secrets Operator + `sa-external-secrets` (kube-system)
- AWS Load Balancer Controller
- ECR 레포 5개: `team-vault/{ingest-api,search-api,ingestor,processor,curator}`
- Secrets Manager 시크릿:
  - `team-vault/anthropic-api-key` (key: `api_key`)
  - `team-vault/voyage-api-key` (key: `api_key`)
  - `team-vault/slack-webhook` (key: `url`)
- IRSA Role 5종 (Terraform으로 별도 생성)
- ACM 인증서 (ALB용)
- Route53 private hosted zone `team.internal`

## IRSA Role 매트릭스

| ServiceAccount | S3 | SQS | AOSS | Secrets Manager |
|---------------|----|----|------|----------------|
| sa-team-vault-for-aws (ingest) | PUT raw/conversations/* | - | - | Get team-vault/* |
| sa-team-vault-search-for-aws | GET processed/* | - | APIAccessAll (read) | Get team-vault/* |
| sa-team-vault-ingestor-for-aws | HeadObject raw/* | s3-events R/D, work-queue Send | - | - |
| sa-team-vault-processor-for-aws | GET raw, PUT processed/embeddings | work-queue R/D, dlq Send | APIAccessAll (read+write) | Get team-vault/* |
| sa-team-vault-curator-for-aws | GET processed, PUT proposals | - | APIAccessAll (read+write) | Get team-vault/* |

## 환경별 적용

```bash
# Helm dry-run으로 검증
helm template ingest-api ./ingest-api -f ./ingest-api/values.yaml -f ./ingest-api/values-prod.yaml

# 또는 ArgoCD가 자동 sync (위 Application)
```

## 주의

- 모든 차트의 `values.yaml`에 `ACCOUNT`, `OPENSEARCH_ENDPOINT`, `SQS_*_URL`은 placeholder
- 실 환경 값은 `values-{env}.yaml` 또는 Terraform output에서 주입
- 이미지 태그는 CI에서 생성된 값으로 ArgoCD Image Updater 또는 PR로 갱신
