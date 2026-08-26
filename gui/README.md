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
- MCP 키는 생성된 `config/departments/<학과>.yaml`에만 저장되며 UI/API에 반환되지 않습니다.

## 제공 기능

- 학과 상태 대시보드
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
