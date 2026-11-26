# MCP 검색

Cloud Run 서비스 `rag-mcp` 하나가 모든 학과의 교직원·학생 검색을 처리한다. 모든 연결이 같은
`<서비스 URL>/mcp`를 쓰고, 학과·대상별 API 키가 검색 범위를 정한다. 학과를 추가해도 MCP
서비스는 늘지 않는다.

- 운영 URI: `https://rag-mcp-42w47ff6ba-du.a.run.app/mcp`
- Transport: Streamable HTTP. 키는 `Authorization: Bearer <키>` 또는 `X-API-Key: <키>`
- 2 vCPU · 1 GiB · 동시 요청 40 · 최소 인스턴스 0 · 검색 캐시 60초.
  2026-09-11 측정에서 순차 지연은 1·2 vCPU가 비슷했고 동시 요청에서만 2 vCPU가 나았다.
  FactChat 동시 40 대화에서 MCP 오류는 없었고 11건은 FactChat 게이트웨이의 HTTP 429였다.

## 검색 범위

| 인증 정보 | 검색 범위 |
|---|---|
| 학과 교직원 키 | 그 학과 교직원 코퍼스와 등록 드라이브 |
| 학과 학생 키 | 그 학과 학생 코퍼스와 등록 드라이브의 `audience=STUDENT` 문서 |
| 없거나 등록되지 않은 키 | HTTP 401 |

- `search` 인자는 `query`, `top_k`, 선택 `drive_id`뿐이다. 학과·대상·코퍼스는 인자로 바꿀 수 없고
  허용되지 않은 `drive_id`는 거부한다.
- 결과는 문서 상태가 있고 허용 드라이브에 속한 문서만 반환한다.
- 범위는 요청마다 따로 만들고 검색 캐시도 학과·대상·코퍼스·드라이브·레지스트리 버전으로 나눈다.
  실행 서비스 계정은 등록 코퍼스를 모두 조회할 수 있으므로 학과 경계는 이 서버가 강제한다.
- 검색 로그에 `scope=<학과>/<대상>`이 남는다.

## 응답 계약

```json
{
  "schemaVersion": 2,
  "currentDate": "2026-09-18",
  "timeZone": "Asia/Seoul",
  "documents": [
    {
      "citationId": 1,
      "source": {
        "fileId": "drive-file-id",
        "name": "content.html",
        "label": "성적우수장학금 / content.html",
        "path": "Drive/장학금/content.html",
        "bundle": "성적우수장학금",
        "title": null,
        "websiteUrl": null,
        "sourceUri": "https://drive.google.com/file/d/drive-file-id/view",
        "modifiedTime": null,
        "driveId": "drive-id"
      },
      "chunks": [{"text": "검색된 근거 구간", "score": 0.12, "scoreType": "vertex_raw"}]
    }
  ],
  "documentCount": 1,
  "chunkCount": 1,
  "retrievalDiagnostics": null
}
```

- `top_k`는 최대 문서 수다. 기본 7(`TOP_K_DEFAULT`), 상한 20(`SEARCH_TOP_K_MAX`).
  문서당 3청크·전체 15청크 상한이 있어 결과는 더 적을 수 있다.
- 배열 순서가 관련도 순위다. `score`는 Vertex 원값이며 답변 확률이 아니다.
- `citationId`는 이 응답 안의 인용 번호, 영구 식별자는 `source.fileId`다.
- `currentDate`는 서버가 매 요청(캐시 적중 포함) 한국 시간으로 계산한 날짜다. 기간·마감 비교용이며
  문서 날짜가 아니다.
- `sourceUri`는 Drive 미리보기 링크, HWP/HWPX는 다운로드 링크다. 학생 키 응답에서 게시글 주소가 있는
  문서는 `sourceUri`가 게시글 주소이고, PDF·HWP·Office 첨부만 Drive 링크를 `attachmentUri`로 함께 준다.
- `title`·`websiteUrl`은 게시글 metadata.json의 제목·주소다. 없으면 null.
- `chunks`는 선택된 청크일 뿐 원문 전체·연속 구간을 보장하지 않는다.
- `retrievalDiagnostics`는 `SEARCH_EXPERIMENT_DIAGNOSTICS=true` 배포에서만 채워진다.
- 같은 응답이 `structuredContent`와 `content` 텍스트로 함께 직렬화된다. 구조화 응답을 우선 쓴다.
- 도구 설명·스키마가 바뀌면 FactChat 연결에서 도구 목록을 다시 불러와야 새 설명을 쓴다.

HTML 원본은 수집 때 제목·목록·표 관계를 보존한 텍스트로 바꾸고, 예전에 색인된 HTML은 응답에서
정제한다. 예전 HTML을 다시 변환하려면 `refreshContent: true`로 `/sync/ingest`를 호출한다.

평가용으로 같은 인증을 거치는 `POST /retrieval/{vector|body|title|bundle|metadata}`가 있다.
후보는 Vertex vector만 만들고 나머지는 그 후보를 다른 기준으로 정렬한다.

## 키와 설정

- 학과 설정과 API 키 원본은 Secret Manager `rag-mcp-departments`, 활성 revision과 키 SHA-256 해시
  경로는 Firestore `mcp_registry/current`다. 관리 콘솔이 쓰고, 배포 스크립트는 서비스 URL만 기록한다.
- 변경은 새 Secret 버전을 만들고 Firestore를 갱신한다. 이전 revision과 수정 시각이 다르면 거부해 동시
  수정의 덮어쓰기를 막는다. 게시 실패 시 기존 설정이 유지된다.
- 키 교체는 레지스트리 게시만으로 반영되며 재배포가 필요 없다. 런타임의 경로 캐시는 인스턴스별 5초,
  조회 실패나 잘못된 설정은 HTTP 503으로 거부한다.
- Secret 접근과 Firestore 쓰기 권한은 관리자에게만 준다.

검색 런타임 계정은 `rag-mcp-search@<프로젝트 ID>.iam.gserviceaccount.com`이며 커스텀 역할
`mcpSearchReader`에 다음 조회 권한만 있다. Secret Manager 접근 권한은 없다.

| 권한 | 용도 |
|---|---|
| `aiplatform.ragCorpora.query`, `aiplatform.ragCorpora.get` | RAG 검색·코퍼스 조회 |
| `aiplatform.endpoints.predict` | 검색어 임베딩 |
| `datastore.entities.get`, `datastore.entities.list`, `datastore.databases.get` | 경로·문서 상태 조회 |
| `serviceusage.services.use` | 프로젝트 API 사용 |

Cloud Run 진입은 공개하고 애플리케이션이 키를 검증한다. `/health`는 기동 확인용이다.

## 배포와 검증

```powershell
python scripts/deploy_unified_mcp.py                       # 현재 소스를 빌드해 배포
python scripts/deploy_unified_mcp.py --no-traffic --tag <태그>   # 트래픽 없이 비교 리비전
python scripts/verify_unified_mcp.py --target "prod=https://<서비스 주소>/mcp" --out tests/_bench_out/unified-verify.json
```

- `--image <digest>`로 이미 빌드한 이미지를, `--skip-build`로 `mcp:latest`를 쓴다. vCPU·동시성은
  `scripts/common_config.py`의 `MCP_CPU`·`MCP_CONCURRENCY`를 따른다. `deploy_mcp.ps1`도 이 스크립트를 부른다.
- 비교 리비전은 태그 URL로 검증한 뒤 `gcloud run services update-traffic rag-mcp --to-revisions=<리비전>=100`으로
  전환한다. 태그는 운영 키로 예전 코드에 닿는 통로이므로 `--remove-tags=<태그>`로 지운다.
- 검증 스크립트는 모든 학과·대상 키로 반환 문서의 드라이브·학생 범위와 잘못된 키의 401을 확인한다.
  결과가 0건인 범위는 검증된 것으로 보지 않는다.
- 예전 학과별 MCP 서비스(`rag-mcp-<학과>-<대상>`)는 의도적으로 남겨 둔다. 최소 인스턴스 0이며 배포 당시
  키를 환경변수로 가진다.
