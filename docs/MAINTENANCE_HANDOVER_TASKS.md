# 유지보수·인수인계 개선 작업

이 문서는 2026-09-03 저장소 전체 검토에서 확인한 위험을 실제 작업 단위로 정리한다.
운영 장애, 데이터 접근 경계, 비밀정보 노출 가능성을 먼저 처리하고 구조 개선과
문서화는 그 다음 단계에서 진행한다.

## 사용 방법

- 상태: `TODO` → `IN PROGRESS` → `DONE` 또는 `BLOCKED`
- 우선순위: `P0`은 즉시 조치, `P1`은 다음 릴리스 전, `P2`는 계획된 구조 개선
- 각 작업을 시작할 때 담당자와 목표 릴리스를 채운다.
- 완료 조건과 검증 항목을 모두 만족해야 `DONE`으로 변경한다.
- 운영 설정이나 비밀값은 이 문서에 기록하지 않는다.

## 작업 목록

| ID | 우선순위 | 상태 | 작업 | 담당자 | 목표 릴리스 |
|---|---|---|---|---|---|
| RAG-001 | P0 | TODO | RAG 삭제 실패를 fail-closed로 변경 | 미지정 | 미지정 |
| SEC-001 | P0 | TODO | 학과 설정의 Cloud Build 업로드 차단 및 키 회전 | 미지정 | 미지정 |
| OPS-001 | P0 | TODO | Workflow의 실패·무실행 정상 처리 제거 | 미지정 | 미지정 |
| CFG-001 | P1 | TODO | 학과 라우팅 설정을 fail-closed로 변경 | 미지정 | 미지정 |
| ASYNC-001 | P1 | TODO | Cloud Tasks 멱등성 및 문서 버전 보호 | 미지정 | 미지정 |
| ASYNC-002 | P1 | TODO | 장시간 재색인을 내구성 있는 실행기로 이전 | 미지정 | 미지정 |
| CI-001 | P1 | TODO | CI를 실제 운영 빌드와 동일한 범위로 확장 | 미지정 | 미지정 |
| REL-001 | P1 | TODO | 재현 가능한 빌드·배포·롤백 체계 구축 | 미지정 | 미지정 |
| IAM-001 | P1 | TODO | 서비스별 전용 계정과 최소 권한 적용 | 미지정 | 미지정 |
| OPS-002 | P1 | TODO | 위험한 정리 작업에 삭제 안전장치 추가 | 미지정 | 미지정 |
| GUI-001 | P2 | TODO | 운영 GUI 모듈화 및 작업 상태 영속화 | 미지정 | 미지정 |
| CFG-002 | P2 | TODO | 설정·비밀정보의 중앙 원본과 복구 절차 구축 | 미지정 | 미지정 |
| DOC-001 | P2 | TODO | 운영·인수인계 문서와 책임 체계 보완 | 미지정 | 미지정 |
| TECH-001 | P2 | TODO | 미완성 운영 경로 정리 | 미지정 | 미지정 |

## P0 — 즉시 조치

### RAG-001 RAG 삭제 실패를 fail-closed로 변경

문제:

- `delete_files_by_ids()`가 개별 삭제 예외를 삼키므로 호출자는 실패를 알 수 없다.
- 삭제가 실패해도 새 청크를 import하고 문서를 `INDEXED`로 기록할 수 있다.
- 학생 자료가 교직원 전용으로 이동한 경우 학생 코퍼스에 이전 청크가 남아도 성공으로
  처리될 수 있다.

작업:

- [ ] 삭제 결과를 성공, 이미 없음, 실패 목록으로 구분하는 구조화된 결과로 변경한다.
- [ ] 허용된 not-found 외 실패가 하나라도 있으면 import와 상태 변경을 중단한다.
- [ ] 학생 코퍼스의 `removed` 값을 요청 건수가 아닌 실제 삭제 결과로 계산한다.
- [ ] 삭제 실패 시 RAG 매핑을 제거하거나 `INDEXED`로 전환하지 않도록 한다.
- [ ] 실제 하위 `rag.delete_file` 예외를 재현하는 테스트를 추가한다.
- [ ] 학생→교직원 이동 시 삭제 실패 테스트를 추가한다.

완료 조건:

- 삭제 실패가 발생한 실행은 성공으로 보고되지 않는다.
- 기존 청크와 새 청크가 함께 남은 상태에서 `INDEXED`가 되지 않는다.
- 학생 코퍼스 삭제 실패가 운영 알림 대상 오류로 전파된다.

근거:

- `../shared/rag_engine.py:529`
- `../shared/rag_engine.py:551`
- `../services/sync/main.py:1838`
- `../services/sync/main.py:2201`
- `../services/sync/main.py:2230`

### SEC-001 학과 설정의 Cloud Build 업로드 차단 및 키 회전

문제:

- 학과 YAML은 MCP 키를 평문으로 저장하고 Git에서만 제외한다.
- `.gcloudignore`에는 `config/departments/*.yaml` 제외 규칙이 없다.
- Cloud Build가 저장소 루트 전체를 build context로 제출한다.

작업:

- [ ] 현재 학과 MCP 키를 모두 회전한다.
- [ ] 과거 Cloud Build source/staging 접근 주체와 보존 상태를 점검한다.
- [ ] `.gcloudignore`에 학과 실설정 제외 규칙을 추가한다.
- [ ] `.dockerignore`를 추가하고 비밀·로컬 설정 파일을 제외한다.
- [ ] 빌드 전 `gcloud meta list-files-for-upload` 결과에 금지 경로가 있으면 실패시키는
  preflight를 추가한다.
- [ ] 가능하면 저장소 전체가 아닌 서비스별 최소 build context를 사용한다.
- [ ] MCP 키를 Secret Manager로 이전하는 `CFG-002` 작업을 연계한다.

완료 조건:

- 실제 학과 YAML이 Cloud Build 업로드 목록과 Docker context에 나타나지 않는다.
- 이전 키가 폐기되고 새 키의 배포·연결 확인이 끝난다.
- CI 또는 배포 preflight가 동일한 회귀를 자동 차단한다.

근거:

- `../.gitignore:38`
- `../.gcloudignore:1`
- `../cloudbuild.sync.yaml:9`
- `../config/departments/README.md:5`

### OPS-001 Workflow의 실패·무실행 정상 처리 제거

문제:

- ingest 예외가 HTTP 오류 대신 `200`과 `DLQ` 상태로 반환된다.
- Workflow는 DLQ를 `parked`로 처리하고 pageToken 커밋을 허용한다.
- 최종 실패도 `{ok: false}`만 반환하므로 Workflows 실행 상태는 성공이 된다.
- `driveIds`가 누락되면 아무 작업도 하지 않고 `ok: true`로 종료한다.
- backlog 복구는 `runRecovery=false`가 기본이라 별도 실행이 없으면 고착된다.

작업:

- [ ] Workflow 시작 시 `driveIds`, `syncUrl`, `parserUrl`을 검증한다.
- [ ] 빈 `driveIds`는 명시적인 dry-run이 아닌 이상 실패 처리한다.
- [ ] 재시도 가능한 ingest 오류와 영구 DLQ 사유를 구분한다.
- [ ] `totals.failed` 또는 `totals.indexFailed`가 있으면 Workflow 자체를 실패시킨다.
- [ ] `parked`, DLQ, split queue 적체에 별도 metric과 alert를 추가한다.
- [ ] `runRecovery=true` 정기 실행 또는 독립 복구 Workflow를 배포한다.
- [ ] 성공·실패·무실행 세 경우에 대한 Workflow 계약 테스트를 추가한다.

완료 조건:

- 처리 대상이 없거나 필수 인자가 누락된 실행이 성공으로 기록되지 않는다.
- 운영 실패가 Cloud Workflows, Scheduler, Monitoring에서 동일한 실패로 관찰된다.
- 일시 장애 문서가 수동 개입 없이 재시도되고 영구 실패만 DLQ에 남는다.

근거:

- `../services/sync/main.py:1017`
- `../workflows/daily_sync.yaml:27`
- `../workflows/daily_sync.yaml:35`
- `../workflows/daily_sync.yaml:505`
- `../workflows/daily_sync.yaml:709`
- `../workflows/daily_sync.yaml:901`

## P1 — 다음 릴리스 전

### CFG-001 학과 라우팅 설정을 fail-closed로 변경

작업:

- [ ] 다학과 모드에서 `DEPARTMENTS_JSON`이 비었거나 파싱 실패하면 기동을 거부한다.
- [ ] 각 학과의 Drive ID, corpus, bucket, folder 범위를 완전하게 검증한다.
- [ ] Drive ID 중복과 공용 설정으로의 묵시적 fallback을 차단한다.
- [ ] 배포 설정과 런타임 설정의 hash 또는 revision을 기록한다.
- [ ] `/health`와 readiness에 학과 맵·필수 의존성 검증 결과를 포함한다.
- [ ] 깨진 JSON, 부분 설정, 빈 folder scope, 알 수 없는 Drive 테스트를 추가한다.

완료 조건:

- 잘못된 학과 설정으로 서비스가 정상 상태를 보고하지 않는다.
- 한 학과 설정 오류가 다른 학과 corpus/bucket 사용으로 이어지지 않는다.

근거:

- `../shared/config.py:68`
- `../services/sync/main.py:193`
- `../services/sync/main.py:2111`
- `../scripts/_load_env.ps1:119`

### ASYNC-001 Cloud Tasks 멱등성 및 문서 버전 보호

작업:

- [ ] part의 `PENDING → RUNNING` 획득을 Firestore transaction으로 처리한다.
- [ ] lease 만료 시에만 다른 worker가 작업을 인계할 수 있게 한다.
- [ ] task payload에 `modifiedTime`, content hash 또는 GCS generation을 포함한다.
- [ ] 상태 변경 시 예상 문서 버전을 비교하는 CAS/fencing을 적용한다.
- [ ] 오래된 작업은 삭제·import·`INDEXED` 갱신 전에 중단한다.
- [ ] 동시 중복 delivery와 앞뒤 revision 역전 테스트를 추가한다.

완료 조건:

- 동일 task가 동시에 두 번 전달돼도 RAG 변경은 한 번만 반영된다.
- 이전 revision 작업이 최신 문서의 청크나 상태를 변경하지 못한다.

근거:

- `../services/sync/main.py:143`
- `../services/sync/main.py:2485`
- `../services/sync/main.py:2491`

### ASYNC-002 장시간 재색인을 내구성 있는 실행기로 이전

작업:

- [ ] `background=true`의 FastAPI `BackgroundTasks` 실행을 제거하거나 운영에서 차단한다.
- [ ] Cloud Run Job, Cloud Tasks 또는 Workflow 기반 실행으로 이전한다.
- [ ] job에 deadline, heartbeat, lease, cancel, retry 정보를 저장한다.
- [ ] 고착된 `RUNNING` job을 탐지하고 실패·재시도 상태로 전환하는 watchdog을 추가한다.
- [ ] 서비스 재시작·scale-to-zero 중단 복구 테스트를 추가한다.
- [ ] 내부 ingest worker 수와 Cloud Run request concurrency의 곱을 기준으로 메모리 한도를
  다시 산정한다.

완료 조건:

- 요청 응답이나 인스턴스 종료와 무관하게 재색인 진행 상태를 복구할 수 있다.
- 영구 `RUNNING` 상태와 동일 job 중복 실행이 발생하지 않는다.

근거:

- `../services/sync/main.py:2589`
- `../services/sync/main.py:2615`
- `../services/sync/main.py:2679`
- `../scripts/deploy.ps1:269`

### CI-001 CI를 실제 운영 빌드와 동일한 범위로 확장

작업:

- [ ] parser 네이티브 의존성을 설치하고 실제 import·기동 테스트를 수행한다.
- [ ] parser, sync, MCP Docker 이미지를 CI에서 smoke build한다.
- [ ] `npm ci`, build, lint와 모든 `gui/tests/*.test.mjs`를 실행한다.
- [ ] `console-undef.test.mjs`를 `npm test`에 포함한다.
- [ ] Ruff를 차단 게이트로 전환하고 기존 위반을 정리한다.
- [ ] 테스트 모듈의 전역 환경변수 설정을 fixture로 이전한다.
- [ ] 각 테스트 파일 단독 실행 또는 무작위 순서 실행을 추가한다.
- [ ] staging에서 Drive→parser→GCS/Firestore→RAG→MCP smoke test를 운영한다.

완료 조건:

- 운영 Dockerfile과 네이티브 parser가 PR 단계에서 빌드·기동 검증된다.
- 전체 실행과 단독 실행의 테스트 결과가 동일하다.
- 백엔드·프런트엔드 lint와 테스트 실패가 merge를 차단한다.

근거:

- `../.github/workflows/ci.yml:19`
- `../.github/workflows/ci.yml:30`
- `../gui/package.json:12`
- `../gui/tests/console-undef.test.mjs:1`
- `../tests/test_size_and_scale.py:20`
- `../services/mcp_server/main.py:44`

### REL-001 재현 가능한 빌드·배포·롤백 체계 구축

작업:

- [ ] Python direct/transitive dependency lock 파일을 생성하고 추적한다.
- [ ] Python base image를 digest로 고정한다.
- [ ] 이미지를 Git commit SHA로 태그하고 source revision label을 기록한다.
- [ ] parser, sync, MCP 모두 검증된 digest로 배포한다.
- [ ] dirty working tree 배포는 기본 거부하고 명시적인 예외만 허용한다.
- [ ] `ReuseExisting`을 이미지 존재가 아니라 기대 commit/digest 일치로 판단한다.
- [ ] 릴리스 manifest에 commit, image digest, 설정 revision, 배포 시각을 기록한다.
- [ ] 이전 revision으로 traffic을 되돌리는 rollback 절차를 자동화하고 검증한다.
- [ ] API, Run, Workflow, IAM, Scheduler 리소스를 IaC로 이전하거나 최소한 plan/diff와
  부분 실패 reconciliation을 구현한다.

완료 조건:

- 같은 commit을 다시 빌드했을 때 동일한 의존성과 식별 가능한 이미지가 생성된다.
- 현재 운영 revision을 Git commit과 설정 revision까지 역추적할 수 있다.
- 부분 배포 실패 후 재실행 또는 rollback으로 일관된 상태를 복구할 수 있다.

근거:

- `../.gitignore:16`
- `../services/sync/Dockerfile:2`
- `../scripts/deploy.ps1:17`
- `../scripts/deploy.ps1:209`
- `../scripts/deploy.ps1:253`

### IAM-001 서비스별 전용 계정과 최소 권한 적용

작업:

- [ ] parser, sync, MCP, Workflow, Scheduler용 서비스 계정을 분리한다.
- [ ] 각 서비스의 실제 API 호출을 기준으로 최소 IAM role을 정의한다.
- [ ] Cloud Run과 Workflow 배포 시 서비스 계정을 명시한다.
- [ ] 기본 Compute 서비스 계정 fallback을 제거한다.
- [ ] 프로젝트 단위 권한을 가능한 resource 단위 권한으로 축소한다.
- [ ] IAM 정책을 IaC 또는 검토 가능한 manifest로 관리한다.

완료 조건:

- 기본 Compute 서비스 계정이 애플리케이션 런타임에 사용되지 않는다.
- 한 서비스 계정 침해가 다른 서비스·학과 리소스로 확장되지 않는다.

근거:

- `../scripts/deploy.ps1:130`
- `../scripts/deploy.ps1:251`
- `../docs/DEV_SPEC.md:438`

### OPS-002 위험한 정리 작업에 삭제 안전장치 추가

작업:

- [ ] `--apply`에서는 `DELETED` 상태만 기본 대상으로 허용한다.
- [ ] 상태 미확인 객체 삭제는 별도 위험 승인 옵션으로 분리한다.
- [ ] 삭제 전 Drive 존재 여부와 대상 project/database/bucket을 재검증한다.
- [ ] CSV manifest 생성과 별도 승인 후 동일 manifest만 적용하는 2단계 절차를 만든다.
- [ ] 최대 삭제 건수와 비율 제한을 추가한다.
- [ ] GCS generation precondition을 사용한다.
- [ ] Firestore PITR·삭제 보호와 GCS versioning 사전 검사를 추가한다.
- [ ] 잘못된 database, 빈 doc_state, 부분 손상 상태에 대한 안전 테스트를 추가한다.

완료 조건:

- 단일 `--apply` 입력만으로 상태 미확인 객체를 대량 삭제할 수 없다.
- 승인 시점 이후 변경된 객체는 삭제되지 않는다.
- 삭제 전 복구 가능 여부와 정확한 대상 수를 확인할 수 있다.

근거:

- `../scripts/cleanup_orphans.py:102`
- `../scripts/cleanup_orphans.py:169`
- `../scripts/cleanup_orphans.py:177`
- `../scripts/cleanup_orphans.py:212`

## P2 — 구조 개선과 인수인계

### GUI-001 운영 GUI 모듈화 및 작업 상태 영속화

작업:

- [ ] `dept_gui.py`를 config, provisioning, IAM, deploy, teardown, status router/service로
  분리한다.
- [ ] GCP SDK와 CLI 호출을 provider interface 뒤로 이동한다.
- [ ] 메모리의 run/plan 상태를 영속 job store로 이전한다.
- [ ] daemon thread 대신 재시작 가능한 worker를 사용한다.
- [ ] 작업마다 시작자, 대상, 계획, 결과, 오류, 재시도, 완료 시각을 감사 기록으로 남긴다.
- [ ] 3,730줄 전역 `app.js`를 화면·API client·state 단위로 분리한다.
- [ ] Python 운영 콘솔과 Vinext/Cloudflare 프런트의 지원 범위를 결정한다.
- [ ] API proxy가 없는 비기능 `npm start` 경로는 제거하거나 preview 전용으로 명시한다.

완료 조건:

- GUI 프로세스를 재시작해도 실행 중 작업의 상태와 결과를 조회·복구할 수 있다.
- 리소스 생성·배포·철거 변경이 독립 모듈과 테스트로 격리된다.
- 운영자가 지원되는 GUI 실행 명령을 하나로 식별할 수 있다.

근거:

- `../scripts/dept_gui.py:89`
- `../scripts/dept_gui.py:3448`
- `../gui/app/page.tsx:1`
- `../gui/worker/index.ts:28`
- `../gui/public/console/app.js:1`

### CFG-002 설정·비밀정보의 중앙 원본과 복구 절차 구축

작업:

- [ ] MCP 키를 Secret Manager에 저장하고 학과 YAML에는 secret reference만 둔다.
- [ ] 학과별 corpus, bucket, Drive 범위를 접근 통제된 중앙 설정 저장소에서 관리한다.
- [ ] 설정 schema version과 변경 이력을 기록한다.
- [ ] 키 생성, 배포, 회전, dual-key cutover, 폐기 절차를 자동화한다.
- [ ] 운영 PC 분실·교체 상황을 가정한 설정 복원 훈련을 수행한다.
- [ ] GUI의 키 반환 API가 필요한지 재검토하고, 유지한다면 실제 보안 경계를 문서화한다.
- [ ] 브라우저·clipboard·API 응답·로그에서 비밀값 노출을 검증하는 테스트를 추가한다.

완료 조건:

- 개인 PC와 Git 저장소 없이도 승인된 운영자가 전체 설정을 복원할 수 있다.
- 비밀값의 소유자, 접근자, 회전일, 폐기 상태를 감사할 수 있다.
- 문서의 키 전달 정책과 실제 GUI/API 동작이 일치한다.

근거:

- `../config/departments/README.md:15`
- `../config/departments/README.md:20`
- `../scripts/dept_gui.py:6`
- `../scripts/dept_gui.py:5807`
- `../gui/public/console/app.js:376`

### DOC-001 운영·인수인계 문서와 책임 체계 보완

작업:

- [ ] 시스템 owner, 운영 담당자, 보안 승인자와 escalation 경로를 정한다.
- [ ] 장애 등급, RTO, RPO, SLO와 alert 대응 시간을 정의한다.
- [ ] 최초 설치에 Python, Node, PowerShell, gcloud 버전과 필수 IAM/billing을 명시한다.
- [ ] 정상 배포, 부분 실패 복구, rollback, 키 회전, 백업·복원 runbook을 작성한다.
- [ ] `docs/ENV_MIGRATION.md`를 복구하거나 참조를 제거한다.
- [ ] 존재하지 않는 벤치 문서와 샘플 이미지 링크를 정리한다.
- [ ] 문서 링크 검사와 명령 smoke test를 CI에 추가한다.
- [ ] CODEOWNERS, CONTRIBUTING, SECURITY, CHANGELOG 또는 release note 정책을 추가한다.
- [ ] 측정 참고값과 운영 SLO를 명확히 분리한다.

완료 조건:

- 새 담당자가 별도 구두 설명 없이 개발환경 구성, 배포, 상태 확인, rollback을 수행한다.
- 모든 운영 변경과 장애에 책임자와 대응 절차가 연결된다.
- 저장소 내 문서 링크와 재현 명령이 CI에서 검증된다.

근거:

- `../README.md:57`
- `../README.md:144`
- `../README.md:176`
- `../gui/README.md:5`
- `../docs/DEV_SPEC.md:436`
- `../docs/DEV_SPEC.md:460`

### TECH-001 미완성 운영 경로 정리

작업:

- [ ] `doc_split_queue` 소비자를 구현하거나 지원하지 않는 문서를 명시적으로 거부한다.
- [ ] split queue 적체량, 체류 시간과 최장 미처리 시간을 모니터링한다.
- [ ] DocAI fallback을 완성하거나 코드·설정·의존성을 제거한다.
- [ ] fallback을 유지한다면 LibreOffice 존재와 DocAI 설정을 readiness에서 검증한다.
- [ ] 알림 정책을 수동 create-only 스크립트가 아닌 배포/IaC에 포함한다.
- [ ] 기존 알림 정책도 코드 변경에 맞춰 update하도록 한다.
- [ ] Workflow 본문의 `ok` 값과 플랫폼 실행 상태를 함께 감시한다.

완료 조건:

- 설정에는 존재하지만 운영 이미지에서 동작하지 않는 기능이 없다.
- split/DLQ 문서는 소비자 또는 문서화된 운영 절차와 SLA를 가진다.
- 알림 정책 변경이 다음 배포에서 실제 운영에 반영된다.

근거:

- `../docs/PARSER_DOCAI_FALLBACK.md:1`
- `../docs/PARSER_DOCAI_FALLBACK.md:7`
- `../docs/DEV_SPEC.md:443`
- `../docs/DEV_SPEC.md:454`
- `../scripts/setup_alerts.ps1:150`

## 권장 마일스톤

### 마일스톤 1 — 노출·정합성 차단

- [ ] RAG-001
- [ ] SEC-001
- [ ] OPS-001

### 마일스톤 2 — 안전한 다음 릴리스

- [ ] CFG-001
- [ ] ASYNC-001
- [ ] ASYNC-002
- [ ] CI-001
- [ ] REL-001
- [ ] IAM-001
- [ ] OPS-002

### 마일스톤 3 — 인수인계 가능 상태

- [ ] GUI-001
- [ ] CFG-002
- [ ] DOC-001
- [ ] TECH-001

## 최종 인수인계 판정 기준

다음 항목이 모두 충족되어야 운영 인수인계를 완료한 것으로 본다.

- [ ] 운영 배포를 Git commit, image digest, 설정 revision까지 역추적할 수 있다.
- [ ] 개인 PC 없이 설정과 비밀정보를 복원할 수 있다.
- [ ] 잘못된 설정, 빈 대상, 부분 실패가 정상 상태로 보고되지 않는다.
- [ ] 문서 중복·순서 역전·삭제 실패가 corpus 접근 경계를 깨지 않는다.
- [ ] 장시간 작업은 프로세스 재시작 후에도 상태와 재시도 경로를 유지한다.
- [ ] parser·sync·MCP·GUI 운영 산출물이 CI에서 검증된다.
- [ ] 백업 복원과 이전 revision rollback을 실제로 연습했다.
- [ ] 운영자, 승인자, 장애 대응자와 연락·escalation 경로가 지정돼 있다.
