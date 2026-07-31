# 파이프라인 전수 점검 결과 (2차)

- 점검일: 2026. 7. 30.
- 범위: 1차 점검(2026-07-28, [`OPS_AUDIT.md`](./OPS_AUDIT.md)) 이후 전 구간 재점검
- 방법: 코드 검토 + **배포된 실제 환경·데이터 상태 직접 조회**
- 1차와 달라진 점: 1차는 인프라·데이터 상태 중심이었고, 2차는 거기에
  **코드 경로와 의존성**을 함께 봤다

---

## Ⅰ. 요약

| 구분 | 건수 |
|---|---:|
| 발견·코드 조치 완료 | 9건 |
| 발견·문서로만 남김 | 6건 ([`OPS_DEFERRED.md`](./OPS_DEFERRED.md) 10~15) |
| 운영 조치가 남은 것 | 2건 |

- □ 1차에서 조치한 3건은 **재발 흔적 없음**
- □ 9건 중 **6건이 "지금은 안 터지는" 잠복 결함**이었음
  - ○ 원인이 사라진 것이 아니라, 마침 그 조건이 안 들어온 상태였음

### 정상 확인

```
스케줄러      7/28·7/29 연속 SUCCEEDED
문서 상태     INDEXED 1,155 / SKIPPED 393 / DELETED 100 / FAILED 0 / PENDING 0
DLQ           0건
MCP           /health ok, 무인증 401, 정상 키 200
검색 지연     top_k=5 1.34초 / top_k=20 1.44초
정규화 버킷    고아 0건, DELETED 잔존 0건
재색인 잡      8건 전부 DONE
```

---

## Ⅱ. 발견 및 조치

### 1. 삭제한 문서의 원본이 GCS 에 영구 잔존 〔심각〕

#### 가. 현상

```
doc_state DELETED                    100건
  └ raw/ 에 원본이 남아 있는 것       52건   (전부 .hwp)
```

- □ Drive 에서 지운 문서의 **원본 파일이 GCS 에 그대로 남아 있음**
- □ `raw/` 에는 명단·인사발령 등 원문이 그대로 있으며 버킷 IAM 도 미분리 상태임
  ([`OPS_DEFERRED.md`](./OPS_DEFERRED.md) 6번)
- □ 즉 **삭제 요청이 이행되지 않는 구조**임. 버킷에 버전관리·lifecycle 도 없어
  사람이 지울 때까지 잔존함

#### 나. 원인

- □ `/sync/delete` 가 **손으로 적은 확장자 목록**으로만 삭제함
- □ 그 목록은 `normalized/` 만 대상이며 `raw/` 는 아예 대상이 아니었음
- □ 같은 원인으로 두 가지가 더 누락되고 있었음

| 누락 대상 | 현황 |
|---|---|
| `raw/` 원본 전체 | 52건 잔존 |
| `.partN.pdf` (분할 PDF 조각) | 6건 존재 — 소유 문서 삭제 시 전량 고아화 |
| `.rtf` / `.doc` | 코퍼스에 0건이라 아직 안 걸림 |

#### 다. 부수 발견 — `extract_file_id` 가 `.hwp` 를 모름

- □ `_FILE_SUFFIXES` 에 `.hwp` / `.hwpx` / `.doc` 가 빠져 있었음
- □ 빠지면 `abc.hwp` 가 fileId 로 접히지 않아 두 곳에서 조용히 잘못 쓰임

| 위치 | 결과 |
|---|---|
| `rag_engine._file_index` | 코퍼스 삭제가 대상을 못 찾음 |
| `sync._clean_file_ids` | 점 때문에 malformed 로 판정해 버림 |

#### 라. 조치

- □ 삭제를 **prefix 훑기**로 전환 (`GcsClient.delete_for_file`)
  - ○ fileId 경계 검사는 `_normalized_uris_for_file` 에 이미 검증돼 있던 로직을
    재사용함 → 색인과 삭제가 같은 규칙을 쓰므로 불일치가 재발할 수 없음
- □ `raw/` 도 삭제 대상에 포함함
- □ `_FILE_SUFFIXES` 에 누락 확장자 3개 추가함
- □ 일회성 정리용 [`scripts/cleanup_orphans.py`](../scripts/cleanup_orphans.py)
  추가 (기본 조회만, `--apply` 로 삭제)

#### 마. 지우는 것이 안전한 근거

- □ 두 버킷 모두 **파생물**이며 원본은 Drive 임
  - ○ `raw/` : ingest 마다 Drive 에서 다시 받아 올림
    (`_ingest_hwp`: `download_file` → `upload_raw`)
  - ○ 업로드 직후 파서가 한 번 읽는 것 외에 **읽는 코드가 없음**
  - ○ `normalized/` 도 재색인 경로가 다시 생성함

#### 바. 남은 운영 조치

- □ 코드 수정은 앞으로의 삭제만 막음. **이미 쌓인 52건은 수동 정리 필요**

---

### 2. 매일 `failed: 1` 을 만들던 500 〔주의〕

#### 가. 현상

- □ 일일 동기화가 최근 6회 중 **3회**(7/23·24·29) `failed: 1` 로 끝남
- □ 대시보드·reconcile 은 `ok: true` 였음 (`failed` 가 `accounted` 에 포함되어 균형)

#### 나. 원인

```
WARNING scope check: cannot read parents for :     ← fileId 가 빈 문자열
        HttpError 400 ... /drive/v3/files/?...
ERROR   sync/main.py:476 store.upsert(...)
        InvalidArgument: 400 Document name ".../doc_state/" has invalid trailing "/"
→ POST /sync/ingest 500
```

- □ 공유 드라이브 **자체**의 변경(`changeType="drive"`)은 `fileId` 가 없음
- □ 그 항목이 그대로 `ingest` 까지 흘러가 Firestore 문서 경로를 `doc_state/` 로 만듦

#### 다. 영향

- □ 데이터 유실 없음. 다만 두 가지가 문제였음
  - ○ reconcile 에 상시 `failed: 1` 이 박혀 **신규 실패가 묻힘**
  - ○ 5xx 알림을 걸면 baseline 이 1 이라 태어나자마자 무용지물이 됨
    ([`OPS_DEFERRED.md`](./OPS_DEFERRED.md) 2번)

#### 라. 조치

- □ `list_changes` 에서 `fileId` 없는 change 를 버림
- □ `/sync/ingest` 는 빈 `fileId` 에 400 을 반환 (500 이 될 이유가 없음)

---

### 3. 배포 스크립트가 운영 설정을 지우는 구조 〔주의〕

`--set-env-vars` 는 기존 env 를 **치환**한다. 스크립트에 없는 값은 배포 순간 사라진다.

| 스크립트 | 누락 | 사라지면 |
|---|---|---|
| `deploy_mcp.sh` | `FIRESTORE_DATABASE` | `(default)` = Datastore 모드를 보게 됨 → 검색 결과의 파일명·경로·bundle 이 **조용히 전부 null** |
| `deploy.sh` (rag-mcp) | `MCP_API_KEY` | **무인증 공개** — 1차 점검 Ⅱ.1 의 전제조건 재생산 |
| `deploy.sh` (rag-sync) | `RAG_DELETE_*` | 삭제 페이싱이 기본값으로 되돌아감 |

- □ 1차 점검은 "배포 스크립트는 키를 필수로 강제한다"고 기록했으나, 그것은
  `deploy_mcp.sh` 만 해당됐음. `deploy.sh` 는 키를 아예 넘기지 않았음
- □ **조치**: 세 스크립트에 누락 변수 등록, `deploy.sh` 는 빌드 전에
  `MCP_API_KEY` 를 `:?` 로 강제

---

### 4. 의존성 3개가 이미 조용히 메이저를 넘어 있었음 〔주의〕

`mcp<2` 교훈(1차 점검 이후 `cebaecb`)이 한 패키지에만 적용돼 있었음.

| 패키지 | requirements 기준 | 실제 해석 |
|---|---|---|
| `google-cloud-storage` | `>=2.18.0` | **3.13.0** |
| `pypdf` | `>=5.1.0` | **6.14.2** |
| `starlette` | `>=0.40.0` | **1.3.1** |

- □ 지금 안 터진 건 우리가 쓰는 API 가 마침 안 바뀌었기 때문임
- □ 검증: starlette 1.x 를 실제로 설치하니
  `TypeError: Router.__init__() got an unexpected keyword argument` 로
  **테스트 수집이 통째로 깨짐** (sync 이미지는 `fastapi<0.116` 이 간접적으로
  막아주고 있었을 뿐)
- □ **`google-cloud-aiplatform` 은 더 심각함.** `shared/rag_engine.py` 가
  전적으로 의존하는 `vertexai.rag` 가 이미 폐기 예고 상태임

  ```
  UserWarning: The `vertexai.rag` module is deprecated and will be removed in a
  future version. Please migrate to the `agentplatform` client.
  ```

  사라지는 날 **rag-sync·rag-mcp 가 둘 다 import 에서 죽고 파이프라인이 정지**함
- □ **조치**: 세 requirements 파일 전부에 메이저 상한 추가. 현재 버전이 모두
  만족하므로 오늘의 해석은 불변이며 세 파일 다 `pip install --dry-run` 통과함
- □ **남은 과제**: 상한은 시한폭탄을 미루는 것뿐임. `agentplatform` 이관을
  별도로 잡을 것

---

### 5. 인용 라벨이 문서를 특정하지 못함 〔주의〕

#### 가. 현상 — `answer` 응답 실측

```
[1] name   = content.txt
    bundle = 147294_[안내]2026학년도 1학기 개강, 수강정정기간 및 추가 증원 교과목 안내(최종)
```

- □ 제목은 `bundle` 에만 있고 `name` 은 `content.txt` 임
- □ `content.txt` 는 **코퍼스에서 가장 흔한 파일명**(27건, 2위의 7배)이며
  게시판 공지 본문이라 자주 걸림
- □ 같은 검색의 [3]·[5] 가 `매뉴얼_pc.pdf` / `매뉴얼_mobile.pdf` — **같은
  게시글의 다른 첨부**였으나 파일명만으로는 구분되지 않음

#### 나. 왜 문제인가

- □ `[n] 파일명` 라벨은 "어느 문장이 어느 문서에서 왔는지 복원"하려고 넣은
  것임(`4eaeea8`). 라벨이 전부 `content.txt` 면 그 목적을 달성하지 못함

#### 다. 조치

- □ `citation_label()` 추가 — `자료묶음 / 파일명` 형태 (파일명이 이미 자료묶음을
  담고 있으면 중복 회피, 자료묶음이 없고 파일명도 기계가 붙인 것이면 경로)
- □ `source.label` 을 **추가**한 것이라 기존 필드는 그대로 → 소비자 호환 유지

---

### 6. 실패 큐 잔재가 신규 실패를 가림 〔주의〕

- □ `doc_split_queue` 에 유령 1건 (27.7MB xlsx). `doc_state` 로는 이미
  `INDEXED` 인데 큐 항목만 남아 있었음
- □ 원인: `clear_dlq` 는 `/sync/retry-failed` 경로에서만 호출되고, 이 문서는
  **정상 ingest 로 회복**되어 큐가 비워지지 않았음
- □ 1차 점검 Ⅱ.3 의 "DLQ 892건이 신규 실패를 묻음"과 **같은 유형**임
- □ **조치**: `mark_indexed()` 를 만들어 FAILED 에서 회복할 때만 두 큐를 비움
  (정상 경로에는 쓰기가 늘지 않음). 중복돼 있던 private 접근 루프 2벌도 정리됨

---

### 7. 그 외 코드 조치

| 항목 | 내용 |
|---|---|
| Dockerfile `$PORT` 무시 | CMD 에 `--port 8080` 이 박혀 있어 Cloud Run 규약 위반. `main.py` 의 `__main__` 은 `$PORT` 를 읽는데 CMD 만 안 읽어 서로 어긋나 있었음 |
| 재색인 잡 상태 오독 | `/sync/jobs/{id}` 가 루프 중 스냅샷 `totals` 와 최종 `result.totals` 를 나란히 반환. `reindex-0f06ea24ff7a` 가 `candidates=115 indexed=48` 로 보였으나 완주였음 → 완료 시 최종값으로 덮어씀 |
| 재색인 클라이언트 남발 | `_normalized_uris_for_file` 이 문서마다 `storage.Client` 를 새로 만듦(200건이면 200번) → 한 번만 생성 |
| CI 레드 | 브랜치가 테스트 2건을 깨뜨린 상태였음(main 은 통과). 제품 버그가 아니라 갱신 누락 — `max_chunks_per_file` 도입과 인덱스 캐시 추가 때 테스트가 따라가지 않음 |
| `view_logs.py` Windows | `gcloud` 가 `.cmd` 배치라 `shell=True` 없이는 `FileNotFoundError` |

---

## Ⅲ. 문서로만 남긴 것

[`OPS_DEFERRED.md`](./OPS_DEFERRED.md) 10~15 참조.

| # | 항목 | 왜 지금 안 고쳤나 |
|---|---|---|
| 10 | 세 서비스가 프로젝트 Editor 로 동작 | IAM 변경이라 잘못 건드리면 파이프라인이 멈춤 |
| 11 | 품질 게이트 G2·G3 발동 불가 | 파서 계측 추가 + 벤치 재측정이 필요 |
| 12 | 검색 다양성이 bundle 단위가 아님 | 검색 순위를 바꾸는 변경 — 골든 100 측정 선행 |
| 13 | 재질의 폭주 (캐시로 부분 완화) | 근본은 호출측 프롬프트 영역 |
| 14 | 운영에만 손으로 들어간 설정값 | 값 선택이 실측한 사람의 판단 |
| 15 | 자잘한 것 (색인 상한, 큐 미정리 등) | 현 규모에서 무해 |

---

## Ⅳ. 남은 운영 조치 (코드 아님)

```bash
# 1) 잔존 원본 52건 정리 — 근거 목록을 먼저 확인할 것
python scripts/cleanup_orphans.py --only-deleted --csv orphans.csv
python scripts/cleanup_orphans.py --only-deleted --apply

# 2) 파서 동시성을 컨테이너 메모리에 맞춤 (운영 160 → 8)
gcloud run services update rag-parser --region=asia-northeast3 --concurrency=8
```
