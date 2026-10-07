# 선택 문서 재처리

**색인 보류·오류 → 학과 선택 → 문서 클릭 → 이 문서만 재처리**에서 실행한다.
목록은 읽기 전용이며 문서를 열거나 새로고침하는 것만으로 처리하지 않는다.
상세 창에서 원본, 개별 RAG 결과, 관련 배치와 재처리 실행 결과를 확인한다.

## 동작

- GUI가 Cloud 등록부와 문서 상태로 학과 범위를 확인한다.
- `rag-daily-sync`의 `reprocessFile` 분기는 변경분 조회·전체 적재·토큰 커밋을 하지 않는다.
- Sync의 `/sync/reprocess-file`이 현재 Drive 메타데이터와 수집 폴더를 다시 확인한다.
  삭제·이동된 문서, 정상 색인된 문서, 진행 중인 색인 작업은 재처리를 거부한다.
- 현재 학과의 OCR/fallback 옵션으로 최신 원본을 정규화한다. 기존 보호·품질 게이트를
  그대로 적용한다. 새로 켠 옵션이 없는 미지원 문서는 다시 보류될 수 있다.
- 선택 문서 한 건으로 독립적인 Cloud Tasks 색인 작업을 만들고, Workflow가 완료를
  확인한다. 학생·교직원 대상 판정과 문서 버전 검증은 기존 색인 경로를 따른다.
- HTTP 접수만으로 색인 성공을 표시하지 않는다. `SKIPPED`, `DLQ`, `SPLIT_QUEUED`는
  재처리 완료로 간주하지 않으며, 본문 없이 색인된 문서는 계속 점검 대상으로 남는다.

## 중복과 불확실한 결과

기존 Firestore 문서 변경 잠금과 요청별 영속 기록을 사용한다. 동일한 요청 ID는
OCR/적재를 반복하지 않는다. 통신이 끊긴 변경 요청은 자동 재전송하지 않는다.
결과가 불확실하거나 변경 잠금이 남은 경우 실행 이력과 RAG 상태를 확인해야 한다.
운영자가 잠금을 강제로 해제하는 동작은 이 화면에서 제공하지 않는다.

접수한 작업은 GUI를 닫아도 Workflow에서 계속 진행된다. 상세 창을 다시 열면 최근
학과별 재처리 실행 최대 100건 중 해당 문서의 최근 3건을 조회한다. 더 오래된 이력이
있을 수 있으면 제한을 표시한다. 실행 기록 보존 기간은 Workflows 정책을 따른다.
실행 중에는 10초마다 상태를 조회하며 조회 실패 시 수동 새로고침으로 재확인한다.

## 배포와 검증

새 Sync 이미지와 `workflows/daily_sync.yaml`을 함께 배포해야 한다. GUI는 구버전
Workflow가 `reprocessFile` 인자를 무시해 일반 동기화를 시작하지 않도록 배포 소스를
확인하고 지원되지 않으면 접수를 차단한다. `-ReuseExisting`이나 환경변수만 갱신하는
배포로는 새 Sync 코드가 반영되지 않는다.

실행 버튼은 OCR 및 색인 비용을 발생시킬 수 있다. 첫 검증은 운영자가 의도한 문서
한 건으로 진행하고 원본·처리 결과·검색을 대조한다.

회귀 검증:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_selected_reprocess.py tests/test_index_issues.py tests/test_index_tasks.py tests/test_workflow_source_recovery.py -q
cd gui
npm ci
npm test
```

`npm test`는 빌드와 `tests/*.test.mjs` 전체를 실행한다. GitHub Actions의 `gui` 작업도
동일한 명령을 사용한다. 로컬 빌드가 이미 있으면 `npm run test:regression`으로 재검증한다.

실행 이력 API의 필터와 조회 범위는 [Workflows 실행 목록 API](https://docs.cloud.google.com/workflows/docs/reference/executions/rest/v1/projects.locations.workflows.executions/list)를 따른다.

### 2026-10-06 운영 반영 확인

- 프로젝트 `tuk-mcp-rag`, 리전 `asia-northeast3`.
- Sync revision: `rag-sync-00031-fd5` (Ready, 트래픽 100%).
- Sync image digest: `sha256:3dac680c96cc41d9633ddffc15bcb6cbbee7bf17b70319405a36707b1af5a174`.
- Workflow revision: `000010-513` (ACTIVE, 선택 문서 분기 컴파일 완료).
- Cloud Build: `5cb44a11-83ae-4200-a515-3986f4bd0669` (SUCCESS).
- Sync health 및 OpenAPI의 새 경로, 실제 학과의 읽기 전용 재처리 이력 API를 확인했다.
- Python 전체 905 passed / 36 skipped / 2 xfailed 이후 추가 이력 범위 테스트를 포함한
  재처리 전용 19개가 통과했다. GUI 빌드 및 회귀 테스트 33개가 통과했다.
- 실제 문서 재처리·OCR 호출은 실행하지 않았다. 연결된 브라우저가 없어 시각적 화면
  검증은 수행하지 않았으며, UI 동작은 자동 회귀 테스트로 확인했다.
- GitHub Actions 실행 자체는 커밋/푸시 후 확인한다.
