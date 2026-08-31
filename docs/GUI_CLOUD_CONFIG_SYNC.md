# GUI 설정의 클라우드 실시간 연동 정리

## 핵심 결론

현재 GUI는 GCP와 설정을 실시간 동기화하지 않는다.

현재 설정 원본은 로컬의 `config/departments/<학과>.yaml`이다. 따라서 다른 PC에서 같은 GCP 관리자 계정으로 로그인해도 해당 YAML이 없으면 GUI가 기존 학과 설정과 MCP 키를 자동 복원하지 못한다.

Secret Manager 접근 권한이 있다는 사실만으로는 해결되지 않는다. GUI가 Secret Manager 또는 별도의 원격 설정 저장소를 직접 읽고 쓰는 기능이 구현돼 있어야 한다.

## 현재 동작

```text
로컬 YAML
   ↓
GUI에서 조회·수정
   ↓
배포 스크립트가 YAML을 환경 변수로 변환
   ↓
Cloud Run 배포
```

- GUI는 로컬 YAML을 설정 원본으로 사용한다.
- MCP 키도 로컬 YAML의 `keys.staff`, `keys.student`에서 읽는다.
- 배포 시 키는 `MCP_API_KEY` 환경 변수로 Cloud Run에 전달된다.
- Cloud Run에 이미 배포된 값을 GUI가 다시 읽어 로컬 설정을 복원하지 않는다.
- Secret Manager API가 활성화돼 있어도 GUI의 설정 불러오기와는 연결돼 있지 않다.
- 다른 환경에는 YAML을 별도로 복사하거나 Git으로 전달해야 동일한 GUI 상태가 나타난다.

즉, 현재 구조에서 `.gitignore`로 실제 학과 YAML을 제외하면 다른 환경의 GUI는 자동으로 동일 상태가 되지 않는다.

## 원하는 동작

사용자가 원하는 동작은 다음과 같다.

```text
어느 관리자 PC에서든 GCP 로그인
   ↓
GUI 실행 또는 새로고침
   ↓
GCP에서 최신 학과 설정과 MCP 키 조회
   ↓
동일한 상태로 조회·수정·배포
```

이를 위해서는 로컬 YAML이 아니라 GCP의 원격 데이터가 기준 원본이어야 한다.

## 권장 구조

### 단순한 방식: 학과 YAML 전체를 Secret Manager에 저장

학과별 YAML 전체를 하나의 Secret으로 저장한다.

예시:

```text
rag-dept-config-cs
```

Secret payload에는 현재 `config/departments/cs.yaml`의 내용을 그대로 넣는다. Drive ID, corpus ID, 버킷, 서비스 설정, 교직원·학생 MCP 키가 한 버전으로 함께 관리된다.

GUI 동작:

1. GCP 프로젝트와 관리자 로그인을 확인한다.
2. Secret Manager에서 학과 설정 Secret 목록을 조회한다.
3. 선택한 학과의 최신 Secret version을 읽어 GUI에 표시한다.
4. 저장 시 새 Secret version을 추가한다.
5. 배포는 GUI 메모리의 설정 또는 방금 저장한 Secret version을 사용한다.
6. 로컬 YAML은 선택적 캐시 또는 내보내기 파일로만 사용한다.

장점:

- 다른 환경에서도 GCP 로그인만 하면 즉시 같은 설정을 받는다.
- 설정과 키가 서로 다른 저장소에서 어긋나지 않는다.
- Secret version으로 변경 이력과 롤백이 가능하다.
- Git과 Cloud Build 소스에 실제 키를 넣지 않아도 된다.

단점:

- YAML의 비밀이 아닌 항목까지 Secret Manager에 들어간다.
- 여러 관리자가 동시에 저장할 때 충돌 방지가 필요하다.
- Secret version 수와 폐기 정책을 운영해야 한다.

현재 규모와 “관리자는 모두 같은 GCP 권한을 가진다”는 전제에서는 이 방식이 가장 단순하다.

### 분리 방식: 일반 설정과 키를 별도 저장

- 일반 설정: Firestore 또는 GCS의 버전 관리 JSON/YAML
- 실제 MCP 키: Secret Manager
- 원격 설정에는 Secret resource 이름만 기록

이 방식은 권한을 세밀하게 나눌 수 있지만 GUI가 두 저장소를 조합해야 하므로 구현과 장애 지점이 늘어난다. 설정 조회 권한과 키 조회 권한을 분리할 필요가 없는 현재 관리자 환경에서는 우선순위가 낮다.

## “Cloud Run에서 다시 받기”가 적합하지 않은 이유

Cloud Run 서비스 설정을 조회해 환경 변수를 역으로 복원하는 방법도 관리자 권한으로는 가능하다. 그러나 Cloud Run은 배포 결과이지 설정 원본으로 쓰기 어렵다.

- 교직원·학생 서비스의 값이 서로 다를 수 있다.
- 배포되지 않은 GUI 설정은 Cloud Run에 존재하지 않는다.
- 환경 변수와 Secret 참조가 섞일 수 있다.
- 학과 YAML의 모든 필드가 Cloud Run 환경 변수에 전달되는 것은 아니다.
- revision별 값을 합쳐 원래 YAML을 정확히 복원하기 어렵다.

따라서 Cloud Run은 상태 확인 대상으로만 사용하고, GUI의 원격 원본은 별도로 두는 것이 맞다.

## 실시간의 의미

여기서 실시간 연동은 다음 수준으로 정의한다.

- GUI 시작 시 원격 최신 version 자동 조회
- 사용자의 새로고침으로 즉시 다시 조회
- 저장 시 새 version 생성 후 화면 갱신
- 배포 직전에 최신 version 재확인
- 다른 관리자가 저장한 경우 version 불일치를 감지하고 덮어쓰기 전에 경고

Secret Manager는 push 알림으로 GUI를 자동 갱신해 주지 않는다. 화면을 계속 열어 둔 상태의 자동 반영까지 원하면 일정 주기 polling 또는 Pub/Sub 기반 알림이 추가로 필요하다. 일반 운영 GUI에는 시작·새로고침·배포 직전 조회와 version 충돌 검사가 충분하다.

## 필요한 GUI 변경

- GCP 프로젝트 및 로그인 상태 확인
- Secret Manager의 학과 설정 목록 조회
- 최신 Secret version 다운로드 및 YAML 검증
- 새 version 저장
- version 번호 또는 etag를 이용한 동시 수정 충돌 검사
- 로컬 YAML 가져오기·내보내기 기능 유지
- 화면 및 API 응답에서 MCP 키 마스킹
- 로그, 예외, 명령행 인자에 Secret payload를 출력하지 않기
- 배포 시 Secret에서 읽은 키를 사용하되 임시 파일로 남기지 않기
- 원격 조회 실패 시 로컬 캐시 사용 여부를 명시적으로 표시

## Git 및 Cloud Build 정책

원격 연동이 구현된 뒤에는 다음 구성이 적절하다.

- Git에 포함: 스키마, 예제 YAML, GUI 코드, Secret 이름 규칙
- Git에서 제외: 실제 학과 YAML 캐시와 실제 MCP 키
- Cloud Build에서 제외: 실제 학과 YAML 캐시와 실제 MCP 키
- Secret Manager: 실제 학과 설정 전체와 MCP 키

원격 연동 구현 전에는 실제 YAML을 Git에서 제외하면 다른 환경에 설정이 전달되지 않는다. 이것이 현재 GUI의 제약이다.

## 현재 상태와 다음 작업

현재는 로컬 YAML 방식이며 GCP 실시간 연동은 구현되지 않았다. 따라서 이 문서는 구현 완료 기록이 아니라 목표 구조와 현재 제약을 정리한 설계 문서다.

구현 순서:

1. 학과별 Secret 이름과 YAML payload 스키마를 확정한다.
2. 기존 로컬 YAML을 Secret Manager의 최초 version으로 마이그레이션한다.
3. GUI에 원격 목록·불러오기·저장 기능을 추가한다.
4. 배포가 로컬 파일 대신 선택된 원격 version을 사용하도록 변경한다.
5. 다른 관리자 환경에서 로그인 후 설정 복원과 배포를 종단 검증한다.
6. 검증 후 실제 학과 YAML을 로컬 캐시로 격하하고 Git/Cloud Build 제외를 유지한다.

