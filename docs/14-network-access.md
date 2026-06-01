# 14. 네트워크 접근 정책

회사 IP 기반 게이트로 마찰을 0으로, 동시에 외부 노출을 차단하는 설계.

## 설계 원칙

> **"회사 IP에서 오는 트래픽 = 신뢰 게이트". 팀원 PC에 AWS 자격증명을 두지 않는다.**

- 팀원은 AWS 계정/키/SSO 셋업 없이 그냥 동작
- 사내 게이트웨이가 IP 검증 후 *자기 IAM Role*로 S3에 씀
- 외부 인터넷에서는 진입조차 불가능
- 정적 위키는 별도 경로(CloudFront + WAF)

## 트래픽 분리 — 각자의 게이트

```
[데이터 평면 = 대화 transcript 업로드]
   팀원 PC → 사내 ALB Internal → ingest-api Pod → S3 (IRSA)

[제어 평면 = AI 검색 / MCP]
   팀원 PC → 사내 ALB Internal → search-api Pod → Qdrant (클러스터 내부)

[열람 평면 = 정적 위키 (선택)]
   브라우저 → CloudFront + WAF (IP 제한) → S3 (OAC)
```

세 평면이 *독립된 게이트*. 한 곳이 뚫려도 나머지는 안전.

---

## 회사 IP 식별 — 단일 진실 공급원

```hcl
# infrastructure/network/corporate_ips.tf
locals {
  corporate_ips = {
    hq        = "203.0.113.42/32"      # 본사 NAT 외부 IP
    branch    = "198.51.100.10/32"     # 지사
    vpn_gw    = "10.0.0.0/8"           # 사내 VPN 게이트웨이 (사내 대역)
    tailscale = "100.64.0.0/10"        # (옵션) Tailscale 대역
  }

  all_corporate_cidrs = [for ip in local.corporate_ips : ip]
}
```

모든 정책이 이 한 곳 참조 → IP 변경 시 한 파일만 PR.

---

## 데이터 평면 — ingest 게이트웨이

### ALB Security Group

```hcl
resource "aws_security_group" "ingest_alb" {
  name   = "team-vault-ingest-alb"
  vpc_id = var.vpc_id

  ingress {
    description = "HTTPS from corporate networks only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.all_corporate_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
```

→ 회사 IP가 아니면 **TCP handshake 자체가 안 됨**. 게이트웨이 코드까지 오지 않음.

### Ingress (내부 ALB)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: team-vault-ingest
  annotations:
    alb.ingress.kubernetes.io/scheme: internal       # ★ 외부 IP 없음
    alb.ingress.kubernetes.io/security-groups: sg-xxxxx   # 위 SG
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:...
    alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS-1-2-2017-01
spec:
  ingressClassName: alb
  rules:
    - host: wiki.team.internal
      http:
        paths:
          - path: /ingest
            pathType: Prefix
            backend:
              service:
                name: ingest-api
                port:
                  number: 8080
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: search-api
                port:
                  number: 8080
          - path: /mcp
            pathType: Prefix
            backend:
              service:
                name: search-api
                port:
                  number: 8081
```

ALB `scheme: internal` = 외부 IP 없음. 인터넷에서는 보이지도 않음.

### DNS — Route53 Private Hosted Zone

```hcl
resource "aws_route53_zone" "internal" {
  name = "team.internal"

  vpc {
    vpc_id = var.vpc_id
  }
}

resource "aws_route53_record" "wiki" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = "wiki.team.internal"
  type    = "A"

  alias {
    name                   = aws_lb.ingest.dns_name
    zone_id                = aws_lb.ingest.zone_id
    evaluate_target_health = true
  }
}
```

사내 DNS 해석 = `wiki.team.internal` → ALB.
사외 DNS = NXDOMAIN (찾을 수도 없음).

### 사외에서 들어오는 경우 — VPN

재택근무자/외근:
```
팀원 PC (집)
   ↓ Tailscale 또는 OpenVPN 연결
   ↓ 출구 IP = 회사 VPN 게이트웨이 IP
   ↓ DNS query: wiki.team.internal → 사내 Route53
   ↓ 회사 IP로 ALB 도착
   ↓ 통과 ✓
```

VPN 통과만 하면 사무실 사용자랑 똑같은 경험. 별도 처리 0.

---

## 열람 평면 — 정적 위키 + CloudFront (선택 기능)

비개발자/매니저가 브라우저로 볼 수 있는 정적 위키. *MVP 이후 추가*.

### S3 → CloudFront → WAF

```hcl
# WAF Web ACL
resource "aws_wafv2_ip_set" "corporate" {
  name               = "team-vault-corporate"
  scope              = "CLOUDFRONT"   # ★ us-east-1
  ip_address_version = "IPV4"
  addresses          = local.all_corporate_cidrs
}

resource "aws_wafv2_web_acl" "wiki" {
  name  = "team-vault-wiki"
  scope = "CLOUDFRONT"

  default_action { block {} }

  rule {
    name     = "AllowCorporate"
    priority = 1
    action { allow {} }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.corporate.arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "AllowCorporate"
      sampled_requests_enabled   = true
    }
  }

  # AWS Managed: 공격 패턴
  rule {
    name     = "CommonAttackPatterns"
    priority = 2
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }
    visibility_config { ... }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "team-vault-wiki"
    sampled_requests_enabled   = true
  }
}
```

### CloudFront Distribution

```hcl
resource "aws_cloudfront_origin_access_control" "wiki" {
  name                              = "team-vault-wiki-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "wiki" {
  enabled         = true
  is_ipv6_enabled = true
  web_acl_id      = aws_wafv2_web_acl.wiki.arn

  aliases = ["wiki-static.team.com"]   # 비공개 도메인

  origin {
    domain_name              = aws_s3_bucket.team_vault.bucket_regional_domain_name
    origin_id                = "s3-team-vault"
    origin_access_control_id = aws_cloudfront_origin_access_control.wiki.id

    # exports/site/ 만 사용
    origin_path = "/exports/site"
  }

  default_cache_behavior {
    target_origin_id       = "s3-team-vault"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
  }

  viewer_certificate {
    acm_certificate_arn = var.us_east_1_cert_arn
    ssl_support_method  = "sni-only"
  }

  restrictions {
    geo_restriction {
      restriction_type = "whitelist"
      locations        = ["KR"]   # 보조 안전망
    }
  }
}
```

### S3 Bucket Policy (CloudFront만 허용)

```json
{
  "Sid": "AllowOAC",
  "Effect": "Allow",
  "Principal": { "Service": "cloudfront.amazonaws.com" },
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::team-vault/exports/site/*",
  "Condition": {
    "StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::ACCOUNT:distribution/DIST_ID"
    }
  }
}
```

`raw/`, `processed/`, `embeddings/`는 CloudFront로 *절대* 노출 안 함.

### 도메인 비공개 정책

- `wiki-static.team.com` — *짐작 어려운* 서브도메인 사용
- 사내 위키/슬랙 핀에만 공유
- Certificate Transparency 로그에 뜨므로 공개 도메인은 secrecy 한계 인정
- → **WAF IP 제한이 본질, 비공개 도메인은 보조**

---

## 제어 평면 — MCP / Search API

데이터 평면과 같은 ALB internal 공유.

- 외부 인터넷에서 접근 불가 (`scheme: internal`)
- 사내망/VPN 경유만
- 별도 인증(Cognito/토큰) 없음 — 접근 = 신뢰. 식별은 호스트네임 (`15-zero-touch-onboarding.md`)

```
mcp.team-vault.search(...) 호출
   ↓ HTTPS to wiki.team.internal/mcp
   ↓ ALB SG (회사 IP 확인)
   ↓ search-api Pod
```

ALB가 internal scheme이면 IP 제한과 함께 *2중 차단* (이미 인터넷에서 보이지 않음 + SG로 회사 IP만).

---

## 네트워크 다이어그램 (최종)

```
┌─ 인터넷 ────────────────────────────────────┐
│                                              │
│  외부 사용자 / 봇 ────❌──> 모든 엔드포인트   │
│                          (DNS도 없고, ALB도   │
│                           internal, WAF 차단) │
└──────────────────────────────────────────────┘

┌─ 회사 네트워크 (사무실 / VPN) ──────────────┐
│                                              │
│  팀원 PC                                      │
│    ├─ 데몬 ──HTTPS──> wiki.team.internal     │
│    │                  /ingest, /api, /mcp    │
│    │                  (ALB internal + SG)    │
│    │                                          │
│    └─ 브라우저 ─HTTPS─> wiki-static.team.com │
│                        (CloudFront + WAF)    │
└──────────┬───────────────────────────────────┘
           ↓
┌─ AWS VPC (private) ────────────────────────┐
│                                              │
│  ALB Internal                                │
│    ├─ /ingest → ingest-api Pod              │
│    ├─ /api    → search-api Pod              │
│    └─ /mcp    → search-api Pod              │
│                                              │
│  Pods → VPC Endpoints → S3 / SM / ECR       │
│  search-api ↔ Qdrant (클러스터 내부 서비스)  │
└──────────────────────────────────────────────┘

┌─ AWS Edge (CloudFront) ────────────────────┐
│  WAF (회사 IP set 화이트리스트)              │
│    ↓                                          │
│  OAC → S3 exports/site/ (only)              │
└──────────────────────────────────────────────┘
```

---

## 운영 — IP 변경 시

```
1. corporate_ips.tf 수정 → PR
2. 머지 → Terraform apply
3. ALB SG 즉시 반영 (수 초)
4. WAF IP set 즉시 반영 (수 분)
```

자동화: GitHub Actions로 머지 시 자동 apply.

### IP 변경 알림

PagerDuty/Slack에 변경 알림:
```
🌐 회사 IP 화이트리스트 업데이트
+ 추가: 198.51.100.20/32 (제2지사)
- 제거: 198.51.100.5/32 (이전 사무실)
영향: ingest ALB SG, WAF, S3 Bucket Policy
```

---

## 실패 시나리오 / 대비

| 시나리오 | 영향 | 대비 |
|---------|------|-----|
| 회사 IP 변경 (미공지) | 전 팀원 동기화 실패 | 데몬이 5xx/timeout 감지 → 슬랙 알림 |
| VPN 게이트웨이 다운 | 재택 인원 차단 | VPN 이중화 + 일시 IP allowlist |
| 회사 NAT 장애 | 전사 차단 | 백업 NAT IP도 사전 등록 |
| ALB 장애 | 게이트웨이 다운 | Multi-AZ + Health Check + Pod replica 2+ |
| WAF 룰 잘못 | 정상 사용자도 차단 | staging WAF로 사전 테스트 |
| 데몬이 인터넷 끊김 감지 못함 | WAL이 무한 누적 | 디스크 사용량 알림 |

---

## 비용

| 항목 | 월 비용 |
|------|--------|
| ALB (internal) | ~$25 |
| WAF Web ACL | $5 |
| WAF Rule (IP set) | $1 |
| WAF Managed Rule | $1 |
| WAF 요청 | 거의 0 |
| CloudFront (정적 위키) | 트래픽 적으면 무료 티어 |
| Route53 Hosted Zone | $0.5 |
| **합계** | **~$32 + 트래픽** |

---

## 보안 평가 (이 설계의 강도)

| 위협 | 방어 |
|------|-----|
| 외부 무작위 스캔 | DNS도 없고 ALB도 internal → 보이지 않음 ✓ |
| 외부 작정한 공격 | ALB SG가 TCP 레벨에서 차단 ✓ |
| 회사 Wi-Fi 침입자 | IP 통과 후엔 토큰/식별 필요 (15번 문서) |
| 퇴사자 외부에서 접근 | IP 차단 + SSO 차단 ✓ |
| 내부자 위변조 | 토큰 + 감사 로그 (15번 문서) |
| DDoS | ALB internal이라 대상 안 됨 ✓ |
| 도메인 leak | 비공개 도메인은 보조, WAF IP가 본질 ✓ |

---

## 한 줄 요약

**ALB `scheme: internal` + SG 회사 IP 화이트리스트 = 마찰 0 + 외부 노출 0.
사내 DNS만 해석. CloudFront는 정적 위키만 별도 경로로.
팀원 식별은 다음 문서(15-zero-touch-onboarding)에서.**
