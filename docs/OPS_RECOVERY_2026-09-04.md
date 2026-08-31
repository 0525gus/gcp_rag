# 운영 복구 진행 기록 — 2026-09-04

## 범위와 현재 결론

- 대상 프로젝트: `tuk-mcp-rag`
- 리전: `asia-northeast3`
- 대상 서비스: `rag-mcp-cs-staff`, `rag-mcp-cs-student`, `rag-sync`, `rag-parser`
- 교직원·학생 MCP 모두 Cloud Run 최소 인스턴스를 `1`로 적용했다.
- 대형 Vertex RAG 코퍼스의 파일 목록 순회가 429 이후 첫 페이지부터 반복되던 문제를 수정했다.
- Workflow가 내부 실패를 포함하고도 `SUCCEEDED`로 끝나던 문제를 수정했다.
- 실제 운영 재실행에서 새로 확인된 텍스트 없는 스캔 PDF 4건의 반복 색인 실패를 사이드카 폴백으로 수정하고 `rag-sync`에 배포했다.
- 마지막 종단 Workflow는 이 문서 작성 시점에도 `ACTIVE`였다. 사용자의 요청에 따라 여기서 추가 모니터링과 작업을 중단했다.

## 최초 장애 증상과 원인

2026-09-04 00:00 KST 정기 실행은 GCP 상태가 `SUCCEEDED`였지만 결과 본문은 아래처럼 실패를 포함했다.

```text
ok=false, deleted=32, failed=1, listed=33
```

`rag-sync /sync/delete`가 7회 HTTP 500을 반환했다. 직접 원인은 Vertex RAG `ragFiles.list`의 지역별 분당 요청 쿼터 초과(`ResourceExhausted`)였다.

기존 구현은 `list(rag.list_files(...))` 전체를 재시도했다. 뒤쪽 페이지에서 429가 발생하면 이미 읽은 첫 페이지부터 다시 시작하므로, 파일 수가 많은 코퍼스는 같은 지점에서 쿼터를 반복 소진할 수 있었다.

전날 실행에는 비동기 색인 작업 deadline 초과도 있었다.

## 적용한 변경

### Cloud Run 콜드 스타트 완화

`config/departments/cs.yaml`의 최소 인스턴스를 다음처럼 설정했다. 이 파일은 실제 운영 설정이며 Git 및 Cloud Build 업로드 대상에서 제외된다.

```yaml
minInstances:
  staff: 1
  student: 1
```

배포 결과:

- 교직원: `rag-mcp-cs-staff-00002-q5x`
- 학생: `rag-mcp-cs-student-00002-vv9`
- 두 서비스 모두 `minScale=1`, Ready, 100% 트래픽을 확인했다.

### Vertex RAG 목록 순회 안정화

`shared/rag_engine.py`를 다음과 같이 변경했다.

- 페이지 크기 100으로 명시
- `page_token`을 호출자가 보존
- 429 발생 시 실패한 현재 페이지만 재시도
- 페이지 사이 1.5초 간격 적용
- 페이지별 최대 10회 throttle 재시도

운영 검증에서는 교직원 코퍼스 1,779개를 18페이지, 학생 코퍼스 87개를 1페이지로 완주했으며 429가 발생하지 않았다.

### HTTP 및 Workflow 실패 의미 보존

- `/sync/delete`에서 최종 쿼터 소진을 HTTP 429로 반환하도록 변경했다.
- `workflows/daily_sync.yaml`의 모든 종료 경로를 `finalize_summary`로 모았다.
- `totals.failed > 0` 또는 `totals.indexFailed > 0`이면 Workflow 자체를 실패로 종료한다.
- 정상 종료만 `ok: true`를 반환한다.

### Cloud Build 비밀 파일 제외

`.gcloudignore`에 `config/departments/*.yaml`을 추가했다. 운영 YAML에는 API 키와 corpus/Drive 식별자가 있으므로 빌드 소스에 포함하면 안 된다.

확인 결과:

```text
upload_count=103
forbidden_count=0
```

학과 YAML, `.venv`, 진단용 임시 PDF는 업로드 목록에 없었다.

### RAG 매핑 백필

수정 전 dry-run은 93.4초 뒤 429로 실패했다. 수정 배포 후 결과는 다음과 같다.

- dry-run: 37.4초, 총 1,866건
- 교직원: 1,779건
- 학생: 87건
- 실제 Firestore 매핑 기록: 1,866건, skipped 0
- `RAG_MAPPING_FALLBACK_SCAN_ENABLED=true`는 안전장치로 유지했다.

### 텍스트 없는 PDF 반복 실패 방지

첫 복구 Workflow에서 비동기 색인 작업 `03762491d2114d2d9d7669d09b87bf2d`가 43개 URI 중 39개 성공, 4개 실패를 반복했다. 실패는 모두 `INVALID_ARGUMENT`였고 Vertex 메시지는 PDF가 유효하지 않거나 텍스트 페이지가 없다는 내용이었다.

실패한 4개 PDF를 읽기 전용으로 검사한 결과는 다음과 같다.

| 파일 ID | 페이지 | 추출 텍스트 | 암호화 |
|---|---:|---:|---|
| `1-NS_0H4PKbvMPjbxITQk1AnCSOjBmO_w` | 3 | 0자 | 아니오 |
| `1DRvfWoV-CEwHCd1A266VPY1NDoWfBeEF` | 10 | 0자 | 아니오 |
| `1fHUs3E9kB1cOHI41_vZ9Voa7saHn-Ww4` | 1 | 0자 | 아니오 |
| `1yDgzSpTVzZdkQR-2euX2vG30gjLzVRgv` | 1 | 0자 | 아니오 |

모두 구조상 읽을 수 있는 비암호화 PDF였지만 전 페이지가 이미지 스캔본이었다.

`services/sync/main.py`에 PDF 사전 판정을 추가했다.

- 한 페이지라도 텍스트가 있으면 기존처럼 원본 PDF와 경로 사이드카를 색인한다.
- 전 페이지에 추출 텍스트가 없거나 PDF를 읽지 못하면 원본을 Vertex import 목록에서 제외한다.
- 대신 파일명과 Drive 경로가 든 `.meta.md` 사이드카를 색인한다.
- 상태의 `error`에는 `PDF_NO_EXTRACTABLE_TEXT` 또는 `PDF_UNREADABLE`을 남긴다.
- 이렇게 하면 문서는 파일명·경로로 검색 가능하고, 같은 영구 실패가 정상 파일 39건까지 반복 재색인시키지 않는다.

향후 본문 OCR이 필요하면 `rag-parser`의 Document AI 경로를 일반 PDF에도 확장하는 별도 작업이 필요하다. 현재 변경은 반복 장애를 끊는 안전 폴백이다.

## 빌드와 배포 기록

### 1차 sync 안정화 배포

- Cloud Build ID: `a81818b3-46c4-48b5-9aa5-9b746af66256`
- 이미지 digest: `sha256:e2260fcb2ca9f2196539517df1e1185c00ea04c0692112e72a41e064d0e964bf`
- Cloud Run revision: `rag-sync-00020-txs`

### Workflow 배포

- Workflow: `rag-daily-sync`
- revision: `000009-3da`
- 상태: `ACTIVE`

### 스캔 PDF 폴백 배포

- Cloud Build ID: `417b12cd-ac68-475e-b30e-35e6317e0abc`
- 이미지 태그: `sync:scanned-pdf-fallback-20260904`
- 이미지 digest: `sha256:356417265e3ee1ff660bc7998ce38ff22fe624abdf3afb65da03592720bde1f9`
- Cloud Run revision: `rag-sync-00021-m89`
- Ready, 100% 트래픽 확인

## 검증 결과

### 자동 테스트와 정적 검사

최종 로컬 결과:

```text
559 passed, 2 skipped, 2 xfailed
```

- 핵심 Ruff 검사(`E9`, `F`, `B023`) 통과
- `compileall` 통과
- `git diff --check` 통과
- Workflow YAML 파싱 및 finalizer 회귀 테스트 통과
- PDF 무텍스트 사이드카 폴백 회귀 테스트 추가 및 통과

### 런타임 확인

MCP health 3회씩 모두 HTTP 200:

- 교직원: 240ms, 99ms, 68ms
- 학생: 160ms, 74ms, 68ms

실제 MCP 검색:

- 교직원: HTTP 200, 약 1.35초, JSON-RPC 오류 없음
- 학생: HTTP 200, 약 1.14초, JSON-RPC 오류 없음

`rag-sync /health`도 인증 호출 HTTP 200을 확인했다.

## Workflow 실행 이력과 인계 상태

### 실패 원인을 재현한 실행

- execution: `e4ad6692-dd02-4fc0-9b9e-c2ecbfeaaac4`
- 시작: `2026-09-04T02:56:22Z`
- 종료: `2026-09-04T03:20:31Z`
- 최종 상태: `FAILED`
- 원인: 위 인덱스 작업이 스캔 PDF 4건 때문에 deadline 전에 완료되지 못함

이 실행이 실제로 `FAILED`로 표시된 것은 Workflow finalizer 수정이 의도대로 동작한 결과다.

### 빈 인자 검증 실행

- execution: `c35d4ac1-7728-4837-a331-e98ef8742a00`
- 상태: `SUCCEEDED`
- 입력이 `{}`라 Drive 0건만 처리했다. 운영 종단 검증 결과로 간주하면 안 된다.

### 현재 진행 중인 운영 인자 실행

- execution: `052e202e-c6ba-4030-8fa3-726c6482549d`
- 시작: `2026-09-04T03:34:48Z`
- 문서 작성 시 상태: `ACTIVE`
- 마지막 단계: `do_ingest`
- 입력: Scheduler와 동일한 운영 `syncUrl`, `parserUrl`, Drive ID 목록
- 새 revision의 확인 시점까지 Cloud Run ERROR 로그 0건

사용자의 “여기까지만” 요청에 따라 이 실행을 취소하지 않았고 추가 모니터링도 중단했다. Workflow는 GCP에서 계속 진행할 수 있으므로 다음 작업 시작 시 가장 먼저 최종 상태를 확인해야 한다.

## 다음 확인 순서

1. execution `052e202e-c6ba-4030-8fa3-726c6482549d`의 최종 상태와 결과 집계를 확인한다.
2. `rag-sync-00021-m89` 로그에서 스캔 PDF 4건의 `사이드카만 색인한다` 경고를 확인한다.
3. 새 비동기 index job이 `DONE`이고 `failed=0`인지 확인한다.
4. Workflow 결과에서 `failed=0`, `indexFailed=0` 및 pageToken 커밋 로그를 확인한다.
5. 교직원·학생 MCP에서 파일명 또는 경로로 스캔 PDF가 검색되는지 확인한다. 본문 검색은 OCR 미적용 상태이므로 기대하지 않는다.
6. OCR 본문이 필요하면 Document AI 일반 PDF 폴백을 별도 설계·비용 검토 후 구현한다.

## 작업 트리 주의사항

이 작업은 커밋하지 않았다. 문서 작성 시 변경된 추적 파일은 다음과 같다.

- `.gcloudignore`
- `services/sync/main.py`
- `shared/rag_engine.py`
- `tests/test_dept_config.py`
- `tests/test_ingest_direct.py`
- `tests/test_sync_fixes.py`
- `tests/test_workflow_source_recovery.py`
- `workflows/daily_sync.yaml`
- `docs/OPS_RECOVERY_2026-09-04.md`

`config/departments/cs.yaml`은 실제 운영 설정이라 ignore 대상이며 Git 상태에 나타나지 않는다. 교직원·학생 `minInstances: 1` 변경을 덮어쓰지 않도록 주의한다.

GUI의 다른 환경 복원과 GCP 실시간 설정 연동은 별도 문서
[`GUI_CLOUD_CONFIG_SYNC.md`](GUI_CLOUD_CONFIG_SYNC.md)에 정리했다. 현재 GUI는
로컬 YAML을 원본으로 사용하므로, 동일한 GCP 관리자 로그인만으로 설정이 자동
복원되는 상태는 아니다.
