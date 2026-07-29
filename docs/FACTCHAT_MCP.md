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

## 3-1) 팩트챗 프롬프트에 넣어야 하는 규칙

MCP 툴은 **자기 턴이 없어 사용자에게 되물을 수 없습니다.** 질의가 애매할 때
되묻는 행동은 팩트챗(에이전트 빌더) 프롬프트의 책임이고, 서버는 그 판단 근거만
넘깁니다. 아래 필드를 쓰는 규칙을 시스템 프롬프트에 넣어 주세요.

| 필드 | 위치 | 의미 |
|---|---|---|
| `missingTerms` | `search` 각 청크 | 그 청크 본문에 **없는** 질의어 |
| `uncoveredTerms` | `answer` | 어떤 문서에도 없는 질의어 |
| `coverage` | `answer` | `full`(한 문서가 질의 전체를 덮음) / `partial` / `none` |

권장 규칙:

- `uncoveredTerms` 가 비어 있지 않으면 답을 합성하지 말고, 무엇을 찾는지 되묻는다
- `coverage="partial"` 이면 여러 문서를 이어 붙여 결론을 만들지 않는다.
  "A는 확인되지만 B는 확인되지 않는다"로 **결론을 먼저** 말한다
- 질문의 핵심어가 어느 청크의 `missingTerms` 에 있으면, 그 청크는 그 부분의
  근거가 아니다. 문서 간 관계(인물↔업무 담당 등)를 추론해 잇지 않는다
- **한 질문에 `search` 는 한 번만 호출한다.** 표현을 바꿔 같은 의도로 재질의하지
  않고, `top_k` 도 올리지 않는다. 가능하면 에이전트 빌더의 **턴당 툴 호출 상한을
  2회**로 걸어 둔다

### 재질의 루프 (토큰이 새는 실제 경로)

`"LMS" "명단" "교수학습개발센터" AI` 한 질문에 팩트챗이 19초 동안 **7회** 호출하며
`top_k` 를 10 → 20 으로 스스로 올린 기록이 있습니다. 코퍼스에 없는 '명단'을 찾느라
표현만 바꿔 재시도한 것인데, 매번 관련 문서가 부분적으로는 나오니 멈출 근거가
없었습니다. 비용은 호출 횟수가 아니라 **한 번당 크기**에서 나옵니다 —
`top_k=20` × 문서당 3청크 × 청크 1024토큰 ≈ 한 호출에 6만 토큰이고, 그게 호출마다
에이전트 컨텍스트에 누적됩니다.

- 서버 쪽 조치: `SEARCH_MAX_TOTAL_CHUNKS`(기본 15)로 응답 총량을 묶었습니다
- 남은 절반은 프롬프트 쪽입니다. `uncoveredTerms` 가 곧 멈출 근거이니,
  "없으면 없다고 답한다"를 규칙으로 넣어야 루프가 끝납니다

### 왜 필요한가 (실제 사례)

`"LMS" "명단" "교수학습개발센터" AI` 질의에서 인사발령·기능조사표·LXP 계획
3개 문서가 각각 **다른 키워드만** 만족한 채 반환됐고, 답변 LLM 이 이를 하나로
엮어 특정 인물을 LMS AI 담당자처럼 읽히게 서술한 적이 있습니다. 검색은 정상이었고
(문서 3건 모두 관련 문서), 문제는 "각 문서가 질의의 어느 부분을 덮는가"가
payload 에 없었던 것입니다. 위 필드는 그 공백을 메웁니다.

## 4) 동작 확인 체크리스트

- [ ] `/health` → `{"status":"ok"}`
- [ ] 팩트챗에서 커넥터 연결 성공 / tools 목록에 `search`, `answer` 보임
- [ ] “연말정산 안내 알려줘” 등 → 출처(`source.fileId`/`name`)가 답에 인용되는지
- [ ] Cloud Run 로그에 `search query=...` 기록
- [ ] 키워드 나열 질의(예: `"LMS" "명단" AI`) → `answer.coverage` 가 `partial` 로
      나오고, 팩트챗이 합성 대신 되묻는지 (3-1 규칙 적용 확인)

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
