# 학과 YAML 생성·상태 확인 도구 명세

- 상태: 제안
- 대상 저장소: `gcp-rag`
- 구현 대상: `scripts/deptctl.py`
- 작성일: 2026. 8. 26.

## 1. 목적

- 신규 학과의 `config/departments/<dept>.yaml`을 안전하게 생성
- 배포 전에 YAML 값과 GCP 리소스의 일치 여부 확인
- 배포 뒤 Cloud Run, MCP health, 최근 동기화 상태를 한 화면에서 확인
- 기존 `dept_config.py`, `preflight.ps1`, 배포 스크립트의 검증 규칙을 재사용하고 중복 규칙을 만들지 않음

## 2. 범위

### 포함

- 대화형 YAML 생성
- 비대화형 YAML 생성
- 로컬 설정 검증
- GCP 리소스 및 런타임 상태 조회
- 사람용 표와 자동화용 JSON 출력

### 제외

- 버킷·Firestore·RAG 코퍼스 생성
- Cloud Run 배포 및 재배포
- Drive 권한 자동 변경
- 코퍼스 백필·재색인 실행
- 기존 YAML의 키 자동 회전

상태 확인은 읽기 전용이다. 리소스 생성·권한 변경·배포가 필요하면 실행할 명령만 안내한다.

## 3. CLI

```powershell
# 대화형 생성
python scripts/deptctl.py init --dept ee

# 비대화형 생성
python scripts/deptctl.py init --dept ee --from request.yaml --non-interactive

# 한 학과 상태
python scripts/deptctl.py status --dept ee

# 전체 학과 상태
python scripts/deptctl.py status --all

# 로컬 검사만
python scripts/deptctl.py status --dept ee --offline

# CI/후속 도구용 JSON
python scripts/deptctl.py status --all --format json
```

### 공통 인자

| 인자 | 필수 | 설명 |
|---|---:|---|
| `--dept <code>` | 조건부 | 학과 코드. `--all`과 동시 사용 불가 |
| `--all` | 조건부 | 존재하는 모든 학과 YAML 대상 |
| `--format table\|json` | 아니요 | 기본 `table` |
| `--timeout <sec>` | 아니요 | 외부 조회별 제한. 기본 10초 |

## 4. `init` 명세

### 4-1. 입력

| 필드 | 필수 | 규칙 |
|---|---:|---|
| 학과 코드 | O | `^[a-z][a-z0-9-]{1,19}$` |
| 학과명 | O | 공백 제거 후 비어 있지 않아야 함 |
| 교직원 코퍼스 | O | `projects/{project}/locations/{region}/ragCorpora/{id}` |
| 학생 코퍼스 | O | 교직원 코퍼스와 달라야 함 |
| HWP 원본 버킷 | O | source 버킷과 달라야 함 |
| source 버킷 | O | HWP 원본 버킷과 달라야 함 |
| 공유드라이브 ID | O | 하나 이상. 중복 불가 |
| 동기화 폴더 ID | O | 하나 이상. 중복 불가 |
| 학생 폴더 ID | O | 하나 이상, 모두 동기화 폴더 목록에 포함 |
| 최소 인스턴스 | 아니요 | audience별 0 이상 정수. 기본 0 |

- 프로젝트와 리전은 `config/common.yaml`의 `GCP_PROJECT_ID`, `GCP_REGION`을 기본값으로 사용
- MCP 키는 사용자가 입력하지 않고 `secrets.token_urlsafe(32)`로 staff/student 각각 생성
- 키는 서로 달라야 하며 생성 결과를 콘솔에 재출력하지 않음
- `--from` 파일은 입력값 전용이며 `keys` 필드를 받지 않음
- 쉼표로 여러 ID를 받은 경우 분리·trim·빈 값 제거 후 YAML 배열의 개별 항목으로 저장

### 4-2. 출력 YAML

```yaml
name: 전자공학과

corpora:
  staff: projects/my-project/locations/asia-northeast3/ragCorpora/111
  student: projects/my-project/locations/asia-northeast3/ragCorpora/222

keys:
  staff: GENERATED_SECRET
  student: GENERATED_SECRET

buckets:
  hwpOriginal: rag-ee-hwp-my-project
  source: rag-ee-source-my-project

drive:
  driveIds:
    - 0A_SHARED_DRIVE_ID
  syncFolderIds:
    - STAFF_FOLDER_ID
    - STUDENT_FOLDER_ID
  studentFolderIds:
    - STUDENT_FOLDER_ID

minInstances:
  staff: 0
  student: 0
```

### 4-3. 저장 규칙

- 경로: `config/departments/<dept>.yaml`
- UTF-8, LF, 마지막 개행 포함
- 키 순서는 위 예시와 동일하게 고정
- 기존 파일이 있으면 실패하고 수정하지 않음
- `--force`는 제공하지 않음. 실값 YAML은 git으로 복구되지 않으므로 덮어쓰기는 명시적 수동 작업으로 남김
- 임시 파일에 작성하고, candidate 설정을 받는 공용 검증 함수로 기존 파일들과 함께 검증한 뒤 원자적으로 rename
- rename 직후 기존 `build_env(dept, "staff")`, `build_env(dept, "student")`, `build_departments_map()`을 postcondition으로 호출
- postcondition이 예상 밖으로 실패하면 이번 실행이 만든 파일만 제거하고 오류 반환
- 생성 성공 메시지에는 파일 경로와 다음 명령만 표시. MCP 키 값은 마스킹

```text
OK config/departments/ee.yaml 생성
NEXT python scripts/deptctl.py status --dept ee
```

## 5. `status` 명세

검사는 `LOCAL → RESOURCE → DEPLOY → RUNTIME → SYNC` 순서로 수행한다. 앞 단계 실패로 다음 단계가 불가능하면 다음 검사를 `SKIP`으로 기록하되, 독립적인 검사는 계속 수행한다.

### 5-1. 상태 값

| 상태 | 의미 | 종료 코드 영향 |
|---|---|---:|
| `OK` | 기대 상태와 일치 | 없음 |
| `WARN` | 동작 가능하지만 운영 확인 필요 | 기본 없음, `--strict`에서는 실패 |
| `FAIL` | 누락·불일치·비정상 | 실패 |
| `SKIP` | 선행 조건 부족 또는 `--offline` | 없음 |

전체 판정은 `FAIL > WARN > OK` 우선순위로 계산한다. `SKIP`만 있으면 `WARN`으로 본다.

### 5-2. LOCAL 검사

| 검사 | OK | WARN | FAIL |
|---|---|---|---|
| YAML 파싱 | mapping | - | 파싱 실패·최상위 타입 오류 |
| 필수값 | 전부 존재 | - | 누락·`CHANGE_ME`·빈 값 |
| 코퍼스 | 형식 정상, staff ≠ student | - | 형식 오류·동일 값 |
| 키 | 서로 다르고 24자 이상 | 24자 미만·약한 단어 포함 | 비어 있음·동일·학과 간 중복 |
| 버킷 | 둘 다 있고 서로 다름 | - | 한쪽 누락·동일 |
| Drive | ID 목록 존재 | - | drive ID 없음·학과 간 중복 |
| 폴더 | student ⊆ sync | 중복 항목 | sync 없음·부분집합 위반 |
| 파생 env | 양 audience 생성 성공 | - | `build_env` 실패 |
| 학과 map | 생성 성공, secret 없음 | - | `build_departments_map` 실패 |

기존 검증 로직이 있는 항목은 `scripts/dept_config.py`를 호출한다. `deptctl.py`에 같은 규칙을 복사하지 않는다.

### 5-3. RESOURCE 검사

`--offline`이 아니면 현재 `gcloud` 계정으로 조회한다.

| 대상 | OK | WARN | FAIL |
|---|---|---|---|
| gcloud | 설치·로그인·프로젝트 접근 가능 | - | 명령 없음·인증 실패 |
| GCS 버킷 2개 | 존재하고 접근 가능 | 설정 리전과 다름 | 없음·접근 거부 |
| Firestore | DB 존재, `FIRESTORE_NATIVE` | - | 없음·Datastore 모드 |
| RAG 코퍼스 2개 | 존재, `ACTIVE` | 상태 필드 미제공 | 없음·비활성·접근 거부 |
| RAG 파일 수 | staff > 0, student > 0 | 둘 중 0 | 조회 실패 |
| Drive | Cloud Run SA가 drive 및 delta token 조회 가능 | 권한 판정 불가 | 접근 불가·폴더 ID를 drive ID로 사용 |

- RESOURCE 구현은 `preflight.ps1`의 판정 기준과 동일해야 함
- 장기적으로 공용 Python 검사 모듈로 추출하되, 1차 구현에서는 `preflight.ps1`을 비대화형·무수정 모드로 호출 가능
- status 실행 중에는 `Read-Host`, 권한 추가, API enable을 절대 수행하지 않음

### 5-4. DEPLOY 검사

| 서비스 | 기대 이름 | 판정 |
|---|---|---|
| Parser | `rag-parser` | Cloud Run Ready=True |
| Sync | `rag-sync` | Cloud Run Ready=True |
| Staff MCP | `rag-mcp-{dept}-staff` | Ready=True, URL 존재 |
| Student MCP | `rag-mcp-{dept}-student` | Ready=True, URL 존재 |

- `latestReadyRevisionName == latestCreatedRevisionName`이면 `OK`
- Ready이지만 두 revision이 다르면 최신 배포가 준비되지 않은 것이므로 `FAIL`
- 서비스가 없으면 `FAIL`
- 이미지 digest와 YAML 수정 시각 비교는 신뢰할 수 없으므로 판정에 사용하지 않음

### 5-5. RUNTIME 검사

| 대상 | 호출 | 기대 |
|---|---|---|
| Parser | `GET {parserUrl}/health` + ID token | HTTP 200, `status=ok` |
| Sync | `GET {syncUrl}/health` + ID token | HTTP 200, `status=ok` |
| Staff MCP | `GET {staffUrl}/health` | HTTP 200, `status=ok` |
| Student MCP | `GET {studentUrl}/health` | HTTP 200, `status=ok` |

- parser가 HTTP 200이지만 `status=degraded`이면 `WARN`
- IAM 토큰 발급 실패는 서비스 장애와 구분하여 `WARN(auth)`로 표시
- MCP health에는 API 키를 보내지 않음. `/health`는 인증 미들웨어 예외 경로임
- 응답 본문이나 JSON 출력에 MCP 키·access token을 포함하지 않음

### 5-6. SYNC 검사

- Workflow `rag-daily-sync` 최근 실행 1건 조회
- `SUCCEEDED`이고 완료 시각이 26시간 이내면 `OK`
- `ACTIVE`면 `WARN`과 함께 경과 시간 표시
- `SUCCEEDED`지만 26시간 초과면 `WARN`
- `FAILED`, `CANCELLED`, 실행 없음은 `FAIL`
- 보조 정보로 최근 실행명, 시작/종료 시각, 경과 시간 표시
- Firestore 문서 상태 집계는 현재 인덱스·비용 보장이 없으므로 1차 범위에서 제외

## 6. 출력

### 6-1. 표

```text
DEPT  LAYER     CHECK                 STATUS  DETAIL
ee    LOCAL     yaml                  OK      config/departments/ee.yaml
ee    RESOURCE  rag-corpus-staff      OK      ACTIVE, files=412
ee    DEPLOY    rag-mcp-ee-student    OK      revision ...-00007
ee    RUNTIME   mcp-student-health    OK      200 / 184ms
ee    SYNC      latest-workflow       WARN    ACTIVE / 00:13:21

SUMMARY OK=18 WARN=1 FAIL=0 SKIP=0
```

- 기본 출력에서 secret은 `****`로도 표시하지 않고 필드 자체를 생략
- 오류 detail은 한 줄 200자 이내
- `--all`은 학과 코드 순으로 안정 정렬

### 6-2. JSON

```json
{
  "version": 1,
  "checkedAt": "2026-08-26T13:00:00+09:00",
  "overall": "WARN",
  "departments": [
    {
      "code": "ee",
      "overall": "WARN",
      "checks": [
        {
          "layer": "SYNC",
          "name": "latest-workflow",
          "status": "WARN",
          "detail": "ACTIVE",
          "durationMs": 801000
        }
      ]
    }
  ],
  "summary": {"ok": 18, "warn": 1, "fail": 0, "skip": 0}
}
```

- JSON schema version은 `version: 1`로 시작
- timestamp는 timezone을 포함한 ISO 8601
- 네트워크 검사에는 가능하면 `latencyMs` 포함

## 7. 종료 코드

| 코드 | 의미 |
|---:|---|
| 0 | 모두 OK, 또는 기본 모드에서 WARN만 존재 |
| 1 | 하나 이상의 FAIL |
| 2 | CLI 인자·입력 형식 오류 |
| 3 | YAML 생성 실패·파일 충돌 |
| 4 | `--strict`에서 WARN 존재 |

## 8. 보안 요구사항

- MCP 키를 명령행 인자로 받지 않음: shell history와 process list 노출 방지
- YAML 원문, 전체 env, HTTP Authorization header를 로그에 출력하지 않음
- 예외 traceback에도 secret 값이 포함되지 않도록 별도 redaction 적용
- 생성 파일은 기존 `.gitignore` 정책에 포함되는지 저장 전 확인. 포함되지 않으면 생성 거부
- status는 읽기 전용. `gcloud ... create`, `enable`, `add-iam-policy-binding`, Drive permission POST 금지
- `--format json`에서도 URL은 허용하되 key와 token은 금지

## 9. 테스트 명세

### 단위 테스트

- 정상 입력으로 기대 YAML 구조와 순서 생성
- 여러 ID의 쉼표 입력 정규화
- staff/student 키가 다르고 최소 길이 충족
- 기존 파일이 있을 때 byte 단위 무변경
- 생성 후 `build_env` 양 audience 통과
- student 폴더 부분집합 위반 거부
- 학과 간 drive/key 중복 거부
- secret redaction
- 상태 우선순위와 종료 코드
- JSON schema와 안정 정렬

### 외부 호출 테스트

- `gcloud`는 subprocess adapter로 감싸 fixture 응답 사용
- Cloud Run: 정상, 서비스 없음, Ready=False, revision 불일치
- RAG: ACTIVE, 비활성, 조회 권한 없음, 파일 0건
- health: 200/ok, 200/degraded, 401, timeout, invalid JSON
- Workflow: SUCCEEDED 최신, 오래된 성공, ACTIVE, FAILED, 실행 없음
- status 테스트는 실제 GCP나 네트워크에 의존하지 않음

### 인수 기준

1. 빈 임시 설정 디렉터리에서 `init` 1회로 유효한 학과 YAML이 생성됨
2. 생성된 YAML은 기존 `deploy.ps1`, `deploy_mcp.ps1`, `dept_config.py`에서 수정 없이 사용 가능
3. 동일 명령 재실행 시 기존 YAML과 키를 덮어쓰지 않음
4. `status --offline`은 gcloud·네트워크 없이 완료됨
5. `status --format json`은 stdout에 JSON만 출력하고 진단 로그는 stderr로 분리됨
6. 실패한 검사 하나 때문에 다른 독립 검사 결과가 유실되지 않음
7. 모든 출력과 예외에서 MCP 키·access token이 노출되지 않음

## 10. 구현 순서

1. `dept_config.py`에 순수 검증 함수 보강: 학과 코드, 폴더 부분집합, 중복 검증
2. `deptctl.py init` 및 원자적 YAML 저장 구현
3. LOCAL status와 JSON 출력 구현
4. gcloud adapter 및 RESOURCE/DEPLOY 검사 구현
5. health/Workflow 검사 구현
6. README의 수동 복사 절차를 `deptctl init/status` 기준으로 갱신

## 11. 열린 결정

- 코퍼스와 버킷까지 자동 생성하는 `provision` 명령은 별도 명세로 분리
- Firestore 상태별 문서 수 집계는 운영 비용과 필요한 복합 인덱스를 확인한 뒤 추가
- Workflow는 전 학과 공용이므로 `--all`에서 한 번만 조회하고 각 학과 결과에 참조할지 구현 시 결정
