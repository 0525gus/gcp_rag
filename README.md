# RAG MCP — Drive → GCS → Vertex RAG → MCP search

일 배치로 Google Drive 공유 드라이브를 GCS 단일 진입점으로 동기화하고,
MCP `search`로 검색합니다. **HWP/HWPX는 rhwp-python으로 MD 변환**합니다.

## 파이프라인 (재검토 기준)

```
Cloud Scheduler (00:00 KST, Asia/Seoul)
  └─ Cloud Workflows (workflows/daily_sync.yaml)
       ├─ /sync/changes          Changes API 델타 (pageToken 미커밋)
       │                          SYNC_FOLDER_IDS 있으면 해당 폴더 트리만 ingest
       ├─ MIME 분기
       │    DELETE        → /sync/delete
       │    SKIP          → doc_state SKIPPED
       │    HWP_PARSE     → Drive 다운로드 → /parse(rhwp) → GCS *.md
       │    GOOGLE_EXPORT → Drive export → GCS
       │    FILE_COPY     → PDF 분할 / XLSX 표 변환 / 그 외 복사 → GCS
       ├─ /sync/index-gcs        RAG Engine은 GCS만 import (Drive 커넥터 미사용)
       ├─ /sync/reconcile
       └─ /sync/commit-token     색인 성공 시에만 커밋

MCP Client → Cloud Run MCP (/mcp) → RAG retrieval
```

`DRIVE_IDS` = 공유 드라이브, `SYNC_FOLDER_IDS` = 그 안 하위 폴더(콤마 구분, 비우면 전체).

| 서비스 | 역할 | 런타임 |
|---|---|---|
| `services/parser` | HWP/HWPX → MD (rhwp) | Python 3.12 + rhwp-python |
| `services/sync` | Drive/GCS/RAG 오케스트레이션 API | Python |
| `services/mcp_server` | search tool | Python MCP SDK |

## 품질 게이트

- 기본 `QG_MODE=log` — 미달해도 **색인 계속**, Cloud Logging 경고만
- `QG_DENSITY_THRESHOLD=0.0005` (이미지 많은 공문 오탐 완화)
- 전환: `reject` | `fallback`(+`ENABLE_DOCAI_FALLBACK`)

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
