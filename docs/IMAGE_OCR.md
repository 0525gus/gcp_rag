# 학과별 이미지 OCR — 기본 끄기

## 구현 범위

PNG/JPEG 단일 이미지에 한해 Document AI Enterprise Document OCR
(`OCR_PROCESSOR`)로 글자를 추출한다. HWP/HWPX fallback의 Layout Parser와
프로세서 설정을 분리했다. 이미지 설명 생성, 표 구조 복원, 스캔 PDF, TIFF,
애니메이션 이미지와 HWP/HWPX PDF 변환은 이 옵션에 포함하지 않는다.

| 구분 | HWP/HWPX DocAI fallback | PNG/JPG 이미지 OCR |
| --- | --- | --- |
| 실행 조건 | 기본 파싱 결과가 품질 기준에 미달 | 지원 이미지 수집 시 |
| 처리 | HWP/HWPX → PDF 변환 → Layout Parser | PNG/JPEG → Enterprise Document OCR → 텍스트 |
| 학과 옵션 | `enableDocaiFallback` | `enableImageOcr` |
| 프로세서 설정 | `DOCAI_PROCESSOR_ID`, `DOCAI_LOCATION` | `DOCAI_OCR_PROCESSOR_ID`, `DOCAI_OCR_LOCATION` |
| Parser API | `/parse` | `/ocr` |

두 옵션은 각각 기본 꺼짐이며 서로를 활성화하지 않는다. 한쪽만 켜서 사용할 수 있다.
추출 결과를 Markdown으로 전달한 이후의 RAG 청킹·색인 경로만 공유한다.
실제 검증도 HWP/HWPX PDF 변환·품질 보완과 이미지 OCR 인식 품질을 나눠 수행한다.

학과 생성·수정 또는 대시보드의 **학과 상세 → 고급 관리 → 이미지 텍스트 추출 (PNG/JPG OCR)**에서
선택한다. 등록부의 `enableImageOcr`는 boolean이며 생략 시 false다.
상세 화면의 **이미지 OCR만 저장**은 `/image-ocr`로 이 옵션만 변경하고 fallback 값을 보존한다.
`DEPARTMENTS_JSON` → 학과별 Settings → MIME 라우팅에 전달된다.
델타·전체 동기화·실패 재처리에서 같은 설정을 적용한다.
끄면 이후 OCR 호출을 멈추며, 이미 색인된 문서의 자동 삭제를 의미하지 않는다.

## 처리와 청킹

1. 학과의 수집 범위와 OCR 선택값을 확인한다. 꺼진 학과는 기존처럼 SKIP한다.
2. Drive 이미지 다운로드에는 최대 10MB 한도를 적용한다. 원본은 학과 원본 버킷에 보관한다.
3. Parser `/ocr`가 실제 PNG/JPEG 형식, 단일 프레임, 40MP 이하를 검증한다.
4. 설정한 프로세서의 종류가 `OCR_PROCESSOR`인지 확인하고 OCR을 호출한다.
5. 비어 있지 않은 OCR 텍스트만 Sync에 반환한다. Parser는 색인용 파일을 쓰지 않는다.
6. Sync가 기존 문서 제목·자료묶음 머리말을 붙이고, 해시와 크기를 확인한 뒤
   학과 source 버킷에 `{fileId}.md`를 저장한다. 내용이 같고 이미 색인됐다면 재색인을 생략한다.
7. 기존 RAG import·교직원/학생 코퍼스 분리·색인 완료 확인 경로로 넘긴다.
   RAG Engine이 텍스트를 청킹하고 임베딩한다. 기본은 1,024토큰/겹침 256토큰이다.

OCR 단계는 별도 청크를 만들지 않는다. 이미지 원본은 RAG import에 넣지 않는다.
OCR 텍스트를 Markdown에 저장한다고 표의 행·열 관계가 복원되거나 의미 단위 청킹이
보장되는 것은 아니다. 청크 경계에서 표가 잘릴 수 있으며 표 중심 문서는 별도 평가가 필요하다.

잘못된 파일, 빈 OCR 결과, 잘못된 프로세서, API 실패는 성공으로 색인하지 않고
기존 실패 큐로 전달한다. 파일별 실패 재처리는 기존 재시도 정책을 따른다.
Document AI SDK 내부의 `process_document` 자동 재시도는 끈다.
10MB는 이 구현의 보수적인 상한이며 Document AI 서비스 최대치와 구분한다.

## 운영 적용 전 필요한 작업

**운영 환경 → 공통 설정 · Document AI**에서 기존 OCR 프로세서를 조회·선택하거나
없으면 생성한 뒤 저장한다. [공통 셋업 안내](COMMON_DOCAI_SETUP.md)를 따른다.
수동 설정 시에는 `config/common.yaml`에 실제 OCR 프로세서의 ID와 리전을 명시한다.
값을 비워 두면 GUI에서 OCR 켜기를 거부하고, Parser도 호출을 거부한다.

```yaml
DOCAI_OCR_PROCESSOR_ID: "실제_OCR_프로세서_ID"
DOCAI_OCR_LOCATION: "실제_프로세서_리전"
```

- 해당 리전에 실제 `OCR_PROCESSOR`를 준비하고 Document AI API 및 Parser 실행
  서비스 계정의 프로세서 조회·처리 권한을 확인한다. GCP 기본 리전을 자동으로 쓰지 않는다.
- Parser와 Sync의 새 이미지를 배포하고 `rag-daily-sync` Workflow도 함께 갱신한다.
  기존 Workflow는 `IMAGE_OCR` 라우트를 처리하지 못한다. 환경변수만 갱신하면 충분하지 않다.
- 대상 학과 한 곳에서만 OCR을 켜고, 테스트용 PNG/JPEG를 수집 범위에 넣는다.
- 추출 텍스트와 원문을 대조하고, RAG 검색 결과의 본문·원본 링크·학과 격리를 확인한다.
  긴 문서는 검색된 청크 경계와 겹침도 확인한다.
- OCR 활성화 전에 이미 존재하던 이미지는 전체 동기화로 수집한다.
  옵션을 켜는 것만으로 과거 Drive 변경 이벤트가 다시 생성되지는 않는다.
- 검증 후 다른 학과로 확대한다. OCR 추가 비용과 해당 프로세서의 호출 한도도 확인한다.

현재 확인은 SDK 호출을 대체한 로컬 테스트와 프런트엔드 빌드까지다.
실제 프로세서 호출, OCR 인식 품질, 실제 RAG 청크 결과 및 컨테이너 실행은 별도 검증이 필요하다.

## 공식 근거 (2026-10-02 확인)

- [Document AI 지원 형식](https://docs.cloud.google.com/document-ai/docs/file-types): PNG/JPEG 지원.
- [프로세서 목록](https://docs.cloud.google.com/document-ai/docs/processors-list): OCR 전용 종류는 `OCR_PROCESSOR`.
- [Document AI 제한](https://docs.cloud.google.com/document-ai/limits): 온라인 요청 40MB, 이미지 40MP.
- [RAG Engine 지원 문서](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/rag-engine/supported-documents): Markdown 지원.
- [RAG 청킹 설정](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/rag-engine/layout-parser-integration): 청크 크기는 토큰 단위.
