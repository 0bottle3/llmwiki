# 06. S3 구조 및 라이프사이클

원본/가공본/임베딩/백업을 어떻게 배치하고 어떻게 관리할지.

## 버킷 구조

```
s3://team-vault/
├── raw/                              # 원본 마크다운
│   └── {team_member}/
│       └── {YYYY-MM}/
│           ├── 2026-05-29-결제장애.md
│           └── attachments/
│               └── trace.png
│
├── processed/                        # 가공 결과 (JSON)
│   └── {doc_id}.json
│
├── embeddings/                       # 임베딩 (parquet, 백업/재인덱싱용)
│   └── {YYYY-MM-DD}.parquet
│
├── proposals/                        # 큐레이터 제안
│   ├── duplicates/
│   │   └── {YYYY-MM-DD}/
│   │       └── {proposal_id}.json
│   └── merges/
│       └── ...
│
├── snapshots/                        # 일일 인덱스 백업
│   └── {YYYY-MM-DD}/
│       └── vault.snapshot
│
├── exports/                          # (선택) 정적 HTML
│   └── site/
│
└── system/                           # 시스템 메타
    ├── glossary.md
    ├── owners.json
    └── schema/
        └── v1.json
```

### 단일 버킷 vs 복수 버킷

**단일 버킷 (team-vault) + prefix 분리**를 권장.
- IAM 정책을 prefix 기준으로 쪼개기 충분
- 버킷 한도(계정당 100개) 보존
- 로그/메트릭 통합

예외: `snapshots/`만 따로 빼고 싶으면 `team-vault-backups` 별도 운영도 OK.

## 명명 규칙

### raw 파일명

```
raw/{member}/{YYYY-MM}/{YYYY-MM-DD}-{slug}.md
```

- `member`: Cognito sub 또는 이메일 prefix (`alice`, `bob`)
- `YYYY-MM`: 작성월(폴더), 검색/정렬 편의
- `YYYY-MM-DD-{slug}`: 사람이 보기 좋게

예: `raw/alice/2026-05/2026-05-29-결제-timeout-runbook.md`

### doc_id

```python
doc_id = sha256(s3_key.encode()).hexdigest()[:16]
```

- S3 key 변경 시 doc_id도 바뀜 → 의도된 동작 (이전 버전은 별도)
- 16자 충분 (충돌 확률 무시 가능)

## 객체 메타데이터 (S3 Metadata)

각 raw 객체에 다음 메타 부착 (업로드 측에서):

```
x-amz-meta-author: alice
x-amz-meta-source: obsidian-remotely-save
x-amz-meta-vault-version: 1.0
```

가공본은 시스템이 부착:

```
x-amz-meta-doc-id: {doc_id}
x-amz-meta-source-etag: {raw_etag}
x-amz-meta-model: claude-haiku-4-5-20251001
x-amz-meta-schema-version: 1
```

## 버저닝

```
S3 Versioning: ENABLED on team-vault
```

- raw 영구 보존
- processed는 ETag로 idempotency 보장 (raw etag 동일하면 재가공 skip)
- 실수 삭제 복구 가능

## 라이프사이클 정책

```yaml
Rules:
  - Id: raw-to-glacier
    Filter: { Prefix: raw/ }
    Transitions:
      - Days: 90
        StorageClass: GLACIER_IR    # 즉시 검색 가능한 빙하
    NoncurrentVersionTransitions:
      - NoncurrentDays: 30
        StorageClass: GLACIER

  - Id: processed-current
    Filter: { Prefix: processed/ }
    # 가공본은 영구 Standard (검색 인덱싱에 자주 읽힘)

  - Id: embeddings-rotation
    Filter: { Prefix: embeddings/ }
    Expiration: { Days: 60 }   # 최신 1~2개만 유지, 나머지는 재생성 가능
    NoncurrentVersionExpiration:
      NoncurrentDays: 7

  - Id: proposals-cleanup
    Filter: { Prefix: proposals/ }
    Expiration: { Days: 30 }

  - Id: snapshots-retention
    Filter: { Prefix: snapshots/ }
    Transitions:
      - Days: 30
        StorageClass: GLACIER_IR
    Expiration: { Days: 365 }
```

## 이벤트 알림

```yaml
NotificationConfiguration:
  QueueConfigurations:
    - Id: raw-changes-to-sqs
      QueueArn: arn:aws:sqs:ap-northeast-2:...:s3-events
      Events:
        - s3:ObjectCreated:*
        - s3:ObjectRemoved:*
      Filter:
        Key:
          FilterRules:
            - Name: prefix
              Value: raw/
            - Name: suffix
              Value: .md
```

이미지/첨부는 알림 제외 (suffix 필터).

## 암호화

```
ServerSideEncryptionConfiguration:
  Rules:
    - ApplyServerSideEncryptionByDefault:
        SSEAlgorithm: aws:kms
        KMSMasterKeyID: alias/team-vault
      BucketKeyEnabled: true
```

- 자체 KMS 키 사용 (감사 가능, 권한 분리)
- BucketKey로 비용 절감

## 액세스 정책

### Bucket Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyPublicAccess",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::team-vault",
        "arn:aws:s3:::team-vault/*"
      ],
      "Condition": {
        "Bool": { "aws:SecureTransport": "false" }
      }
    },
    {
      "Sid": "VPCEndpointOnly",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::team-vault/*"],
      "Condition": {
        "StringNotEquals": {
          "aws:SourceVpce": "vpce-xxxxx"
        },
        "ArnNotLike": {
          "aws:PrincipalArn": [
            "arn:aws:iam::*:role/team-vault-*"
          ]
        }
      }
    }
  ]
}
```

- HTTPS 강제
- VPC Endpoint 경유 또는 명시된 IAM Role만 허용

### Public Access Block

```
BlockPublicAcls: true
IgnorePublicAcls: true
BlockPublicPolicy: true
RestrictPublicBuckets: true
```

당연히 전부 ON.

## IAM Role 별 prefix 권한

| Role | raw/ | processed/ | embeddings/ | snapshots/ | proposals/ |
|------|------|-----------|-------------|-----------|-----------|
| ingestor | Head, Get | - | - | - | - |
| processor | Get | Put, Get | Put | - | Put |
| search-api | - | Get | - | - | Get |
| curator | Get | Put, Get | Get | Put | Put, Get |
| sync-uploader | Put, Delete (자기 member prefix만) | - | - | - | - |

`sync-uploader`는 팀원 PC 또는 GitHub Actions가 사용.

## 업로드 경로

### 경로 A: Obsidian → remotely-save plugin

```
팀원 PC: Obsidian
    ↓ S3 자격증명 (Cognito Identity Pool로 STS 발급)
    ↓ 자기 prefix만 쓰기 가능 (Condition으로 제한)
s3://team-vault/raw/{member}/...
```

권한 예시:
```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
  "Resource": ["arn:aws:s3:::team-vault/raw/${cognito-identity.amazonaws.com:sub}/*"]
}
```

### 경로 B: Git → CodeBuild → S3

```
팀원: git push to obsidian-vault repo
    ↓
GitHub Action / CodeBuild
    ↓ aws s3 sync
s3://team-vault/raw/{member or 'team'}/
```

PR 리뷰 가능 + 버전 이력 완벽. 다만 latency 있음.

**권장**: 두 경로 모두 지원. 개인 메모는 remotely-save, 공식 문서는 Git PR.

## Athena 분석 (옵션)

```sql
CREATE EXTERNAL TABLE vault_processed (
  doc_id string,
  frontmatter struct<...>,
  source struct<...>,
  processed_at string
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://team-vault/processed/';
```

월간 리포트, 토픽 분포, 작성자별 통계 등 ad-hoc 분석 가능.

## 백업 / DR

| 데이터 | 1차 보호 | 2차 보호 |
|--------|---------|---------|
| raw/ | Versioning + Glacier | (옵션) S3 CRR to 다른 리전 |
| processed/ | Versioning | raw에서 재생성 가능 |
| embeddings/ | parquet 스냅샷 | raw에서 재생성 가능 |
| snapshots/ | Glacier | 1년 보존 |

**핵심**: raw만 안전하면 나머지는 전부 재생성 가능. raw가 master.

## 비용 가드

- S3 Inventory 매일 → 비정상 증가 알림
- CloudWatch metric `BucketSizeBytes` 임계치 (예: 100GB 초과 시 경고)
- 이상 객체(>50MB) 업로드 시 SNS 알림
