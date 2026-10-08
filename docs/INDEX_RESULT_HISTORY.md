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

## 확정된 파일 실패를 분리한 실행 완료

파일별 반입 결과가 실패로 확정돼도 같은 묶음의 다음 파일을 처리한다. 모든 필수
코퍼스 처리가 끝나면 작업 자체는 DONE으로 반환하되 result.partial 및 failedFileIds로
부분 실패를 구분한다. 실패 파일은 doc_state/{fileId}/index_failures/{jobId}에 원문
버전·라우팅 버전·GCS URI·코퍼스별 결과를 보관하고 PARSED 상태를 유지한다.
이 기록은 작업 완료와 같은 Firestore 트랜잭션에서 저장한다.

Workflow는 성공 파일만 INDEXED 로그를 남기고 실패 파일은 INDEX_FAILED로 기록한다.
성공 URI 수와 별도 보관된 실패 URI 수를 서버의 완료 작업 기록과 대조한 뒤에만
토큰을 커밋하고 다음 페이지로 진행한다. 최종 반환은 totals와 ok=false를 포함하며
GUI는 이를 ‘완료 · 일부 실패’로 표시한다. 재처리는 기존 선택 문서 재처리 또는
미색인 복구 경로를 사용하며, 완료된 묶음의 자동 재전달로 반복 반입하지 않는다.
네트워크 결과 불확실·작업 시간 초과·문서 버전 충돌은 이 정상적인 부분 실패
경로와 구분하며, 기존 실행 오류 및 잠금 보호를 유지한다.

검증: Python 전체 917 passed / 36 skipped / 2 xfailed, GUI 38 passed.
Workflow revision 000011-b79로 컴파일·배포 완료.
Cloud Build e19188ce-7522-41ba-8658-3fe8bd653c8d SUCCESS.
Sync 이미지 sha256:23e34d247ece17ac39fa846d2e1052b1110e935caea599e463c1c59adcd0772e.
실제 문서 반입을 유발하는 동기화는 검증 목적으로 실행하지 않았다.

Sync revision rag-sync-00033-pm6 Ready / 트래픽 100%, health ok 및 배포 API 스키마 확인.

## Drive 시각 표시

동기화 실행 상세의 파일 처리 내역에서 Drive 생성·수정·동기화 처리 시각을
한국 시간으로 구분한다. 생성 시각은 Drive createdTime이며 폴더 이동 시각이 아니다.
Sync 변경분 응답에 createdTime을 추가해 이후 Workflow 파일 로그에도 보존한다.
과거 로그에 없는 시각은 현재 값으로 추정하지 않고 ‘기록 없음’으로 표시한다.
관련 Python 139 passed / 28 skipped, GUI 39 passed.
Cloud Build 5f49c4c6-0a36-4ec1-9adc-51714a46a4ec SUCCESS.
최신 로컬 GUI: http://127.0.0.1:8767.

Sync revision rag-sync-00034-b6p Ready / 트래픽 100%, health HTTP 200 / ok.


## 2026-10-07 실제 OCR·부분 실패·검색 검증

- 배포된 Parser 00009-vj5의 /ocr가 404임을 실제 호출로 확인했다.
- OCR 테스트 28개 통과 후 현재 Parser 코드를 빌드·배포했다.
- Cloud Build c4b52c54-56a7-4e20-bc58-31dfe1fb0626 SUCCESS.
- Parser revision rag-parser-00010-cn5, 트래픽 100%.
- 이미지 digest sha256:9207bf172d53b494edc8c1a1c571b46e19f8460c9e27af85f458304afb89d212.
- 고유 접두사 smokec5ba51e4e7344d1a 합성 자료만 사용했다.
- PNG를 GCS에 업로드하고 배포된 /ocr 호출: HTTP 200, IMAGE_DOCAI.
- 실제 OCR 본문에서 QUARTZ ORCHID 7429, Cedar 314, cobalt lantern 확인.
- 정상 TXT → 빈 DOCX → OCR Markdown 순서로 실제 Cloud Tasks 색인 작업을 제출했다.
- job d5d7d242be5947798b212f4fa5466364: DONE, partial=true, count=2, failed=1.
- 정상/OCR 문서는 INDEXED, 빈 DOCX는 PARSED 및 index_failures 영수증 1개.
- faculty 파일 결과는 DONE/FAILED/DONE. 빈 파일 실패 후 OCR 파일 처리가 계속됐다.
- 교직원 코퍼스 실제 검색에서 정상 문서 청크 3개와 OCR 문서 청크 1개를 확인했다.
- OCR 검색 결과에서 Cedar 314 및 cobalt lantern 본문을 확인했다.
- 테스트 문서는 STAFF 전용이며 학생 코퍼스 반입은 0건이었다.

검증 범위: 실제 배포 OCR → GCS Markdown → Cloud Tasks 색인 → RAG 검색 및 파일별 상태 저장.
Drive 감지·다운로드와 Workflow 전체 실행은 이번 검증에서 실행하지 않았다.
cs 학과 enableImageOcr는 꺼져 있으며, 학과 설정은 변경하지 않았다.
따라서 이 결과는 cs의 일반 PNG 동기화가 활성화됐다는 뜻은 아니다.
증거: tmp/smokec5ba51e4e7344d1a/{ocr,enqueue,job,states,retrieval,cleanup}.json.

정리 완료: 테스트 3개 /sync/delete 모두 HTTP 200, GCS 잔존 0건. 삭제 후 RAG 재검색에서 테스트 정상/OCR 문서 각각 0건 확인. 상태의 DELETED 기록과 검증 작업 이력은 보존했다. Parser /health HTTP 200 및 HWP/HWPX 엔진 ok 확인.
