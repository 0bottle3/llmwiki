# 12. Vault 시드 (Seeding) — 도메인 학습 / 코드 자료 / GitHub Org 일괄 수집

vault를 처음 구성할 때 **무엇을, 어디서, 어떻게 가져와서, 어떤 빈도로 갱신할지**.

## 원칙 — 무엇을 vault에 넣고 무엇을 빼는가

| 자료 | vault에 넣음? | 코드 인덱스? | 이유 |
|------|-------------|------------|------|
| 코드 (소스 파일) | ❌ | ✅ | 청크 깨지고 검색 품질 ↓. IDE/grep이 더 잘함 |
| README.md / docs/*.md | ✅ | ✅ | 시스템 설명의 핵심 |
| ADR / RFC / decisions | ✅ | - | "왜 이렇게 했는지" — 최고 가치 자산 |
| GitHub Wiki | ✅ | - | 이미 위키 포맷 |
| Issues / PR 본문 (선별) | ✅ | - | 장애·결정 컨텍스트 |
| 회의록 / 노션 / 컨플루언스 | ✅ | - | 운영 노하우 |
| 슬랙 thread (선별) | ✅ | - | 단, 자동 수집은 비추 (노이즈 ↑) |
| 외부 docs (Anthropic/AWS) | ⚠️ 선별 | - | 라이선스 확인, 핵심만 |

**핵심 구분**:
- **vault = "왜·어떻게"의 저장소** (의사결정, 컨텍스트, 노하우)
- **코드 인덱스 = "무엇"의 저장소** (구현, 함수, 시그니처)

두 시스템은 **분리**하고, search-api에서 *연동*만 시킴.

## 시드 카테고리

### S1. GitHub Org 자료 (대량, 자동)

도메인 학습의 메인 소스.

### S2. 노션 / 컨플루언스 / Google Docs

기존에 쌓인 문서들.

### S3. 슬랙 / 메신저

신중하게.

### S4. 팀원 개인 노트 (자발)

각자 본인 옵시디언에서.

### S5. 외부 표준 문서

라이선스/저작권 OK인 것만.

---

## S1. GitHub Org 일괄 다운로드

### 한 번에 받는 명령

```bash
gh repo list YOUR_ORG --limit 1000 --json nameWithOwner -q '.[].nameWithOwner' \
  | xargs -n1 -P8 -I{} gh repo clone {} ./repos/{}
```

- `gh repo list`: Org 내 레포 목록
- `--limit 1000`: 한도 (필요시 증가)
- `-P8`: xargs 병렬 8개
- 결과: `./repos/{org}/{repo}/` 구조

### 자주 쓰는 변형

| 목적 | 옵션 |
|------|------|
| Private 포함 | `--visibility all` |
| Archived 제외 | `--no-archived` |
| Fork 제외 | `--no-fork` (또는 jq로 필터) |
| 특정 언어 | `--json nameWithOwner,primaryLanguage` + jq select |
| 히스토리 제외 (가볍게) | `gh repo clone ... -- --depth=1` |
| 특정 토픽 | `--topic platform` |
| 정규식 매칭 | jq에서 `select(.name | test("^api-"))` |

### 인증

```bash
gh auth login          # 또는
gh auth status         # 확인
```

필요 스코프: `repo` (private), `read:org`, `read:wiki` (wiki 사용 시).

### 권장 전략: **vault용 추출 (전체 코드 다운로드 X)**

전체 코드는 무겁고 vault에 안 들어감. **docs만 추출**:

```bash
#!/bin/bash
# fetch-docs.sh
ORG="$1"
OUT="./vault-seed/$ORG"
mkdir -p "$OUT"

gh repo list "$ORG" --limit 1000 --no-archived \
  --json nameWithOwner,description,defaultBranchRef,primaryLanguage \
  > "$OUT/_inventory.json"

jq -r '.[].nameWithOwner' "$OUT/_inventory.json" | while read repo; do
  echo "=== $repo ==="
  name=$(basename "$repo")

  # shallow clone, sparse checkout으로 docs만
  git clone --depth=1 --filter=blob:none --sparse \
    "[MASKED_EMAIL]:$repo.git" "/tmp/repo-$$"

  pushd "/tmp/repo-$$"
  git sparse-checkout set \
    README.md README \
    docs/ doc/ \
    ADR/ adr/ \
    RFC/ rfc/ \
    CHANGELOG.md CHANGELOG \
    CONTRIBUTING.md ARCHITECTURE.md

  mkdir -p "$OUT/$name"
  rsync -a --include='*.md' --include='*/' --exclude='*' \
    ./ "$OUT/$name/" || true
  popd
  rm -rf "/tmp/repo-$$"
done
```

이게 vault에 들어갈 *순수 마크다운*만 추출합니다.

### GitHub Wiki도 함께

```bash
# 위키는 별도 .wiki.git 레포
git clone --depth=1 "[MASKED_EMAIL]:$ORG/$REPO.wiki.git" "$OUT/$name/.wiki"
```

### Issues / PR (선별 수집)

전부 수집하면 노이즈. **라벨/마일스톤 기준 필터**:

```bash
gh issue list -R "$ORG/$REPO" --label "incident,postmortem" --state all \
  --json number,title,body,createdAt,closedAt \
  --limit 1000 > "$OUT/$name/issues-incidents.json"

gh pr list -R "$ORG/$REPO" --label "adr,decision" --state merged \
  --json number,title,body,mergedAt \
  --limit 1000 > "$OUT/$name/prs-decisions.json"
```

JSON → 마크다운 변환은 별도 변환기 (`jsonl → md` 작은 스크립트).

### S3 업로드 형식

```
s3://team-vault/raw/_seed/
├── github/
│   └── YOUR_ORG/
│       ├── _inventory.json
│       ├── service-a/
│       │   ├── README.md
│       │   ├── docs/architecture.md
│       │   ├── ADR/0001-use-dynamodb.md
│       │   ├── .wiki/Home.md
│       │   ├── issues-incidents.json   → 변환 후 .md
│       │   └── prs-decisions.json      → 변환 후 .md
│       └── service-b/
│           └── ...
```

**중요**: 시드는 `raw/_seed/` prefix로 분리. 팀원 개인 메모(`raw/{member}/`)랑 섞이지 않게.

### 프론트매터 자동 부착

추출 직후 모든 파일에 프론트매터 추가:

```yaml
---
source: github
org: YOUR_ORG
repo: service-a
path: ADR/0001-use-dynamodb.md
default_branch: main
fetched_at: 2026-05-29
type: decision           # 경로 패턴으로 추정 (ADR/ → decision, docs/ → knowledge)
sensitivity: internal    # private repo → internal, public → public
---
```

`type` 자동 추정 규칙:
| 경로 패턴 | type |
|----------|------|
| `README.md` | knowledge |
| `docs/`, `doc/` | knowledge |
| `ADR/`, `adr/`, `decisions/` | decision |
| `RFC/`, `rfc/`, `proposals/` | decision |
| `CHANGELOG*` | history |
| `incident*`, `postmortem*` | incident |
| `runbook*`, `playbook*` | runbook |

---

## S2. 노션 / 컨플루언스 / Google Docs

### 노션

- **공식 API**로 페이지 export → 마크다운
- `notion-py` 또는 `notion-exporter` 사용
- 권장 흐름:
  ```
  Notion workspace → 일괄 export (zip) → 압축 해제 → S3 raw/_seed/notion/
  ```
- 자동화: 주 1회 또는 월 1회 cron

```bash
# 예시 (notion-exporter)
notion-exporter \
  --token "$NOTION_TOKEN" \
  --workspace "$WS_ID" \
  --format markdown \
  --output ./vault-seed/notion/
aws s3 sync ./vault-seed/notion/ s3://team-vault/raw/_seed/notion/
```

### 컨플루언스

- REST API로 space별 export
- 마크다운 변환기 (`atlassian-python-api` + `markdownify`)
- 대용량이면 부분 sync

### Google Docs

- Drive API로 폴더 단위 export (`text/markdown` 또는 `text/html`)
- 첨부 이미지는 S3 별도 prefix

---

## S3. 슬랙 / 메신저

**경고**: 슬랙을 통째로 vault에 넣으면 **저품질 노이즈**가 됩니다.

권장 패턴:

### 자동 수집 X, 명시적 등록만
- 특정 채널의 *pinned message*만 export → 가치 검증된 정보
- 또는 `#engineering-decisions` 같은 *결정 채널*만 export
- 일반 채팅 채널은 제외

### 슬랙 → vault 워크플로우 (반자동)
1. 채널/스레드에 `:vault:` 이모지 반응 추가
2. 슬랙봇이 해당 스레드 export → S3 raw/_seed/slack/
3. 사람이 작성자 + 컨텍스트 보강

---

## S4. 팀원 개인 노트 (자발적)

각자가 본인 옵시디언 vault에서 *공유 의도 있는 것만* 선택:

- 본인 vault에 `team-share/` 폴더 운영
- 그 폴더만 S3 raw/{member}/ 로 sync
- 또는 frontmatter에 `share: team` 표시된 파일만

자세한 건 `06-s3-structure.md` 참고.

---

## S5. 외부 표준 문서

라이선스 OK인 것:
- AWS docs (인덱스만, 본문은 fetch on-demand 권장)
- Anthropic / OpenAI docs (마찬가지)
- 사내 표준 외부 라이브러리 docs

**저작권/라이선스 체크 필수**. 모든 외부 자료는 frontmatter에 출처/라이선스 명시:

```yaml
---
source: external
origin_url: https://docs.aws.amazon.com/...
license: AWS Documentation (참조 권한만)
fetched_at: 2026-05-29
---
```

---

## 코드 인덱스 (vault와 분리)

코드 자체는 vault에 안 들어가지만 **코드 검색은 필요**. 두 옵션:

### 옵션 A: 별도 코드 검색 시스템

- **Sourcegraph** (관리형 또는 self-hosted)
- **GitHub Code Search** (Org 단위로 충분)
- **OpenGrok** (오픈소스)
- **각자 IDE의 인덱스** (가장 단순, 분산)

### 옵션 B: vault Pod에 코드 인덱스 추가 (Phase 2)

- 별도 OpenSearch 인덱스 `code`
- 별도 임베딩 (Voyage code 모델 또는 일반 모델)
- 청크 단위: 함수/심볼 기준
- search-api에 `search_code` 도구 추가

### 권장: **Phase 1은 옵션 A**

이미 GitHub Code Search나 사내 IDE 인덱스로 충분. vault는 *왜·어떻게*에 집중.

`search-api`에서는 결과에 *관련 코드 검색 링크* 만 첨부:
```
검색 결과:
1. [ADR-0001 DynamoDB 선택] (vault)
   → 관련 코드: https://github.com/search?q=org%3AYOUR_ORG+DynamoDB
```

---

## 시드 파이프라인 — Pod에서 처리

기존 가공 파이프라인이 시드 데이터도 자동 처리:

```
S3 raw/_seed/* PutObject
       ↓ (기존 S3 Event)
SQS:s3-events
       ↓
ingestor → SQS:work-queue
       ↓
processor (기존 가공)
       ↓
OpenSearch + processed/
```

차이점:
- `raw/_seed/` prefix는 **owner = "_seed"** 로 표시
- 검색 결과에 "📚 시드 자료" 배지
- 갱신 시 doc_id 동일 → 재가공만

---

## 갱신 빈도

| 자료 | 빈도 | 방식 |
|------|------|------|
| GitHub docs/README | 매일 | CronJob (delta sync) |
| GitHub ADR/RFC | 즉시 | GitHub Action → S3 push on merge |
| GitHub Wiki | 매주 | CronJob |
| Issues/PR | 매일 | 새로 closed/merged된 것만 |
| 노션 | 매주 | export + sync |
| 컨플루언스 | 매주 | API delta |
| 외부 문서 | 분기 | 수동 트리거 |
| 팀원 개인 노트 | 실시간 | remotely-save / git plugin |

### CronJob (vault-seed-syncer)

EKS에 추가 워크로드:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: vault-seed-syncer
spec:
  schedule: "0 18 * * *"  # UTC 18:00 = KST 03:00
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: syncer
              image: <ecr>/team-vault/seed-syncer:latest
              env:
                - { name: GITHUB_ORG,   value: "YOUR_ORG" }
                - { name: NOTION_WS_ID, value: "..." }
              volumeMounts:
                - { name: workdir, mountPath: /work }
          restartPolicy: OnFailure
```

내부 로직:
1. GitHub Org 변경된 레포 식별 (since=어제)
2. 변경된 docs/ADR/Wiki만 추출
3. S3에 PUT → 기존 파이프라인이 자동 가공

### GitHub Action (실시간 ADR)

```yaml
# .github/workflows/sync-to-vault.yml
name: Sync ADR to Team Vault
on:
  push:
    branches: [main]
    paths: ['ADR/**', 'docs/**', 'README.md']

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 1 }
      - name: Upload to S3
        run: |
          aws s3 sync ADR/  s3://team-vault/raw/_seed/github/${{ github.repository }}/ADR/  --delete
          aws s3 sync docs/ s3://team-vault/raw/_seed/github/${{ github.repository }}/docs/ --delete
          aws s3 cp README.md s3://team-vault/raw/_seed/github/${{ github.repository }}/README.md
        env:
          AWS_REGION: ap-northeast-2
        # IAM: OIDC for GitHub Actions (no static keys)
```

OIDC 신뢰 관계로 키 없이 동작.

---

## 시드 품질 관리

### 중복 처리
- 같은 README가 여러 레포에 fork되어 있으면? → 임베딩 유사도 기반 dedup
- 시드 자료끼리 중복 → 가장 최신/공식 레포 우선

### 시드 vs 사람 작성 가중치

검색 결과에서 사람이 직접 쓴 노트가 시드보다 *높은 가중치*:

```python
def score(doc):
    base = vector_score * 0.7 + bm25_score * 0.3
    if doc.owner != "_seed":
        base *= 1.2   # 사람 작성 보너스
    if doc.quality > 0.8:
        base *= 1.1
    return base
```

이유: 시드는 *일반론*, 사람 작성은 *팀 특화 노하우*.

### 시드 무한 증식 방지

- 시드 자료에 자동 expiration tag (1년)
- 1년 동안 한 번도 검색되지 않은 시드 → 자동 archive
- 갱신된 원본만 다시 색인

---

## 초기 시드 체크리스트 (Week 2 추가)

`09-roadmap.md`의 Week 2 막바지에 다음 추가:

- [ ] GitHub Org 모든 레포의 README + docs/ + ADR/ 추출
- [ ] 노션 핵심 페이지 export
- [ ] 옵션: 컨플루언스 space 1개 시범 sync
- [ ] 시드 자료 vault 색인 확인 (검색 결과에 등장하는지)
- [ ] 시드 vs 사람 작성 가중치 튜닝

---

## 외부 컨텍스트 추가 (학습 자료)

도메인 학습용 외부 자료 — *수동*으로 큐레이션 권장:

- 회사 도메인 (예: 결제, 음식배달, 게임) 관련 *공식 베스트 프랙티스*
- 사용 중인 핵심 기술 docs (AWS, Anthropic 등 — fetch on-demand가 안전)
- 사내 신입 교육 자료

이건 자동 수집보다 *팀 리드가 직접 큐레이션*해서 `raw/_seed/curated/`에 PR로 추가하는 게 품질 ↑.

---

## 한 줄 요약

**GitHub Org은 `gh repo list ... | xargs gh repo clone`으로 일괄 받고,
거기서 docs/ADR/README/Wiki만 추출해서 `raw/_seed/`에 올림.
코드 자체는 vault에 안 넣고, 검색 시 코드 인덱스 링크만 제공.
시드 갱신은 CronJob(매일/매주) + GitHub Action(실시간 ADR).**
