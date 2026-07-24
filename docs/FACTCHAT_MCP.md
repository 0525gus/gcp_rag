# FactChat(팩트챗) ← Cloud Run MCP 연동

## 목표

Cloud Run에 올린 `rag-mcp`의 `search` tool을 팩트챗 **MCP 커넥터**에 연결합니다.

## 1) 사전 준비

- GCP 프로젝트, `gcloud` 로그인
- Vertex AI RAG 코퍼스 (`RAG_CORPUS_NAME`) — 검색할 문서가 이미 색인되어 있어야 함
  - HWP 파이프라인(parser/sync)은 나중에 돌려도 되고, **MCP만 먼저** 붙이려면 코퍼스에 테스트 문서라도 import 되어 있어야 `search`가 의미 있음
- 강한 랜덤 API 키 하나 생성

```powershell
# 예: API 키 생성
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 2) MCP만 Cloud Run 배포

```powershell
cd c:\dev\gcp-mcp
$env:GCP_PROJECT_ID = "your-project"
$env:GCP_REGION = "asia-northeast3"
$env:RAG_CORPUS_NAME = "projects/.../locations/asia-northeast3/ragCorpora/..."
$env:MCP_API_KEY = "위에서_만든_키"
# bash (Git Bash / WSL)
bash scripts/deploy_mcp.sh
```

출력 예:

```
Server URL : https://rag-mcp-xxxxx-an.a.run.app/mcp
Header     : Authorization: Bearer <MCP_API_KEY>
```

로컬 확인:

```powershell
curl https://rag-mcp-xxxxx-an.a.run.app/health
curl -H "Authorization: Bearer $env:MCP_API_KEY" `
  -H "Content-Type: application/json" `
  https://rag-mcp-xxxxx-an.a.run.app/mcp
```

## 3) 팩트챗 MCP 커넥터에 등록

팩트챗 관리/설정 화면에서 보통 아래 필드를 채웁니다 (UI 문구는 기관별로 다를 수 있음):

| 항목 | 값 |
|---|---|
| 이름 | `rag-search` (임의) |
| URL / Endpoint | `https://rag-mcp-xxxxx-an.a.run.app/mcp` |
| Transport | **Streamable HTTP** (없으면 HTTP / SSE) |
| Auth | Bearer 토큰 또는 Custom Header |
| Header | `Authorization: Bearer <MCP_API_KEY>` |

저장 후 커넥터 활성화 → 채팅에서 문서 검색 질문을 하면 `search` tool이 호출되는지 확인합니다.

### 팩트챗 UI가 “헤더 커스텀”을 안 주는 경우

1. 마인드로직/기관 관리자에게 **원격 MCP + Authorization 헤더** 지원 여부 확인  
2. 임시로 `MCP_API_KEY`를 비우고 공개(`--allow-unauthenticated`)만 쓰는 것은 **비권장** (교내망 IP 제한 등과 함께 쓸 때만)

## 4) 동작 확인 체크리스트

- [ ] `/health` → `{"status":"ok"}`
- [ ] 팩트챗에서 커넥터 연결 성공 / tools 목록에 `search`, `answer` 보임
- [ ] “연말정산 안내 알려줘” 등 → 출처(`source.fileId`/`name`)가 답에 인용되는지
- [ ] Cloud Run 로그에 `search query=...` 기록

## 5) 자주 막히는 지점

| 증상 | 원인 | 조치 |
|---|---|---|
| 401 | API 키 불일치 | 팩트챗 헤더와 `MCP_API_KEY` 동일하게 |
| 연결 타임아웃 | URL에 `/mcp` 누락 | `...run.app/mcp` |
| tools 없음 / 빈 검색 | 코퍼스 미색인 | sync 배치 또는 RAG에 테스트 MD import |
| CORS/브라우저 오류 | 팩트챗이 브라우저에서 직접 호출 | 서버사이드 프록시 지원 여부 벤더 확인 |

## 참고

- Transport: Streamable HTTP (`MCP_TRANSPORT=streamable-http`)
- 리전: `asia-northeast3`
- parser/sync는 이 연동에 **필수 아님** (색인 데이터가 있을 때 MCP만으로 검색 가능)
