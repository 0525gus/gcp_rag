# 문서별 색인 결과 및 과거 실행 재검토

비동기 색인은 문서별·코퍼스별 결과를 `sync_jobs/{jobId}/parts/{part}/fileResults`에
저장한다. 문서별 결과는 `doc_state/{fileId}/index_results/{jobId}`에도 별도로 저장한다.
`DONE`, `FAILED`, `PENDING`을 구분하며 문서 버전과 라우팅 버전을 함께 기록한다.
필수 코퍼스가 모두 완료된 문서만 `doc_state.status=INDEXED`로 확정한다.
같은 작업의 재전달은 완료된 파일을 다시 삭제하거나 반입하지 않는다.
RPC 결과가 불확실하면 기존 mutation lease 보호를 유지하며 성공으로 추정하지 않는다.

부분 실패 구분을 위해 반입은 원문 파일별로 수행한다. 원문 하나의 본문·메타데이터는
함께 확인하며 메타데이터만 성공한 경우 본문 완료로 간주하지 않는다. 대량 배치에서는
반입 호출 수와 처리 시간이 증가할 수 있다. 기존 작업 제한 시간은 변경하지 않았다.

## 과거 실행

`sync_run_reviews/{executionId}`는 당시 작업의 파일별 재검토 결과다. 원래 Workflow
실패 상태와 기존 작업 체크포인트는 변경하지 않는다. 과거 결과를 현재 문서의
완료 상태로 덮어쓰지도 않는다. 새로운 실패 실행은 상세 조회 시 파일별 체크포인트를
읽어 표시한다. 실행 상세에는 완료·실패·일부 완료·확인 필요를 분리한다.
삭제와 미지원 등을 포함한 처리 기록 수는 이 색인 묶음의 고유 문서 수와 별개다.

재검토 도구는 기본 읽기 전용이며 `--apply`일 때 재검토 문서만 저장한다:

```powershell
.venv/Scripts/python.exe scripts/reconcile_sync_history.py --project tuk-mcp-rag --output tmp/history-review.json
.venv/Scripts/python.exe scripts/reconcile_sync_history.py --project tuk-mcp-rag --output tmp/history-review.json --apply
```

기본 범위는 최근 20개 실행이다. 파일별 체크포인트, 완료된 코퍼스 작업 또는 당시
작업 시간대의 정확한 URI·코퍼스 반입 결과를 사용한다. 문서 버전이 달라지거나
결과가 빠진 경우 확인 필요로 남긴다. 학생 대상이 아닌 문서도 학생 코퍼스의
이전 자료 정리 결과가 없으면 전체 완료로 추정하지 않는다.

## 2026-10-07 적용

- 최근 20개 실행 중 실패 실행 14개의 재검토 결과를 운영 Firestore에 저장했다.
- 최신 실행 `458de478-8a9b-4efa-88a6-13aa635af10c`: 15개 중 실패 확인 1개,
  일부 완료 14개. 14개는 교직원 반입 성공이 확인되지만 학생 코퍼스의 정리 완료는
  확인되지 않는다. 실패 문서는 빈 DOCX 본문으로 양쪽 코퍼스 반입이 거부됐다.
- 과거 13개 실행은 파일별 근거 부족으로 확인 필요다. 실패 이력은 삭제하지 않았다.
- Sync revision `rag-sync-00032-f9p`: Ready, 트래픽 100%, `/health` HTTP 200 / ok.
- Cloud Build `bbf30934-139f-4274-8a1d-bbc377e051e1`: SUCCESS.
- 배포 이미지 digest: `sha256:17fc38b9b94a890e43cff578e0fc6714b5b1c6741a43bbaaac96692a91d5ebe4`.
- Python 전체 914 passed / 36 skipped / 2 xfailed. 이후 추가한 GUI API 경로 검증은
  관련 테스트 124 passed / 28 skipped. GUI 빌드 및 회귀 테스트 38 passed.
- 실제 문서 재처리·OCR·RAG 반입 호출은 이번 정리에서 수행하지 않았다.
- 기존 GUI 8765를 유지하고 최종 GUI 서버는 8766에서도 실행했다.
