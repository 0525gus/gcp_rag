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

### 3. 맵 켜기 — `DEPARTMENTS_JSON` 생성

`dept_config.py --departments-json` 이 학과 yaml 들을 한 줄 JSON 으로 낸다.
`deploy.ps1` 이 그 값을 rag-sync 에 넘긴다 — **이걸로 2단계 라우팅이 켜졌다.**

값은 `build_env` 를 거쳐 뽑는다. 병합 규칙과 거부 조건을 한 벌만 두기 위해서다 —
따로 yaml 을 읽으면 배포 env 와 sync 맵이 서로 다른 규칙으로 갈라진다.

거부 조건 두 개를 더 달았다.

- **`driveIds` 없는 학과** — 맵이 비지 않은 이상 sync 는 맵에 없는 드라이브를
  통째로 건너뛴다. 빠뜨리면 그 학과 문서가 영영 처리되지 않는다, 조용히
- **학과 간 `driveId` 중복** — `department_for_drive` 는 첫 일치를 준다.
  겹치면 뒤 학과 문서가 앞 학과 코퍼스로 들어간다

JSON 은 공백 없이 낸다. `--set-env-vars` 인자에 실려 명령줄로 나가므로, 값의
안전이 셸의 인자 분리 규칙에 걸리지 않게 한다. (실측: PowerShell 7.6 →
`gcloud.ps1` → python.exe 경로에서 따옴표는 그대로 보존된다. 다만 `gcloud.cmd`
쪽은 cmd.exe 가 `|` 를 파이프로 먹으므로 이 저장소는 PS 경로만 지원한다.)

### 4. `deploy.ps1` 을 config 기반으로

- `Load-Dotenv` 제거. `config/` 만 읽는다
- **`-Dept` 인자는 없다** — 학과 목록이 곧 배포 대상이다
- MCP 배포는 `deploy_mcp.ps1 -All -SkipBuild` 에 **위임**한다. 키 중복 사전
  검사·digest 고정·요약표가 한 곳에만 있어야 두 경로가 갈라지지 않는다
- 미이관 8개(`QG_MODE`, `INGEST_CONCURRENCY`, `RAG_DELETE_*`, `PARSER_*`,
  `SYNC_CONCURRENCY`)를 `common.yaml` 로
- `SCHEDULER_SA` 는 프로젝트에서 파생하므로 저장하지 않는다
- Scheduler job 의 `driveIds` 는 **전 학과 union** — 학과를 늘리고 Cloud Run 만
  재배포하면 워크플로가 새 드라이브를 영영 못 보던 함정을 여기서 막는다
- 코퍼스 적재량 확인을 학과별로 돌린다. 한 학과만 보면 새로 붙인 학과가 빈 채로
  남아도 조용하다

**parser·sync 기본값은 첫 학과 것이다(union 이 아니다).** 두 서비스는 학과마다
뜨지 않으므로 기본값이 필요한데, union 을 깔면 맵이 깨져 단일 학과로 폴백했을 때
그 한 벌이 **남의 폴더까지 훑는다.** 한 학과로 좁혀 두면 그때 다른 학과가 멈출
뿐 섞이지는 않는다 — 섞인 코퍼스는 파일을 골라 지워야 하므로 훨씬 비싼 실패다.
`DRIVE_IDS` 만 union 이다(서비스 코드가 읽지 않는 값이고, Scheduler·backfill·
share_drive 가 "대상 전체" 라는 뜻으로 쓴다).

### 5. `.env` 삭제

`.env.example`, `Load-Dotenv`, `_env.load_dotenv` 를 지웠다.
`preflight.ps1`·`backfill.ps1`·`share_drive.ps1`·`setup_alerts.ps1` 도 함께 옮겼다
(문서에는 앞의 둘만 적혀 있었다).

파이썬 쪽은 실제로 `.env` 를 읽던 것이 `cleanup_orphans.py`·`view_logs.py` 둘뿐이었다
(`e2e_local.py`·`bench_hwp_corpus.py` 는 애초에 안 읽었고, `eval_golden.py` 는
환경변수만 봤다). `dept_config.load_config_env()` 로 갈아끼웠다.

곁들여 고친 것:

- `cleanup_orphans.py` 가 **전 학과 버킷을 훑는다.** 학과마다 버킷이 다르므로
  한 벌만 보면 나머지 잔존물이 그대로 남았다. 학과 선택은 `Settings.for_drive`
  를 그대로 쓴다 — 서비스가 버킷을 고르는 코드와 같은 경로여야 한다
- `view_logs.py` 의 MCP 서비스 이름을 규칙(`rag-mcp-{학과}-{대상}`)으로 만든다.
  목록을 손으로 적어 두면 새 학과 로그가 조용히 빠진다
- `eval_golden.py --dept` — 키를 학과 설정에서 꺼낸다

### 6. 남은 22개 판정 — **삭제**

`.env.example` 을 지우면서 함께 사라졌다. 배포가 Cloud Run 에 넘긴 적이 없어
운영에서는 줄곧 코드 기본값이 돌고 있었다. 스냅샷과 대조해 확인했다 —
겹치는 키 0개(`test_dropped_keys_were_never_deployed`).

부수 효과도 여기서 없어졌다: 지금까지는 로컬 스크립트만 이 값들을 읽어
**평가와 운영이 다른 파라미터로 측정됐다.**

## 배포 (2026-08-23)

전부 올라갔다. `deploy.ps1` 한 번으로 parser·sync·MCP 2개·Workflows·Scheduler.

- **`DEPARTMENTS_JSON` 이 rag-sync 에 실렸다** — env 15개 → 16개. 학과 라우팅이
  이 시점부터 살아 있다(그전까지는 `for_drive` 가 자기 자신을 반환했다)
- 값이 명령줄을 통과하며 깨지지 않았다. 배포된 env 를 다시 받아
  `_departments_from_json` 으로 파싱해 학과·코퍼스·드라이브까지 대조했다
- 골든 스냅샷을 실배포 상태로 다시 떴다. **차이는 DEPARTMENTS_JSON 추가 하나뿐** —
  나머지 3개 서비스는 이름도 값도 그대로였다(의도치 않은 드리프트 0)
- 코퍼스 적재: cs/staff 38건 · cs/student 19건

배포 중에 스크립트 버그 하나가 더 나왔다(아래 "배포가 잡은 것").

## 배포가 잡은 것

테스트로는 안 잡히고 **실제로 돌려야만** 드러난 두 가지다. 둘 다 회귀 테스트를
붙였다 — 어느 쪽도 표기가 아니라 **동작**으로 검사한다.

| 증상 | 원인 | 테스트 |
|---|---|---|
| 첫 검증에서 즉사 (`MCP_API_KEY_STUDENT: empty`) | `dept_config` 가 STAFF 키만 내보냈다. `Require-McpDeployEnv` 는 STAFF 를, `Require-FullDeployEnv` 는 STUDENT 를 본다 — **반대 방향의 짝**이라 한쪽만 내보내면 멀쩡한 설정이 거부된다 | `test_deploy_checks_only_read_keys_dept_config_exports` (PS 소스에서 `$env:MCP_API_KEY_*` 를 뽑아 대조) |
| parser·sync 배포 후 MCP 직전에 중단 (`ValidateSet`) | `@("-All","-SkipBuild")` — **배열 splat 은 위치 인자**라 `$Dept="-All"`, `$Audience="-SkipBuild"` 가 됐다. 스위치는 해시테이블 splat 이어야 한다 | `test_deploy_passes_switches_to_deploy_mcp_by_name` (실제 param 블록에 splat 해 바인딩 결과를 본다) |

두 번째는 같은 파일의 `@authArgs` 가 **배열이 맞아서** 헷갈린 것이다 —
네이티브 명령(gcloud)은 위치 인자를 받고, PowerShell 스크립트는 이름으로 받는다.

## 남은 것

없다. 다만 운영상 알아 둘 것:

- **MCP 키가 추측 가능한 값이다.** `ALLOW_UNAUTH=true` 라 엔드포인트가 공개이고
  경계는 이 키 하나뿐이라, 유출 없이 추측만으로 그 학과 코퍼스 전량이 열린다.
  `dept_config` 가 배포마다 경고를 찍는다(막지는 않는다 — 판단은 사람이 한다)
- 학과 라우팅은 다음 sync 주기(00:00 KST)부터 실제 문서에 적용된다

## 검증

| 단계 | 방법 | 상태 |
|---|---|---|
| 3 | 맵 생성 결과를 sync 의 파서(`_departments_from_json`)로 되읽어 학과 수·코퍼스·드라이브 대조 | `test_map_round_trips_through_the_parser_sync_uses` |
| 4 | 등가성 테스트를 parser·sync 로 확대. 이름 집합이 스냅샷과 같아야 한다 | `test_service_env_names_unchanged` |
| 5 | `.env` 를 지운 채 전체 테스트 + 배포 1회 | 테스트 410개 통과 · **배포 완료(2026-08-23)** |
| 6 | 삭제한 키가 스냅샷에 없었는지 확인 (있었다면 분류가 틀린 것) | `test_dropped_keys_were_never_deployed` (겹침 0) |

배포 스크립트 쪽에도 두 개를 더 걸었다.

| 검사 | 막는 것 |
|---|---|
| `test_powershell_clears_every_key_dept_config_emits` | `$ConfigKeys` 에서 빠진 키가 **학과 사이로 새는 것**. `-All` 로 20개를 돌리면 앞 학과 코퍼스·키가 남는다 |
| `test_base_config_returns_a_real_department_code` | 학과가 하나일 때 PowerShell 이 배열을 풀어 코드가 첫 글자로 잘리는 것(`"cs"` → `"c"`) |

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
