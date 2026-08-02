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

---

# 인증 설계 메모 (미조치 — 검토 결과 기록)

> 2026-08 파이프라인 검사에서 정리. **아직 코드에 반영하지 않았다.**
> 조치할 때 이 절을 근거로 쓰고, 반영 후 이 경고를 지울 것.

## 왜 API 키뿐인가

Cloud Run 인증은 두 가지뿐이다.

| 방식 | 호출자 요구사항 | FactChat |
|---|---|---|
| IAM (ID 토큰) | Google 자격증명으로 토큰 발급 | ❌ 불가 |
| 공개 + 앱 레벨 인증 | 정적 헤더 하나 | ✅ |

FactChat은 외부 SaaS라 커넥터 설정에 **헤더 하나** 넣는 게 전부다. Google ID 토큰을
만들 수 없으므로 IAM은 구조적으로 못 쓴다. 배포 스크립트가 둘인 이유가 이것이다 —
`scripts/deploy.sh`의 `rag-mcp`는 `--no-allow-unauthenticated`(Cursor 등 IAM 경로),
`scripts/deploy_mcp.sh`는 `--allow-unauthenticated`(FactChat 경로). **설계 실수가 아니라
FactChat 제약이다.**

따라서 개선 여지는 "인증 방식"이 아니라 **키의 저장·비교·회전·노출 범위**에 있다.

## 현재 상태의 약점

| # | 위치 | 문제 |
|---|---|---|
| ① | `services/mcp_server/main.py` 의 `token != MCP_API_KEY` | 상수 시간 비교가 아니다. 파이썬 `!=` 는 첫 불일치 바이트에서 멈추므로 응답 시간이 "앞에서 몇 글자 맞았는지"에 따라 달라진다 → 이론상 한 글자씩 복구 가능. 다만 공용 인터넷 + 콜드스타트 + TLS 지터에 묻혀 실제 공격 난이도는 높다. 고치는 비용이 1줄이라 안 할 이유가 없을 뿐 |
| ② | `scripts/deploy_mcp.sh` · `deploy_mcp.ps1` 의 안내 출력 | 배포마다 키를 평문으로 stdout에 찍는다 → 터미널 스크롤백, **CI 로그(영구 보관)**, 화면 공유에 남는다 |
| ③ | `deploy_mcp.sh` 의 `--set-env-vars=...MCP_API_KEY=...` | 키가 Cloud Run 서비스 설정에 평문 저장 → `roles/run.viewer` 만 있어도 조회된다 |

`config/*.json` 과 이 문서는 `${MCP_API_KEY}` 플레이스홀더만 쓰고 `.env` 는 gitignore
되어 있다 — **저장소에 유출된 키는 없다.**

## 유출 시 영향

- `search`/`answer` 무제한 호출 → 사내 문서 본문 열람
- rate limit 없음 + `--concurrency=40` → 질의를 반복해 **코퍼스 전체 추출 가능**
- 읽기 전용이라 파괴는 없으나 정보 유출은 완전
- 탐지 수단 없음 (`logger.info("search query=%r")` 로 질의는 남지만 알림이 없다)

## 조치 선택지

| 방법 | 막는 것 | 비용 |
|---|---|---|
| ① `secrets.compare_digest` | 타이밍 공격 | 코드 1줄 |
| ② 스크립트 출력을 플레이스홀더로 | 히스토리·CI 로그 유출 | 2줄 |
| ③ Secret Manager | `run.viewer` 로 키 조회 | 배포 플래그 1개 |
| ④ 키 다중 허용 | 회전 시 다운타임 | 몇 줄 |
| ⑤ IP allowlist (Cloud Armor) | 키 유출돼도 임의 위치에서 사용 | **인프라 작업 — 아래 참조** |
| ⑥ rate limit | 코퍼스 통째로 긁기 | Cloud Armor 또는 코드 |

③ 적용 형태:

```bash
echo -n "$MCP_API_KEY" | gcloud secrets create mcp-api-key --data-file=-
gcloud run deploy rag-mcp --set-secrets="MCP_API_KEY=mcp-api-key:latest" ...
# 런타임 SA 에 roles/secretmanager.secretAccessor 부여 필요
```

`--set-env-vars` 와 달리 값이 서비스 설정에 남지 않고, 회전이 Secret 새 버전 +
리비전 재시작으로 끝난다(이미지 재빌드 불필요). 옮길 때 **키도 새로 발급**할 것 —
기존 키는 이미 ②를 통해 로그에 남았을 가능성이 크다.

## IP allowlist 를 택할 경우 (현재 유력안)

**주의: Cloud Run 에 Cloud Armor 를 직접 붙일 수 없다.** 필요한 구성:

```
FactChat → [Global External ALB] → [serverless NEG] → Cloud Run
                  ↑ Cloud Armor 정책은 여기에만 붙는다
```

- 외부 ALB + 도메인 + SSL 인증서 구성
- Cloud Run 을 `--ingress=internal-and-cloud-load-balancing` 으로 변경
  (안 하면 `*.run.app` 직접 호출로 우회된다 — **이걸 빠뜨리면 allowlist 가 무의미**)
- 비용: ALB 시간당 요금 + Cloud Armor 정책·룰 요금

**선행 확인 사항**: FactChat 이 출구 IP 대역을 고정으로 공개하는지. 공개하지 않는
SaaS 도 있고, 그러면 allowlist 자체가 불가능하다. 이것부터 확인할 것.

**IP allowlist 는 키를 대체하지 않는다.** IP 는 "어디서", 키는 "누가"를 막는 다른
층이다. IP만 두면 그 대역 안의 누구나 접근하고, 키만 두면 유출 시 전 세계에서
접근한다. 둘 다 있어야 의미가 있다.

출구 IP 확인이 안 되면 대안은 **코드에 rate limit 직접 구현**(ALB 없이 분당 N회 초과
시 429) — 코퍼스를 통째로 긁히는 것만은 막는다.

---

## 참고

- Transport: Streamable HTTP (코드에 고정 — 설정으로 바꾸지 않는다)
- 리전: `asia-northeast3`
- parser/sync는 이 연동에 **필수 아님** (색인 데이터가 있을 때 MCP만으로 검색 가능)
