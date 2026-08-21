# `.env` 제거 / 학과 확장 계획

목표: 설정 원본을 `config/` 하나로 모으고 `.env` · `.env.example` 을 없앤다.
그 위에서 학과(= 공유 드라이브)를 늘려도 sync 는 한 벌로 돈다.

**전제: PoC 이고 서비스 중이 아니다.** 돌고 있는 배포를 지키기 위한 단계적
호환 작업은 하지 않는다. 다만 "맵이 비면 기존 동작" 이라는 장치는 남긴다 —
테스트가 학과 설정 없이 돌 수 있어야 하기 때문이다.

## 완료

### 0. 검증 도구

- `tests/_golden/deployed_env.json` — 배포된 Cloud Run 스펙 스냅샷.
  키 값은 길이만 기록해 커밋 가능
- `tests/test_deploy_env_parity.py` — config 가 만드는 env 를 그 스냅샷과 대조.
  `--set-env-vars` 가 env 를 통째로 치환하므로 키 하나가 빠지면 그 값이 사라진다

### 1. MCP 를 config 기반으로

`deploy_mcp.ps1 -Dept <학과>` / `-All`. `.env` 를 읽지 않는다.
이미지는 한 번만 빌드하고 digest 로 전 학과에 배포한다.

### 2. sync 학과 라우팅

`Settings.for_drive(driveId)` 를 핸들러 진입점에서 갈아끼우는 방식.
그 아래 호출(RagEngineClient·GcsClient·폴더 스코프)이 전부 따라오므로
코퍼스·버킷 참조 25개 지점을 각각 고치지 않았다.

| 경로 | 방식 |
|---|---|
| backfill · backfill-run · changes · ingest | `for_drive(body.drive_id)` |
| index-gcs | `_split_by_drive` — 요청에 driveId 가 없어 doc_state 로 되짚음 |
| reindex-pending | `_group_docs_by_drive` — DocState 가 driveId 를 들고 있어 재조회 없음 |
| retry-failed | `_dept_ctx` (버킷) + index-gcs 경유 (코퍼스) |
| delete · 범위 밖 정리 | 학과 설정으로 코퍼스·버킷 선택 |
| reconcile | 해당 없음 — 순수 산술, settings 미사용 |

안전 규칙 두 가지를 **반대 방향으로** 잡았다.

- **학과 판정 불가 문서는 처리하지 않는다.** 전역 기본값으로 넘기면 남의 학과에
  섞이거나(코퍼스) 없는 버킷을 뒤져 DLQ 로 묻힌다(버킷). 안 건드리면 다음 주기가
  다시 집는다 — 그쪽이 싼 실패다
- **깨진 `DEPARTMENTS_JSON` 은 폴백한다.** 설정 오타 하나로 sync 가 기동조차
  못 하면 안 된다

## 남은 것

### 3. 맵 켜기 — `DEPARTMENTS_JSON` 생성

- `dept_config.py` 에 `--departments-json` 추가: 학과 yaml 들을 한 줄 JSON 으로
- 시크릿 없음 — sync 는 MCP 키를 쓰지 않는다. 학과 10개에 약 5KB
- `deploy.ps1` 이 sync 에 전달

**이걸 켜기 전까지 2단계 코드는 아무 일도 안 한다**(`for_drive` 가 자기 자신을
반환). 반대로 켠 뒤에는 라우팅이 즉시 활성화된다.

### 4. `deploy.ps1` 을 config 기반으로

- `Load-Dotenv` 제거, `config/` 만 읽는다
- **`-Dept` 인자는 없다** — 학과 목록이 곧 배포 대상이라 순회하면 된다
- 미이관 8개를 `common.yaml` 로: `QG_MODE`, `INGEST_CONCURRENCY`,
  `RAG_DELETE_CONCURRENCY`, `RAG_DELETE_PACING_SECONDS`, `PARSER_TIMEOUT`,
  `PARSER_CONCURRENCY`, `PARSER_MAX_INSTANCES`, `SYNC_CONCURRENCY`
- `SCHEDULER_SA` 는 프로젝트에서 파생되므로 저장하지 않는다
- `preflight.ps1` 도 함께 (deploy.ps1 이 dot-source 한다)

> **함정 — Scheduler job 갱신이 필수다.**
> `driveIds` 는 Cloud Run env 가 아니라 **Scheduler job 의 요청 본문**으로 간다
> ([deploy.ps1](../scripts/deploy.ps1) → Workflows `args` → `for_each_drive`).
> 실측: `DRIVE_IDS` 환경변수를 읽는 서비스 코드가 하나도 없다.
> 학과를 추가하고 Cloud Run 만 재배포하면 **워크플로가 새 드라이브를 영영 못 본다.**

### 5. `.env` 삭제

`.env`, `.env.example`, `Load-Dotenv`, `_env.load_dotenv`.

파이썬 스크립트 5개를 config 로더로 전환해야 한다 —
`cleanup_orphans.py`, `view_logs.py`, `e2e_local.py`, `bench_hwp_corpus.py`,
`eval_golden.py`. 이들은 로컬 도구라 운영 무관이고 가치 대비 비용이 가장 나쁘다.

`test_deploy_env_ps1.py` 19개 중 dotenv 규칙 검사 2개는 제거하고, 나머지
(`Require-*Env`·preflight·PS 파싱)는 **검증 규칙이라 config 기준으로 옮긴다.**

### 6. 남은 22개 판정

배포가 Cloud Run 에 넘기지 않는 값들이라 **운영에서는 코드 기본값이 돌고 있다.**
`.env` 값이 반영된 적이 없다 (`RAG_CHUNK_SIZE`, `SEARCH_DISTANCE_THRESHOLD` 등).

옮기면 "이제 반영되는 값" 이 되어 동작이 바뀐다. 기본은 **삭제** —
근거 주석은 [shared/config.py](../shared/config.py) 에 실측 수치까지 있다.

부수 효과: 지금은 로컬 스크립트만 `.env` 값을 읽어 **평가와 운영이 다른
파라미터로 측정된다.** 지우면 일치한다.

## 검증

| 단계 | 방법 |
|---|---|
| 3 | 맵 생성 결과를 `Settings.from_env()` 로 되읽어 학과 수·코퍼스 대조 |
| 4 | 등가성 테스트를 parser·sync 로 확대. 이름 집합이 스냅샷과 같아야 한다 |
| 5 | `.env` 를 지운 채 전체 테스트 + 배포 1회 |
| 6 | 삭제한 키가 스냅샷에 없었는지 확인 (있었다면 분류가 틀린 것) |

**골든 스냅샷은 의도한 변경일 때만 갱신한다.** 갱신 시 `INTENDED_CHANGES` 에
이유를 적는다. 그러지 않으면 회귀를 스스로 지워버린다.

## 끝 상태

```
config/
  common.yaml              [커밋]  학과 무관 전부
  departments/
    dept.yaml.example      [커밋]  템플릿
    <학과>.yaml            [gitignore]  코퍼스·키·버킷·폴더
```

서비스는 parser 1개 · sync 1개 · Workflows 1벌 · Scheduler 1벌 · MCP 2N개.

## 학과 추가 절차 (끝 상태)

1. 공유 드라이브 생성 + 서비스 계정에 공유
2. 코퍼스 2개, 버킷 2개 생성
3. `config/departments/<학과>.yaml` 작성 (템플릿 복사)
4. `deploy.ps1` — sync 가 새 드라이브 인지, MCP 2개 추가, **Scheduler 갱신**
5. 첫 백필

기존 학과는 건드리지 않는다 — 드라이브가 다르니 커서(`sync_tokens/{driveId}`)도
백필 잠금도 독립이다.
