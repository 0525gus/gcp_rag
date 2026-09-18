# 현재 날짜 필드 운영 배포 — 2026-09-18

## 배포

- 서비스: `tuk-mcp-rag` / `asia-northeast3` / `rag-mcp`
- 운영 MCP: https://rag-mcp-42w47ff6ba-du.a.run.app/mcp
- 소스 커밋: `61b5187939a05f094a4fd8a7e91a479d2fb2df7f` (빌드 시 clean)
- Cloud Build: `3a40437f-fe71-407c-b626-c8bcec6a5987` (SUCCESS)
- 이미지: `asia-northeast3-docker.pkg.dev/tuk-mcp-rag/rag-mcp/mcp@sha256:9d030a4fa6a2cceb5a99a010e50774527d5c4985df3e16b36083dd6acd007bf1`
- 이전 운영 리비전: `rag-mcp-unified-0918-061518`
- 새 운영 리비전: `rag-mcp-current-date-0918`, 트래픽 100%

`deploy_unified_mcp.py --cpu 2 --suffix current-date-0918 --no-traffic
--tag current-date-0918`로 먼저 배포하고 별도 주소에서 검증 후 운영 트래픽을 전환했다.
운영 주소에서 재검증한 후 이번 배포에 사용한 태그만 제거했다.

이전/신규 리비전의 환경변수는 `GIT_COMMIT`, `GIT_DIRTY`를 제외하고 동일했다.
CPU 2 / 메모리 1 GiB / 동시 요청 40 / 타임아웃 300초 / 서비스 계정도 동일했다.
검색 기본 문서 수 7, 캐시 TTL 60초를 유지한다.

## 검증

- 추가 로컬 검사: 배포 설정 및 검색 응답 계약 테스트 57개 통과.
- 검증용 주소와 운영 주소 모두 `tools/list`의 `currentDate`, `timeZone` 필드 확인.
- 활성 CS 교직원·학생 키로 각각 동일 질문을 두 번 호출.
- 두 응답 표현(`structuredContent`, `content` JSON)의 내용 일치 확인.
- 서버 응답 날짜가 검사 시점 한국 날짜 `2026-09-18`과 일치하고 시간대가
  `Asia/Seoul`임을 확인.
- 교직원: 호출마다 문서 7개 / 청크 12개. 학생: 문서 7개 / 청크 10개.
- 반환 문서의 드라이브가 해당 학과 범위에 속하고 학생 문서는 `STUDENT`임을
  Firestore 메타데이터와 대조.
- `/health`: HTTP 200, `auth=scoped_api_key`.
- 누락 키, 잘못된 키, 비활성 EE 교직원·학생 키: HTTP 401.

기존 `verify_unified_mcp.py`는 비활성 연결을 제외하지 않아 EE 키의 HTTP 401에서
종료했다. 이번 검증은 활성 연결의 성공/문서 범위와 비활성 연결의 접근 거부를
구분한 로컬 검증 스크립트로 수행했다. 키와 문서 본문은 기록하지 않았다.

자정·월말·연말 및 빈 검색 결과 처리는 앞선 로컬 회귀 테스트에서 검증했다.
운영 서버 시간을 변경하거나 실서버 캐시 적중을 계측하지는 않았다.
FactChat UI에서의 도구 스키마 갱신 및 실제 답변 동작은 이 검증에 포함되지 않는다.

## 롤백

```powershell
gcloud run services update-traffic rag-mcp --project=tuk-mcp-rag --region=asia-northeast3 --to-revisions=rag-mcp-unified-0918-061518=100 --quiet
```
