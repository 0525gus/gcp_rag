# GCP RAG 학과 관리 GUI

학과별 YAML 생성과 배포 상태 확인을 위한 로컬 운영 콘솔입니다.

## 실행

저장소 루트에서:

```powershell
pip install -r requirements-gui.txt
python scripts/dept_gui.py
```

- 브라우저에서 `http://127.0.0.1:8765`가 열립니다.
- 외부 네트워크에는 bind하지 않습니다.
- MCP 키는 로컬 YAML과 MCP 배포 시 생성되는 Cloud Run 관리 주석에 저장됩니다. 브라우저용
  설정 API에서는 키·토큰 필드를 제거하며 콘솔은 `127.0.0.1`에만 바인딩합니다.

## 제공 기능

- 학과 상태 대시보드
- 다른 관리자 PC에서 Cloud Run 전체 설정 불러오기·수정·재배포
- 3단계 YAML 생성 wizard
- 기존 파일 덮어쓰기 방지
- LOCAL, RESOURCE, DEPLOY, RUNTIME, SYNC 검사
- 오프라인 설정 검사
- 학과별 상세 결과와 조치 안내

## GUI 회귀 테스트

Node.js 22.13 이상에서 CI와 동일하게 설치·빌드·테스트한다:

```powershell
cd gui
npm ci
npm test
```

`npm test`는 프런트엔드를 빌드한 뒤 `tests/*.test.mjs` 전체를 실행한다.
새 회귀 테스트도 이 디렉터리에 `.test.mjs`로 추가하면 자동으로 포함된다.
빌드가 이미 준비돼 있으면 `npm run test:regression`으로 테스트만 실행할 수 있다.

현재 검증 범위:

- 콘솔의 미정의 변수와 HTML 요소 연결
- 최초 공통 셋업 확인과 화면 진입
- Document AI 조회·설정 저장
- 탭 이동 시 불필요한 배포 및 모달 재표시 방지
- 색인 오류 조회·상태 필터·상세·선택 문서 재처리
- 학과 전환 후 늦은 응답 무시, 중복 클릭, 불확실한 실행 결과 처리
- 빌드된 진입 경로와 콘솔 산출물

[GitHub Actions CI](../.github/workflows/ci.yml)의 **GUI build and regression** 작업이
PR 및 `main` 푸시마다 `npm ci`와 `npm test`를 실행한다. Actions의 **Run workflow**로
수동 실행할 수도 있다. 빌드 또는 테스트 실패는 해당 CI 작업을 실패시킨다.
워크플로 변경과 테스트 파일을 원격에 반영해야 GitHub에서도 실행된다.

이 검증은 화면 로직과 빌드 산출물 대상이다. 실제 브라우저의 시각적 배치나
운영 Document AI 호출까지 검증하는 테스트는 포함하지 않는다.
