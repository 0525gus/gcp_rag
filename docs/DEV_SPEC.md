# 문서 검색(RAG) 시스템 개발명세

- 작성: 2026. 7. 28. / 개정: 2026. 8. 26.
- 대상: 한국공학대학교 문서 검색 파이프라인 (Drive → GCS → Vertex RAG → FactChat)
- 규모·품질 수치는 2026. 7. 28. 운영 실측. 아키텍처는 현행 코드 기준

## 목차

- [Ⅰ. 개요](#ⅰ-개요)
- [Ⅱ. 시스템 구성](#ⅱ-시스템-구성)
- [Ⅲ. 처리 명세](#ⅲ-처리-명세)
- [Ⅳ. 검색 품질 실측](#ⅳ-검색-품질-실측-2026-7-28-단일-코퍼스)
- [Ⅴ. 운영](#ⅴ-운영)
- [Ⅵ. 제약·과제](#ⅵ-제약과제)
- [부록. 평가 재현](#부록-평가-재현)

---

## Ⅰ. 개요

### 1. 목적

- 교내 공문·규정·양식을 자연어로 검색하고 근거 문서와 함께 반환
- FactChat이 MCP로 호출하는 검색 도구 제공
- 답변 생성은 호출 측 LLM. 본 시스템은 근거 검색·출처 제공

### 2. 범위

- 포함
  - Drive 공유드라이브 수집·변환·색인
  - HWP/HWPX → 마크다운
  - 학생/교직원 코퍼스 분리 (Drive 경로 기준)
  - 벡터 검색 및 후처리
  - MCP 연동 (학과당 교직원·학생 서비스 2개)
  - 학과별 YAML 생성·수정과 GCP 리소스 상태 확인용 로컬 관리 콘솔
- 제외
  - 답변 문장 생성
  - 문서 단위 ACL (부서·기밀등급). 분리는 폴더 트리 + 코퍼스 2개까지

### 3. 현행 규모 (2026. 7. 28.)

| 구분 | 수량 |
|---|---:|
| 색인 문서 | 1,211건 |
| 색인 객체(URI) | 1,418건 |
| 범위 외 제외 | 394건 |
| 원본 저장 | hwp-original 271MB / source 530MB |

---

## Ⅱ. 시스템 구성

### 1. 전체 구조

`rag-parser`, `rag-sync`, Firestore는 공통 실행 환경을 사용한다. Drive 수집 범위,
GCS 버킷 2개, RAG 코퍼스 2개, MCP 서비스 2개와 API 키는 학과별로 분리한다.
`rag-sync`는 `DEPARTMENTS_JSON`의 Drive ID → 학과 매핑으로 요청을 해당 학과
리소스에 라우팅한다.

```
학과별 Google Drive
  driveIds / syncFolderIds / studentFolderIds
      │
      ▼
 공통 rag-sync ───── HWP/HWPX ─────▶ 공통 rag-parser
      │
      ├─ 학과별 GCS hwp-original / source (객체 키 = fileId)
      ├─ 공통 Firestore DB (`FIRESTORE_DATABASE`, 현행 rag-sync-state)
      │     doc_state.audience = STUDENT | STAFF
      ▼
      ├─ 학과별 교직원 코퍼스  ← 해당 학과 전량
      └─ 학과별 학생 코퍼스    ← audience=STUDENT 만
             ▲                         ▲
             │                         │
 rag-mcp-{dept}-staff      rag-mcp-{dept}-student
             │                         │
          FactChat                  FactChat
```

포함 관계: 교직원 코퍼스 ⊇ 학생 코퍼스.

### 2. 구성요소

#### 가. rag-sync

- Drive 변경 감지 → 다운로드 → 변환 위임 → GCS 적재 → RAG 색인
- 전 학과 설정을 비밀값이 없는 `DEPARTMENTS_JSON`으로 받아 Drive ID별 학과를 결정
- 공유드라이브 ID는 학과 간 중복 불가. 중복이면 배포 전 설정 검증에서 실패
- CPU 2 / MEM 2Gi / timeout 3600s / concurrency 4 (`SYNC_CONCURRENCY`)
- 엔드포인트

| 경로 | 기능 |
|---|---|
| `POST /sync/changes` | Drive 델타, MIME 라우팅 |
| `POST /sync/ingest` | Drive → GCS. `audience` 기록 |
| `POST /sync/index-gcs` | GCS → 교직원 코퍼스 import 후 학생 코퍼스 동기화 |
| `POST /sync/reindex-pending` | 누락분 또는 전량 재색인 |
| `POST /sync/delete` | 양쪽 코퍼스 + GCS 정리 |
| `GET /sync/jobs/{id}` | 장시간 작업 진행률 |

- IAM만 허용 (공개 아님)
- 현행 학과 설정은 `corpora.staff`, `corpora.student`, `drive.studentFolderIds`를 모두 요구한다

#### 나. rag-parser

- HWP/HWPX → 마크다운, 품질 게이트
- CPU 2 / MEM 2Gi / timeout 540s (`PARSER_TIMEOUT`) / concurrency 4 (`PARSER_CONCURRENCY`) / max 10 인스턴스
  - sync 의 httpx 타임아웃 600s 보다 짧아야 한다 — 서버가 먼저 포기해야 sync 가 오류를 받는다
- 엔진: `.hwp` = rhwp, `.hwpx` = python-hwpx (부재 시 rhwp)
- 게이트: `log` / `reject` / `fallback`
  - 실제 발동은 G1 밀도, G2 표 손실, EMPTY_TEXT
  - 기본 `QG_MODE=log` — 미달이어도 색인 계속, 로그만
  - EMPTY_TEXT만 모드와 무관하게 422
- rag-sync 서비스계정만 호출

#### 다. rag-mcp-{dept}-staff / rag-mcp-{dept}-student

- MCP 도구 `search`, `answer`
- CPU 1 / MEM 1Gi / timeout 60s / concurrency 40 (`MCP_CONCURRENCY`)
- 기동 시 코퍼스 하나. 툴 인자로 대상을 고르지 않음
- API 키 (`Authorization: Bearer` 또는 `X-API-Key`). 서비스마다 키를 다르게 둠
- 배포: `deploy.ps1` 이 전 학과 x 교직원·학생을 올린다(`deploy_mcp.ps1 -All` 에 위임).
  공개 여부는 `ALLOW_UNAUTH` (기본 `true` = 공개)
- `scripts/deploy_mcp.ps1` 은 MCP 만 재배포할 때
- 학생 MCP 는 학과 yaml 에 `corpora.student` + `drive.studentFolderIds` 가 둘 다 있을 때만 의미가 있다
- 서비스 이름은 규칙으로 만든다: `rag-mcp-{학과}-{staff|student}` (저장하지 않음)

```
.\scripts\deploy_mcp.ps1 -Dept cs -Audience student
.\scripts\deploy_mcp.ps1 -All
```

- 실측 지연 (단일 MCP, 2026. 7. 28.): 중앙값 1.14초 / 최대 1.39초

#### 라. 저장소

| 저장소 | 용도 |
|---|---|
| 학과별 GCS `hwp-original` (`buckets.hwpOriginal`) | HWP/HWPX **만** 들어간다. parser 전달·재파싱용. 파싱 성공 뒤에도 지우지 않는다. 객체 키 = `{fileId}{확장자}` |
| 학과별 GCS `source` (`buckets.source`) | RAG import 산출물(MD, 사이드카, 통과 PDF 등). PDF·DOCX 등 HWP 외 포맷은 parser·hwp-original을 안 거치고 여기로 직행. 객체 키 = `{fileId}{확장자}` |
| 공통 Firestore DB (`FIRESTORE_DATABASE`, 현행 `rag-sync-state`) | Native 모드. Datastore 모드는 사용 불가. **사전 생성 필수** — 아래 컬렉션 5종을 담는 그릇(컬렉션은 첫 쓰기 때 자동 생성) |
| 컬렉션 `doc_state` (`DOC_STATE_COLLECTION`) | 파일별 상태·경로·해시·`audience`. |
| 컬렉션 `doc_dlq` | 처리 실패 |
| 컬렉션 `doc_split_queue` | 크기 초과 대기. 소비 코드 없음 |
| 컬렉션 `sync_tokens` | Drive pageToken |
| 컬렉션 `sync_jobs` (`SYNC_JOB_COLLECTION`) | 장시간 작업 진행률 |

객체 키에 학과나 student/staff를 넣지 않는다. 격리는 버킷 자체로 수행한다.

```
gs://{hwp-original 버킷}/{fileId}{.hwp|.hwpx}
gs://{source 버킷}/{fileId}.md
gs://{source 버킷}/{fileId}.meta.md
gs://{source 버킷}/{fileId}{.pdf|…}
```

#### 마. 학과 설정·상태 관리 콘솔

- 실행: `python scripts/dept_gui.py`
- 주소: `http://127.0.0.1:8765` (외부 인터페이스 bind 금지)
- 공통 설정이 없으면 gcloud 로그인 → 프로젝트·리전 → 기존 Artifact Registry·Native
  Firestore 선택 순서로 `config/common.yaml`을 생성
- 학과 추가·수정 시 실제 Vertex RAG 코퍼스와 같은 리전의 보호된 GCS 버킷을
  목록에서 선택하거나 웹 콘솔에서 새로 생성한다
- 신규 학과 코드는 기존 `config/departments/{code}.yaml`과 중복될 수 없으며 입력 중
  가용성을 확인하고 서버에서 생성 직전에 다시 검사한다
- 리소스 생성은 `계획 미리보기 → 사용자 확인 → 백그라운드 실행 → 결과 검증` 순서다.
  버킷은 uniform access, public access prevention, Standard, soft delete 7일로 생성한다
- 계획에 자동 생성된 버킷 이름과 코퍼스 표시 이름은 생성 전에 수정할 수 있고 서버가
  형식과 중복을 다시 검증한다. 코퍼스 기본값은 `{code}-rag-corpus-staff`,
  `{code}-rag-corpus-student`다
- 생성은 누락 리소스만 대상으로 하며 부분 실패 시 성공한 리소스를 유지하고 실패
  항목만 다시 시도한다. 상태 확인만으로는 리소스를 생성하지 않는다
- `hwpOriginal`과 `source`, 교직원과 학생 코퍼스는 각각 서로 달라야 한다
- 버킷 선택 시 기존 사용 학과를 표시한다. 공유드라이브 ID는 다른 학과와 중복 불가
- Drive 범위의 `버킷에서 자동 찾기`는 선택한 버킷 객체명에서 `fileId`를 복원하고
  `doc_state.driveId`를 우선 사용한다. 상태가 없으면 Compute SA로 Drive API를 조회하며,
  다른 학과에 이미 연결된 Drive는 결과에 표시하되 입력란에는 자동 추가하지 않는다
- `폴더 정보 확인`은 입력한 `syncFolderIds`를 Compute SA로 Drive API에서 조회한다.
  설정에 저장되는 ID는 유지하고, 확인된 실제 폴더명을 태그와 학생 폴더 선택 목록에 표시한다.
  폴더가 아닌 항목·휴지통 항목·접근 불가 ID는 개별 실패로 안내한다
- `동기화 관리` 탭은 학과별 변경분 동기화 또는 전체 backfill을 수동 실행한다.
  선택 학과의 Drive만 Workflow 인자로 넘기며, 같은 Drive에 ACTIVE 실행이 있으면 중복 실행을
  거부한다. backfill은 확인 창을 거친 뒤 실행한다
- GUI가 만든 수동 실행은 `runId`를 Workflow와 `rag-sync`에 전달한다. backfill 중간 상태는
  Firestore `sync_tokens/__run__{runId}`에 단계·처리 건수·누적 totals로 기록하며 GUI가
  Workflow 실행 상태와 함께 2초 간격으로 조회한다. 진행 기록 실패는 실제 동기화를 중단하지 않는다
- 학과 YAML을 새로 생성하면 `동기화 관리`로 이동해 해당 학과가 선택된다. 생성만으로
  backfill을 자동 시작하지 않으며 비용이 드는 전체 적재는 항상 사용자가 명시적으로 실행한다
- 학과 목록 행을 누르면 실제 Cloud Run 교직원·학생 MCP URL과 준비 상태를 표시한다.
  상세 패널에서 각 URL을 개별 복사할 수 있다
- `코퍼스 대화` 탭은 학과와 교직원·학생 범위를 선택해 Vertex RAG의 실제 상위
  컨텍스트와 출처를 조회한다. 생성형 답변은 만들지 않으며 토큰은 서버 밖으로 노출하지 않는다
- 공유드라이브 ID는 저장 전에 현재 프로젝트의 기본 Compute SA
  (`{projectNumber}-compute@developer.gserviceaccount.com`)로 실제 접근 및 변경 토큰을 확인
- 기존 YAML 수정 시 MCP 키는 API로 반환하지 않고 그대로 보존한다. revision hash가
  달라지면 `409 REVISION_CONFLICT`로 동시 수정을 막는다
- 상태 확인은 LOCAL → RESOURCE → DEPLOY → RUNTIME → SYNC 순서이며 GCP 리소스를
  변경하지 않는다. 공통 조회는 실행당 한 번 캐시하고 학과 검사는 최대 4개 병렬 수행

설정 책임은 다음과 같이 나눈다.

| 파일 | 내용 |
|---|---|
| `config/common.yaml` | 프로젝트, 리전, Artifact Registry, Firestore, 성능·검색 기본값 |
| `config/departments/{code}.yaml` | 학과명, 코퍼스 2개, 버킷 2개, Drive 범위, MCP 키, 최소 인스턴스 |

`dept_config.py`는 레거시 공용 버킷 키를 읽을 수 있지만, 현행 GUI와 신규 학과 운영은
학과 YAML의 버킷 2개를 필수로 취급한다. 한쪽만 지정하거나 두 값이 같으면 거부한다.

학과 설정 검증 계약:

- 파일명과 `code`는 영문 소문자로 시작하는 2~20자 영숫자·하이픈
- 두 코퍼스는 common의 프로젝트·리전과 일치하는 실제 리소스이며 서로 달라야 함
- 두 버킷은 common의 리전에 있고 uniform bucket-level access와 public access
  prevention이 적용된 실제 리소스이며 서로 달라야 함
- `driveIds`, `syncFolderIds`, `studentFolderIds`는 중복·공백을 정규화하고,
  `studentFolderIds`는 `syncFolderIds`의 부분집합이어야 함
- 공유드라이브 ID는 학과 간 중복 불가
- `minInstances.staff`, `minInstances.student`는 0 이상의 정수
- `keys.staff`, `keys.student`는 서로 달라야 하며 신규 생성 때 서버가 자동 발급

상태 레이어:

| 레이어 | 검사 |
|---|---|
| LOCAL | YAML 파싱·스키마·공통 설정 일치·MCP 키 존재/분리/길이 |
| RESOURCE | gcloud 로그인, 버킷 위치, Native Firestore, 코퍼스 ACTIVE, Drive SA 실접근 |
| DEPLOY | parser·sync·학과별 MCP 2개의 Ready 및 latest revision 일치 |
| RUNTIME | 준비된 서비스의 `/health`; parser·sync는 사용자 ID token으로 호출 |
| SYNC | `rag-daily-sync` 최근 실행 상태와 완료 후 경과 시간 |

전체 상태 우선순위는 `FAIL > WARN > UNKNOWN > OK`다. YAML이 없거나 LOCAL이
실패하면 이후 레이어는 `SKIP`; 설정 파일이 바뀌면 이전 결과는 `STALE`로 표시한다.

주요 로컬 API는 다음과 같다. 변경 요청은 `/api/v1/session`이 발급한
`X-Local-Session` 값이 필요하다.

| 메서드·경로 | 기능 |
|---|---|
| `GET /api/v1/environment` | common 설정, gcloud 로그인·프로젝트, Drive 확인용 SA 표시 |
| `POST /api/v1/gcloud-auth/login` | 시스템 브라우저에서 gcloud 사용자 로그인 유도 |
| `GET /api/v1/common-config/resources` | 선택 프로젝트의 Artifact Registry·Firestore 조회 |
| `POST /api/v1/common-config` | 최초 공통 설정 생성(기존 파일 덮어쓰기 금지) |
| `GET /api/v1/departments/resource-options` | 표시 이름이 포함된 코퍼스와 보호된 리전 버킷 조회 |
| `POST /api/v1/departments/drive-preflight` | 입력한 공유드라이브 ID의 Compute SA 실접근 확인 |
| `POST /api/v1/departments/preview` | 신규 YAML 정규화·검증·비밀값 제거 preview |
| `POST /api/v1/departments` | 신규 학과 YAML 원자적 생성과 MCP 키 자동 생성 |
| `GET /api/v1/departments/{code}/config` | 비밀 키를 제외한 기존 학과 설정 조회 |
| `POST /api/v1/corpus-query` | 선택한 학과·범위 코퍼스의 실제 검색 컨텍스트 조회 |
| `POST /api/v1/departments/{code}/preview` | 수정안 검증과 preview |
| `PUT /api/v1/departments/{code}` | revision 확인 후 수정, 기존 MCP 키 보존 |
| `POST /api/v1/status-runs` | 전체 또는 지정 학과 온라인 상태 검사 시작 |
| `GET /api/v1/status-runs/{runId}` | 진행률·검사 결과 조회 |
| `DELETE /api/v1/status-runs/{runId}` | 실행 취소 요청 |

보안 경계:

- 응답에 MCP 키, gcloud access/ID token, Authorization 헤더를 포함하지 않는다
- YAML preview의 키는 `<자동 생성>` 또는 `<기존 키 유지>`로만 표시한다
- 서버는 loopback 주소에만 bind하며 Origin, CSP, frame 차단 헤더를 적용한다
- subprocess는 인자 배열과 `shell=False`를 사용한다
- SA JSON 키를 만들지 않고 짧은 수명의 impersonated access token만 사용한다


---

## Ⅲ. 처리 명세

### 1. 수집·변환

| 형식 | 처리 |
|---|---|
| HWP / HWPX | parser → MD 색인 |
| PDF / PPTX / DOCX / TXT | 복사 후 색인. PDF는 필요 시 분할 |
| Google 문서 | export 후 색인 |
| XLSX | 셀 → MD 표. 원본은 색인 안 함 |
| 이미지 / 압축 | 제외 |

- 색인 확장자: `.md` `.meta.md` `.pdf` `.txt` `.html` `.docx` `.pptx` `.csv`
- 일 배치: Scheduler 00:00 KST → Workflows → rag-sync
  - `/sync/changes` 기본 200건. pageToken은 색인 성공 후에만 커밋
  - 토큰 없으면 `/sync/backfill-run`

### 1-1. Drive 경로 → 소속

- `DRIVE_IDS`: 공유 드라이브 ID (`folders/` URL의 `0A…`)
- `SYNC_FOLDER_IDS`: 수집 범위. 지정 폴더와 하위. **배포 필수**(비우면 거부)
- `STUDENT_FOLDER_IDS`: 학생 코퍼스에 실을 폴더. `SYNC_FOLDER_IDS`의 부분집합. 여기서 빼면 수집 자체가 안 됨
- 판정: ingest 때 조상 폴더가 `STUDENT_FOLDER_IDS` 안이면 `STUDENT`, 아니면 `STAFF`
- 실패·필드 없음·모르는 값 → `STAFF`
- `STUDENT`: 양쪽 코퍼스. `STAFF`: 교직원만
- 범위 밖(`SYNC` 밖): `EXCLUDED`. 다운로드·색인 없음. 기존 청크는 회수
- `SKIPPED`: 대상인데 처리 못 함 (미지원 MIME, 암호 파일 등)

| 폴더 | 교직원 코퍼스 | 학생 코퍼스 |
|---|---|---|
| `STUDENT_FOLDER_IDS` 안 | 들어감 | 들어감 |
| `SYNC` 안, 학생 폴더 밖 | 들어감 | 안 들어감 |
| `SYNC` 밖 | 안 들어감 | 안 들어감 |

검색은 `audience`를 다시 보지 않음. MCP가 가리키는 코퍼스에 들어 있는 것만 나옴.

**제약 — 내용 그대로 폴더만 이동하면 코퍼스가 안 따라감**

- Drive에서 파일을 학생↔교직원 폴더로 끌어다 옮기기만 하면 소속 변경이 코퍼스에 반영되지 않음
  - `modifiedTime`이 안 바뀌면(이동만 하면 보통 안 바뀜) ingest 앞단에서 `UNCHANGED`로 끊김 — 소속 재판정도 안 함
  - 바뀌어도 내용 해시가 같으면 `HASH_UNCHANGED` — `doc_state.audience`만 갱신되고 코퍼스는 그대로
  - 코퍼스 반영 지점은 `/sync/index-gcs`의 `_sync_student_corpus` 하나뿐인데, 두 경로 모두 URI를 안 넘김
- 결과: 학생→교직원 이동분은 **학생 코퍼스에 계속 남고**, 교직원→학생 이동분은 **안 들어옴**
- 수용 사유: 실무에서 자료 위치 이동이 사실상 없음. 자동 감지 비용(전량 소속 재판정)이 빈도에 비해 큼
- 필요할 때의 회피
  - 파일 내용을 실제로 수정 → 해시가 달라져 정상 경로를 탐
  - 또는 `doc_state`에서 해당 `fileId` 문서를 지우고 재동기화 → 신규로 취급
- 삭제(`/sync/delete`)와 범위 밖 이탈(EXCLUDE)은 이 제약과 무관 — 양쪽 코퍼스에서 모두 회수됨

### 1-2. XLSX → 마크다운 표

- RAG 기본 파서가 xlsx를 못 읽음. 셀을 MD 표로 뽑아 `{fileId}.md` 색인
- [`shared/xlsx_md.py`](../shared/xlsx_md.py) — openpyxl 읽기전용·값모드
- 실측 116건: 변환 89 / 암호(OLE2) 27
- 상한: 셀 300,000 (`MAX_CELLS`), 출력 8MB (`MAX_BYTES`). 잘리면 본문 끝에 표기
- 제약: 병합셀은 좌상단만 값. 수식은 캐시 없으면 빈칸. `cellStyle` name 없으면 복구 재시도

### 2. 크기 한도

- RAG: PDF·DOCX 50MB, MD·텍스트 10MB
- PDF 초과: `{fileId}.partN.pdf` 분할 (안전계수 0.85). 검색 시 `.partN` 떼고 원문으로
- 한 페이지가 한도면 DLQ
- 그 외 포맷은 분할 없음. `doc_split_queue` + `FAILED`. 큐 소비 코드 없음
- HWP→md 실측 최대 ≈ 64KB (한도의 0.6%). PPTX 한도 초과는 이미지 용량이라 분할해도 텍스트가 안 늘음

### 3. 색인

| 항목 | 값 |
|---|---|
| 청크 | 1,024 / 중첩 256 |
| 임베딩 | `text-multilingual-embedding-002` |
| 벡터 DB | RagManagedDb |
| import | 호출당 URI 최대 25. 배치 24 |

- 같은 fileId는 선삭제 후 import
- `index-gcs`: 교직원 코퍼스 먼저, 이어서 `_sync_student_corpus`
  - 학생 쪽은 이번 배치 fileId를 학생 코퍼스에서 지운 뒤 `audience=STUDENT`만 다시 import
  - 학생 폴더 → 교직원 폴더 이동은 여기서만 학생 노출이 내려감
- `import_files`는 호출 실패만 예외. 파일 거부는 카운트 (`imported` / `failed` / `skipped`)
- 부분 실패면 `INDEXED`로 올리지 않음 (`PARSED` 유지 → 다음 주기 회수)
- `skipped`는 성공으로 셈 (이미 코퍼스에 있는 경우). WARNING 로그는 남김
- 전량 재색인 실측: 1,211건 / 약 50분 / 실패 0

### 4. 검색

```
질의 → retrieveContexts(fetch_k, 거리상한 0.30)
     → 어휘 재정렬(BM25 + RRF)
     → SKIPPED / EXCLUDED / DELETED 제외
     → 문서 단위 병합(문서당 최대 3청크)
     → 상위 k건
```

| 항목 | 값 |
|---|---|
| `TOP_K_DEFAULT` | 5 (1~20) |
| `SEARCH_FETCH_MULTIPLIER` | 3 |
| `SEARCH_FETCH_MAX` | 60 |
| `SEARCH_DISTANCE_THRESHOLD` | 0.30 |
| `SEARCH_LEXICAL_RERANK` | true |
| `SEARCH_MAX_CHUNKS_PER_FILE` | 3 |
| `SEARCH_MAX_TOTAL_CHUNKS` | 15 |

- Vertex `score`는 거리(작을수록 유사). 유사도로 변환하면 순위가 뒤집힘. RRF는 순위만 결합
- 거리 0.30 근거 (골든 100): 정답 0.118~0.275, 무관 질의 0.330~. 여유 ≈ 0.05
- Vertex `metadata_filter`는 retrieve에 인자가 있으나 호출하지 않음. 소속 차단은 코퍼스 분리로 함

### 5. 켜는 순서

이미 INDEXED인 문서는 ingest가 `UNCHANGED`로 빠져 `audience`를 안 찍음. `reindex-pending`도 경로를 다시 판정하지 않음.

1. 학과 yaml 에 `drive.studentFolderIds` / `corpora.student` 설정 후 `rag-sync` 재배포
2. 기존 문서 `audience` 일괄 기록. 건너면 학생 코퍼스는 빈 채
3. 학생 코퍼스 적재
4. 학생 MCP 배포 (위 다항). 4를 먼저 하면 학생 검색만 빔

---

## Ⅳ. 검색 품질 실측 (2026. 7. 28., 단일 코퍼스)

### 1. 방법

- 색인 1,211건에서 시드 고정 무작위 100건. 본문 읽고 질의. 제목 어휘 베끼지 않음 (중복률 17%)
- 본문 없는 문서(엑셀 스텁, 텍스트층 없는 PDF) 제외
- 대상: 배포 MCP `search` 전 구간
- 데이터 [`tests/golden100.json`](../tests/golden100.json) / 전량 [`GOLDEN_EVAL.md`](./GOLDEN_EVAL.md)

| 기준 | 정의 |
|---|---|
| 정확히 그 파일 | 기대 fileId가 상위 k (본문 동일 사본 포함) |
| 같은 자료묶음 | 동일 폴더의 공문·붙임 |

### 2. 결과 (top_k=5)

| 기준 | hit@1 | hit@3 | hit@5 | MRR |
|---|---:|---:|---:|---:|
| 정확히 그 파일 | 46% | 81% | 94% | 0.652 |
| 같은 자료묶음 | 63% | 90% | 97% | 0.766 |

- 빈 결과 1/100, 무관 질의 차단 6/6, 평균 본문 9,006자
- hit@5는 신규 62건이 본문 200자 이상 필터라 유리. 기존 38건 hit@5는 89%. 구간으로 89~94%
- hit@1은 두 구간이 45% vs 47%

### 3. 미검출 6건

| 유형 | 건수 |
|---|---:|
| 같은 묶음이 대신 검출 | 4 |
| 실제 미검출 | 2 |

- 18번: 표 셀 한 칸, 거리 임계로 제외
- 39번: 일상어 질의면 전량 차단. 문서 어휘로 바꾸면 1위 (거리 0.283)

### 4. 개선 시도

| 시도 | 결과 | 판정 |
|---|---|---|
| 문서 단위 청크 병합 | 조문 누락 완화 | 채택 |
| 거리 임계값 | 무관 질의 6/6 | 채택 |
| 어휘 재정렬(BM25+RRF) | 편향 골든셋 기준. 무편향 재측정 없음 | 유지, 효과 미확정 |
| 중복 사본 제거 | hit@1 +1 | 미미 |
| 내용 없는 문서 제외 | hit@1 -2%p | 없음 |
| 머리말 축약 | MRR +0.003, hit@3 -5%p | 가설 기각 |
| 청크 2048 | 미실시 | 보류 |

---

## Ⅴ. 운영

### 1. 로컬 관리 콘솔

저장소 루트에서 실행한다.

```powershell
pip install -r requirements-gui.txt
python scripts/dept_gui.py
```

- 기본 브라우저가 `http://127.0.0.1:8765`로 열린다
- `전체 상태 확인`은 YAML 구조, GCP 인증, 학과별 버킷·코퍼스·Drive SA,
  Cloud Run 배포·`/health`, 최근 Workflow 실행을 검사한다
- gcloud 로그인 창을 닫은 경우 `로그인 다시 열기`로 기존 로그인 프로세스를 정리하고 재시도한다
- 상태 조회는 읽기 전용이다. 학과 생성·수정과 최초 common 생성만 파일을 변경한다
- 2026. 8. 26. 실측: 학과 2개 전체 확인 5.7초(콜드 스타트 없음)

검증:

```powershell
python -X utf8 -m pytest -q tests/test_dept_gui.py
cd gui
npm test
```

### 2. 자동화

- Scheduler → Workflows → rag-sync, 매일 00:00 KST
- 변경 없을 때 약 18초

### 3. 처리량

| 항목 | 값 |
|---|---|
| Vertex RagDataService | 300 req/min |
| import 지연 | 약 21초/회 (URI 수 무관) |
| 재색인 배치 | URI 24 |
| 삭제 동시 / 페이싱 | 1 / 1.1초 (`RAG_DELETE_CONCURRENCY` / `RAG_DELETE_PACING_SECONDS`) |

### 4. 접근제어

| 서비스 | 방식 |
|---|---|
| `rag-mcp-{dept}-staff` | 공개 URL + 학과 교직원 키 (`ALLOW_UNAUTH=false`면 IAM 전용) |
| `rag-mcp-{dept}-student` | 같은 학과의 별도 학생 키. 공개 여부는 위와 같음 |
| rag-sync | 프로젝트 IAM |
| rag-parser | rag-sync SA |

Drive 권한은 GCP IAM과 별개다. 현재 프로젝트의 기본 Compute SA를 각 학과
공유드라이브 멤버(뷰어 이상)로 추가해야 한다. 콘솔의 `연결 확인`은 이 멤버십과
Drive 변경 토큰 발급을 실제로 확인하지만 SA 생성이나 Drive 멤버 추가는 수행하지 않는다.

### 5. 상태 (2026. 7. 28.)

```
INDEXED 1,211 / SKIPPED·EXCLUDED 394 / FAILED 0 / PENDING 0
DLQ 0 / 분할 대기 0 / 고아 객체 0
```

당시 단일 코퍼스. 분리 켠 뒤의 코퍼스별 건수는 재집계 필요.

---

## Ⅵ. 제약·과제

### 1. 기능

- 암호 xlsx 27건: OLE2라 변환 불가
- 스캔 PDF: 텍스트 계층 없으면 본문 없음 (표본 50건 중 3)
- 동일 파일명 사본 122건(10.1%), 초안·의견수렴본 201건(16.6%). 최신본 판별 없음
- 기존 INDEXED는 경로가 안 바뀌면 `audience`가 안 갱신됨. 이미 색인된 코퍼스에서 분리를 켜면 일괄 기록이 필요 (빈 `doc_state`에서 전량 backfill로 시작하면 해당 없음)
- 내용 불변 + 폴더만 이동하면 학생 코퍼스가 안 따라감 — 수용된 제약, 상세는 Ⅲ-1-1

### 2. 운영 (PoC 밖)

| 과제 | 사유 |
|---|---|
| MCP 키 → Secret Manager | 현재 Cloud Run 환경변수와 학과 YAML에 평문 저장 |
| 서비스별 전용 SA·최소 권한 | 기본 Compute SA의 프로젝트 단위 과도한 권한 축소 필요 |
| 알림·예산 정책 적용 확인 | 설정 스크립트는 있으나 실제 GCP 적용 여부 별도 확인 필요 |
| Firestore PITR·삭제 보호 | 상태 DB 오조작 시 즉시 복구 수단 없음 |
| 학과별 GCS 버전관리·IAM 분리 | 원본 공문 보호와 덮어쓰기 복구 필요 |
| MCP `minInstances` 조정 | 0이면 첫 요청에 콜드 스타트 지연 발생 |
| `doc_split_queue` 소비자 구현 | 분할 불가능한 한도 초과 문서는 현재 큐에만 적재 |

### 3. 품질

- 측정 없는 개선은 권하지 않음 (Ⅳ-4)
- 어휘 재정렬 on/off 재측정
- 암호·스캔 문서는 검색 불가로 남김

---

## 부록. 평가 재현

```bash
export MCP_URL=https://<서비스>/mcp
export MCP_API_KEY=<키>
python scripts/eval_golden.py tests/golden100.json
```

- 골든셋: `tests/golden100.json` (100건, 시드 고정)
- 결과 문서: `python scripts/gen_eval_doc.py <결과.json> docs/GOLDEN_EVAL.md`
- 청크 분석: `python scripts/analyze_chunking.py <코퍼스 디렉터리>`
- 학생 MCP를 재려면 URL·키를 학생 서비스로 둔다. 골든셋은 교직원 코퍼스 기준이라 학생 hit는 따로 봐야 함
