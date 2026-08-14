# 문서 검색(RAG) 시스템 개발명세

- 작성: 2026. 7. 28. / 개정: 2026. 8. 13.
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
  - MCP 연동 (서비스 2벌)
- 제외
  - 답변 문장 생성
  - 문서 단위 ACL (부서·기밀등급). 분리는 폴더 트리 + 코퍼스 2개까지

### 3. 현행 규모 (2026. 7. 28.)

| 구분 | 수량 |
|---|---:|
| 색인 문서 | 1,211건 |
| 색인 객체(URI) | 1,418건 |
| 범위 외 제외 | 394건 |
| 원본 저장 | raw 271MB / normalized 530MB |

---

## Ⅱ. 시스템 구성

### 1. 전체 구조

GCS는 공용 1세트. 소속은 Firestore `audience`에 두고, RAG 코퍼스만 둘로 나눈다.

```
Google Drive
  DRIVE_IDS / SYNC_FOLDER_IDS / STUDENT_FOLDER_IDS
      │
      ▼
 rag-sync ────── HWP/HWPX ──────▶ rag-parser
      │
      ├─ GCS raw (1) / normalized (1)     키 = fileId, 경로에 소속 없음
      ├─ Firestore DB doc-state
      │     doc_state.audience = STUDENT | STAFF
      ▼
      ├─ RAG 교직원 코퍼스  ← 전량
      └─ RAG 학생 코퍼스    ← audience=STUDENT 만
            ▲                    ▲
            │                    │
      rag-mcp (교직원)     rag-mcp-student
            │                    │
         FactChat             FactChat
```

포함 관계: 교직원 코퍼스 ⊇ 학생 코퍼스.

### 2. 구성요소

#### 가. rag-sync

- Drive 변경 감지 → 다운로드 → 변환 위임 → GCS 적재 → RAG 색인
- CPU 2 / MEM 2Gi / timeout 3600s / concurrency 4
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
- 분리 스위치: `RAG_CORPUS_NAME_STUDENT` + `STUDENT_FOLDER_IDS` 둘 다 있어야 켜짐. 하나라도 비면 단일 코퍼스

#### 나. rag-parser

- HWP/HWPX → 마크다운, 품질 게이트
- CPU 2 / MEM 2Gi / timeout 900s
- 엔진: `.hwp` = rhwp, `.hwpx` = python-hwpx (부재 시 rhwp)
- 게이트: `log` / `reject` / `fallback`
  - 실제 발동은 G1 밀도, G2 표 손실, EMPTY_TEXT
  - 기본 `QG_MODE=log` — 미달이어도 색인 계속, 로그만
  - EMPTY_TEXT만 모드와 무관하게 422
- rag-sync 서비스계정만 호출

#### 다. rag-mcp / rag-mcp-student

- MCP 도구 `search`, `answer`
- CPU 1 / MEM 1Gi / timeout 60s / concurrency 40
- 기동 시 코퍼스 하나. 툴 인자로 대상을 고르지 않음
- 공개 URL + API 키 (`Authorization: Bearer` 또는 `X-API-Key`). 서비스마다 키를 다르게 둠
- 배포: 교직원 `scripts/deploy_mcp.sh`, 학생은 값만 바꿔 한 번 더

```
MCP_SERVICE_NAME=rag-mcp-student \
RAG_CORPUS_NAME="${RAG_CORPUS_NAME_STUDENT}" \
MCP_API_KEY="${MCP_API_KEY_STUDENT}" ./scripts/deploy_mcp.sh
```

- `MCP_SERVICE_NAME`은 `.env`에 고정하지 않음. 남기면 다음 배포가 학생 서비스를 덮어씀
- 실측 지연 (단일 MCP, 2026. 7. 28.): 중앙값 1.14초 / 최대 1.39초

#### 라. 저장소

| 저장소 | 용도 |
|---|---|
| GCS `raw` | HWP/HWPX 원본. parser 전달·재파싱. 버킷 1개 |
| GCS `normalized` | 변환 MD, 사이드카, PDF 등. RAG import 대상. 버킷 1개 |
| Firestore DB `doc-state` | Native 모드. `(default)` Datastore는 사용 불가 |
| 컬렉션 `doc_state` | 파일별 상태·경로·해시·`audience` |
| 컬렉션 `doc_dlq` | 처리 실패 |
| 컬렉션 `doc_split_queue` | 크기 초과 대기. 소비 코드 없음 |
| 컬렉션 `sync_tokens` | Drive pageToken |
| 컬렉션 `sync_jobs` | 장시간 작업 진행률 |

객체 키에 student/staff를 넣지 않음.

```
raw/{fileId}{.hwp|.hwpx}
normalized/{fileId}.md
normalized/{fileId}.meta.md
normalized/{fileId}{.pdf|…}
```

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
- `SYNC_FOLDER_IDS`: 수집 범위. 지정 폴더와 하위. 비우면 드라이브 전체
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

1. `.env`에 `STUDENT_FOLDER_IDS` / `RAG_CORPUS_NAME_STUDENT` 설정 후 `rag-sync` 재배포
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

### 1. 자동화

- Scheduler → Workflows → rag-sync, 매일 00:00 KST
- 변경 없을 때 약 18초

### 2. 처리량

| 항목 | 값 |
|---|---|
| Vertex RagDataService | 300 req/min |
| import 지연 | 약 21초/회 (URI 수 무관) |
| 재색인 배치 | URI 24 |
| 삭제 동시 / 페이싱 | 4 / 0.6초 |

### 3. 접근제어

| 서비스 | 방식 |
|---|---|
| rag-mcp | 공개 URL + 교직원 키 |
| rag-mcp-student | 공개 URL + 학생 키 (교직원과 다른 값) |
| rag-sync | 프로젝트 IAM |
| rag-parser | rag-sync SA |

### 4. 상태 (2026. 7. 28.)

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

- 상세 [`OPS_DEFERRED.md`](./OPS_DEFERRED.md)

| 과제 | 사유 |
|---|---|
| 시크릿 → Secret Manager | 뷰어에게 평문 노출 |
| 알림·모니터링 | 스케줄러 3일 중단 사례 |
| Firestore PITR·GCS 버전 | 오조작 복구 수단 없음 |
| raw 버킷 IAM 분리 | 원본 공문 접근 범위 |
| min-instances=0 | 최초 질의 지연 |
| 예산 알림 | 미설정 |

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
