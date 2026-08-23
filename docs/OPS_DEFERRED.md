# 운영 전환 시 처리할 것 (PoC 범위 밖)

PoC 동안 넘긴 항목. **실제 운영 전에** 한 번씩 훑을 것.

- 판단 근거가 사라지면 그때가 처리할 시점
- GCP 숫자(건수·IAM·알림 0건)는 **2026-07-28 / 07-30 스냅샷**. 이후 콘솔 재조회 없음
- 코드·스크립트 사실은 **2026-08-19 재검증**

---

## 코드에서 이미 바뀐 것 (운영 GCP 적용은 별도)

- 버킷 역할명: `hwp-original`(HWP 원본) / `source`(RAG import 산출물). 옛 env `GCS_RAW_BUCKET`·`GCS_NORMALIZED_BUCKET` 은 fallback
- 객체 키: `{fileId}{ext}` (prefix `raw/`·`normalized/` 제거)
- 품질 게이트: 구 G2(셀 실패)·G3(이미지 면적) 삭제. G2 = 표 손실률 (`QG_TABLE_LOSS_RATIO`)
- 무효 env `QG_TABLE_FAIL_RATIO` / `QG_IMAGE_RATIO` 제거. 기본값은 `QG_TABLE_LOSS_RATIO=0.3`
- ingest 병렬: `INGEST_CONCURRENCY` (옛 `RAW_UPLOAD_CONCURRENCY`)
- 검색: TTL 캐시, `search_max_total_chunks=15`, 파일당 최대 3청크
- 의존성: `requirements-mcp.txt` 메이저 상한 (`mcp<2`, `starlette<0.47` 등)
- 배포 전 실물 검사: `scripts/preflight.ps1` (`SKIP_PREFLIGHT=1` 로 건너뜀)
- 알림 **스크립트**: `scripts/setup_alerts.ps1` (채널 + 정책 3 + 예산). **GCP 에 돌렸는지는 미확인**
- DocAI 폴백은 이미지에 LibreOffice 없어 구조적으로 실패 → [`PARSER_DOCAI_FALLBACK.md`](./PARSER_DOCAI_FALLBACK.md)

---

## 1. 시크릿이 평문 환경변수

- `MCP_API_KEY` → Cloud Run env 평문. Secret Manager 미사용
- `deploy.ps1` 은 `secretmanager.googleapis.com` 만 enable. `--set-secrets` 없음
- 키 유실 사고: 리비전 00005(7/25) ~ 00013. `allUsers` 와 겹쳐 무인증 검색 (2026-07-28, `rag-mcp-00014` 복구)
- 배포 스크립트는 키 필수. 우회 경로: `gcloud run services update --set-env-vars`(통째 치환)

**괜찮은 이유:** 프로젝트 IAM 보유자만 읽음  
**문제 시점:** `roles/viewer` 외부 부여. `gcloud run services describe` 에 키 노출  
**조치:** Secret Manager + `--set-secrets`. 그 전엔 `--update-env-vars` 만

---

## 2. 알림 — 스크립트는 있음, 적용은 미확인

07-30 스냅샷: 정책 0 / 로그 지표 0 / 채널 0.

이미 한 번 겪음. 스케줄러 2026-07-25부터 **3일간 400**. 대시보드는 초록(ENABLED·워크플로·Run 정상). 그 기간 신규 문서 없어 유실 없음.

코드 쪽: `$env:ALERT_EMAIL = "…"; .\scripts\setup_alerts.ps1`

- 24시간 무성공
- 워크플로 ERROR
- sync 정체(`pageToken NOT committed` / 학생 코퍼스 cleanup 실패)
- 예산(결제 계정 권한 필요. 없으면 예산만 건너뜀)

**조치:** 스크립트 실행 후 `gcloud alpha monitoring policies list` 로 실존 확인. 5xx 비율은 스크립트에 없음 — 필요하면 추가

---

## 3. 백업·복구 장치 없음

07-30 스냅샷:

```
Firestore  PITR 비활성 / 삭제보호 비활성
GCS        hwp-original · source 둘 다 버전관리 없음
```

- DB 이름: `rag-sync-state`. 컬렉션 `doc_state` 가 색인 상태
- source = RAG import 산출물(MD·통과 PDF·사이드카). 「정규화본」이 아님

**괜찮은 이유:** Drive 가 원본. 최악은 전량 재수집(HWP 재파싱 + 재색인)  
**문제 시점:** `doc_state` 삭제 → 증분이 처음부터. source 덮어쓰기 → 되돌릴 수 없음  
**조치:** Firestore 삭제보호 + PITR, source 버킷 버전관리

---

## 4. 드라이브 파일 위생

07-28 실측(색인 1,211건 / 표본 50):

| | 건수 | 비율 |
|---|---:|---:|
| 파일명 사본 (`폴더 (1)` 등) | 122 / 1,211 | 10.1% |
| 내용 없는 xlsx (스텁만 색인) | 116 / 1,211 | 9.6% |
| 텍스트층 없는 PDF | 표본 50 중 3 | ~6% |
| 초안·의견수렴본 | 201 / 1,211 | 16.6% |

**괜찮은 이유:** 사본 제거 hit@1 **+1건**. 빈 문서 제거는 34% → 32% 로 하락. 점수 문제가 아님  
**문제 시점:** 「최신본」질의. 초안·확정본이 같이 걸리면 인용 통제 불가  
**조치:** 드라이브 정리(사람). 코드로는 경로 패턴 감점 정도

---

## 5. 암호 걸린 xlsx 27건은 못 읽는다

- 본문 파싱: [`shared/xlsx_md.py`](../shared/xlsx_md.py). 116건 중 89건 변환
- 남은 27건: OLE2 + `EncryptedPackage` (MS-OFFCRYPTO). 암호 없이는 불가
- 매직바이트 `D0CF11E0` = 구형 `.xls` 와 동일. 확장자·매직으로 구분 안 됨

**괜찮은 이유:** 파일명·경로 사이드카로 검색됨. 작성자가 암호를 건 문서는 색인 제외가 맞을 수 있음  
**문제 시점:** 그 27건 **본문**을 물을 때. 실패는 로그에만 남음  
**조치:** 소유자에게 암호 해제 요청. 대량 해독은 범위 밖

### 곁들여: 구형 오피스 0건

- `.xls` / `.doc` / `.ppt` 확장자 0, 매직 위장 0
- `xlrd`·LibreOffice 변환 경로 없음. 실물 유입 때 판단

---

## 6. hwp-original 버킷 IAM 미분리

- 두 버킷 IAM 이 프로젝트 기본 역할만으로 동일 (07-30 스냅샷)
- hwp-original: 원본 공문 상주(학사경고자 명단, 인사발령 등)
- source: Vertex 가 import 하는 쪽. RAG SA 가 읽어야 하는 건 여기뿐

**문제 시점:** RAG 서비스 에이전트가 원본 버킷을 읽을 이유 없는데 막혀 있지도 않음  
**조치:** hwp-original 에서 RAG SA 읽기 명시적 배제. `preflight.ps1` 은 버킷 실존·이름 충돌만 봄(IAM 미검사)

---

## 7. 콜드스타트

- `deploy.ps1` 에 `min-instances` 없음 → Cloud Run 기본 0
- `rag-mcp` / `rag-sync` / `rag-parser` 동일

**괜찮은 이유:** 내부 검증. 첫 질의 지연 감수  
**문제 시점:** FactChat 사용자 증가 + 커넥터 타임아웃  
**조치:** `rag-mcp` 만 `min-instances=1`. sync/parser 는 배치라 0

---

## 8. 한도 초과 큐를 비우는 코드가 없다

- `doc_split_queue`: [`firestore_state.py`](../shared/firestore_state.py) `enqueue_split` **쓰기만**. 소비자 없음
- 이름과 달리 **한도 초과 DLQ**. `/sync/retry-failed` 도 같은 게이트에 걸려 `stillFailed`
- 큰 PDF 는 `{fileId}.partN.pdf` 분할 경로가 있음. 쪼갤 수 없는 포맷만 큐로

**괜찮은 이유:** 07-30 큐 0건. HWP→md 실측 최대 ≈ 64KB (10MB 한도의 0.6%). [`DEV_SPEC.md`](./DEV_SPEC.md) 「2. 크기 한도」  
**문제 시점:** 이미지 많은 PPTX·큰 XLSX. 색인에서 빠지는데 큐를 보는 사람 없음  
**조치:** 분할이 답이 아님(용량이 이미지). (1) 적재 시 알림 → 2번 (2) PPTX/XLSX 텍스트 추출 → 5번과 같이 판단

---

## 9. 예산 알림 미확인

- 07-30: 빌링 권한 없어 확인 못 함
- 비용: Vertex RAG(임베딩·검색), Cloud Run, GCS
- `setup_alerts.ps1` 가 예산도 만듦(기본 `100USD`, 50/90/100%). 결제 계정 없으면 건너뜀

**조치:** 스크립트 실행 결과에서 예산 줄이 나왔는지 확인. 없으면 `BILLING_ACCOUNT` 지정

---

# 2차 점검(2026-07-30) 추가분

같은 날 고친 것은 여기 없음. **남긴 것만.** 코드가 이후 바뀐 항목은 본문에 「현재」로 표시.

---

## 10. 세 서비스가 프로젝트 Editor 로 돈다 〔심각〕

07-30 스냅샷:

```
rag-sync / rag-mcp / rag-parser
  → 327280624781-compute@developer.gserviceaccount.com
  → roles/editor
```

- 전용 SA 없음. 기본 Compute Engine 계정. 공개 `rag-mcp` 도 동일
- 1차는 ingress만 봄. 6번은 버킷 IAM만. **런타임 identity 는 사각**
- 공급망: 07-30에 메이저 상한 추가됨(현재 `requirements-mcp.txt`). 상한은 시한만 미룰 뿐

**괜찮은 이유:** API 키만으로는 코드 실행 불가. 키가 새도 검색 이상 못 함  
**문제 시점:** 컨테이너 임의 코드. Editor 면 Firestore·GCS·코퍼스 삭제까지 프로젝트 전체  
**조치:** 서비스별 전용 SA + 최소 권한. 우선 `rag-mcp`(공개)

| 서비스 | 필요 권한 |
|---|---|
| rag-mcp | Vertex RAG 조회, Firestore 읽기 |
| rag-sync | + Firestore 쓰기, GCS 읽기/쓰기, Vertex RAG import/delete, parser 호출 |
| rag-parser | GCS 읽기/쓰기, Firestore 읽기 |

---

## 11. 품질 게이트 — 죽은 판정은 걷어냄. 남는 공백은 G1·reject

**현재** ([`quality_gate.py`](../services/parser/quality_gate.py)):

| 게이트 | 상태 |
|---|---|
| G1 밀도 | 살아 있음. 임계 `QG_DENSITY_THRESHOLD=0.0005` |
| G2 표 손실 | 살아 있음. `table_count` 대비 `tables_rendered`. `QG_TABLE_LOSS_RATIO=0.3` |
| G3 이미지 | **제거**. 페이지 기하가 없어 면적비 계산 경로 없음 |
| EMPTY_TEXT | 살아 있음 |

- 구 G2(셀 실패율)는 빈 셀이 정상이라 판별 자체가 불가했음 → 지표·env 삭제
- G1을 0.0005까지 낮춘 대가(이미지 많은 공문 오탐 방지)는 **그대로**. 진짜 파싱 실패와 이미지 문서를 가르는 신호는 없음
- `QG_MODE=log`(기본). 게이트가 색인을 막지 않음
- `QG_MODE=fallback` 은 LibreOffice 없어 실패 → [`PARSER_DOCAI_FALLBACK.md`](./PARSER_DOCAI_FALLBACK.md)

**괜찮은 이유:** log 모드라 파이프라인을 안 막음  
**문제 시점:** `reject` 전환. G1만으로는 오탐·미탐을 못 가름  
**조치:** 이미지/스캔 신호를 새로 만들거나, reject 를 쓰지 말 것. 셀 실패율·이미지 면적비로 되돌리지 말 것(구조적으로 불가)

---

## 12. 검색 다양성이 파일 단위라 게시글이 결과를 독점할 수 있다

- `postprocess_hits` 는 **fileId** 단위. bundle 상한 없음
- 코퍼스 의미 단위는 자료묶음(게시글)

07-30 스냅샷:

```
INDEXED 1,155건 / bundle 369개   = 평균 3.1
bundle 당 파일 중앙값            2
2개 이상인 bundle               296 / 369 (80.2%)
최다 28·27·27건                 규정 제·개정 / 의견 수렴 류
```

실측: `수강신청 정정기간` top_k=5 중 3건이 같은 게시글(`147294_…`) 첨부.

**괜찮은 이유:** 같은 게시글 첨부는 서로 관련. 항상 손해는 아님  
**문제 시점:** 규정 질의. 상위 bundle 이 4번의 초안·의견수렴. LLM 이 초안 인용  
**조치:** `max_chunks_per_file` 형태의 bundle 상한. **골든 100 먼저** ([`GOLDEN_EVAL.md`](./GOLDEN_EVAL.md), `scripts/eval_golden.py`). 측정 없이 켜지 말 것

---

## 13. 재질의 폭주 — 지시문으로는 안 잡힘

툴 설명에 「한 질문 한 번」을 넣음(커밋 `4eaeea8`). 지켜지지 않음.

운영 로그 7/29–7/30:

```
총 search 호출   50
고유 질의        37       ← 13회(26%)가 바이트 동일 재질의
최다 반복        8회      LMS AI 기능 수요조사 …
```

한 질문에 18분·5버스트(21초에 8회). top_k=10~20 응답 ~60KB → 질문당 1MB 초과.

**완화됨:** `(query, top_k, drive_id)` TTL 캐시. `SEARCH_CACHE_TTL_SECONDS` 기본 60. 동일 문자열만 잡음  
**남은 문제:** 표현만 바꾼 재질의. `uncoveredTerms`/`coverage` 가 응답에 실려도 호출측이 「없음」결론으로 안 씀  
**문제 시점:** 비용·지연. 인스턴스 최대 20, Vertex 호출당 과금  
**조치:** (1) 없는 것을 없다고 말하는 응답 설계 (2) FactChat 프롬프트. 서버가 강제 불가

---

## 14. 운영에만 손으로 들어간 설정값

`--set-env-vars` 는 치환. 스크립트에 없는 값은 배포 한 번에 사라짐.

07-30 운영 vs **현재** 스크립트 기본 (`deploy.ps1` / `config/common.yaml`):

| 대상 | 07-30 운영 | 현재 스크립트 | 판단 |
|---|---|---|---|
| `SEARCH_FETCH_MULTIPLIER` | 6 | 3 | 운영을 3으로 맞출 것 |
| `RAG_DELETE_CONCURRENCY` | 4 | 1 | 쿼터 올렸으면 운영이 맞음 |
| `RAG_DELETE_PACING_SECONDS` | 0.6 | 1.1 | 위와 같음 |
| parser `--concurrency` | **160** | **4** (구문서의 8은 폐기) | 운영을 4로 |

- `SEARCH_FETCH_MULTIPLIER=6` 은 불변식 위반. [`config.py`](../shared/config.py): `search_fetch_max >= search_top_k_max * multiplier`. 운영이면 `60 < 20×6`. `top_k<=10` 까지 무해, 그 이상 여유분 조용히 절단. `SEARCH_FETCH_MAX=100`(Vertex 상한) vs `SEARCH_TOP_K_MAX` 하향은 실측자가 정할 것
- parser conc 160: HWP 통째 메모리 + 네이티브, 컨테이너 2Gi. 지금은 `INGEST_CONCURRENCY=8` + 워크플로 순차가 실효 동시성을 묶음
- 운영 반영: `gcloud run services update rag-parser --region=asia-northeast3 --concurrency=4`
- DELETE 4 / 0.6 은 300rpm 가정. 쿼터 없으면 429. `config/common.yaml` 주석과 동일
- 배포·알림은 PowerShell (`deploy.ps1` / `deploy_mcp.ps1` / `setup_alerts.ps1`)

---

## 15. 자잘한 것

- **델타 색인 상한 ~1,050 URI.** `index_batch_size` 는 backfill/복구 전용. 델타는 `pending_uris` 한 방, timeout 900초. import 25개씩 + 배치 사이 2초. 호출당 ~21초 → 42회 ≈ 1,050. 하루 100건이면 무해. YAML 주석과 일치
- **`sync_jobs` 미정리.** 07-30 기준 8건. 재색인 기록이 쌓임
- **`doc_state` DELETED 영구 잔존.** 07-30 기준 100건. 복구 재색인용이라 의도. 무한 적재
- **`cleanup.py` `_PAGE_NO` 가 `mix` 를 페이지번호로 오인.** 로마숫자 정규식이 m+x+i 로 붙음(손본 뒤에도 동일). 한글 공문에 `mix` 단독 줄이 나올 일 없어 **위해는 없음**
