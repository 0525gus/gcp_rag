# 삭제·학과 라우팅·비동기 색인 안전성 수정

2026-09-10 로컬 구현. 운영 배포는 아직 수행하지 않았다.
대상: 인수인계 작업 RAG-001, CFG-001, ASYNC-001.

## 변경 사항

- RAG 삭제는 명시적인 `NotFound`만 이미 없는 파일로 처리한다. 권한 오류,
  쿼터 소진, 통신 실패 등은 호출자에게 전달해 import와 성공 처리를 중단한다.
  실패한 RagFile의 매핑은 남긴다. 학생 코퍼스의 `removed`는 실제 삭제한
  RagFile 수다(Drive 문서 수가 아니며 본문과 사이드카는 별개로 센다).
  매핑이 없고 fallback scan도 꺼져 있으면 삭제를 확인할 수 없어 실패한다.
  Cloud Tasks 사용 시 fallback scan은 다른 worker의 변경을 반영하도록
  프로세스 캐시를 재사용하지 않는다. 매핑 누락이 많으면 목록 조회 비용이 늘어난다.
- 학과 JSON의 문법 오류, 중복 키, 잘못된 필드 타입, 필수 corpus/bucket/Drive/
  folder 누락, Drive·corpus 중복, 학생 corpus와 folder의 불완전한 쌍을 거부한다.
  학과별 값은 배포 단계에서 병합된 완전한 맵을 사용하며 런타임에서 공용값을
  상속하지 않는다. 학생 설정이 없는 학과는 학생 분리가 꺼진다.
- sync 이미지와 배포 스크립트는 `DEPARTMENTS_REQUIRED=true`를 지정한다.
  잘못된 설정은 FastAPI startup과 `/health`에서 실패한다. 기존 단일 코퍼스
  로컬 도구는 맵과 필수 플래그를 지정하지 않은 경우에만 기존 모드를 유지한다.
- 비동기 job과 task에 문서 버전 및 학과 라우팅 hash를 기록한다. 문서 버전은
  modifiedTime, contentHash, audience, Drive, 경로 등에서 계산한다.
  상태 전이와 lastSyncedAt은 버전에 포함하지 않는다.
- Firestore transaction으로 part와 해당 문서들의 실행 소유권을 함께 획득한다.
  동일 task의 동시 delivery와 동일 문서의 다른 작업은 함께 변경을 수행하지
  못한다. 같은 문서의 faculty/student part도 순서대로 실행된다.
- Cloud Tasks가 켜져 있으면 ingest, 동기 색인, 복구 색인의 import/학생 동기화,
  삭제도 같은 문서 잠금을 사용한다. 따라서 import 도중 정상 ingest 경로가
  같은 GCS URI를 새 내용으로 덮어쓰지 않는다.
- 이전 버전은 RAG 변경 전에 중단한다. 작업 도중의 검사와 part 완료에도
  소유권·버전을 확인한다. 전체 part 성공 후 문서의 `INDEXED`와 job의 `DONE`은
  같은 transaction에서 확정한다. 완료 직전 버전 변경도 CAS로 차단한다.
- part 완료 후 응답 전에 프로세스가 종료돼도 DONE task 재전달 또는 job 상태
  polling이 finalizer를 다시 실행한다. 이미 실패한 job은 성공으로 되살리지 않는다.
- Cloud Tasks dispatch deadline은 job timeout에 맞추되 60~1800초로 제한한다.

## 외부 호출 중단 시의 처리

Vertex RAG 변경은 Firestore transaction에 포함할 수 없다. 잠금 만료만으로
다른 worker를 시작하면 이전 RPC가 나중에 완료되어 다시 덮어쓸 수 있다.
이 구현은 다음과 같이 안전 쪽으로 실패한다.

| 단계 | 재획득 |
|---|---|
| `CLAIMED` | 120초 만료 후 가능. 이전 소유자는 변경을 시작할 수 없다. |
| `MUTATING` | 시간 만료만으로는 불가. 정상 종료 또는 확인 후 해제가 필요하다. |
| `UNCERTAIN` | RPC timeout/연결 오류 등으로 결과 불명. 자동 해제하지 않는다. |

잠금은 설정된 `SYNC_TOKEN_COLLECTION`(기본 `sync_tokens`)의
`__mutation__<fileId>` 문서다. `owner`, `phase`, `jobPath`, `partPath`를 기록한다.
관련 job은 오류를 기록하거나 deadline 도달 시 `FAILED`로 관찰된다.
실패한 작업의 문서는 재색인이 필요하며 성공으로 간주하지 않는다.

### 고착된 MUTATING/UNCERTAIN 잠금 복구

1. 관련 Scheduler/Workflow와 큐의 새 실행을 멈추고, 해당 Cloud Run 요청이 더
   실행되지 않는지 확인한다. 큐를 멈추는 것만으로 이미 실행 중인 요청이 끝나지는 않는다.
2. 잠금의 job/part, 현재 doc_state, GCS 산출물, Vertex 작업과 RagFile을 대조한다.
   외부 RPC가 아직 진행 중이거나 결과를 확인할 수 없다면 잠금을 유지한다.
3. 기존 실행과 Vertex 작업의 종료를 확인한 뒤 해당 job을 `FAILED`로 둔다.
   복구 직전에 `owner`가 바뀌지 않았는지 transaction으로 재확인하고 **해당
   문서 잠금만** 제거한다. 컬렉션 전체 삭제나 TTL 정책을 적용하지 않는다.
4. 현재 Drive 버전으로 ingest 후 새 비동기 job을 생성한다. 두 코퍼스의 결과와
   `INDEXED`를 확인하고 정기 실행을 재개한다.

이 변경은 프로세스 중단 뒤 자동 복구를 완성하는 작업(ASYNC-002)이 아니다.
완료 여부를 알 수 없는 외부 변경은 운영 확인을 요구한다.

## 배포 순서

1. 학과 맵 생성 결과가 새 검증을 통과하는지 확인한다. 공용 버킷을 사용하는
   학과도 맵에는 해석된 bucket/corpus/folder 값을 명시해야 한다.
2. Scheduler/수동 Workflow의 새 실행을 멈추고 기존 작업과 요청을 종료까지
   기다린다. 이전 revision은 새 문서 잠금을 모르므로 동시에 변경 작업을
   처리하도록 두지 않는다.
3. sync 새 이미지를 배포하고 startup/health와 `DEPARTMENTS_REQUIRED=true`를
   확인한다. Firestore transaction은 기존 sync 계정의 읽기·쓰기 권한을 사용한다.
4. 이전 schema의 큐 작업은 버전 정보가 없어 실패 처리된다. 기존 실패/PARSED
   문서는 새 job으로 재접수한다. pageToken을 임의로 건너뛰지 않는다.
5. 정상 색인, 같은 task 재전달, 학생→교직원 이동을 staging에서 확인하고 실행을 재개한다.

## 검증 범위

최종 로컬 결과: `651 passed, 6 skipped, 2 xfailed`.
수정 Python 파일의 Ruff `E9,F,B023`, Python compileall, 배포 PowerShell 구문 검사,
`git diff --check`를 통과했다. 기존 openpyxl 기본 스타일 경고 1건은 남아 있다.

`test_fail_closed.py`, `test_index_guard.py`, `test_index_tasks.py`와 기존 라우팅·
삭제·색인 테스트로 검증한다. Firestore의 실제 SDK transaction 재시도 decorator를
사용하는 메모리 저장소로 경합 및 CAS 충돌을 재현한다. GCP 서비스와의 종단 검증,
실제 권한 오류 및 프로세스 강제 종료 복구 훈련은 배포 전에 별도로 수행해야 한다.
