# 관리 콘솔

학과 설정(Cloud 등록부) 관리, 리소스·배포·동기화 상태 확인, 선택 문서 재처리, 해체를 하는 로컬 웹
콘솔이다. 백엔드는 `scripts/dept_gui.py`, 화면은 `gui/public/console/`이다.

## 실행

```powershell
.\run.cmd                          # .venv 생성·requirements-gui.txt 설치 후 실행, 포트 8765
.\run.cmd --port 9000 --no-browser
python scripts/dept_gui.py         # 직접 실행
```

`127.0.0.1`에만 바인딩한다. 상태 조회는 읽기 전용이며 저장·배포·실행 버튼을 누를 때만 GCP를 바꾼다.

## 학과 설정

- 원본은 Cloud 등록부다(Secret Manager `rag-mcp-departments` + Firestore `mcp_registry/current`,
  [MCP.md](MCP.md#키와-설정)). 로컬 YAML이나 Cloud Run 주석은 원본이 아니다. 다른 PC에서도 같은
  프로젝트로 로그인하면 같은 설정을 불러와 수정·재배포할 수 있다.
- 저장은 revision을 비교해 동시 수정을 거부한다. 기존 키와 모르는 필드는 보존한다.
- 브라우저용 설정 API는 키·토큰을 빼고 주며, 키는 복사 버튼을 눌렀을 때만 돌려준다.
- 상태 검사(LOCAL·RESOURCE·DEPLOY·RUNTIME·SYNC)는 끝난 것부터 화면에 반영한다. 재확인 중에는 이전
  “정상” 대신 “조회 중”을 보인다.

## 동기화 실행 상세

- Workflow 실행 ID로 로그를 조회해 색인·삭제·정합성 검사의 경고·오류와 영향받은 파일을 보인다.
- 파일 하나의 반입이 실패해도 같은 묶음의 나머지는 처리하고, 실행은 ‘완료 · 일부 실패’로 표시한다.
- 파일 내역은 Drive 생성·수정 시각과 처리 시각을 한국 시간으로 구분한다. 과거 로그에 없는 시각은
  ‘기록 없음’이다.
- 파일별 체크포인트가 없던 예전(2026-10 이전) 실행은 `scripts/reconcile_sync_history.py`가 만든
  `sync_run_reviews/{executionId}` 재검토 결과로 보인다. 도구는 기본 읽기 전용이며 `--apply`일 때만 저장한다.

## 선택 문서 재처리

**색인 보류·오류 → 학과 선택 → 문서 클릭 → 이 문서만 재처리**. 목록을 열거나 새로고침하는 것만으로는
처리하지 않는다. 콘솔 "색인 문제" 목록에는 `PARSED`(색인 완료 미확인)·`FAILED`(처리 오류)·`SKIPPED` 등이
남으며, Cloud Tasks 자동 재시도(최대 5회)가 끝난 문서는 여기서 관리자가 처리한다.

- `rag-daily-sync`의 `reprocessFile` 분기로 실행되며 변경분 조회·전체 적재·토큰 커밋은 하지 않는다.
- Sync `/sync/reprocess-file`이 현재 Drive 메타데이터와 수집 폴더를 다시 확인한다. 삭제·이동된 문서,
  정상 색인된 문서, 진행 중인 색인은 거부한다. 현재 학과의 OCR·fallback 옵션과 기존 품질 게이트를 적용한다.
- 한 건짜리 Cloud Tasks 색인 작업을 만들고 Workflow가 완료를 확인한다. HTTP 접수만으로 성공을 표시하지
  않으며 `SKIPPED`·`DLQ`·`SPLIT_QUEUED`는 완료가 아니다.
- 같은 요청 ID는 OCR·적재를 반복하지 않고, 끊긴 요청은 자동 재전송하지 않는다. 잠금 강제 해제는 없다.
- 상세 창은 학과별 최근 재처리 100건 중 그 문서의 최근 3건을 보이고, 실행 중에는 10초마다 갱신한다.
- 콘솔은 배포된 Workflow가 `reprocessFile`을 지원하는지 확인하고 아니면 접수를 막는다. 구버전 Workflow는
  이 인자를 무시하고 일반 동기화를 시작하기 때문이다. OCR·색인 비용이 생길 수 있다.

## 해체

학과 철거는 계획을 먼저 보여 주고, 확인 문구(학과 코드 / 공통 런타임은 프로젝트 ID)를 손으로 받는다.
다른 학과가 참조하는 코퍼스·버킷·드라이브는 선택과 상관없이 건너뛴다 — 이름만 보고 지우면 남은 학과의
검색이 오류 없이 빈 결과가 된다.

| 대상 | 계획 키 |
|---|---|
| MCP 접근 중지 (통합 `rag-mcp`에서 학과·대상 키 경로만 끔, 서비스는 유지) | `mcp-{audience}` |
| Vertex RAG 코퍼스 | `corpus-{audience}` |
| 학과 GCS 버킷 (`hwpOriginal`, `source`) | `bucket-{slot}` |
| Firestore `doc_state`(driveId 기준)·하위 `rag_files`·`sync_tokens`·`doc_dlq`·`doc_split_queue` | `firestore-state` |
| 공용 메타데이터 버킷의 `import-results/{corpusId}/` | `metadata-{audience}` |
| Cloud 등록부의 학과 설정 (MCP 키 포함) | `registry-config` |
| rag-sync `DEPARTMENTS_JSON` 갱신 | `sync-env` |

- 실행 순서는 위 표 순서다. 등록부 설정은 앞 단계가 실패해도 다시 찾을 수 있게 뒤에, `sync-env`는 남은
  학과만으로 맵을 만들도록 맨 뒤에 둔다. 공통 런타임은 Scheduler → Workflow → rag-sync → rag-parser 순이다.
- 각 줄을 끌 수 있고, 끈 줄은 “선택하지 않아 남깁니다”로 남는다. 위험한 조합에는 경고가 붙는다.
  - `firestore-state`를 남기면 같은 드라이브를 다시 등록해도 `HASH_UNCHANGED`로 건너뛰어 새 코퍼스가 빈다.
  - `sync-env`를 끄면 없어진 버킷으로 계속 동기화해 업로드가 404 → DLQ로 간다.
  - 등록부 설정을 지우고 리소스를 남기면 그 리소스는 콘솔에서 다시 찾을 수 없다.
- 공용 Cloud Tasks 큐, 메타데이터 버킷 자체, Firestore 데이터베이스는 학과 철거에서 건드리지 않는다.
- `google-cloud-firestore`가 없는 환경에서는 `firestore-state`만 실패로 남고 나머지는 진행된다.

API: `GET /api/v1/departments/{code}/teardown-plan`, `GET /api/v1/common-runtime/teardown-plan`,
`POST …/teardown` (`{confirm, targets}`, `targets` 생략 시 전체), `GET /api/v1/teardowns/{run_id}`.

## 테스트

```powershell
cd gui
npm ci
npm test        # 빌드 후 tests/*.test.mjs 전체. 빌드가 있으면 npm run test:regression
```

Node.js 22.13 이상. GitHub Actions의 GUI 작업이 PR과 `main` 푸시마다 같은 명령을 실행한다. 화면 로직과
빌드 산출물을 검증하며 실제 브라우저 배치나 Document AI 호출은 포함하지 않는다.
