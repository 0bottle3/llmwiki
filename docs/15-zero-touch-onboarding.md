# 15. Zero-Touch 온보딩 (호스트네임 기반 Ingest)

팀원이 AWS SSO/Access Key/토큰 발급 같은 셋업을 *전혀 하지 않고도* 동작하는 구조.
회사 IP에서 오는 트래픽은 게이트가 신뢰하고, 식별은 PC 호스트네임으로 한다.

## 설계 한 줄

> **팀원 PC는 AWS 자격증명을 가지지 않는다. 사내 ALB 뒤의 `ingest-api` Pod가
> *자기 IRSA Role*로 S3에 쓰며, 작성자는 요청 헤더의 호스트네임으로 식별한다.**

회사 IP 게이트(14번)와 짝을 이룬다.

## 신뢰 모델

```
[회사 IP에서 옴]  ──>  IP가 회사면 신뢰
                       호스트네임 → 사용자 매핑
                       Pod가 자기 권한으로 S3에 씀
```

- 신뢰 단위: **회사 네트워크 + 회사 지급 디바이스**
- 위장 위험: 회사 안에서 hostname을 바꿔 alice 행세 가능 (낮은 위협 모델 전제)
- 강화 옵션은 본 문서 후반 "강화 경로" 참고

## 전체 흐름

```
Claude/Codex/Kiro
    ↓ transcript JSONL (자동)
team-vault-syncd (팀원 PC)
    ↓ HTTPS POST + X-Vault-Hostname / X-Vault-OS-User
사내 ALB Internal (회사 IP SG 통과)
    ↓
ingest-api Pod (IRSA: sa-team-vault-for-aws)
    ├ 호스트네임 → 사용자 매핑 (ConfigMap)
    ├ unknown은 격리 prefix
    └ S3 PUT
S3: raw/conversations/{user}/{YYYY-MM}/{session_id}.jsonl
    ↓ CronJob (매시간 S3 raw/ 스캔, 변경분만)
기존 가공 파이프라인 (processor(CronJob) → search-api)
```

---

## ingest-api 워크로드 — Helm chart 구조

표준 GitOps Helm 패턴(Chart.yaml + values + templates)을 따른다.

### 디렉토리

```
manifests/
└── team-vault-gitops/
    ├── ingest-api/
    │   ├── Chart.yaml
    │   ├── values.yaml
    │   ├── values-dev.yaml
    │   ├── values-prod.yaml
    │   └── templates/
    │       ├── _helpers.tpl
    │       ├── serviceaccount.yaml
    │       ├── rollout.yaml          # Argo Rollouts (blue/green)
    │       ├── services.yaml         # active + preview
    │       ├── ingress.yaml          # ALB internal
    │       ├── externalsecret.yaml   # Secrets Manager (Anthropic/Voyage 키 등)
    │       ├── hostname-map-configmap.yaml
    │       ├── hpa.yaml
    │       └── analysis-template.yaml
    ├── processor/    # 가공 파이프라인 CronJob (3번 문서)
    ├── search-api/
    └── curator/
    # 참고: ingest-api(업로드 게이트웨이)는 유지, 실시간 가공용 ingestor는 제거(CronJob으로 대체)
```

### Chart.yaml

```yaml
apiVersion: v2
name: app
description: Team Vault — Ingest API
type: application
version: 0.1.0
appVersion: "0.1.0"
```

### values.yaml 핵심

```yaml
# ------------------------------------------
# ! GENERAL
# ------------------------------------------
app:
  name: &appName "team-vault"
  namespace: "team-vault"
  nameWithRole: &nameWithRole "team-vault-ingest"
nameOverride: *nameWithRole
fullnameOverride: *nameWithRole

image:
  repository: "ACCOUNT.dkr.ecr.ap-northeast-2.amazonaws.com/team-vault/ingest-api"
  pullPolicy: IfNotPresent
  tag: &version ""

commonLabels:
  sg: was-using-db
  team-vault/service: *appName
  team-vault/role: *nameWithRole
  tags.datadoghq.com/service: *nameWithRole

nodeSelector:
  Position: workload

serviceAccount:
  create: false
  name: "sa-team-vault-for-aws"   # IRSA 부착된 SA

podSecurityContext:
  runAsUser: 1001
  runAsGroup: 1001
  runAsNonRoot: true
  fsGroup: 1001
  seccompProfile: { type: RuntimeDefault }

securityContext:
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: { drop: [ALL] }

# ------------------------------------------
# ! EXPOSE
# ------------------------------------------
service:
  type: ClusterIP
  port: 80

container:
  containerPort: &containerPort 8080

livenessProbe:
  httpGet: { path: "/healthz", port: *containerPort }
  initialDelaySeconds: 20
  periodSeconds: 5
  timeoutSeconds: 2
  successThreshold: 1
  failureThreshold: 3

readinessProbe:
  httpGet: { path: &healthCheckPath "/readyz", port: *containerPort }
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 2
  successThreshold: 2
  failureThreshold: 3

# ------------------------------------------
# ! INGRESS (사내 ALB)
# ------------------------------------------
ingress:
  enabled: true
  className: "alb"
  annotations:
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS": 443}]'
    alb.ingress.kubernetes.io/scheme: internal
    alb.ingress.kubernetes.io/security-groups: SG-ELB-INTERNAL,SG-OPS
    alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-2021-06
    alb.ingress.kubernetes.io/ssl-redirect: "443"
    alb.ingress.kubernetes.io/target-group-attributes: deregistration_delay.timeout_seconds=10
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: *healthCheckPath
  hosts:
    - paths:
        - path: /ingest
          pathType: Prefix
          target: team-vault-ingest
        - path: /api
          pathType: Prefix
          target: team-vault-search
        - path: /mcp
          pathType: Prefix
          target: team-vault-search

# ------------------------------------------
# ! DEPLOYMENT (Argo Rollouts blue/green)
# ------------------------------------------
replicaCount: 2
terminationGracePeriodSeconds: &terminationGracePeriodSeconds 30

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 8
  targetCPUUtilizationPercentage: 70

rollout:
  revisionHistoryLimit: 3
  autoPromotionEnabled: true
  scaleDownDelaySeconds: *terminationGracePeriodSeconds
  maxUnavailable: 0

resources:
  limits:   { cpu: 1,    memory: 1Gi }
  requests: { cpu: 250m, memory: 512Mi }

affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            app.kubernetes.io/name: *nameWithRole
        topologyKey: kubernetes.io/hostname

# ------------------------------------------
# ! ENV / SECRETS
# ------------------------------------------
container:
  env:
    - name: AWS_REGION
      value: ap-northeast-2
    - name: S3_BUCKET
      value: team-vault
    - name: HOSTNAME_MAP_PATH
      value: /etc/team-vault/hostname-map.yaml
  volumeMounts:
    - name: hostname-map
      mountPath: /etc/team-vault
      readOnly: true

# Secrets Manager에서 주입 (External Secrets Operator)
envVariablesFromSecrets:
  - secretKey: ANTHROPIC_API_KEY
    remoteRef:
      key: team-vault/anthropic-api-key
      property: api_key
  - secretKey: VOYAGE_API_KEY
    remoteRef:
      key: team-vault/voyage-api-key
      property: api_key

volumes:
  - name: hostname-map
    configMap:
      name: team-vault-hostname-map

# ------------------------------------------
# ! HOSTNAME MAP
# ------------------------------------------
hostnameMap:
  # 호스트네임 -> 사용자명
  mappings:
    alice-mbp:           alice
    alice-mbp.local:     alice
    bob-mbp:             bob
    carol-laptop:        carol
  # OS 사용자명 폴백 (회사 표준이면 신뢰)
  trustOsUserFallback: true
  knownOsUsers: [alice, bob, carol]
  # 매핑/폴백 모두 실패 시
  unknownPrefix: "unknown"
```

### values-dev.yaml / values-prod.yaml

환경별 차이만 오버라이드:

```yaml
# values-prod.yaml
replicaCount: 3
autoscaling:
  minReplicas: 3
  maxReplicas: 12
image:
  tag: "v0.1.0"
ingress:
  annotations:
    alb.ingress.kubernetes.io/security-groups: SG-ELB-INTERNAL-PROD,SG-OPS
```

### templates/serviceaccount.yaml

```yaml
{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Values.serviceAccount.name }}
  namespace: {{ .Values.app.namespace }}
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT:role/{{ .Values.serviceAccount.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
{{- end }}
```

IRSA Role 권한 (Terraform):

```hcl
resource "aws_iam_role" "ingest_pod" {
  name = "sa-team-vault-for-aws"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust.json
}

resource "aws_iam_role_policy" "ingest_s3" {
  role = aws_iam_role.ingest_pod.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteConversations"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "arn:aws:s3:::team-vault/raw/conversations/*"
      },
      {
        Sid    = "ListBucket"
        Effect = "Allow"
        Action = "s3:ListBucket"
        Resource = "arn:aws:s3:::team-vault"
      }
    ]
  })
}
```

### templates/rollout.yaml (요지)

표준 Argo Rollouts blue/green:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Values.app.namespace }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  revisionHistoryLimit: {{ .Values.rollout.revisionHistoryLimit | default 3 }}
  selector:
    matchLabels:
      {{- include "app.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "app.selectorLabels" . | nindent 8 }}
        {{- include "app.commonLabels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "app.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds | default 30 }}
      containers:
        - name: {{ include "app.fullname" . }}
          securityContext:
            {{- toYaml .Values.securityContext | nindent 12 }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          {{- if hasKey .Values "envVariablesFromSecrets" }}
          envFrom:
            - secretRef:
                name: {{ include "app.fullname" . }}-secret
          {{- end }}
          {{- with .Values.container.env }}
          env:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          ports:
            - { name: http, containerPort: {{ .Values.container.containerPort }}, protocol: TCP }
          livenessProbe:
            {{- toYaml .Values.livenessProbe | nindent 12 }}
          readinessProbe:
            {{- toYaml .Values.readinessProbe | nindent 12 }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          volumeMounts:
            {{- toYaml .Values.container.volumeMounts | nindent 12 }}
      volumes:
        {{- toYaml .Values.volumes | nindent 8 }}
      nodeSelector:
        {{- toYaml .Values.nodeSelector | nindent 8 }}
      affinity:
        {{- toYaml .Values.affinity | nindent 8 }}
  strategy:
    blueGreen:
      activeService: {{ include "app.fullname" . }}
      previewService: {{ include "app.fullname" . }}-preview
      autoPromotionEnabled: {{ .Values.rollout.autoPromotionEnabled | default true }}
      maxUnavailable: {{ .Values.rollout.maxUnavailable | default 0 }}
      scaleDownDelaySeconds: {{ .Values.rollout.scaleDownDelaySeconds | default 30 }}
      postPromotionAnalysis:
        templates:
          - templateName: {{ include "app.fullname" . }}
```

### templates/services.yaml

active + preview 서비스 쌍:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "app.fullname" . }}
  namespace: {{ .Values.app.namespace }}
  labels: { {{- include "app.labels" . | nindent 4 }} }
  annotations:
    alb.ingress.kubernetes.io/healthcheck-path: {{ .Values.readinessProbe.httpGet.path }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - { port: {{ .Values.service.port }}, targetPort: {{ .Values.container.containerPort }}, protocol: TCP, name: http }
  selector:
    {{- include "app.selectorLabels" . | nindent 4 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "app.fullname" . }}-preview
  namespace: {{ .Values.app.namespace }}
  labels: { {{- include "app.labels" . | nindent 4 }} }
spec:
  type: {{ .Values.service.type }}
  ports:
    - { port: {{ .Values.service.port }}, targetPort: {{ .Values.container.containerPort }}, protocol: TCP, name: http }
  selector:
    {{- include "app.selectorLabels" . | nindent 4 }}
```

### templates/externalsecret.yaml

External Secrets Operator로 Secrets Manager → K8s Secret 자동 주입:

```yaml
{{- $prefix := include "app.fullname" . }}
{{- $labels := include "app.labels" . }}
{{- with .Values.envVariablesFromSecrets }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $prefix }}-external-secret
  labels: { {{- $labels | nindent 4 }} }
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: {{ $prefix }}-cluster-secret-store
    kind: ClusterSecretStore
  target:
    creationPolicy: Owner
    name: {{ $prefix }}-secret
  data:
    {{- toYaml . | nindent 4 }}
---
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: {{ $prefix }}-cluster-secret-store
  namespace: external-secrets
  labels: { {{- $labels | nindent 4 }} }
spec:
  retrySettings: { maxRetries: 5, retryInterval: "10s" }
  provider:
    aws:
      service: SecretsManager
      region: ap-northeast-2
      auth:
        jwt:
          serviceAccountRef:
            name: sa-external-secrets
            namespace: kube-system
{{- end }}
```

### templates/hostname-map-configmap.yaml

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: team-vault-hostname-map
  namespace: {{ .Values.app.namespace }}
  labels: { {{- include "app.labels" . | nindent 4 }} }
data:
  hostname-map.yaml: |
    mappings:
      {{- range $hostname, $user := .Values.hostnameMap.mappings }}
      {{ $hostname }}: {{ $user }}
      {{- end }}
    trustOsUserFallback: {{ .Values.hostnameMap.trustOsUserFallback }}
    knownOsUsers:
      {{- range .Values.hostnameMap.knownOsUsers }}
      - {{ . }}
      {{- end }}
    unknownPrefix: {{ .Values.hostnameMap.unknownPrefix | quote }}
```

ConfigMap 업데이트 시 Pod 재기동 없이 마운트된 파일이 자동 갱신됨 (`kubelet sync` 기준 ~1분).

---

## ingest-api 애플리케이션 로직

### 요청 스펙

```
POST /ingest
Host: wiki.team.internal
Content-Type: application/json
X-Vault-Hostname: alice-mbp
X-Vault-OS-User: alice
X-Vault-Client: claude-code
X-Vault-Session-Id: cc-20260529-abcd1234

{
  "messages": [
    {
      "msg_id": "msg-0042",
      "ts": "2026-05-29T22:30:00Z",
      "role": "user",
      "content_hash": "sha256:...",
      "content": "..."
    },
    ...
  ]
}
```

### Python 구현 (FastAPI)

```python
# app/main.py
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
import boto3
import yaml
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

S3 = boto3.client("s3")
BUCKET = os.environ["S3_BUCKET"]
HOSTNAME_MAP_PATH = Path(os.environ["HOSTNAME_MAP_PATH"])

app = FastAPI()


class Message(BaseModel):
    msg_id: str
    ts: str
    role: str
    content_hash: str
    content: str


class IngestRequest(BaseModel):
    messages: list[Message]


def load_hostname_map() -> dict:
    """ConfigMap 마운트 파일을 매 요청마다 읽음 (캐시 1분)."""
    # 실제로는 mtime 기반 캐시 + 1분 폴링
    return yaml.safe_load(HOSTNAME_MAP_PATH.read_text())


def resolve_user(hostname: str, os_user: str) -> tuple[str, bool]:
    """returns (user, is_known)."""
    hm = load_hostname_map()
    hostname = (hostname or "").lower().strip()
    os_user = (os_user or "").lower().strip()

    if hostname in hm["mappings"]:
        return hm["mappings"][hostname], True
    if hm.get("trustOsUserFallback") and os_user in hm.get("knownOsUsers", []):
        return os_user, True
    fallback = f"{hm['unknownPrefix']}-{hashlib.sha1(hostname.encode()).hexdigest()[:8]}"
    return fallback, False


\[MASKED_EMAIL]("/healthz")
def healthz():
    return {"ok": True}


\[MASKED_EMAIL]("/readyz")
def readyz():
    HOSTNAME_MAP_PATH.read_text()
    S3.head_bucket(Bucket=BUCKET)
    return {"ok": True}


\[MASKED_EMAIL]("/ingest")
async def ingest(
    body: IngestRequest,
    x_vault_hostname: str | None = Header(None),
    x_vault_os_user: str | None = Header(None),
    x_vault_client: str | None = Header(None),
    x_vault_session_id: str | None = Header(None),
):
    if not x_vault_hostname:
        raise HTTPException(400, "X-Vault-Hostname required")
    if not x_vault_session_id:
        raise HTTPException(400, "X-Vault-Session-Id required")

    user, is_known = resolve_user(x_vault_hostname, x_vault_os_user or "")
    client = (x_vault_client or "unknown").lower()
    session_id = x_vault_session_id

    # 멱등성: (user, client, session_id, msg_id)
    # 같은 session_id면 같은 객체에 append (newline-delimited JSON)
    today = date.today().strftime("%Y-%m")
    key = f"raw/conversations/{user}/{today}/{client}-{session_id}.jsonl"

    # 기존 객체 fetch → 이미 있는 msg_id는 skip → append → put
    existing = fetch_existing_lines(key)
    seen = {line["msg_id"] for line in existing}
    new_lines = [m.dict() for m in body.messages if m.msg_id not in seen]
    if not new_lines:
        return {"saved": 0, "user": user, "is_known": is_known}

    merged = existing + new_lines
    payload = "\n".join(json.dumps(line, ensure_ascii=False) for line in merged)

    S3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=payload.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={
            "ingested-from-hostname": x_vault_hostname,
            "os-user": x_vault_os_user or "",
            "client": client,
            "session-id": session_id,
            "is-known-user": str(is_known).lower(),
            "ingested-at": datetime.utcnow().isoformat() + "Z",
        },
    )

    return {"saved": len(new_lines), "user": user, "is_known": is_known}
```

**대용량 세션 처리**: 객체가 5MB 넘으면 day별 분할 또는 multipart. MVP는 그대로 PUT.

---

## team-vault-syncd 데몬 (팀원 PC)

### 설치 (1줄)

```bash
curl -sSL https://wiki.team.internal/install.sh | bash
```

### `install.sh` 내부

```bash
#!/bin/bash
set -euo pipefail

INSTALL_DIR="${HOME}/.team-vault"
BIN_DIR="/usr/local/bin"
PLIST="${HOME}/Library/LaunchAgents/com.team-vault.syncd.plist"

mkdir -p "$INSTALL_DIR"/{wal,state,backup,cache}

# 바이너리 다운로드
curl -sSL "https://wiki.team.internal/download/team-vault-syncd-$(uname -m)" \
  -o "$BIN_DIR/team-vault-syncd"
chmod +x "$BIN_DIR/team-vault-syncd"

# launchd plist
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.team-vault.syncd</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/team-vault-syncd</string>
    <string>--endpoint</string><string>https://wiki.team.internal/ingest</string>
    <string>--wal</string><string>${HOME}/.team-vault/wal</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>${HOME}/.team-vault/syncd.log</string>
  <key>StandardErrorPath</key><string>${HOME}/.team-vault/syncd.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "✓ team-vault-syncd installed and running"
echo "✓ Hostname: $(hostname)"
echo "  (관리자가 hostname-map.yaml에 추가하면 정상 분류됩니다)"
```

### 데몬 동작 (요지)

```python
# 의사 코드
WATCHED = [
    Path.home() / ".claude" / "projects",         # Claude Code
    Path.home() / ".codex" / "sessions",          # Codex
    *load_kiro_workspaces(),                       # Kiro
]

watcher = FSEvents(WATCHED)
watcher.on_modified = enqueue_offset_diff

while True:
    batch = drain_queue(max=100, timeout=2.0)
    if not batch:
        continue
    try:
        post_to_ingest(batch)
    except Exception:
        retry_with_backoff(batch)
```

**식별 헤더**는 매 요청에 자동:
```python
headers = {
    "X-Vault-Hostname": socket.gethostname(),
    "X-Vault-OS-User": getpass.getuser(),
    "X-Vault-Client": batch.client,
    "X-Vault-Session-Id": batch.session_id,
}
```

---

## hostname-map 운영

### 신규 호스트 감지 → 자동 PR

```
1. unknown 호스트로 첫 업로드 도착
   → ingest-api가 CloudWatch 메트릭 emit:
      team_vault_unknown_host_total{hostname="xyz", os_user="someone"}
2. curator CronJob이 일일 다이제스트에 포함
   → Slack: "🆕 새 호스트 감지: xyz (os_user: someone, 첫 업로드 22:30)"
3. 본인이 Slack에서 ack:
   /vault-claim xyz
4. Slackbot → GitHub Action → hostname-map PR
5. 코드 오너 머지 → ConfigMap 업데이트 → 1분 내 반영
6. 다음 업로드부터 정상 분류
```

### 매핑 변경 자동화

`values.yaml`의 `hostnameMap.mappings`만 PR로 추가하면 끝:

```yaml
hostnameMap:
  mappings:
    ...
    newperson-mbp: newperson    # ← PR 한 줄
```

ArgoCD가 sync → ConfigMap 업데이트 → mount 갱신.

---

## 보안 정책

### Bucket Policy (raw/conversations 한정)

```json
{
  "Sid": "AllowIngestPodOnly",
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::ACCOUNT:role/sa-team-vault-for-aws"
  },
  "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::team-vault",
    "arn:aws:s3:::team-vault/raw/conversations/*"
  ]
}
```

팀원 PC는 S3를 모름. *Pod만 씀.*

### 시크릿 마스킹

업로드 전 데몬 단에서 1차, ingest-api에서 2차, 가공 단계에서 3차.

```python
# 데몬과 ingest-api 양쪽에서 동일 regex — 접두사가 명확한 형식만 (오탐 방지, C2)
SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{32,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[bpas]-[0-9A-Za-z-]{10,}",
    r"ghp_[A-Za-z0-9]{36}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
# 광범위 base64 {40,} 패턴은 코드/해시 오탐이 심해 사용 금지 (07-§6 참고).
```

3중 안전망. **단, 마스킹은 데몬 WAL write *전*에 1차 적용** — 로컬 평문 JSONL에
시크릿이 남지 않도록 (M4). 검출 시 대화를 버리지 않고 마스킹 후 통과시킨다.

### 감사 로그

```python
log.info({
    "event": "ingest",
    "hostname": x_vault_hostname,
    "os_user": x_vault_os_user,
    "resolved_user": user,
    "is_known": is_known,
    "client": client,
    "session_id": session_id,
    "msg_count": len(new_lines),
    "source_ip": request.client.host,
})
```

CloudWatch Logs Insights로 *이상 패턴* 쿼리:
- 같은 호스트에서 5초에 1000 메시지 (자동화 의심)
- unknown 호스트가 10개 이상 (Wi-Fi 손님)
- 같은 hostname이 두 IP에서 동시 사용

---

## 강화 경로 (필요 시 단계적 적용)

### Level 1 — 현재 (zero-touch, 회사 IP만)
- 회사 IP SG + 호스트네임 + ConfigMap
- 마찰 0, 내부자 위장은 가능
- **권장 시작점**

### Level 2 — 머신 ID 추가
```python
headers["X-Vault-Machine-Id"] = stable_machine_id()  # IOPlatformUUID 등
```
호스트네임이 바뀌어도 추적 가능. 데몬만 업데이트.

### Level 3 — 디바이스 인증서 (MDM)
- MDM(Jamf/Kandji)이 모든 회사 맥북에 클라이언트 인증서 자동 설치
- 데몬이 mTLS로 ingest-api 호출
- ALB가 인증서 CN으로 사용자 식별
- 위장 불가

### Level 4 — Cognito + STS
- 1회 SSO 로그인 → keychain 토큰
- 매 요청에 Bearer 토큰
- 가장 강력, 마찰 약간

**언제 올라갈지**:
- unknown 호스트가 정기적으로 등장 → Level 2
- 영업비밀/개인정보 비중 ↑ → Level 3
- 외부 협력사 합류 → Level 4

---

## 운영 절차 (1줄 정리)

| 행위 | 누가 | 어떻게 |
|------|------|--------|
| 신규 합류 | 팀원 | `curl install.sh \| bash` |
| 호스트 매핑 | 관리자 | values.yaml PR 1줄 |
| 퇴사 | 관리자 | values.yaml에서 매핑 제거 + 노트북 회수 |
| 매핑 분쟁 | 팀원 | Slack `/vault-claim {hostname}` |
| 토큰 회수 | (해당 없음) | - |
| 비밀 회전 | 관리자 | Secrets Manager 갱신 → ExternalSecret 자동 |
| 정책 변경 | 관리자 | values.yaml + Argo Rollouts 자동 blue/green |

---

## 한 줄 요약

**팀원은 `curl install.sh | bash` 한 번만. 이후는 평소대로 LLM 사용.
사내 ALB 뒤 `ingest-api` Pod가 IRSA(`sa-team-vault-for-aws`)로 S3에 쓰고,
작성자는 호스트네임 → ConfigMap 매핑으로 식별. Argo Rollouts blue/green +
ExternalSecret + 사내 ALB internal로 운영.**
