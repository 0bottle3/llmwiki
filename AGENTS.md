# AGENTS.md — llmwiki (Team Vault) 작업 가이드

> 이 파일은 **도구 중립** 작업 인수인계 문서다. Claude Code / Codex CLI / Gemini CLI /
> 사람 누구든 이걸 읽고 현재 상태를 파악하고 이어서 작업할 수 있도록 작성됐다.
> (Codex/Gemini는 이 파일명 `AGENTS.md`를 자동 인식한다.)
>
> **마지막 업데이트**: 2026-06-01

---

## 1. 이 프로젝트가 뭔가

**Team Vault (llmwiki)** — 팀의 도메인 지식·보고서·히스토리·AI 대화 기록을 한곳에 모아,
각자의 AI(Claude/Codex/Gemini 등)가 **MCP로 검색해서 참조**하게 하는 사내 지식 베이스.

> 목표: "LLM이 팀 컨텍스트를 빠르게 파악해서, 사람이 매번 설명하지 않아도 요청을 잘 수행하게."

핵심 데이터 흐름:
```
S3 raw/ (노트·transcript·코드docs)
   ↓  processor (CronJob, 매시) — LLM으로 요약/태그/마스킹 + 임베딩
Qdrant (벡터 검색 DB, EKS 파드)
   ↑  search-api (상시 파드) — REST + MCP
AI 클라이언트 (MCP로 검색)
```

전체 설계는 `docs/01~16`에 있다. **`docs/16-design-review.md`를 먼저 읽어라** —
설계 리뷰 결과와 확정된 결정이 거기 다 정리돼 있다.

---

## 2. 확정된 핵심 설계 결정 (꼭 지킬 것)

| 결정 | 내용 | 근거 문서 |
|------|------|----------|
| **가공 = CronJob** | processor를 1시간 주기 CronJob으로. ingestor/SQS/DLQ/KEDA **제거**(확장경로로 강등) | `docs/01,03,16` |
| **검색 저장소 = Qdrant on EKS** | 파드 1개. OpenSearch Serverless는 확장경로(비용 $170+) | `docs/02,16` |
| **업로드 = ALB → ingest-api Pod** | 팀원 PC에 AWS 키 없음. Pod가 IRSA로 S3 write. (CloudFront 아님) | `docs/01,14,15` |
| **MCP는 검색(읽기) 전용** | 업로드는 평범한 HTTPS POST. MCP와 무관 | `docs/05` |
| **transcript는 기본 비공개** | opt-in + owner만 검색. 공유 표식만 공용 인덱스 | `docs/07,11` |
| **인증 = 네트워크 게이트** | 회사 IP SG + 호스트네임 식별. 토큰/Cognito 없음 | `docs/07,14` |
| **단일 진실 = S3 raw/** | Qdrant·processed는 raw에서 재생성 가능 → 백업/HA 부담 작음 | `docs/06,11` |

---

## 3. 디렉토리 구조 (현재)

```
llmwiki/
├── AGENTS.md                  # ← 이 파일 (작업 인수인계)
├── README.md
├── docs/                      # 설계 문서 01~16 (한국어). 16번이 리뷰/결정 요약
├── deploy/
│   └── qdrant/                # ★ 이번 작업물: Qdrant Helm umbrella 차트
│       ├── Chart.yaml         # 공식 qdrant 차트를 dependency로 (umbrella)
│       ├── values.yaml        # 공통: 1 replica, gp2 기본, 무인증, on_disk_payload
│       ├── values-dev.yaml    # dev 오버라이드 (ns=llmwiki, 작은 리소스)
│       ├── values.lint.yaml   # helm lint용
│       ├── .helmignore
│       └── templates/
│           └── networkpolicy.yaml   # team-vault Pod만 6333/6334 ingress 허용
├── gitops/
│   └── qdrant/
│       └── application-dev.yaml      # ArgoCD Application (dev)
└── manifests/
    └── team-vault-gitops/     # ⚠️ 이전 세션 산출물 — 옛 설계 기반 (아래 6번 참고)
        ├── processor/         # CronJob 차트
        └── search-api/        # Rollout 차트
```

---

## 4. 지금까지 한 일 (2026-06-01 세션)

1. **설계 문서 전체 리뷰 + 반영** — 문서 간 모순(C1: 아키텍처)과 보안 리스크
   (C2: 마스킹 오작동, C3: transcript 프라이버시) 등을 잡아 `docs/01~15`에 반영,
   결정 기록을 `docs/16-design-review.md`로 신설.
2. **Qdrant Helm 차트 작성** (`deploy/qdrant/`) — 회사 표준인 **umbrella dependency 패턴**
   (참고: 다른 도메인 레포 `inara-k8s`의 Langfuse 차트)을 따라 작성.
3. **ArgoCD Application(dev) 작성** (`gitops/qdrant/application-dev.yaml`).

### 이번에 따른 회사 컨벤션 (참고 레포에서 확인)
- 공식 차트를 직접 안 짜고 **`dependencies:`로 가져오는 umbrella** 방식.
- dependency 차트는 **`charts/` 아래에 vendoring** (Git에 커밋 → ArgoCD가 인터넷 없이 읽음).
- stateful 서브차트는 **storageClass 미지정 → 클러스터 기본 `gp2`** (WaitForFirstConsumer라
  Pod 뜨는 AZ에 EBS 자동 생성).
- 배포는 **항상 ArgoCD가** 한다. 사람이 `helm install` 직접 안 함. (`helm lint/template`은
  배포 아닌 로컬 검증이므로 OK.)

---

## 5. 남은 일 (TODO) — ⚠️ 미완료

아래는 `helm` 바이너리 실행이 필요한데, 작업 당시 환경 제약으로 **실행하지 못해 보류**된
항목이다. 다음 작업자가 반드시 완료할 것:

### TODO-1. Qdrant 차트 버전 확정 (placeholder 교체)
현재 파일에 **placeholder 버전**이 박혀 있다 (`grep -rn "TODO" deploy/qdrant/`로 확인):
- `deploy/qdrant/Chart.yaml` → `dependencies[].version: "1.13.0"` (placeholder)
- `deploy/qdrant/values.yaml`, `values-dev.yaml` → `image.tag: "v1.13.0"` (placeholder)

확정 방법:
```bash
helm repo add qdrant https://qdrant.github.io/qdrant-helm
helm repo update qdrant
helm search repo qdrant/qdrant            # → Chart.yaml의 dependency version 확정
curl -s https://api.github.com/repos/qdrant/qdrant/releases/latest | grep tag_name  # → image.tag 확정
```
→ 얻은 실제 버전으로 위 placeholder들을 교체.

### TODO-2. values 키 대조
내가 쓴 키(`qdrant.service.ports.{http,grpc}`, `qdrant.apiKey`, `qdrant.persistence.size`)가
**공식 차트의 실제 키와 일치하는지 미검증**이다. 차트 버전에 따라 키 구조가 다를 수 있음:
```bash
helm show values qdrant/qdrant | grep -iE 'apiKey|service|persistence|image'
```
→ 불일치하면 `deploy/qdrant/values*.yaml`을 실제 키에 맞게 수정.

### TODO-3. dependency vendoring + 로컬 검증
```bash
cd deploy/qdrant
helm dependency update          # → charts/qdrant-*.tgz 생성 (또는 압축해제 디렉토리)
                                #    이걸 Git에 커밋해야 ArgoCD가 읽음 (회사 컨벤션)
helm lint . --values values.lint.yaml
helm template . --values values.yaml --values values-dev.yaml \
  | grep -A5 -E 'kind: StatefulSet|volumeClaimTemplates|kind: NetworkPolicy'
```
→ StatefulSet + gp2 PVC + NetworkPolicy가 제대로 렌더되는지 확인.

### TODO-4. ArgoCD Application placeholder 교체
`gitops/qdrant/application-dev.yaml`의 TODO 주석들:
- `repoURL` → 이 레포의 실제 Git URL
- `destination.name` → 실제 대상 클러스터 (dev)
- `project` → 실제 ArgoCD 프로젝트명
- `targetRevision` → 배포 브랜치

---

## 6. ⚠️ 알려진 정합성 부채 (주의)

`manifests/team-vault-gitops/` (processor/search-api 차트)는 **이전 세션 산출물로,
리뷰 전 옛 설계를 담고 있다.** 현재 확정 결정과 불일치하는 부분:
- README/values가 **CloudFront 업로드**를 전제 (→ 지금은 ALB → ingest-api 로 변경됨)
- **OpenSearch/AOSS** 참조 (`OPENSEARCH_ENDPOINT`, `aoss:APIAccessAll`) (→ 지금은 Qdrant)
- search-api가 **Argo Rollouts(blue/green)** (→ 리뷰에서 일반 Deployment로 강등 권고)

**다음에 이 차트들을 만질 때**: `docs/16` 결정에 맞춰 Qdrant/ALB-ingest/Deployment로
정합화해야 한다. 지금은 `deploy/qdrant/`(신규, 정합)와 `manifests/`(구버전)가 공존하는 상태.

---

## 7. 다음 작업자를 위한 빠른 시작

```bash
# 1. 전체 설계와 결정 파악
cat docs/16-design-review.md          # 결정 요약 (제일 먼저)
cat docs/01-architecture.md           # 전체 그림

# 2. 이번 작업물 확인
ls -R deploy/qdrant gitops/qdrant
grep -rn "TODO" deploy/qdrant gitops/qdrant   # 채워야 할 placeholder 목록

# 3. 위 5번 TODO-1~4 순서대로 완료
```

도구별 메모:
- **Codex CLI / Gemini CLI**: 이 `AGENTS.md`가 자동 로드된다. 위 TODO를 그대로 수행하면 됨.
- **사람**: `helm`만 있으면 5번 TODO를 직접 실행 가능. 배포는 ArgoCD로만.

---

## 8. 용어

- **vault**: 옵시디언 vault(마크다운 노트 폴더) 또는 team-vault(팀 지식 저장소). 암호화 키 vault 아님.
- **processor**: S3 raw/를 주기 스캔해 LLM 가공하는 CronJob.
- **search-api**: REST + MCP 검색 제공 상시 파드.
- **ingest-api**: 업로드 게이트웨이 파드 (PC 대신 IRSA로 S3 write). 초기엔 `aws s3 sync`로 대체 가능.
- **Qdrant**: 벡터 검색 DB (EKS 파드, gp2 PVC).
