# 공통 Document AI 설정

처음 공통 환경을 만들 때 또는 **운영 환경 → 공통 설정 · Document AI**에서 연다.
HWP/HWPX fallback의 Layout Parser와 PNG/JPG의 Enterprise Document OCR은
각각 선택 사항이다. 프로세서를 저장하거나 생성해도 학과 옵션은 자동으로 켜지지 않는다.

## 조회·선택·생성

1. GCP 프로젝트와 각 프로세서의 리전을 선택한다. Document AI 리전은 Cloud Run의
   서울 리전과 별개다. 문서는 선택한 프로세서 리전으로 전송된다.
2. 필요한 경우 **Document AI API 활성화**를 누른다. 조회 중 자동 활성화하지 않는다.
3. **기존 프로세서 조회**로 해당 프로젝트·리전의 모든 페이지를 조회한다.
   일치하는 종류의 프로세서만 표시하며, ENABLED 상태만 선택할 수 있다.
4. 기존 프로세서가 있으면 선택하고 **연결 확인**을 누른다.
   연결 확인은 현재 로그인 계정의 메타데이터 조회로 존재·종류·활성 상태를 확인한다.
   문서를 전송하지 않으며 OCR 품질이나 Parser 실행 계정의 처리 권한을 증명하지 않는다.
5. 해당 종류가 없고 API가 생성 가능한 종류·리전이라고 응답한 경우에만 생성 버튼을 연다.
   프로젝트·리전·종류·이름을 검토한 뒤 생성한다. 생성 직전에 목록과 가용성을 다시 조회한다.
   조회 실패(권한, API 미활성화, timeout 등)는 ‘없음’으로 취급하지 않는다.
6. 생성 결과가 불명확하면 자동 재시도하지 않는다. 다시 조회해 결과를 확인한다.
7. 공통 설정 저장 시 실제 프로세서를 다시 검증하고, 네 설정 키만 변경한다.
   다른 공통 값은 보존하며 화면을 연 후 파일이 변경됐다면 저장을 거부한다.

| 기능 | 종류 | 공통 설정 |
| --- | --- | --- |
| HWP/HWPX 품질 보완 | `LAYOUT_PARSER_PROCESSOR` | `DOCAI_PROCESSOR_ID`, `DOCAI_LOCATION` |
| PNG/JPG 텍스트 추출 | `OCR_PROCESSOR` | `DOCAI_OCR_PROCESSOR_ID`, `DOCAI_OCR_LOCATION` |

조회에는 `documentai.processors.list`, `documentai.processorTypes.get`,
연결 확인에는 `documentai.processors.get`, 생성에는 `documentai.processors.create` 권한이 필요하다.
Parser의 실행 계정에는 조회와 문서 처리 권한이 별도로 필요하다.
이 화면은 IAM 역할을 자동 부여하지 않는다.

## 저장 이후

- 최초 사용이면 새 Parser·Sync 이미지와 Workflow를 배포한다.
- 코드가 이미 배포됐다면 공통 런타임의 환경변수 반영으로 새 ID·리전을 전달한다.
  환경변수 차이 검사에 네 Document AI 키도 포함된다.
- 테스트 학과에서 필요한 옵션 하나를 켜고 샘플 한 건으로 실제 처리와 검색을 검증한다.
  [이미지 OCR](IMAGE_OCR.md), [HWP/HWPX fallback](PARSER_DOCAI_FALLBACK.md)을 각각 따른다.
- 설정 저장과 프로세서 생성은 실제 문서 처리 테스트와 구분한다.

## 과금 구조 (2026-10-02 공식 문서 확인)

기본 Enterprise Document OCR과 Layout Parser는 Google이 운영하는 관리형 API이며
처리 페이지 기준으로 과금된다. 이 생성 경로는 두 기본 프로세서만 만들고,
예약 처리 용량이나 커스텀 프로세서 버전의 호스팅 배포를 요청하지 않는다.
별도의 예약 용량·커스텀 프로세서에는 시간 기준 과금이 있을 수 있다.
[Document AI 공식 요금](https://cloud.google.com/products/document-ai/pricing)

‘서버리스’와 ‘호출 없으면 전체 비용 0’은 같은 뜻이 아니다.
배포 스크립트의 Parser·Sync는 Cloud Run 최소 인스턴스 0을 사용하지만, 실제 배포의
과금 모드·최소 인스턴스 설정은 따로 확인해야 한다. Cloud Run 호출·실행 비용,
Cloud Storage·Firestore·Artifact Registry 저장 비용, 네트워크·임베딩 비용은 별도다.
[Cloud Run 공식 요금](https://cloud.google.com/run/pricing)

특히 RAG Managed DB의 Basic/Scaled tier는 Spanner를 사용하며, 요청량과 별개로
프로비저닝된 DB 자원 비용이 발생할 수 있다. 따라서 시스템 전체를 순수 요청당
과금이라고 안내하면 안 된다. 이 작업에서는 운영 DB tier나 실제 청구서를 조회하지 않았다.
[RAG Engine 공식 과금 안내](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/rag-engine/rag-engine-billing)

## 이번 검증 범위

로컬 테스트에서 목록 페이지 처리, 조회 실패 차단, 생성 직전 재조회, 중복 요청 방지,
잘못된 종류 거부, 기존 설정 보존, 충돌 감지, 배포 환경변수 차이 검사를 확인했다.
실제 프로젝트의 us 리전에 두 종류의 메타데이터 조회를 실행했으며 조회 당시 목록은 비어 있고
생성 가능한 상태였다. 다른 리전의 존재 여부를 의미하지 않는다.
클라우드 프로세서 생성·유료 문서 처리·운영 배포는 실행하지 않았다.
