# MCP 검색 응답 v2

2026-09-10 구현 및 로컬 검증 완료. 교직원용 MCP에 배포·트래픽 전환·실서버 검증 완료.
학생용 MCP와 sync 수집 변환은 이번 배포에 포함하지 않았고, 기존 HTML 코퍼스 재색인도 아직 수행하지 않았다.

## 변경 이유

기존 `search`와 `answer`는 모두 질문을 받아 같은 검색을 실행했다. `answer`는
검색 결과를 재사용하는 후속 도구가 아니었다. 두 도구의 연속 호출을 유도할 수 있는
역할 중복을 제거하고, 필요한 근거와 출처를 단일 `search` 응답에 담는다.

## 계약

공개 도구는 `search(query, top_k?, drive_id?)` 하나다. `top_k`는 최대 문서 수이며
기본 5, 요청 상한 20이다. 서버의 전체 청크 예산도 적용되므로 결과는 이보다 적을 수 있다.

```json
{
  "schemaVersion": 2,
  "documents": [
    {
      "citationId": 1,
      "source": {
        "fileId": "drive-file-id",
        "name": "content.html",
        "label": "성적우수장학금 / content.html",
        "path": "Drive/장학금/content.html",
        "bundle": "성적우수장학금",
        "sourceUri": "https://drive.google.com/file/d/drive-file-id/view",
        "modifiedTime": null,
        "driveId": "drive-id"
      },
      "chunks": [
        {"text": "검색된 근거 구간", "score": 0.12, "scoreType": "vertex_raw"}
      ]
    }
  ],
  "documentCount": 1,
  "chunkCount": 1
}
```

- `citationId`는 이번 응답 안에서 문서를 인용할 번호다. 영구 식별자는 `source.fileId`다.
- `sourceUri`는 일반 문서에는 Drive 미리보기 링크를, HWP/HWPX에는 Drive 다운로드
  링크를 반환한다.
- `chunks`는 실제 선택된 검색 청크다. 원문 전체·연속 구간·원문 순서를 보장하지 않는다.
  문자열 구분자를 역으로 세지 않고, 선택 시점의 청크와 점수를 보존한다.
- 문서당 최대 청크 수와 전체 청크 수는 서버 설정으로 제한한다. 기본 3개/15개다.
  예전처럼 top_k가 전체 예산보다 클 때 전체 상한을 초과하지 않는다.
- 문서 관련도는 배열 순서로 읽는다. `score`는 Vertex 원값이며 답변 확률이 아니다.
- `documentCount`와 `chunkCount`는 각각 반환 문서 수와 청크 수다. 빈 결과는 둘 다 0이다.
- `answer`, `context`, 별도 `citations` 목록, 잘못 명명한 `chunk_count`를 제거했다.
  키워드 매칭을 답변 가능성처럼 표현한 `coverage`, `uncoveredTerms`와
  `matchedTerms`/`missingTerms`도 공개 응답에서 제거했다. 어휘 재정렬은 유지한다.
- SDK는 같은 응답을 `structuredContent`와 `content` 텍스트로 직렬화할 수 있다.
  클라이언트는 구조화 응답을 우선 사용하고 텍스트는 대체 경로로 사용해야 한다.
  이것은 도구 실행 두 건이 아니다. 응답 내부에 통합 본문을 다시 복사하지 않는다.

## HTML

수집 시 `text/html`을 텍스트로 변환한 뒤 breadcrumb, 해시, 크기 검사, GCS 적재를 한다.
제목, 목록, 링크, 표의 행/열 관계를 보존하고 script/style/숨김 컨트롤/레이아웃 속성을 제거한다.
짧은 병합 셀은 행마다 반복하고 긴 공통 조건은 표 안의 명시적 셀 참조와 설명으로 보존한다.
서로 다른 청크에 참조와 설명이 분리되면 한 청크만으로 전체 조건을 알 수 없다는 한계는 남는다.

기존 HTML 색인은 원본 MIME/파일명이 HTML인 결과에 한해 검색 응답에서도 정제한다.
일반 텍스트·프로그래밍 문서의 HTML 예제는 이 변환 대상으로 삼지 않는다.
응답 정제만으로 기존 벡터·색인 청크가 바뀌지는 않는다.

기존 자료 재색인에는 원본 재수집이 필요하다. sync 배포 후 대상 HTML 파일의 현재
Drive 메타데이터와 `refreshContent: true`로 `/sync/ingest`를 호출하면 수정 시각이
같아도 정규화 파일을 다시 만든다. 원래 `modifiedTime`은 그대로 전달한다.
`GCS_READY` 응답의 URI를 기존 색인 경로로 처리한다. 단순 `/sync/reindex-pending`은
기존 GCS 파일을 다시 색인할 뿐 HTML 변환을 다시 실행하지 않는다.
일반 수집에서는 `refreshContent` 기본값 false이며 기존 변경 감지를 유지한다.

## 배포 및 검증

이 변경은 응답 계약 변경이다. 배포 후 연결 클라이언트의 도구 목록/스키마를 갱신해야 한다.
기존 응답 배열 또는 `answer`에 의존하는 호출자는 v2 계약으로 전환한다.
`scripts/eval_golden.py`와 지연 측정 스크립트는 v1/v2 응답을 모두 읽는다.

1. MCP와 sync 이미지를 검증된 설정으로 배포한다. 기존 HTML의 응답 정제는 MCP만으로 적용된다.
2. `tools/list`에 `search`만 있는지, 실제 `tools/call`에 v2 응답이 나오는지 확인한다.
3. 동일 6개 질문으로 지연을 재측정하고 골든셋으로 검색 품질을 비교한다.
4. HTML 재수집·재색인은 대상 목록을 확정한 후 기존 색인 작업 흐름으로 진행한다.

로컬 프로토콜 테스트는 실제 ASGI `/mcp`를 호출하되 RAG/Firestore만 가짜로 대체한다.
검증 범위는 도구 노출, 검색 1회, 인용/청크 경계, 정확한 개수, 빈 응답, 캐시,
숨김 문서 필터, 전체 청크 상한, HTML 조건/금액 보존과 재수집 경로다.
운영 지연·코퍼스 전체 recall 개선은 배포 전 로컬 테스트만으로 주장하지 않는다.

최종 검사: 전체 pytest 658 passed, 6 skipped, 2 xfailed. 변경한 Python 파일의
Ruff E9/F/B023, compileall, git diff --check 통과. 저장소 전체 Ruff 검사에는
이번 작업에서 수정하지 않은 `scripts/bench_hwp_corpus.py`의 기존 미사용 import 1건이 남아 있다.

사용자가 제공한 응답의 첫 문서에 대한 오프라인 HTML 정제 비교:
4,452자 → 1,285자(71.1% 감소). 학번·학점·평점·장학금액·등록휴학 표현의 보존을 확인했다.
이는 해당 문서의 문자 수 비교이며 전체 응답·토큰 수·실서버 지연 감소율이 아니다.
결과: `tests/_bench_out/search_v2_html_comparison.json`.


## 교직원 MCP 배포 결과 (2026-09-10)

- 서비스: `rag-mcp-cs-staff`, 기존 URL 유지.
- 이전 리비전: `rag-mcp-cs-staff-00002-q5x`.
- 배포 리비전: `rag-mcp-cs-staff-search-v2-0910`, 트래픽 100%.
- 빌드: `cd0c445b-a23f-482a-b844-147291b16d4e`.
- 이미지 digest: `sha256:a7a1afcbfc92dd7eab44b51a663a64349462829e1237c1686ce8b095b7038681`.
- 환경변수(키 포함)와 서비스 계정은 이전 리비전과 동일함을 메모리에서 비교했다.
- 실서버 `tools/list`: `search` 하나. 문서/청크 수와 인용 ID·Drive 출처 검증 통과.
- 동일 6개 질문: 구/신 버전의 반환 문서와 순위 모두 일치.
- 순차 6회 평균: 검색 1.631초 → 1.292초, 연결 초기화 포함 2.016초 → 1.923초.
  캐시·부하를 통제한 통계 실험이 아니므로 속도 개선율로 해석하지 않는다.
- 추가 장학금 질의: 동일 문서 순위를 유지하며 해당 HTML 본문 1,933자 → 450자.
  새 본문은 구버전 본문을 정제한 결과와 정확히 일치했다. 이 질의에서는 두 버전 모두
  `15학점`을 포함한 앞 청크가 검색되지 않았다. 기존 청크 검색 범위의 한계이며,
  HTML 정제나 도구 통합이 모든 질문의 근거 충분성을 해결했다는 뜻은 아니다.
- 검증용 구/신 리비전 태그는 검증 후 제거했다.
- FactChat은 연결의 도구 목록/스키마를 다시 불러와야 새 계약을 사용한다.

근거 파일: `tests/_bench_out/search_v2_live_comparison.json`,
`latency_staff_v1_before_deploy.json`, `latency_staff_v2_after_deploy.json`,
`search_v2_deployment.json` (모두 같은 디렉터리).

롤백이 필요하면 다음 명령으로 기존 MCP 리비전에 트래픽을 되돌린다.

```powershell
gcloud run services update-traffic rag-mcp-cs-staff --project=tuk-mcp-rag --region=asia-northeast3 --to-revisions=rag-mcp-cs-staff-00002-q5x=100
```
