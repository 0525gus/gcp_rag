# GUI Cloud 설정 저장 방식

## 결정

학과 설정은 별도 Secret Manager 저장소로 분리하지 않고, MCP를 배포할 때 Cloud Run
관리 주석에 학과 YAML 전체를 저장한다.

- 주석: `gcp-rag.dev/department-metadata`
- 형식: base64url로 인코딩한 JSON
- 스키마: `schemaVersion: 2`
- 포함 항목: 학과명, 코퍼스, MCP 키, 버킷, Drive 범위, 최소 인스턴스 및 향후 추가 필드

필드별 허용 목록을 사용하지 않으므로 YAML에 새 필드가 추가돼도 Cloud 사본에서 누락되지
않는다. 교직원·학생 서비스에는 동일한 전체 YAML과 각 서비스의 audience 정보가 기록된다.

## 사용 흐름

```text
학과 설정 생성·수정
   ↓
MCP Cloud Run 배포
   ↓
전체 YAML을 관리 주석에 기록
   ↓
다른 관리자 PC에서 GCP 로그인
   ↓
Cloud Run 주석을 읽어 설정 복원
```

기존 `schemaVersion: 1` 주석도 계속 읽을 수 있다. 다만 v1에는 MCP 키와 임의 추가 필드가
없으므로 전체 복원이 필요하면 해당 학과 MCP를 다시 배포해야 한다.

## 운영 기준

Cloud Run 주석은 Secret Manager가 아니며 base64url도 암호화가 아니다. `keys.staff`와
`keys.student`까지 저장되므로 다음 기준을 지킨다.

- `run.services.get`을 포함한 Cloud Run Viewer 이상 권한은 관리자에게만 부여한다.
- GUI 서버는 `127.0.0.1`에만 바인딩한다.
- 주석 원문과 복원된 키를 로그 또는 브라우저 저장소에 남기지 않는다.
- 키를 교체할 때는 재배포하고 필요하면 키가 남은 이전 revision을 정리한다.
- 프로젝트를 외부 운영자와 공유하게 되면 Secret Manager 분리를 다시 검토한다.

현재 운영 규모에서는 권한 분리보다 단순한 복원과 유지보수를 우선한다.

## 현재 범위

Cloud Run에서 전체 YAML을 읽는 기능과 기존 v1 호환은 구현돼 있다. v2로 배포된 Cloud 전용
학과는 GUI가 설정을 수정하고, 기존 키와 향후 추가 필드를 보존한 채 로컬 YAML 없이 새
Cloud Run revision으로 직접 재배포한다. 동시에 수정된 설정은 revision hash로 감지한다.

기존 v1 주석과 신규 학과의 최초 생성·공통 동기화 설정은 아직 로컬 YAML 배포 경로를
사용한다. v1 학과는 원래 환경에서 v2로 한 번 재배포한 뒤 다른 환경에서 수정할 수 있다.
