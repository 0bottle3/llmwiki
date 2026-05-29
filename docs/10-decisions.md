# 10. 결정 필요한 사항

시작 전에 정해야 하는 것들. 각 항목에 권장안 표시.

## D1. 벡터 DB

**옵션**
- A. OpenSearch Serverless (관리형, $170/월)
- B. Qdrant on EKS (자체 호스팅, $0 추가)
- C. Pinecone / 외부 SaaS (락인, 데이터 외부)

**권장**: **B (MVP)** → 안정화 후 **A로 이관 검토**

**고려 요인**
- 운영 인력 여유 있나? → 있으면 B
- 가용성 99.9% 필요? → A
- 미래에 권한 격리(팀별 인덱스) 계획? → A가 더 깔끔

**결정**: ☐ A  ☐ B  ☐ C

---

## D2. 임베딩 모델

**옵션**
- A. Voyage-3 (1024차원, $0.06/1M)
- B. Voyage-3-large (1024, $0.18/1M, 품질 ↑)
- C. Cohere embed-v3 (1024, 다국어 강함)
- D. OpenAI text-embedding-3-large (3072, 저장비용 ↑)

**권장**: **A**

**고려 요인**
- 한국어 비중? → Voyage 또는 Cohere
- 문서 양 폭증 가능성? → 차원 낮은 게 유리 (저장/RAM)
- Anthropic 생태계 통일 선호? → Voyage (공식 파트너)

**결정**: ☐ A  ☐ B  ☐ C  ☐ D

---

## D3. 인증 방식

**옵션**
- A. Cognito + ALB 통합 (정석)
- B. 정적 토큰 + Secrets Manager (MVP)
- C. mTLS + 사내 PKI
- D. 사내 OIDC IdP (예: Okta, Google Workspace)

**권장**: **B (MVP) → D (운영)**

**고려 요인**
- 이미 SSO 있나? → D
- 1주 안에 시작? → B
- 외부 사용자 가능성? → A

**결정**: ☐ A  ☐ B  ☐ C  ☐ D

---

## D4. Vault 동기화 방식

**옵션**
- A. Obsidian remotely-save plugin → S3 직접 sync
- B. Obsidian Git plugin → GitHub → CodeBuild → S3
- C. 두 가지 병행 (개인 메모는 A, 공식 문서는 B)

**권장**: **C**

**고려 요인**
- PR 리뷰 워크플로우 원함? → B (또는 C)
- 즉시 반영 중요? → A (또는 C)
- 비개발자 팀원 많음? → A (또는 C)

**결정**: ☐ A  ☐ B  ☐ C

---

## D5. 가공 LLM 라인업

**옵션**
- A. Haiku 4.5 only (저비용)
- B. Haiku 가공 + Sonnet 4.6 검증 (균형)
- C. Sonnet 가공 + Opus 큐레이션 (고품질)

**권장**: **B**

**고려 요인**
- 문서 품질 중요도? 운영 매뉴얼은 정확해야 → B 이상
- 비용 한도? → A 가능
- LLM 무제한이면 → 품질 우선, B 또는 C

**결정**: ☐ A  ☐ B  ☐ C

---

## D6. 가공 실시간성

**옵션**
- A. Batch API 전용 (24h SLA, 50% 할인)
- B. Realtime API 전용 (수 초, 정가)
- C. 하이브리드 (긴급 태그면 realtime, 평소 batch)

**권장**: **A (MVP) → C (운영)**

**고려 요인**
- "방금 올린 노트를 바로 검색"이 자주 필요한가? → C
- 비용 민감? → A
- 운영 단순화? → A

**결정**: ☐ A  ☐ B  ☐ C

---

## D7. 큐레이션 다이제스트 채널

**옵션**
- A. Slack incoming webhook
- B. 이메일 (SES)
- C. 옵시디언 vault 자체에 매일 노트 생성 (메타!)
- D. 안 함 (Phase 2)

**권장**: **A + C (병행)**

**고려 요인**
- 팀 의사소통 도구? → 그쪽
- vault 사용 빈도 높임 효과 원함? → C

**결정**: ☐ A  ☐ B  ☐ C  ☐ D

---

## D8. 권한 모델

**옵션**
- A. 전원 read-all (Phase 1, 가장 단순)
- B. 팀별 인덱스 분리 + RBAC
- C. 문서별 sensitivity tag + 필터

**권장**: **A (Phase 1) → C (Phase 2)**

**고려 요인**
- 민감 문서 비중? → 높으면 C 우선
- 팀 크기? → 5명 이하면 A 충분

**결정**: ☐ A  ☐ B  ☐ C

---

## D9. 도메인 / 네트워크

**옵션**
- A. `wiki.team.internal` (사내 DNS, Tailscale/VPN 필수)
- B. `wiki.team.com` (퍼블릭 + Cognito 인증)
- C. Cloudflare Tunnel (외부망에서 접근, ZeroTrust 게이트)

**권장**: **A**

**고려 요인**
- 원격 근무 비중? → C도 좋음
- 보안 정책상 외부 노출 금지? → A

**결정**: ☐ A  ☐ B  ☐ C

---

## D10. IaC 도구

**옵션**
- A. Terraform
- B. AWS CDK (Python/TypeScript)
- C. Pulumi
- D. 기존 회사 표준 따라감

**권장**: **D (회사 표준 우선)**

**결정**: ☐ A  ☐ B  ☐ C  ☐ D

---

## D11. 컨테이너 베이스 이미지

**옵션**
- A. `python:3.12-slim`
- B. `gcr.io/distroless/python3`
- C. 사내 hardened 베이스
- D. Alpine

**권장**: **B (보안) 또는 C (회사 표준)**

**결정**: ☐ A  ☐ B  ☐ C  ☐ D

---

## D12. 모니터링 스택

**옵션**
- A. CloudWatch 전용
- B. CloudWatch + Grafana (Managed)
- C. 사내 Prometheus / Datadog

**권장**: **C (회사 표준)** → 없으면 **B**

**결정**: ☐ A  ☐ B  ☐ C

---

## 결정 시트 (작성용)

| 항목 | 선택 | 결정자 | 결정일 | 비고 |
|------|------|--------|--------|------|
| D1. 벡터 DB | | | | |
| D2. 임베딩 모델 | | | | |
| D3. 인증 방식 | | | | |
| D4. Vault 동기화 | | | | |
| D5. 가공 LLM | | | | |
| D6. 가공 실시간성 | | | | |
| D7. 다이제스트 채널 | | | | |
| D8. 권한 모델 | | | | |
| D9. 도메인 / 네트워크 | | | | |
| D10. IaC 도구 | | | | |
| D11. 베이스 이미지 | | | | |
| D12. 모니터링 | | | | |

## 결정 후 액션

각 결정이 영향 주는 문서:

| 결정 | 영향 문서 |
|------|----------|
| D1 | 02, 03, 08 |
| D2 | 02, 04, 08 |
| D3 | 03, 05, 07 |
| D4 | 06 |
| D5, D6 | 04, 08 |
| D7 | 03 (curator) |
| D8 | 07 |
| D9 | 03, 05, 07 |
| D10, D11, D12 | manifests/, code-skeleton/ |

결정 후 위 문서들을 가서 *선택지 → 확정안*으로 정리해야 함.

## 다음 단계

1. 위 12개 항목 결정 → `결정 시트` 채우기
2. 영향 문서 업데이트 (옵션 → 확정)
3. `09-roadmap.md`의 Week 1 시작
4. `manifests/` 와 `code-skeleton/` 작성
