# 자료 대상별 Document AI 옵션

학과 설정에 다음 boolean 값을 선택적으로 저장한다.

- enableDocaiFallbackStaff / enableDocaiFallbackStudent: HWP/HWPX Layout Parser fallback
- enableImageOcrStaff / enableImageOcrStudent: PNG/JPEG 이미지 OCR

학생자료 폴더 안의 문서는 STUDENT, 그 밖/분류 불확실은 STAFF로 판정한다.
동일 문서의 파싱 결과를 코퍼스별로 다르게 만들지 않는다. STUDENT 문서의 처리 결과는 교직원 코퍼스에도 공유된다.
이 옵션은 앞으로 실행되는 파싱/OCR에 적용되며 기존 색인을 삭제하거나 자동으로 재처리하지 않는다.
암호 문서 거부와 기본 품질 게이트는 기존 동작을 유지한다.

이전 설정은 enableDocaiFallback 및 enableImageOcr를 양쪽의 기본값으로 읽는다.
명시한 대상별 값이 있으면 기존 전체 플래그보다 우선한다.
GUI는 신규 등록·전체 수정·학과 상세 고급 관리에서 대상별 옵션을 제공한다.
상세 저장은 선택한 옵션 하나와 configRevision만 전송해 다른 옵션을 보존한다.
OCR 활성화는 변경 목록에서 어느 쪽이라도 켜져 있으면 후보에 포함하고,
실제 ingest에서는 Drive 분류 후 다시 확인하여 꺼진 대상의 다운로드/OCR을 차단한다.
과거 큐가 IMAGE_OCR 경로를 직접 전달해도 같은 검사를 거친다.

## 2026-10-08 적용 상태

- 운영 Sync 배포 완료: `rag-sync-00035-hmz`, 트래픽 100%, Ready 및 `/health` HTTP 200 확인.
- Cloud Build: `25763cc8-75eb-42ca-b1d2-6a39e2f01792` (SUCCESS).
- 이미지 digest: `sha256:4f4fb18bea6aca68b80074ec7f879ed7433029e9b73ee3a0ef7589fb5d4c99bd`.
- cs의 교직원 Layout Parser fallback / 이미지 OCR을 명시적으로 false로 저장했다.
- 학생 옵션은 기존 값(false)을 유지했다. 공통 registry와 Sync DEPARTMENTS_JSON 일치 확인.
- GUI 신규 등록·전체 수정·학과 상세 고급 관리에 네 옵션을 구현했다. 로컬 GUI 서버 재시작은 자동 승인 검토에서 거절되어 수행하지 못했으며, 새 Python API를 사용하려면 GUI를 재실행해야 한다.
- 전체 Python 테스트: 928 passed, 36 skipped, 2 xfailed. GUI 빌드 및 회귀테스트: 41 passed.
- 이번 변경에서 실제 유료 DocAI 호출은 하지 않았다. 대상 분류·호출 차단·설정 보존은 자동 테스트로 검증했다.
