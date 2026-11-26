# Document AI — 이미지 OCR · HWP/HWPX 보완

두 기능은 서로 독립된 선택 사항이며 기본 꺼짐이다. 추출 결과를 Markdown으로 넘긴 뒤의
RAG 청킹(1,024토큰 / 겹침 256)·색인 경로만 공유한다.

| 구분 | 이미지 OCR | HWP/HWPX 품질 보완(fallback) |
|---|---|---|
| 대상 | PNG/JPEG 단일 이미지, 이미지 위주 PDF | 기본 파싱이 품질 게이트에 미달한 HWP/HWPX |
| 처리 | Enterprise Document OCR → 텍스트 | LibreOffice로 PDF 변환 → Layout Parser |
| 프로세서 종류 | `OCR_PROCESSOR` | `LAYOUT_PARSER_PROCESSOR` |
| 공통 설정 | `DOCAI_OCR_PROCESSOR_ID`, `DOCAI_OCR_LOCATION` | `DOCAI_PROCESSOR_ID`, `DOCAI_LOCATION` |
| 학과 옵션 | `enableImageOcrStaff` / `…Student` | `enableDocaiFallbackStaff` / `…Student` |
| Parser API | `/ocr` | `/parse` |

## 프로세서 설정

관리 콘솔 **운영 환경 → 공통 설정 · Document AI**에서 한다.

1. 프로젝트와 프로세서 리전을 고른다. Document AI 리전은 Cloud Run 리전과 별개이며 문서는 그
   리전으로 전송된다.
2. 필요하면 **Document AI API 활성화**를 누른다. 조회 중에는 자동 활성화하지 않는다.
3. **기존 프로세서 조회**로 종류가 맞는 ENABLED 프로세서를 고르고 **연결 확인**을 누른다.
   연결 확인은 메타데이터 조회라 OCR 품질이나 Parser 계정의 처리 권한까지 증명하지는 않는다.
4. 없을 때만 생성한다. 생성 직전에 목록을 다시 조회하고, 결과가 불명확하면 재시도하지 않고 다시
   조회한다. 조회 실패는 ‘없음’으로 취급하지 않는다.
5. 저장하면 네 설정 키만 바꾸고 다른 공통 값은 보존한다. 화면을 연 뒤 설정이 바뀌었으면 거부한다.

필요 권한: 조회 `documentai.processors.list`·`documentai.processorTypes.get`, 연결 확인
`documentai.processors.get`, 생성 `documentai.processors.create`. Parser 실행 계정에는 처리 권한이
따로 필요하며 콘솔은 IAM을 부여하지 않는다. 저장 후에는 공통 런타임 환경변수를 다시 반영한다.
프로세서 ID가 비어 있으면 콘솔은 옵션 켜기를, Parser는 호출을 거부한다.

## 학과·대상 옵션

- 학과 상세 → **고급 관리**에서 교직원·학생 대상별로 켠다. 저장은 바꾼 옵션 하나만 보내 다른 값을 보존한다.
- 예전 `enableImageOcr`·`enableDocaiFallback` 값은 양쪽 대상의 기본값으로 읽고, 대상별 값이 있으면 우선한다.
- 학생자료 폴더 안 문서는 STUDENT, 그 밖과 판정 실패는 STAFF다. 같은 문서의 처리 결과를 코퍼스마다
  다르게 만들지 않으며 STUDENT 문서 결과는 교직원 코퍼스에도 들어간다.
- 변경 목록에서는 어느 대상이든 켜져 있으면 후보로 넣고, ingest에서 대상을 판정한 뒤 꺼진 대상의
  다운로드·OCR을 막는다.
- 옵션은 이후 처리에만 적용된다. 끄더라도 이미 색인된 문서를 지우지 않고, 켜더라도 과거 변경을 다시
  만들지 않는다. 기존 이미지는 전체 동기화로 수집한다.

## 이미지 OCR

- Drive 다운로드 10MB 이하, Parser가 실제 PNG/JPEG·단일 프레임·40MP 이하를 확인한다.
- 비어 있지 않은 OCR 텍스트에 문서 제목·자료묶음 머리말을 붙여 `{fileId}.md`로 저장한다. 이미지 원본은
  RAG에 넣지 않는다. 표의 행·열 관계는 복원되지 않는다.
- 빈 OCR 결과는 `SKIPPED`(`OCR_EMPTY_TEXT`, 처리 보류 · 텍스트 없음)로 두고 DLQ에 넣지 않는다. 원본 수정
  시각이 바뀌거나 `refreshContent=true` 재처리 때만 다시 OCR한다. 그 밖의 실패는 `FAILED`로 실패 큐에 간다.
- Document AI SDK 자체 재시도는 끈다.

PDF는 Sync가 내려받아 판정한다.

| 조건 | 처리 |
|---|---|
| 100MB 초과(`MAX_GCS_BYTES`가 더 작으면 그 값) | 내려받지 않고 `SKIPPED` · `ADMIN_REVIEW_SIZE`. 파일이 바뀌면 다시 판정 |
| 이미지 위주(텍스트 20자 이상 페이지가 절반 미만) · OCR 켜짐 · 20MB·15쪽 이하 | OCR 본문 `.md`만 색인 |
| OCR 실패·한도 초과·옵션 꺼짐 | 텍스트가 있으면 원본 PDF, 없으면 파일명·경로 사이드카만 색인 |
| 텍스트 PDF | 원본 색인. 50MB 초과분은 페이지 경계로 분할 |

## HWP/HWPX 보완

- Document AI는 HWP/HWPX를 직접 받지 않는다. Parser 이미지의 LibreOffice Writer(한글 폰트 포함)로 PDF를
  만든 뒤 성공한 PDF만 보낸다. 모든 한글 문서의 변환이 검증된 것은 아니다.
- 학과 옵션을 켜면 품질 게이트가 fallback 모드로 동작한다. 끄면 공용 `QG_MODE`가 fallback일 때만 log로
  처리하고 log·reject는 그대로 둔다.
- 변환 실패는 `FALLBACK_FAILED`로 DLQ에 남는다. 원본 파싱 예외와 빈 본문은 기존처럼 `PARSE_FAILED`,
  `EMPTY_TEXT`다.
- 학과 옵션 없이 공용 환경변수로만 쓰려면 `QG_MODE=fallback`과 `ENABLE_DOCAI_FALLBACK=true`가 모두 필요하다.

## 처음 켤 때와 비용

- 한 학과·한 대상에서 먼저 켜고, 샘플 문서로 추출 텍스트·검색 결과·학과 격리를 원문과 대조한 뒤 넓힌다.
- 두 프로세서는 처리 페이지 기준으로 과금된다. 콘솔은 예약 용량이나 커스텀 프로세서를 만들지 않는다.
- 시스템 전체가 요청당 과금은 아니다. RAG Managed DB의 Basic/Scaled tier는 요청량과 별개로 Spanner 자원
  비용이 든다.

참고: [지원 형식](https://docs.cloud.google.com/document-ai/docs/file-types),
[프로세서 목록](https://docs.cloud.google.com/document-ai/docs/processors-list),
[제한](https://docs.cloud.google.com/document-ai/limits),
[요금](https://cloud.google.com/products/document-ai/pricing),
[RAG Engine 과금](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/rag-engine/rag-engine-billing)
