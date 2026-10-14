# 게시글 폴더 metadata.json

본문·첨부파일과 같은 Drive 폴더에 UTF-8 metadata.json을 둔다.

```json
{
  "source_url": "https://example.org/posts/153759",
  "title": "게시글 제목"
}
```

- 직계 폴더의 파일 하나만 적용한다. 상위 폴더에서 상속하지 않는다.
- title/source_url만 읽고 그 외 키는 무시한다. 학생·교직원 권한은 Drive 폴더 설정이 결정한다.
- 없으면 기존 처리. 잘못된 JSON·주소·중복 파일·64 KiB 초과는 로그를 남기고 기존 처리한다.
- metadata.json은 초기·변경 목록에서 제외하며 직접 ingest/과거 큐도 EXCLUDED 처리한다.
- 원본 name/path/Drive sourceUri를 보존하고 Firestore에 sourceTitle/websiteUrl/metadataFileId를 저장한다.
- OCR/TXT/HWP 정규화 텍스트 및 바이너리 첨부의 경로 sidecar에 게시글 제목·주소를 반영한다.
- 검색 청크의 제목·주소는 해당 파일의 Firestore 메타데이터로 연결한다. RAG 청크별 custom attributes를 등록하는 방식은 아니다.
- MCP documents[].source에 title/websiteUrl을 추가한다. sourceUri는 원본 파일 주소를 유지한다.
- GUI 검색 결과에는 게시글 원문과 원본 파일 링크를 구분한다.
- 메타데이터는 ingest 때 읽으며, metadata.json 변경은 다음 변경분 동기화에서 자동 반영한다. 수동 재처리나 전체 동기화도 가능하다.
  metadata.json 변경은 다음 Drive 변경분 동기화에서 같은 폴더의 메타데이터 갱신 작업을 자동 생성한다.
  Cloud Tasks로 한 번에 5개씩 진행하며 기존 OCR Markdown을 재사용하므로 OCR을 다시 호출하지 않는다.
  메타데이터가 이동·삭제되면 기록된 이전 폴더도 갱신한다. 최초 도입 이전에 추적되지 않은 삭제 이벤트는 폴더 이력이 없을 수 있다.
- 동일 원본 수정 시각이어도 전체 동기화에서 title/websiteUrl이 바뀌면 UNCHANGED를 건너뛰고 재처리한다.

검증: Python 953 passed, 36 skipped, 2 xfailed. GUI build/regression 48 passed.

## 2026-10-14 운영 검증

- Build `758a8e89-9d38-443e-b1d1-9dfd7ebfd306` SUCCESS.
- Sync `rag-sync-00040-4wf`, MCP `rag-mcp-00034-2t5`에 반영.
- 학생 test_example/test_네이버클라우드_아카데미_소버린/한국공학대_포스터_55_2.png 재처리.
- 해당 metadata.json의 title/source_url 및 metadataFileId 저장 확인. 원래 Drive sourceUri 유지.
- 학생·교직원 색인 작업 DONE, 개별 실패 0.
- GUI 조회 함수 및 기본 운영 MCP URL에서 해당 문서의 청크 2개에 게시글 제목·주소 연결 확인.
- GUI Python API는 재시작 후 사용할 수 있다. 이번 작업에서 로컬 GUI 서버 재시작은 수행하지 않았다.

## OCR 텍스트 없음

OCR_EMPTY_TEXT는 SKIPPED / 처리 보류 · 텍스트 없음으로 저장하고 DLQ에서 제외한다.
자동 실패 복구는 과거 FAILED의 명시적 OCR_EMPTY_TEXT도 보류로 전환하며 OCR을 다시 호출하지 않는다.
변경 없는 전체 동기화 및 메타데이터 갱신에서도 OCR을 반복하지 않는다.
원본 Drive 수정 시각이 바뀌거나 수동 재처리(refreshContent=true)하면 다시 OCR할 수 있다.
이미지 없음·잘못된 응답·권한/통신 오류 등은 FAILED 처리를 유지한다.

## 자동 반영·보류 처리 검증

- metadata.json 생성·수정 시 다음 변경분 동기화에서 같은 폴더 갱신 작업을 Cloud Tasks에 넣는다.
- 목록 전체를 Workflow에 넘기지 않고 페이지당 5개씩 이어서 처리한다. 큐 등록 실패 시 Drive 토큰을 커밋하지 않는다.
- 잘못된 JSON은 기존 메타데이터를 유지한다. 조회 오류는 문서 변경 잠금을 정상 해제한 뒤 페이지를 재시도한다.
- 원본 파일 수정 시각이 바뀌었으면 메타데이터 작업은 덮어쓰지 않고 정상 동기화에 맡긴다.
- 학생 테스트 폴더의 저장 제목을 이전 값으로 두고 실제 Cloud Tasks로 갱신 검증: PNG/TXT 2개 INDEXED, 작업 DONE.
- Drive 원본을 수정하지 않았고 PNG의 GCS 세대도 그대로였다. OCR 본문을 재사용하여 Markdown만 갱신했다.
- 과거 OCR_EMPTY_TEXT 실패 1건을 SKIPPED로 보정하고 DLQ에서 제외했다.
- 운영 ingest API에 같은 수정 시각을 전달했을 때 SKIPPED / automaticRetry=false 응답 확인.
- 전체 Python: 967 passed, 36 skipped, 2 xfailed. GUI 회귀: 48 passed.

최종 자동 반영·OCR 보류 버전: `rag-sync-00042-czf` (트래픽 100%, health 200).
Build `b15f05f9-aa50-4103-afeb-d1faa3f12b13` SUCCESS.
정적 화면 변경까지 포함한 최종 GUI build/regression: 48 passed.
