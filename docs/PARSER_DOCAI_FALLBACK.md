# 학과별 Doc AI fallback

PNG/JPEG는 별도 [이미지 OCR 옵션](IMAGE_OCR.md)을 사용한다. 이 문서의
HWP/HWPX fallback을 켜는 것만으로 이미지 OCR이 활성화되지는 않는다.

학과 생성·수정 화면의 **고급 설정 → HWP/HWPX 품질 보완 (DocAI fallback)**에서 켜거나 끈다.
기본값은 끄기이며, 기존 학과도 설정이 없으면 끄기로 처리한다.
검토 화면과 YAML 미리보기에도 선택값이 표시된다.

Document AI는 HWP/HWPX를 직접 지원하지 않는다. 현재 구현은 LibreOffice로
PDF 변환을 시도한 뒤 성공한 PDF만 `application/pdf`로 Document AI에 전송한다.
HWP/HWPX 전체에 대해 PDF 변환이 검증된 것은 아니므로, 옵션을 켜는 것만으로
모든 한글 문서의 재처리를 보장하지 않는다.

대시보드에서 학과를 클릭해 연 상세 패널 맨 아래의 **고급 관리 → HWP/HWPX 품질 보완 (DocAI fallback)**을
변경하고 **HWP/HWPX fallback만 저장**을 누른다. `/docai-fallback`은 해당 옵션만 변경하며
이미지 OCR 선택값과 코퍼스·버킷·키 등은 보존한다. 이미지 OCR은 별도 항목·저장 버튼을 사용한다.

학과 등록부의 `enableDocaiFallback` boolean 값은 `DEPARTMENTS_JSON`을 통해
Sync로 전달되고, Sync는 Parser 요청에 해당 학과의 값을 명시한다.
켜면 품질 게이트의 fallback 모드를 사용한다. 끄면 공용 fallback 모드는
log로 처리하며, 공용 log/reject 모드는 유지한다. 학과 간 설정은 공유하지 않는다.
옵션을 생략한 이전 Parser 직접 호출은 기존 환경변수 설정을 따른다.

## 운영 준비

- **운영 환경 → 공통 설정 · Document AI**에서 Layout Parser를 조회·선택하거나
  없으면 생성한 뒤 저장한다. [공통 셋업 안내](COMMON_DOCAI_SETUP.md)를 따른다.
- Parser의 `DOCAI_PROCESSOR_ID`, `DOCAI_LOCATION`에 사용할 프로세서를 설정한다.
- Document AI API와 Parser 서비스 계정의 프로세서 호출 권한이 필요하다.
- Parser 이미지를 다시 빌드·배포한다. 이미지에는 PDF 변환용 LibreOffice Writer와
  한글 폰트가 포함되며, 동시 요청은 별도 LibreOffice 프로필을 사용한다.
- Sync도 새 버전으로 배포해야 학과별 선택값이 Parser에 전달된다.
- 학과 설정 저장 시 기존 배포 흐름에서 Sync 학과 맵을 갱신한다.

Doc AI 처리는 추가 비용이 발생할 수 있다. PDF 변환 지원 여부와 변환 품질은
실제 HWP/HWPX 문서로 확인해야 한다. 변환 실패는 `FALLBACK_FAILED`로 DLQ에 기록된다.
이 옵션은 기존 품질 게이트 fallback에 적용되며, 원본 파싱 예외와 빈 본문은
기존처럼 각각 `PARSE_FAILED`, `EMPTY_TEXT`로 처리한다.

공용 환경변수만으로 직접 호출을 운영할 때는 기존과 같이
`QG_MODE=fallback`, `ENABLE_DOCAI_FALLBACK=true`가 모두 필요하다.
