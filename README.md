# RAG MCP — Drive → GCS → Vertex RAG → MCP search

일 배치로 Google Drive 공유 드라이브를 GCS 단일 진입점으로 동기화하고,
MCP `search`로 검색합니다. **HWP/HWPX는 rhwp-python으로 MD 변환**합니다.

## 파이프라인 (재검토 기준)

```
Cloud Scheduler (00:00 KST, Asia/Seoul)
  └─ Cloud Workflows (workflows/daily_sync.yaml)
       └─ Drive 하나당 페이지 루프 (hasMore 인 동안 반복)
            ├─ /sync/changes          Changes API 델타 (기본 200건씩, pageToken 미커밋)
            │                          토큰 없으면 mode=backfill_required → backfill-run
            │                          SYNC_FOLDER_IDS 있으면 해당 폴더 트리만 ingest
            ├─ MIME 분기
            │    DELETE        → /sync/delete
            │    SKIP          → doc_state SKIPPED
            │    HWP_PARSE     → Drive 다운로드 → /parse(rhwp) → GCS *.md
            │    GOOGLE_EXPORT → Drive export → GCS
            │    FILE_COPY     → 원본 복사 → GCS
            ├─ /sync/index-gcs        RAG Engine은 GCS만 import (Drive 커넥터 미사용)
            ├─ /sync/reconcile
            └─ /sync/commit-token     색인 성공 시에만 커밋 → 다음 페이지

MCP Client → Cloud Run MCP (/mcp) → RAG retrieval
```

### 델타를 왜 끊어서 받나

Cloud Workflows 는 실행당 변수 누적 **512KB** 가 상한이다. 변경 1건이 응답·복사본·URI
까지 합쳐 워크플로우 변수를 ~900B 먹으므로 **약 586건에서 실행 자체가 죽는다**. 죽으면
pageToken 이 커밋되지 않아 다음 실행의 델타가 더 커지고 — 한 번 넘으면 자력으로 못
돌아온다. 그래서 `/sync/changes` 는 `SYNC_MAX_CHANGES`(기본 200) 건씩만 주고 `hasMore`
로 잔량을 알리며, 워크플로우가 **배치마다 토큰을 커밋하고** 다음 페이지를 받는다.
중간에 실패해도 앞 배치는 확정되므로 재실행 부담이 줄어든다.

최초 실행(토큰 없음)은 전체 스냅샷이라 재개 지점이 없다 — 목록을 워크플로우에 올리지
않고 서버 안에서 끝내는 `/sync/backfill-run` 으로 넘긴다.

`DRIVE_IDS` = 공유 드라이브, `SYNC_FOLDER_IDS` = 그 안 하위 폴더(콤마 구분, 비우면 전체).

| 서비스 | 역할 | 런타임 |
|---|---|---|
| `services/parser` | HWP/HWPX → MD (rhwp) | Python 3.12 + rhwp-python |
| `services/sync` | Drive/GCS/RAG 오케스트레이션 API | Python |
| `services/mcp_server` | search tool | Python MCP SDK |

## 품질 게이트

| 게이트 | 판정 | 설정 |
|---|---|---|
| G1 추출 밀도 | 텍스트 길이 / 원본 바이트 | `QG_DENSITY_THRESHOLD=0.0005` (이미지 많은 공문 오탐 완화) |
| G2 표 손실률 | 문서 구조상 표 N개 중 마크다운에 안 남은 비율 | `QG_TABLE_LOSS_RATIO=0.3` |
| EMPTY_TEXT | 추출 결과가 사실상 없음 | `QG_MIN_TEXT_LENGTH=20` |

- 기본 `QG_MODE=log` — 미달해도 **색인 계속**, Cloud Logging 경고만
- 전환: `reject` | `fallback`(+`ENABLE_DOCAI_FALLBACK`)
- `EMPTY_TEXT` 만은 QG_MODE 와 무관하게 422 (색인할 내용이 없으므로)

> 구 G2(셀 단위 실패율)·G3(이미지 면적비)는 **제거**했다. 두 판정이 읽던
> `table_cell_failures` / `image_area_ratio` 를 채우는 파서가 없어 셀이 전부 비어도,
> 이미지가 지면의 100%여도 통과했다. 셀 '실패'는 빈 셀이 정상이라 판별이 불가능하고
> 이미지 면적비는 페이지 기하 정보가 없어 계산 경로가 없다. G3 조건은 실질적으로
> G1 의 완화판이기도 했다.

## 로컬

```bash
# CPython 3.12 권장 (rhwp 휠). mingw/msys Python은 빌드 실패할 수 있음.
pip install -r requirements-parser.txt
pip install -r requirements.txt
set PYTHONPATH=.
python scripts/hwp_to_md.py sample.hwp -o sample.md
```

## 배포

`scripts/deploy.sh` — parser / sync / mcp Cloud Run + Scheduler  
리전: `asia-northeast3`
