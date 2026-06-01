# llmwiki

Team Vault MVP: 팀 노트와 AI 대화 기록을 수집하고, REST/MCP로 검색하는 사내 지식 베이스.

## MVP services

### Ingest API

```bash
uv run team-vault-ingest
```

기본값은 로컬 저장소(`.data/team-vault`)다. EKS에서는 IRSA와 함께 S3 backend를 켠다.

```bash
TEAM_VAULT_STORAGE_BACKEND=s3 \
TEAM_VAULT_S3_BUCKET=team-vault \
uv run team-vault-ingest
```

### Search API + MCP

```bash
uv run team-vault-search
```

REST:

```bash
curl "http://127.0.0.1:8080/api/search?q=timeout"
```

MCP streamable HTTP endpoint:

```text
http://127.0.0.1:8080/mcp
```
