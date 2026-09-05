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

프런트엔드 빌드 확인:

```powershell
cd gui
npm ci
npm test
```
