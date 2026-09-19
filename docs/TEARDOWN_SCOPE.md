# 삭제 범위 (학과 / 공통 런타임)

## 왜 이 문서가 필요한가

만드는 쪽은 되돌릴 수 있고 지우는 쪽은 아니다. 그런데 한동안 학과 철거는 **눈에 보이는
네 가지**(MCP Cloud Run, 코퍼스, 버킷, 설정 파일)만 지웠다. 나머지는 오류를 내지 않고
남았고, 남았다는 사실 자체가 화면 어디에도 없었다.

남은 것이 조용히 일으키는 사고는 두 가지다.

- **Firestore 동기화 이력이 남는다.** 같은 공유드라이브를 다시 등록하면 `contentHash`가
  같아 전부 `HASH_UNCHANGED`로 건너뛴다. 새 코퍼스는 영원히 비어 있고, 오류 로그는
  한 줄도 없다.
- **rag-sync의 `DEPARTMENTS_JSON`이 낡는다.** 없어진 버킷으로 계속 라우팅해 업로드가
  전량 404 → DLQ로 간다(`runtime_env_drift` 주석의 실측: 1445건).

## 학과 하나가 소유하거나 건드리는 것

| 대상 | 근거 코드 | 계획 키 |
|---|---|---|
| MCP Cloud Run `rag-mcp-{code}-{audience}` | `_deploy_and_runtime_status`, `deploy_mcp.ps1` | `mcp-{audience}` |
| Vertex RAG 코퍼스 `corpora.staff` / `.student` | `shared/rag_engine.py` | `corpus-{audience}` |
| GCS 버킷 `buckets.hwpOriginal` / `.source` | `shared/gcs.py` | `bucket-{slot}` |
| Firestore `doc_state` (driveId 기준) + 하위 `rag_files` | `shared/firestore_state.py`, `shared/rag_mapping.py` | `firestore-state` |
| Firestore `sync_tokens` (driveId 문서) | `firestore_state.set_start_page_token` | `firestore-state` |
| Firestore `doc_dlq` / `doc_split_queue` (fileId 문서) | `firestore_state.enqueue_dlq` / `enqueue_split` | `firestore-state` |
| 공용 메타데이터 버킷의 `import-results/{corpusId}/` | `rag_engine._new_import_result_sink` | `metadata-{audience}` |
| 학과 설정 YAML (**MCP 키가 여기에만 있다**) | `scripts/dept_config.py` | `config` |
| rag-sync `DEPARTMENTS_JSON` 갱신 (삭제가 아니라 갱신) | `update_sync_department_map` | `sync-env` |

`doc_state`는 fileId 하나로 전 학과가 같은 컬렉션을 쓴다. 학과를 가르는 축은 **`driveId`
하나뿐**이라 그 값으로 골라 지운다. Firestore는 부모 문서를 지워도 하위 컬렉션이 남으므로
`doc_state/{fileId}/rag_files`를 따로 지운다.

공용인 것은 학과 철거에서 건드리지 않는다 — Cloud Tasks 큐(`faculty-rag-sync-queue`,
`student-rag-sync-queue`), 메타데이터 버킷 자체, Firestore 데이터베이스.

## 세 겹의 안전장치

1. **계획을 먼저 보여 준다.** 무엇이 지워지는지 목록으로 확정한 뒤에만 실행한다.
2. **확인 문구를 손으로 받는다.** 학과는 학과 코드, 공통 런타임은 프로젝트 ID.
3. **다른 학과가 참조하는 것은 건너뛴다.** 코퍼스·버킷·공유드라이브를 다른 학과도
   가리키면 화면이 무엇을 보내든 지우지 않는다. 이름만 보고 지우면 남은 학과의 검색이
   **오류 없이 빈 결과**가 된다.

선택을 켜고 끌 수 있게 되면서 3번이 특히 중요해졌다. "체크했으니 지운다"가 공유 검사보다
위에 오면 안 된다 — `apply_teardown_selection`이 공유 대상을 먼저 걸러낸다.

## 선택형 삭제

계획의 각 줄은 켜고 끌 수 있다. 끈 것은 목록에서 사라지지 않고 "선택하지 않아 남깁니다"로
남는다 — **지우지 않은 것이 무엇인지가 지운 것만큼 중요하다.**

조합에 따라 화면과 실행 기록에 같은 경고가 붙는다.

- 설정 파일을 지우면서 리소스를 남기면 → 그 리소스는 콘솔에서 다시 찾을 수 없다.
- 설정 파일을 지우면서 `sync-env`를 끄면 → 없어진 버킷으로 계속 동기화를 시도한다.
- 코퍼스·버킷을 지우면서 `firestore-state`를 끄면 → 같은 드라이브를 다시 등록해도
  재색인되지 않는다.

## 실행 순서

```text
MCP Cloud Run → 코퍼스 → 버킷 → Firestore 이력 → import 결과 객체 → 설정 파일 → sync-env
```

설정 파일이 뒤인 이유: 앞이 하나라도 실패하면 남겨야 콘솔에서 다시 찾아 재시도할 수 있다.
`sync-env`가 맨 뒤인 이유: 설정이 사라진 **뒤**라야 남은 학과만으로 라우팅 맵이 만들어진다.

공통 런타임은 `Scheduler → Workflow → rag-sync → rag-parser` 순이다. 거꾸로 지우면 살아
있는 잡이 없어진 워크플로를 호출해 실패 이력만 쌓인다.

## 화면

- **서비스 현황** 표의 행마다 삭제 버튼(⌫). 상세를 거치지 않고 계획 창이 바로 열린다.
  모든 학과는 Cloud 등록부를 기준으로 삭제 계획을 만들 수 있다.
- **운영 환경**의 공통 런타임 카드에서 `rag-parser`, `rag-sync`, Workflow, Scheduler의
  상태(리비전·Ready·스케줄)를 보고 카드마다 하나씩 삭제한다. 미배포 카드에는 삭제
  버튼이 없다.

## 관련 API

| 엔드포인트 | 하는 일 |
|---|---|
| `GET /api/v1/departments/{code}/teardown-plan` | 학과 삭제 계획 |
| `GET /api/v1/common-runtime/teardown-plan` | 공통 런타임 삭제 계획 |
| `GET /api/v1/common-runtime/status` | rag-parser·rag-sync·Workflow·Scheduler 상태 |
| `POST .../teardown` (`{confirm, targets}`) | 고른 대상만 실행. `targets` 생략 시 전체 |
| `GET /api/v1/teardowns/{run_id}` | 진행 상황 |

`google-cloud-firestore`가 없는 환경에서는 `firestore-state` 대상만 실패로 남고 나머지
삭제는 그대로 진행된다. 콘솔은 gcloud만으로 도는 것을 전제로 `requirements-gui.txt`를
최소로 유지하기 때문이다.
