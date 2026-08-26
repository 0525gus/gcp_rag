# 학과 YAML 생성·상태 확인 GUI 명세

- 상태: 제안
- 선행 명세: [`DEPT_CONFIG_STATUS_SPEC.md`](./DEPT_CONFIG_STATUS_SPEC.md)
- 대상 사용자: GCP RAG 운영자
- 기본 형태: 로컬 웹 콘솔
- 작성일: 2026. 8. 26.

## 1. 목표

- 운영자가 YAML 문법이나 CLI 인자를 몰라도 학과 설정을 생성
- 학과별 설정·GCP 리소스·배포·서비스 health·최근 동기화 상태를 한 화면에서 확인
- 오류의 원인과 다음 조치를 함께 제공
- CLI와 GUI가 동일한 검증·상태 판정 로직을 사용

GUI는 별도 판정 엔진이 아니다. `deptctl`의 application service를 호출하는 표현 계층이며, CLI와 결과가 달라지면 안 된다.

## 2. 제품 형태

### 2-1. 실행 방식

```powershell
python scripts/dept_gui.py
```

- 서버: `http://127.0.0.1:8765`
- 실행 후 기본 브라우저 자동 열기
- 종료: 터미널 `Ctrl+C` 또는 GUI의 서버 종료 버튼
- 저장소 루트가 아닌 위치에서 실행해도 스크립트 위치를 기준으로 경로 해석
- Windows PowerShell 환경을 1차 지원, macOS/Linux는 후속 지원

### 2-2. 로컬 전용 원칙

- 기본 bind는 `127.0.0.1`로 고정
- `0.0.0.0`, 사설 IP, 공인 IP bind 옵션을 제공하지 않음
- 원격 배포, 다중 사용자, 사용자 계정·권한 관리는 범위 밖
- 브라우저 종료 여부와 관계없이 백엔드 프로세스가 종료되면 모든 실행 상태 제거

로컬 전용인 이유:

- `config/departments/*.yaml`에 MCP 키가 평문 저장됨
- status가 로컬 사용자의 `gcloud` 인증을 사용함
- GUI가 로컬 저장소 파일을 생성함

## 3. 정보 구조

```text
학과 관리
├─ 상태 대시보드
│  ├─ 전체 요약
│  ├─ 학과 목록
│  └─ 학과 상세
├─ 학과 추가
│  ├─ 기본·GCP
│  ├─ Drive 범위
│  └─ 검토·생성
└─ 실행 환경
   ├─ 저장소 경로
   ├─ gcloud 인증
   └─ 공통 설정
```

## 4. 화면 명세

### 4-1. 공통 셸

- 상단
  - 제품명: `GCP RAG 학과 관리`
  - 현재 프로젝트와 리전
  - `상태 새로고침` 버튼
  - 마지막 확인 시각
- 좌측 메뉴
  - `상태 대시보드`
  - `학과 추가`
  - `실행 환경`
- 본문 최소 폭: 1,024px 기준
- 상태 색상에만 의존하지 않고 아이콘과 텍스트를 함께 표시

### 4-2. 상태 대시보드

```text
┌─────────────────────────────────────────────────────────────────────┐
│ GCP RAG 학과 관리       project / region     [상태 새로고침]       │
├──────────────┬──────────────────────────────────────────────────────┤
│ 상태 대시보드│ 정상 2   확인 필요 1   오류 1   확인 중 0           │
│ 학과 추가    ├──────────────────────────────────────────────────────┤
│ 실행 환경    │ [검색________] [경고 이상 ▼]   [전체 상태 확인]     │
│              │                                                      │
│              │ 학과       전체  설정  리소스  배포  서비스  동기화 │
│              │ 컴퓨터공학  OK    OK    OK      OK    OK      OK     │
│              │ 인공지능    WARN  OK    WARN    OK    OK      WARN   │
│              │ 전자공학    FAIL  OK    FAIL    SKIP  SKIP    WARN   │
└──────────────┴──────────────────────────────────────────────────────┘
```

#### 전체 요약

상단에 다음 카드 4개를 표시한다.

| 카드 | 값 |
|---|---|
| 정상 | `OK` 학과 수 |
| 확인 필요 | `WARN` 학과 수 |
| 오류 | `FAIL` 학과 수 |
| 확인 중 | 현재 실행 중인 검사 수 |

#### 학과 목록

| 열 | 설명 |
|---|---|
| 학과 | `name`과 학과 코드 |
| 전체 상태 | `OK`, `WARN`, `FAIL`, `CHECKING`, `UNKNOWN` |
| 설정 | LOCAL 결과 |
| GCP 리소스 | RESOURCE 결과 |
| 배포 | DEPLOY 결과 |
| 서비스 | RUNTIME 결과 |
| 동기화 | SYNC 결과 |
| 마지막 확인 | 상대 시간 + tooltip 절대 시각 |
| 작업 | `상세`, `다시 확인` |

- 기본 정렬: `FAIL → WARN → UNKNOWN → OK`, 같은 상태에서는 학과 코드 순
- 필터: 전체, 오류만, 경고 이상, 정상
- 검색: 학과명 또는 학과 코드
- `전체 상태 확인`은 모든 학과를 대상으로 한 status run 1개 생성
- 이미 전체 검사가 실행 중이면 새 실행을 만들지 않고 기존 실행에 연결

#### 빈 상태

- 학과 YAML이 없으면 `등록된 학과가 없습니다` 표시
- primary action: `첫 학과 추가`
- YAML이 있지만 아직 상태를 확인하지 않았으면 `UNKNOWN` 표시

### 4-3. 학과 상세

상단:

- 학과명·코드
- YAML 경로
- 전체 상태
- `다시 확인`, `파일 위치 열기` 버튼

검사 결과는 다음 5개 섹션으로 고정한다.

1. LOCAL — YAML 및 설정
2. RESOURCE — 버킷, Firestore, 코퍼스, Drive
3. DEPLOY — Cloud Run 배포
4. RUNTIME — health endpoint
5. SYNC — 최근 Workflow 실행

각 검사 행:

| 항목 | 설명 |
|---|---|
| 상태 | OK/WARN/FAIL/SKIP/CHECKING |
| 검사명 | 예: `rag-corpus-student` |
| 결과 | ACTIVE, HTTP 200, revision 등 |
| 소요 시간 | 네트워크 검사만 표시 |
| 조치 | 실패·경고 시 실행 가능한 명령 또는 문서 링크 |

- 오류 detail은 기본 한 줄, `자세히`로 전체 표시
- 명령은 복사 버튼 제공
- GUI가 명령을 자동 실행하지 않음
- secret·access token·Authorization header는 detail에도 표시하지 않음

### 4-4. 학과 추가

3단계 wizard로 구성한다. 단계 이동 시 현재 단계의 로컬 검증을 통과해야 한다.

```text
학과 추가       ● 기본·GCP ─── ○ Drive 범위 ─── ○ 검토·생성

학과 코드       [ ee                 ]  ✓ 사용 가능
학과명          [ 전자공학과          ]
교직원 코퍼스   [ projects/.../111   ]
학생 코퍼스     [ projects/.../222   ]
HWP 버킷        [ rag-ee-hwp-project ]
source 버킷     [ rag-ee-source-...  ]

                                      [취소] [다음: Drive 범위]
```

#### 1단계 — 기본·GCP

| 입력 | UI | 동작 |
|---|---|---|
| 학과 코드 | text | 소문자 자동 변환, 허용 문자 안내 |
| 학과명 | text | 필수 |
| 프로젝트 | readonly | `common.yaml` 값 |
| 리전 | readonly | `common.yaml` 값 |
| 교직원 코퍼스 | text | 전체 resource path 입력 |
| 학생 코퍼스 | text | 전체 resource path 입력 |
| HWP 원본 버킷 | text | `gs://` 없이 이름만 입력 |
| source 버킷 | text | `gs://` 없이 이름만 입력 |
| staff 최소 인스턴스 | number | 기본 0, 0 이상 |
| student 최소 인스턴스 | number | 기본 0, 0 이상 |

- 학과 코드 입력 즉시 `config/departments/<code>.yaml` 충돌 검사
- 코퍼스는 경로 형식과 staff/student 동일 여부 검사
- 버킷 이름은 GCS 이름 형식과 동일 여부 검사
- 실제 GCP 존재 여부는 wizard를 막지 않고 3단계 사전 확인에서 표시

#### 2단계 — Drive 범위

| 입력 | UI | 동작 |
|---|---|---|
| 공유드라이브 ID | tag input | 하나 이상 |
| 동기화 폴더 ID | tag input | 하나 이상 |
| 학생 폴더 ID | sync 폴더 multi-select | 하나 이상 |

- 쉼표·줄바꿈 붙여넣기 지원
- trim·빈 값 제거·중복 제거 결과를 chip으로 표시
- 학생 폴더는 동기화 폴더에서만 선택 가능하여 `student ⊆ sync`를 UI 구조로 보장
- 공유드라이브 ID와 폴더 ID의 역할 차이를 입력란 아래에 설명

#### 3단계 — 검토·생성

- 생성될 YAML을 읽기 전용 preview로 표시
- `keys.staff`, `keys.student` 값은 `<자동 생성>`으로 표시
- LOCAL 검증 결과 표시
- 선택적으로 `GCP 실물 미리 확인` 실행
- 확인 checkbox: `이 파일은 git으로 복구되지 않으며 기존 파일을 덮어쓰지 않는다는 것을 확인했습니다.`
- primary action: `YAML 생성`

생성 버튼을 누른 시점에만 MCP 키를 생성한다. preview나 validation API는 키를 만들거나 반환하지 않는다.

#### 생성 완료

- 생성 경로 표시
- secret 값은 표시하지 않음
- 다음 작업 버튼
  - `상태 확인`
  - `파일 위치 열기`
  - `배포 안내 보기`
- 실제 리소스가 없으면 status 결과의 조치 명령으로 연결

#### 생성 실패

- `409 FILE_EXISTS`: 기존 파일을 변경하지 않았음을 명확히 표시
- `422 VALIDATION_FAILED`: 필드별 오류를 해당 입력으로 연결
- `500 WRITE_FAILED`: 생성 파일이 남았는지 여부를 함께 표시
- 실패 뒤 사용자가 입력한 비밀값은 없음. 일반 입력은 현재 세션 동안 유지

### 4-5. 실행 환경

읽기 전용으로 다음을 표시한다.

- 저장소 절대 경로
- `config/common.yaml` 존재 여부
- 학과 설정 디렉터리와 발견된 YAML 수
- Python 버전
- gcloud 설치 여부와 버전
- 현재 gcloud 계정: 이메일 일부 마스킹
- 설정 프로젝트와 gcloud 현재 프로젝트 일치 여부

제공 작업:

- `오프라인 검사 실행`
- `gcloud 로그인 명령 복사`
- `common.yaml 파일 위치 열기`

GUI에서 gcloud 로그인 창을 직접 실행하거나 자격증명을 받지 않는다.

### 4-6. 상태 표현

| 상태 | 색상 의미 | 아이콘 | 표시 문구 |
|---|---|---|---|
| `OK` | 녹색 | check | 정상 |
| `WARN` | 황색 | triangle | 확인 필요 |
| `FAIL` | 적색 | x-circle | 오류 |
| `CHECKING` | 청색 | spinner | 확인 중 |
| `SKIP` | 회색 | minus | 건너뜀 |
| `UNKNOWN`·`STALE` | 회색 outline | question | 미확인·변경됨 |

- WCAG AA 대비를 만족하는 색상 token 사용
- 상태 badge에는 항상 아이콘과 문구를 함께 표시
- 같은 상태 token을 카드, 표, 상세 화면에서 일관되게 사용

## 5. 상호작용 상태

```text
UNKNOWN ──검사 시작──▶ CHECKING ──완료──▶ OK | WARN | FAIL
   ▲                         │
   └──── 설정 변경 감지 ─────┘
```

- YAML의 수정 시각 또는 내용 hash가 마지막 검사와 달라지면 결과를 `STALE`로 표시
- `STALE`은 기존 결과를 보여주되 전체 상태를 `UNKNOWN`으로 취급
- 검사는 중간 결과가 도착하는 즉시 행 단위로 갱신
- 사용자가 화면을 이동해도 실행은 계속됨
- 브라우저 새로고침 후 실행 중 job이 있으면 자동 재연결
- 네트워크 검사는 개별 취소하지 않고 전체 status run만 취소 가능
- 취소 완료 전 결과는 `CHECKING`, 취소된 미실행 검사는 `SKIP(cancelled)`

## 6. GUI 백엔드 API

API prefix는 `/api/v1`이다. 모든 응답은 `Cache-Control: no-store`를 사용한다.

### 6-1. 학과 목록

```http
GET /api/v1/departments
```

```json
{
  "departments": [
    {
      "code": "ee",
      "name": "전자공학과",
      "path": "config/departments/ee.yaml",
      "configRevision": "sha256:...",
      "lastStatus": "UNKNOWN"
    }
  ]
}
```

- `keys`와 파생된 `MCP_API_KEY*`는 응답에 포함하지 않음
- YAML 파싱 실패 파일도 목록에 포함하고 `name: null`, `lastStatus: FAIL`로 표시

### 6-2. 생성 preview·검증

```http
POST /api/v1/departments/preview
Content-Type: application/json
```

```json
{
  "code": "ee",
  "name": "전자공학과",
  "corpora": {"staff": "projects/.../111", "student": "projects/.../222"},
  "buckets": {"hwpOriginal": "rag-ee-hwp-project", "source": "rag-ee-source-project"},
  "drive": {
    "driveIds": ["0A_SHARED"],
    "syncFolderIds": ["STAFF", "STUDENT"],
    "studentFolderIds": ["STUDENT"]
  },
  "minInstances": {"staff": 0, "student": 0}
}
```

```json
{
  "valid": true,
  "yamlPreview": "name: 전자공학과\n...",
  "fieldErrors": {},
  "warnings": []
}
```

- preview의 key 값은 `<자동 생성>` 고정
- request에 `keys`, `MCP_API_KEY`, token 계열 필드가 있으면 `400 UNSUPPORTED_SECRET_INPUT`

### 6-3. YAML 생성

```http
POST /api/v1/departments
Content-Type: application/json
```

- body는 preview와 동일
- 서버가 key 두 개 생성
- CLI 명세의 candidate 검증·원자적 저장·postcondition 수행

```json
{
  "code": "ee",
  "path": "config/departments/ee.yaml",
  "created": true,
  "nextActions": ["status", "deploy"]
}
```

### 6-4. status 실행

```http
POST /api/v1/status-runs
Content-Type: application/json
```

```json
{
  "departments": ["ee"],
  "offline": false,
  "strict": false
}
```

```json
{
  "runId": "01J...",
  "status": "RUNNING",
  "eventsUrl": "/api/v1/status-runs/01J.../events"
}
```

- `departments: []`는 전체 학과
- 같은 범위·옵션의 실행이 이미 RUNNING이면 기존 `runId` 반환
- 서로 다른 status run은 최대 2개까지 허용, 초과 시 `429 STATUS_CAPACITY`

### 6-5. status 조회·스트림

```http
GET /api/v1/status-runs?status=RUNNING
GET /api/v1/status-runs/{runId}
GET /api/v1/status-runs/{runId}/events
DELETE /api/v1/status-runs/{runId}
```

- `events`는 Server-Sent Events 사용
- event type
  - `run.started`
  - `check.started`
  - `check.completed`
  - `department.completed`
  - `run.completed`
  - `run.cancelled`
- event payload의 check 구조는 CLI JSON 출력의 `checks[]`와 동일
- 완료된 run은 메모리에 15분 보관 후 삭제
- RUNNING 목록 조회는 브라우저 새로고침 뒤 기존 실행 재연결에 사용

### 6-6. 환경

```http
GET /api/v1/environment
```

- 실행 환경 화면에 필요한 비민감 정보만 반환
- access token, application-default credentials 경로, 전체 계정 이메일은 반환하지 않음

### 6-7. 로컬 작업

```http
POST /api/v1/local-actions/open-path
POST /api/v1/local-actions/shutdown
```

- `open-path`는 `{ "kind": "department", "code": "ee" }` 또는 `{ "kind": "common-config" }`만 허용
- 클라이언트가 임의 파일 경로를 넘기는 API는 제공하지 않음
- `shutdown`은 확인 dialog 뒤 호출하고 응답 전송 후 서버를 종료
- 두 요청 모두 Origin과 `X-Local-Session` 검증 필수

## 7. 오류 모델

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "입력값을 확인해 주세요.",
    "fieldErrors": {
      "drive.studentFolderIds": ["동기화 폴더에 포함되지 않은 ID입니다: X"]
    },
    "requestId": "01J..."
  }
}
```

| HTTP | code | 의미 |
|---:|---|---|
| 400 | `INVALID_REQUEST` | JSON·요청 형식 오류 |
| 400 | `UNSUPPORTED_SECRET_INPUT` | secret 입력 시도 |
| 404 | `DEPARTMENT_NOT_FOUND` | 대상 학과 없음 |
| 404 | `STATUS_RUN_NOT_FOUND` | 실행 없음·만료 |
| 409 | `FILE_EXISTS` | YAML 경로 충돌 |
| 409 | `STATUS_ALREADY_RUNNING` | 충돌하는 실행 |
| 422 | `VALIDATION_FAILED` | 설정 규칙 위반 |
| 429 | `STATUS_CAPACITY` | 동시 실행 상한 |
| 500 | `WRITE_FAILED` | 파일 생성 실패 |

## 8. 보안

- frontend와 API 모두 동일 origin만 허용
- 모든 mutation 요청은 `Origin`이 정확히 local server origin인지 확인
- CORS 비활성화
- 서버 시작 시 session nonce 생성, HTML에 주입하고 POST/DELETE의 `X-Local-Session`으로 검증
- CSP: `default-src 'self'`; 외부 CDN·분석·폰트 사용 금지
- API·SSE·브라우저 저장소에 MCP 키나 gcloud token 저장 금지
- `localStorage`, `sessionStorage`, IndexedDB 사용 금지
- YAML preview와 오류 메시지에 secret redaction 적용
- subprocess는 argument array로 실행하고 shell 사용 금지
- 파일 경로는 학과 코드로만 만들고 resolve 결과가 `config/departments` 내부인지 재확인
- status는 읽기 전용 명령 allowlist만 사용

## 9. 접근성·사용성

- 키보드만으로 wizard와 상태 표 조작 가능
- focus indicator 제공
- 상태 아이콘에 accessible label 제공
- 오류 발생 시 첫 오류 필드로 focus 이동
- 동적 status 갱신은 `aria-live=polite`, 실행 완료는 한 번만 알림
- 코드·ID·resource path는 monospace와 줄바꿈 제공
- 한국어를 기본 언어로 사용하고 GCP의 원문 상태값은 병기
- 버튼 문구는 동사와 결과가 드러나게 작성: `생성`, `실행` 대신 `YAML 생성`, `상태 확인`

## 10. 비기능 요구사항

- 첫 화면 표시: 로컬 기준 2초 이내
- LOCAL 검사: 학과당 500ms 이내
- 외부 검사: 각 호출 10초 timeout, 전체 기본 90초 상한
- status 진행 상황은 검사 시작 후 1초 이내 표시
- frontend production bundle은 외부 네트워크 없이 동작
- YAML 생성은 클릭 1회당 최대 1개 파일만 변경
- 서버 로그는 INFO 기본, 민감값 redaction 후 stderr 출력

## 11. 구현 구조

```text
scripts/
├─ deptctl.py              # CLI adapter
├─ dept_gui.py             # localhost server entrypoint
└─ dept_admin/
   ├─ application.py       # 생성·status use case
   ├─ validation.py        # 공용 규칙
   ├─ status.py            # 검사 orchestration
   ├─ gcloud.py            # subprocess adapter
   └─ redaction.py

gui/
├─ src/
│  ├─ pages/
│  ├─ components/
│  └─ api/
└─ dist/                   # dept_gui.py가 제공하는 build 산출물
```

- `dept_config.py`의 기존 public 함수는 유지
- CLI와 GUI는 `dept_admin.application`만 호출
- status 판정과 exit code 변환은 core에서 하고 UI에서 재해석하지 않음
- frontend framework는 구현 시 선택하되 외부 CDN 없이 정적 bundle 생성

## 12. 테스트

### 백엔드

- CLI 명세의 모든 단위·외부 호출 테스트 재사용
- API schema, 오류 코드, secret 필드 거부
- path traversal 학과 코드 거부
- Origin/session nonce 검증
- 동시 run deduplication·상한·취소·TTL
- SSE event 순서와 완료 이벤트
- YAML 생성 경쟁 조건에서 하나만 성공하고 다른 요청은 409

### 프론트엔드

- 필드별 validation과 단계 이동
- tag paste 정규화
- student 폴더 multi-select 제약
- preview secret 마스킹
- status 필터·정렬·검색
- SSE 재연결과 중간 결과 렌더링
- 오류 focus 및 keyboard navigation

### E2E

1. 빈 설정 디렉터리에서 wizard로 학과 생성
2. 생성 직후 대시보드 이동 및 LOCAL OK 확인
3. 동일 학과 재생성 시 409과 기존 파일 무변경 확인
4. offline status 전체 실행 및 완료 확인
5. 외부 검사 fixture로 OK/WARN/FAIL 혼합 상태 표시 확인
6. UI/API/로그/브라우저 저장소에 MCP 키가 없음을 확인

## 13. 인수 기준

1. 운영자가 터미널에서는 GUI 실행 명령 한 번만 입력하고 YAML을 생성할 수 있음
2. GUI 생성 YAML이 기존 배포 스크립트에서 수정 없이 사용됨
3. GUI와 CLI status의 check 이름·상태·detail이 동일함
4. 한 학과 및 전체 학과 status를 실행하고 진행 상황을 실시간 확인할 수 있음
5. FAIL 항목마다 원인과 수동 조치가 제공됨
6. 기존 YAML을 GUI에서 덮어쓰거나 삭제할 수 없음
7. status 실행이 GCP 리소스·권한·배포 상태를 변경하지 않음
8. MCP 키와 access token이 UI, API, 로그, 브라우저 저장소에 노출되지 않음

## 14. 구현 단계

### MVP

- 공용 application/validation 계층 추출
- 학과 목록·상세
- 3단계 YAML 생성 wizard
- offline status
- online status와 polling
- 로컬 전용 보안

### 1.1

- SSE 실시간 진행
- 환경 화면
- YAML 변경 감지와 STALE 표시
- 명령 복사 및 파일 위치 열기

### 후속 검토

- 기존 YAML 편집
- MCP 키 회전
- GCP 리소스 provision
- 백필 실행과 진행률
- 원격 다중 사용자 운영 콘솔

후속 기능은 파일 덮어쓰기, secret 노출, GCP 상태 변경을 포함하므로 별도 권한·감사·복구 명세 없이 MVP에 추가하지 않는다.
