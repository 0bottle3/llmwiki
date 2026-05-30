# 11. 데이터 내구성 (Durability) & 클라이언트별 캡처 전략

"창 강제 종료 / 슬립 / 크래시 / 배터리 방전" 같은 비정상 종료에도
**대화가 vault에 누락 없이 저장되도록** 보장하는 설계.

대상 클라이언트: **Claude (Code/Desktop) · Codex · Kiro**.

## 핵심 원칙

> **"종료 시점에 저장"은 깨진다. "쓰는 즉시 영속화" 한다.**

종료 hook을 신뢰하지 않는다. 매 메시지가 끝날 때마다 로컬 디스크에 즉시 기록하고,
백그라운드 데몬이 그것을 비동기로 S3에 동기화한다.

---

## 위협 모델 — 어디서 손실되는가

| 시나리오 | 손실 가능성 (방어 전) | 방어 후 목표 |
|---------|-------------------|------------|
| Cmd+Q / 창 강제 종료 | 진행 중 세션 전체 | 0 메시지 |
| Cmd+Opt+Esc 강제 종료 | 진행 중 세션 전체 | 0 메시지 |
| 맥북 슬립 → 재개 안 됨 | 진행 중 세션 | 마지막 메시지 |
| 배터리 완전 방전 | 메모리 전체 | 마지막 30초 |
| OS 크래시 / 커널 패닉 | 메모리 전체 | 마지막 1 메시지 |
| 자동 OS 업데이트 재부팅 | 메모리 전체 | 0 메시지 (사전 flush) |
| 디스크 손상 | 미동기화분 전체 | 0 (Time Machine + 2분 sync) |
| 네트워크 끊김 | 0 (큐잉) | 0 |
| 동기화 데몬 죽음 | 0 (재시작 후 재개) | 0 |

**SLA 목표**: "마지막 1 메시지 또는 30초 이내" 손실.

---

## 4단계 안전망 (공통)

모든 클라이언트에 공통 적용.

### 안전망 1 — Write-Ahead Log (WAL)

매 메시지가 끝난 직후 **로컬 append-only JSONL에 즉시 fsync**.

```
~/.team-vault/wal/
├── claude-code/
│   └── 2026-05-29-session-abc123.jsonl
├── claude-desktop/
│   └── 2026-05-29-session-def456.jsonl
├── codex/
│   └── 2026-05-29-session-ghi789.jsonl
└── kiro/
    └── 2026-05-29-session-jkl012.jsonl
```

각 라인:
```json
{
  "client": "claude-code",
  "session_id": "abc123",
  "msg_id": "msg-0042",
  "ts": "2026-05-29T21:30:00Z",
  "role": "assistant",
  "content_hash": "sha256:...",
  "content": "...",
  "metadata": { "model": "...", "tokens_in": 123, "tokens_out": 456 }
}
```

**핵심**:
- append-only (덮어쓰기 금지)
- 매 write 후 `f.flush() + os.fsync(fd)` — 디스크 강제 플러시
- 세션 종료 시 마지막 라인에 `{"event":"session_end"}` 추가

### 안전망 2 — 백그라운드 동기화 데몬 (`team-vault-syncd`)

LLM 클라이언트랑 **완전히 분리된 별도 프로세스**.
클라이언트가 죽어도 살아남는다.

```
~/.team-vault/wal/   (writer = 클라이언트 또는 hook)
       ↓ fsevents 감지
[team-vault-syncd]   ← launchd로 항상 살아있음
       ↓
S3: raw/conversations/{client}/{date}/{session}.jsonl
       ↓
업로드 성공 → WAL 옆에 .uploaded 마커
       ↓
7일 후 압축 → ~/.team-vault/archive/
```

#### launchd 등록

```xml
<!-- ~/Library/LaunchAgents/com.team-vault.syncd.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.team-vault.syncd</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/team-vault-syncd</string>
    <string>--wal</string><string>/Users/USERNAME/.team-vault/wal</string>
  </array>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>          <!-- 죽으면 즉시 재기동 -->
  <key>ThrottleInterval</key><integer>5</integer>

  <key>StandardOutPath</key>
  <string>/Users/USERNAME/.team-vault/syncd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USERNAME/.team-vault/syncd.err</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>TEAM_VAULT_ENDPOINT</key>
    <string>https://wiki.team.internal/api/ingest</string>
  </dict>
</dict>
</plist>
```

등록:
```bash
launchctl load -w ~/Library/LaunchAgents/com.team-vault.syncd.plist
```

### 안전망 3 — 멱등성

각 메시지에 `(client, session_id, msg_id)` 유니크 키.
Pod 측 ingest API가 같은 키를 받으면 **무시**.

→ 데몬이 중복 업로드해도 안전 → 무한 재시도 가능.

### 안전망 4 — 시간 기반 강제 플러시

| 트리거 | 동작 |
|--------|------|
| 매 메시지 종료 | WAL append + fsync |
| 30초마다 | 메모리 버퍼 → WAL flush (보조) |
| 2분마다 | WAL → S3 sync 트리거 |
| Sleep 직전 (`NSWorkspaceWillSleepNotification`) | 강제 flush + S3 sync 시도 |
| Wake 직후 | 미업로드분 재시도 |
| 배터리 < 15% | 우선순위 ↑ 즉시 동기화 |
| 네트워크 복구 | 큐잉된 거 일괄 전송 |

---

## 클라이언트별 캡처 전략

### A. Claude (Code & Desktop)

#### A-1. Claude Code ← **가장 쉬움**

**핵심 사실**: Claude Code는 **transcript를 JSONL로 자동 저장**한다.
```
~/.claude/projects/<encoded-path>/<session-id>.jsonl
```

이게 *공짜로 제공되는 WAL*이다. 별도 hook 필요 없음.

**전략**: 데몬이 이 디렉토리를 watch한다. 끝.

```python
# team-vault-syncd 의 claude-code 어댑터
CLAUDE_CODE_TRANSCRIPTS = Path.home() / ".claude" / "projects"

watcher = FSEventStream([str(CLAUDE_CODE_TRANSCRIPTS)])
watcher.on_modified = lambda path: enqueue_upload(
    client="claude-code",
    source_path=path,
)
```

**선택 사항 (강화)**: hooks로 *세션 종료 이벤트* 명시:
```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "team-vault-tag --client=claude-code --event=post-tool --session=$CLAUDE_SESSION_ID"
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "team-vault-tag --client=claude-code --event=session-end --session=$CLAUDE_SESSION_ID"
      }]
    }]
  }
}
```

이 hook은 **데이터를 저장하지 않는다**. transcript는 이미 디스크에 있으므로,
단지 "세션 끝났음" 마커만 데몬에 통보한다. hook이 안 불려도 데이터는 안전.

**손실 한계**: 0 메시지 (transcript는 메시지 단위로 디스크 sync됨).

---

#### A-2. Claude Desktop

**핵심 사실**: Claude Desktop은 공식 transcript export가 **없다**.
DB는 SQLite로 로컬에 있긴 하지만 비공식 포맷 + 버전 따라 바뀜.

**전략**: **MCP 도구로 *대화 중에* 저장하도록 강제**.

##### 방법 1: MCP `append_message` 도구 + 시스템 프롬프트 강제

`team-vault` MCP 서버에 새 도구 추가:

```python
\[MASKED_EMAIL]()
def append_conversation(
    session_id: str,
    msg_id: str,
    role: str,
    content: str,
    ts: str | None = None,
) -> dict:
    """
    현재 대화 내용을 vault WAL에 저장합니다.
    매 사용자 메시지 직후, 그리고 어시스턴트 응답 직후 호출해야 합니다.
    """
    append_to_wal(client="claude-desktop", ...)
    return {"saved": True}
```

시스템 프롬프트:
```
모든 응답을 마치기 직전에 반드시 `team-vault.append_conversation`을 호출해
방금의 사용자 메시지와 본인 응답을 저장하세요.

이것은 강제 규칙이며, 어떤 상황에서도 생략하면 안 됩니다.
session_id는 일관되게 유지하세요. msg_id는 단조 증가하는 정수.
```

**단점**:
- 모델이 가끔 까먹는다 (특히 짧은 답변)
- 토큰 비용 약간 추가
- "툴 콜이 보여서 거슬림" UX 이슈

##### 방법 2: 로컬 SQLite 폴링 (비공식)

Claude Desktop의 SQLite 파일을 watch:
```
~/Library/Application Support/Claude/IndexedDB/...
```

- 메시지 테이블을 주기적으로 select → WAL에 변환
- **비공식 = 업데이트 시 깨질 위험**
- fallback 용도로만

##### 방법 3: 시스템 프롬프트 + MCP `auto_save` 리소스

새 대화 시작 시 시스템 프롬프트에 다음 추가:
```
<auto_save_protocol>
session_id: <UUID 생성>
당신은 매 응답에서 `team-vault.append_conversation`을 호출해야 합니다.
응답을 사용자에게 보여주기 *직전*에 호출하세요.
</auto_save_protocol>
```

**권장**: 방법 1 + 방법 2 병행.
- 방법 1이 주: 정확한 메시지 캡처
- 방법 2가 보조: 모델이 빼먹은 메시지 보강

**손실 한계**: 모델이 도구 호출 빼먹은 메시지 (낮은 빈도).

---

### B. Codex (ChatGPT / OpenAI 계열)

여기서 "Codex"는 **OpenAI Codex CLI / ChatGPT 데스크톱**을 의미.
(레거시 Codex API는 deprecated)

#### B-1. Codex CLI (터미널)

**핵심 사실**: 대부분의 Codex CLI는 transcript를 디스크에 저장한다.
경로는 버전 따라 다름 — 일반적으로:
```
~/.codex/sessions/<session-id>.jsonl
~/.codex/history.jsonl
```

(설치 후 `codex --config-path` 또는 docs 확인 필요)

**전략**: Claude Code와 동일한 패턴 — **transcript 파일 watch**.

```python
# team-vault-syncd 의 codex 어댑터
CODEX_PATHS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "history",
]
for path in CODEX_PATHS:
    if path.exists():
        watch_for_changes(path, client="codex")
```

**MCP 지원 시**: Codex CLI도 MCP 클라이언트로 동작 가능 →
Claude Code와 같은 방식으로 hook 또는 도구 강제.

**손실 한계**: 0~1 메시지 (transcript flush 정책에 따라).

#### B-2. ChatGPT 데스크톱

**핵심 사실**: 로컬 transcript 저장 안 함. 서버에만 보관.

**전략**: **브라우저 확장 / OS 자동화로 export**.

##### 방법 1: 공식 데이터 export
- "Settings → Data Controls → Export Data"
- 수동, 비실시간 — **자동화 불가** → MVP에서 제외

##### 방법 2: 브라우저 확장
- `Save ChatGPT to Markdown` 같은 확장
- 매 대화 종료 시 마크다운 다운로드
- 다운로드 폴더를 데몬이 watch → vault로 sync
- 단점: 클릭 필요, 자동 안 됨

##### 방법 3: MCP 미지원이면 *수동 강제*
- 대화 끝나고 "이거 vault에 저장해줘" 매크로
- 텍스트 만들어진 걸 클립보드 → 핫키로 WAL에 append하는 작은 CLI

**권장**: Codex CLI를 쓰도록 팀 규칙으로 통일.
ChatGPT 데스크톱은 *지원 한계가 명확*하다고 문서화.

**손실 한계**: 자동화 불가 시 사용자 의지에 의존.

---

### C. Kiro (AWS Kiro IDE)

**핵심 사실**: Kiro는 AWS의 agentic IDE (VS Code 기반).
대화/세션을 로컬 워크스페이스 하부에 저장하는 경향.

일반적 위치:
```
<workspace>/.kiro/sessions/...
~/.kiro/global/...
```

또는 IDE의 `globalStorage`:
```
~/Library/Application Support/Kiro/User/globalStorage/...
```

(정확한 경로는 설치 후 확인 필요. 버전마다 다를 수 있음.)

#### 전략

##### 방법 1: 워크스페이스 transcript watch ← **가장 안정적**

```python
KIRO_PATHS = [
    Path.home() / ".kiro",
    # 워크스페이스 단위는 동적 — config 파일 또는 환경변수로 명시
]

# 사용자가 등록한 워크스페이스 목록을 watch
for ws in load_kiro_workspaces():
    watch_for_changes(ws / ".kiro" / "sessions", client="kiro")
```

설정 파일로 사용자가 자기 워크스페이스 경로 등록:
```yaml
# ~/.team-vault/config.yaml
kiro:
  workspaces:
    - ~/Repository/projectA
    - ~/Repository/projectB
  global_storage: ~/Library/Application Support/Kiro/User/globalStorage
```

##### 방법 2: Kiro MCP 통합

Kiro는 MCP 서버를 등록할 수 있다 (`mcp.json` 설정):
```jsonc
// <workspace>/.kiro/mcp.json
{
  "mcpServers": {
    "team-vault": {
      "url": "https://wiki.team.internal/mcp"
    }
  }
}
```

→ Kiro의 시스템 프롬프트에 "매 응답 후 `team-vault.append_conversation` 호출" 강제.

##### 방법 3: Kiro Hooks (지원 시)

Kiro가 VS Code 확장 모델을 따른다면 onDidSaveSession 같은 이벤트가 있을 수 있음.
공식 hook API가 있으면 PostMessage → CLI 호출.

**권장**: 방법 1 (transcript watch) + 방법 2 (MCP append) 병행.

**손실 한계**: 방법 1이 안정 — 0~1 메시지.

---

## 3개 클라이언트 통합 표

| 항목 | Claude Code | Claude Desktop | Codex CLI | ChatGPT Desktop | Kiro |
|------|-------------|----------------|-----------|-----------------|------|
| 로컬 transcript | ✅ JSONL | ❌ (SQLite, 비공식) | ✅ JSONL | ❌ | ✅ (워크스페이스) |
| 공식 Hook | ✅ | ⚠️ MCP만 | ✅ | ❌ | ⚠️ (extension API) |
| MCP 지원 | ✅ | ✅ | ✅ | ❌ | ✅ |
| **권장 캡처 방식** | transcript watch | MCP append 도구 강제 | transcript watch | (지원 제한, 문서화) | transcript watch + MCP append |
| 손실 한계 | 0 | 1~수 메시지 | 0~1 | N/A | 0~1 |
| 추가 셋업 | hook 1줄 | MCP 도구 + 시스템 프롬프트 | 없음 | 브라우저 확장 | 워크스페이스 경로 등록 |

---

## `team-vault-syncd` 데몬 사양

### 책임

1. WAL 디렉토리 + 클라이언트별 transcript 디렉토리 watch (fsevents)
2. 변경 감지 시 미업로드 라인을 식별 (offset 기록)
3. ingest API로 batch 업로드 (멱등성 키 포함)
4. 응답 200 OK → `.uploaded` 마커 / offset 갱신
5. 실패 → 지수 백오프 재시도, 24시간+ 실패 시 사용자 알림
6. sleep/wake/battery 이벤트 처리

### 상태 파일

```
~/.team-vault/state/
├── offsets.json           # 각 파일의 마지막 업로드 offset
├── pending.jsonl          # 업로드 대기 큐
└── adapters/
    ├── claude-code.json   # 클라이언트별 상태
    ├── claude-desktop.json
    ├── codex.json
    └── kiro.json
```

### 클라이언트 어댑터 인터페이스

```python
class ClientAdapter(Protocol):
    name: str

    def watch_paths(self) -> list[Path]: ...
    def parse_event(self, path: Path) -> list[Message]: ...
    def offset_key(self, path: Path) -> str: ...
```

각 클라이언트마다 어댑터 1개. 새 클라이언트 추가는 어댑터 1개 추가로 끝.

### 업로드 형식 (Pod 측 ingest API)

```http
POST https://wiki.team.internal/api/ingest/conversations
Content-Type: application/json
X-Vault-Hostname: alice-mbp
X-Vault-OS-User: alice

{
  "client": "claude-code",
  "session_id": "abc123",
  "messages": [
    {
      "msg_id": "msg-0042",
      "ts": "2026-05-29T21:30:00Z",
      "role": "user",
      "content_hash": "sha256:...",
      "content": "..."
    }
  ]
}
```

인증 헤더 없음 (회사 IP 게이트). `user`는 본문이 아니라 *서버가* 호스트네임으로 결정
(`15-zero-touch-onboarding.md`) → 다른 사람 사칭 방지.
Pod는 `(user, client, session_id, msg_id)`로 dedup 후
`s3://team-vault/raw/conversations/{client}/{date}/{session}.jsonl`에 append.

이후 가공 파이프라인이 처리.

---

## OS 이벤트 통합 (macOS 기준)

```python
# team-vault-syncd 안

from AppKit import NSWorkspace, NSWorkspaceWillSleepNotification, NSWorkspaceDidWakeNotification

def on_will_sleep(_):
    log("system going to sleep — forcing flush")
    flush_all_wal()
    sync_pending_to_s3(timeout=10)   # 10초만 시도, 못 보내면 다음 wake에

def on_did_wake(_):
    log("system woke up — retrying pending uploads")
    retry_all_pending()

nc = NSWorkspace.sharedWorkspace().notificationCenter()
nc.addObserverForName_object_queue_usingBlock_(
    NSWorkspaceWillSleepNotification, None, None, on_will_sleep)
nc.addObserverForName_object_queue_usingBlock_(
    NSWorkspaceDidWakeNotification, None, None, on_did_wake)
```

### 배터리 통합
- `pmset -g batt` 폴링 또는 IOKit notification
- 배터리 < 15% → 동기화 주기 30초로 단축
- 배터리 < 5% → 즉시 동기화 + 사용자 토스트

---

## 보안 / 프라이버시

- WAL은 로컬에만 → 자기 PC 디스크 암호화 (FileVault) 필수
- ingest API 인증: 없음 (회사 IP 게이트 + 호스트네임 식별, `14`/`15`)
- 멱등키에 user(서버가 호스트네임으로 결정) 포함 → 다른 사람 세션 덮어쓰기 불가
- 민감 정보는 **Pod 가공 단계에서** 마스킹 (WAL 자체는 raw 보존)
- 7일 후 로컬 WAL은 압축 → archive/, 30일 후 삭제 (S3는 영구)

---

## 테스트 시나리오 (구현 후 반드시 통과)

| 테스트 | 기대 결과 |
|--------|----------|
| 대화 중 Cmd+Q | 마지막 메시지까지 S3 도착 |
| 대화 중 Cmd+Opt+Esc | 마지막 메시지까지 S3 도착 |
| 대화 중 강제 슬립 | wake 후 자동 동기화 |
| 대화 중 Wi-Fi 끄기 | WAL에 누적, 켜면 일괄 동기화 |
| 데몬 kill -9 | launchd 즉시 재기동, offset부터 재개 |
| 동시 5개 클라이언트 | 모두 독립적으로 캡처 |
| 같은 메시지 5회 재시도 | Pod에서 dedup, S3에 1번만 |
| 디스크 가득 | WAL append 실패 시 사용자 알림, 데이터 손상 X |

---

## 구현 우선순위

1. **Phase 1 (1주)**: `team-vault-syncd` + Claude Code 어댑터 (transcript watch + S3 업로드)
   → 가장 쓰는 도구를 가장 안전하게 먼저
2. **Phase 2 (1주)**: launchd 등록, 멱등 ingest API (Pod 측), sleep/wake hook
3. **Phase 3 (1주)**: Codex CLI 어댑터, Kiro 어댑터
4. **Phase 4**: Claude Desktop MCP append 도구 + 시스템 프롬프트 표준화
5. **Phase 5**: 배터리/네트워크 이벤트, 진단/리포트 UI

---

## 운영 절차

### 사용자 셋업

```bash
# 1. 데몬 설치 (토큰 설정 단계 없음 — 회사 IP 게이트)
curl -sSL https://wiki.team.internal/install.sh | bash

# 2. 클라이언트 자동 감지
team-vault detect
# → Claude Code 발견 ✓
# → Codex CLI 발견 ✓
# → Kiro 워크스페이스 후보: ~/Repository/projectA (추가하시겠어요? y/n)

# 3. launchd 등록
team-vault install-agent

# 4. 상태 확인
team-vault status
# → syncd running, last sync 12s ago, 0 pending, 0 errors
```

### 진단 명령

```bash
team-vault status          # 데몬 상태, pending 수
team-vault tail            # 최근 동기화 로그
team-vault verify          # WAL과 S3 비교, 누락 발견 시 재업로드
team-vault flush           # 강제 즉시 동기화
team-vault doctor          # 권한/네트워크/디스크 전반 점검
```

### 슬랙 알림 (선택)

데몬이 1시간 이상 동기화 실패 시 사용자 슬랙 DM:
> "team-vault-syncd가 1시간째 업로드 실패. `team-vault doctor` 실행해주세요."

---

## 한 줄 요약

**"매 메시지마다 로컬 WAL append → launchd로 항상 살아있는 데몬이 비동기 S3 sync"**.
Claude Code/Codex/Kiro는 transcript watch만으로 거의 무손실,
Claude Desktop은 MCP 도구 강제로 보완.
SLA는 "마지막 1 메시지 또는 30초 이내" 손실.
