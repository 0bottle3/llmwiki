# team-vault-gitops

ArgoCD/Helm으로 배포하는 Team Vault 매니페스트.

MVP 구성은 **차트 2개**다. 대화 업로드는 Pod 없이 **CloudFront(회사 IP 게이트) → S3 직접 업로드**로 처리한다.

## 디렉토리

```
team-vault-gitops/
├── namespace.yaml      # team-vault 네임스페이스, ResourceQuota, LimitRange
├── networkpolicy.yaml  # default-deny + 명시 허용
├── search-api/         # REST + MCP (Rollout, blue/green) — 유일한 상시 Pod
└── processor/          # LLM 배치 변환 (CronJob, 주기 실행)
```

## 차트별 책임

| 차트 | 종류 | 트리거 | 외부 노출 |
|-----|------|--------|---------|
| search-api | Rollout (blue/green) | HTTPS GET / MCP | ALB internal `/api`, `/mcp` |
| processor | CronJob | 시간 (기본 매시 정각) | 없음 |

## 업로드 경로 — Pod 없음 (CloudFront → S3)

팀원 PC는 AWS 자격증명 없이 **CloudFront 엔드포인트로 바로 PUT**한다.
CloudFront 앞단 WAF가 회사 IP만 통과시키고(OAC로 S3 서명), 게이트웨이 Pod는 불필요.

```
팀원 PC (데몬/Obsidian)
   ↓ HTTPS PUT  https://<cloudfront-dist>/raw/...
CloudFront + WAF (회사 IP set 화이트리스트)
   ↓ OAC (SigV4)
S3: team-vault/raw/...
   ↓ (주기) processor CronJob 이 스캔 → 가공 → processed/ + 검색 인덱스
```

- 자격증명/토큰 없음 (네트워크 게이트 = 신뢰), 식별은 경로/오브젝트 메타로
- CloudFront/WAF/OAC/S3 구성은 Terraform(인프라), 본 repo는 K8s 워크로드만
- 상세: `docs/14-network-access.md`, `docs/06-s3-structure.md`

## ArgoCD Application 예시 (search-api)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: team-vault-search-api-prod
  namespace: argocd
spec:
  project: team-vault
  source:
    repoURL: [MASKED_EMAIL]:YOUR_ORG/team-vault-gitops.git
    targetRevision: main
    path: search-api
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

`processor`도 동일 패턴으로 Application 1개 추가 (path: processor).

## Helm 직접 설치 (수동)

```bash
# 0. 네임스페이스/네트워크폴리시 (순수 yaml)
kubectl apply -f namespace.yaml
kubectl apply -f networkpolicy.yaml

# 1. search-api (상시 Pod)
helm upgrade --install team-vault-search ./search-api \
  -n team-vault -f ./search-api/values.yaml -f ./search-api/values-prod.yaml

# 2. processor (CronJob)
helm upgrade --install team-vault-processor ./processor \
  -n team-vault -f ./processor/values.yaml -f ./processor/values-prod.yaml

# 검증 (dry-run)
helm template ./search-api -f ./search-api/values.yaml -f ./search-api/values-prod.yaml
```

## 사전 요구사항

- EKS 클러스터 (`Position: workload` 라벨 노드 풀)
- ArgoCD + **Argo Rollouts** 컨트롤러 (search-api `kind: Rollout`)
- External Secrets Operator + `sa-external-secrets` (kube-system)
- AWS Load Balancer Controller (ALB Ingress)
- ECR 레포 2개: `team-vault/{search-api,processor}`
- Secrets Manager 시크릿:
  - `team-vault/anthropic-api-key` (key: `api_key`)
  - `team-vault/voyage-api-key` (key: `api_key`)
- IRSA Role 2종 (Terraform으로 별도 생성)
- ACM 인증서 (ALB용)
- Route53 private hosted zone `team.internal`
- (업로드용, 인프라) CloudFront + WAF(회사 IP set) + OAC + S3

> KEDA / SQS는 더 이상 필요 없다 (processor가 CronJob).

## IRSA Role 매트릭스

| ServiceAccount | S3 | AOSS | Secrets Manager |
|---------------|----|------|----------------|
| sa-team-vault-search-for-aws | GET processed/* | APIAccessAll (read) | Get team-vault/* |
| sa-team-vault-processor-for-aws | GET raw, PUT processed/embeddings | APIAccessAll (read+write) | Get team-vault/* |

## 주의

- `values.yaml`의 `ACCOUNT`, `OPENSEARCH_ENDPOINT`는 placeholder → `values-{env}.yaml` 또는 Terraform output에서 주입
- 이미지 태그는 CI 생성값으로 ArgoCD Image Updater 또는 PR로 갱신
- 대화 캡처를 원치 않으면 CloudFront 업로드 경로 없이 search-api + processor만으로 "노트 위키" 단독 운영 가능
