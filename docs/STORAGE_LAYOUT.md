# GCS 버킷을 왜 둘로 나눠 쓰는가

`raw` 와 `normalized` 두 버킷을 운영한다. 이 문서는 **각각이 실제로 무슨 일을
하는지**, 그리고 **지금 이 분리가 값을 하고 있는지**를 코드와 실측으로 정리한다.

결론부터: **raw 객체는 없으면 파이프라인이 안 돌아간다. 다만 "버킷을 둘로
나눈 것" 자체는 현재 아무것도 벌고 있지 않다.** 둘은 다른 이야기다.

---

## 1. 실제 흐름

```
Drive
  │  drive.download_file()            메모리로 받음
  ▼
[sync] ─── HWP/HWPX ────────────────────────────────┐
  │                                                  │
  │  gcs.upload_raw()                                │  그 외(PDF/XLSX/PPTX/TXT)
  ▼                                                  │  본문 변환 없이 그대로
gs://…-raw/raw/{fileId}.hwp                          │
  │                                                  │
  │  POST /parse {gcsUri: …}                         │
  ▼                                                  │
[parser] gcs.download_bytes(gcsUri)                  │
  │      rhwp/hwpx 파싱 → markdown                    │
  │      gcs.upload_normalized_md()                  │
  ▼                                                  ▼
gs://…-normalized/normalized/{fileId}.md    gs://…-normalized/normalized/{fileId}.pdf
  │                                                  + {fileId}.meta.md (경로·묶음 사이드카)
  │  sync 가 다시 받아 머리말(제목·자료묶음)을 붙여 재업로드
  ▼
Vertex AI RAG Engine  ← normalized 버킷만 읽는다
```

관련 코드
- 업로드: [`shared/gcs.py`](../shared/gcs.py) `upload_raw` / `upload_normalized_md` / `upload_path_sidecar_md`
- HWP 경로: [`services/sync/main.py`](../services/sync/main.py) `_ingest_hwp`
- 그 외 경로: 같은 파일 `_ingest_google_export` / `_ingest_file_copy`
- 파서: [`services/parser/main.py`](../services/parser/main.py)

## 2. 실측 (2026-07-28)

| | 객체 수 | 용량 | 담는 것 |
|---|---:|---:|---|
| `…-raw/raw/` | 892 | 271 MB | `.hwp` / `.hwpx` **원본만** |
| `…-normalized/normalized/` | 1,526 | 530 MB | `.md`, `.meta.md`, 복사된 `.pdf`/`.xlsx`/`.pptx` |

raw 에 HWP 만 있는 이유: **변환이 필요한 포맷만 raw 를 거친다.** PDF·XLSX 는
RAG Engine 이 직접 읽거나(PDF) 사이드카로 대체하므로(XLSX) normalized 로 바로
간다.

## 3. raw 객체는 무슨 일을 하나

두 가지다. 둘 다 실재한다.

### (1) sync → parser 사이의 페이로드 채널

sync 와 parser 는 **별개의 Cloud Run 서비스**다. 20MB 짜리 HWP 바이트를 HTTP
본문에 실어 보내는 대신, GCS 에 놓고 URI 만 넘긴다.

```python
raw_uri = gcs.upload_raw(raw, body.file_id, ext)      # sync
resp = client.post(parser_url + "/parse", json={"gcsUri": raw_uri, ...})
raw = gcs.download_bytes(req.gcs_uri)                  # parser
```

이게 raw 의 **1차 존재 이유**다. 버킷이 아니라 RPC 버퍼로 쓰인다.

### (2) 재파싱 소스

파서를 고쳤을 때 Drive 892건을 다시 받지 않고 raw 에서 바로 다시 돌릴 수 있다.
Drive API 쿼터·속도·권한을 한 번 더 통과하지 않아도 된다.

> 실제로 최근 머리말 개편은 **본문을 안 건드렸기 때문에** normalized 만 다시
> 써서 끝났다. 파싱 결과 자체를 바꿔야 했다면 raw 가 유일한 출발점이었다.

### raw 를 지우면

파이프라인이 선다. `_ingest_hwp` 가 URI 를 만들 곳이 없다. **"오래돼서 안 쓰는
백업" 이 아니다.**

---

## 4. 그런데 버킷을 둘로 나눈 건 지금 값을 못 하고 있다

분리가 벌어야 할 것은 셋인데, **현재 셋 다 설정돼 있지 않다.**

```
$ gcloud storage buckets get-iam-policy gs://…-raw
$ gcloud storage buckets get-iam-policy gs://…-normalized
→ 두 버킷 IAM 이 바이트 단위로 동일 (프로젝트 기본 legacy 역할만)

$ gcloud storage buckets describe …
→ lifecycle 없음 / STANDARD / 버전관리 없음  ← 양쪽 동일
```

즉 지금은 **관리 대상만 둘이고 이득은 하나치**다. 분리를 유지하려면 아래 셋 중
최소 하나는 실제로 설정해야 하고, 아니면 프리픽스 하나짜리 단일 버킷으로
합치는 게 정직하다.

### (a) IAM 분리 — 가장 값이 큼

raw 에는 **원본 공문**이 그대로 있다. 학사경고자 명단, 재입학생 명단, 인사발령
같은 개인정보가 포함된다. normalized 는 RAG Engine 서비스 에이전트가 읽어야
하지만, **raw 는 읽을 이유가 없다.**

버킷이 나뉘어 있으면 이건 바인딩 한 줄 차이다. 한 버킷이었다면 IAM Conditions
로 리소스 이름 프리픽스를 걸어야 하는데, 조건식을 잘못 쓰면 조용히 열린다.

### (b) 수명주기 분리

raw 는 파싱이 끝나면 식는다 — 재파싱할 때만 읽는다. normalized 는 재색인마다
읽히는 뜨거운 데이터다.

```
raw:        90일 후 Nearline, 365일 후 Coldline
normalized: STANDARD 유지
```

271 MB 규모에서는 푼돈이지만, 코퍼스가 10배가 되면 의미가 생긴다. 프리픽스
기반 lifecycle 도 가능하긴 하므로, 이것만으로 분리를 정당화하진 못한다.

### (c) 광역 삭제·와일드카드 사고 차단

이건 **이미 한 번 값을 했다.** 머리말 마이그레이션 때 이런 명령을 썼다.

```
gcloud storage cp <local>/*.md gs://…-normalized/normalized/
gcloud storage cp "gs://…-normalized/normalized/*.md" <local>/
```

raw 가 같은 버킷에 있었다면 프리픽스 하나 잘못 쓰는 순간 892개 원본이
사정권에 들어온다. RAG import 도 마찬가지다 — `gs://bucket/**` 같은 글롭을
넘기면 `.hwp` 가 딸려 들어가고, RAG Engine 은 HWP 를 지원하지 않으므로 실패가
난다. 그 실패는 조용히 삼켜지기 쉬운 종류다(실제로 그런 결함을 한 번 고쳤다 —
`21efed9`).

버킷이 다르면 이 사고는 **문법적으로 불가능**해진다.

---

## 5. 권고

| | |
|---|---|
| raw 객체 | **유지.** 파이프라인이 의존한다 |
| 버킷 분리 | **유지하되 실제로 설정할 것.** 지금은 이름만 나뉘어 있다 |

우선순위:

1. **raw 에서 RAG Engine 서비스 에이전트 읽기 권한을 명시적으로 배제** — 개인정보가
   들어있고, 읽을 이유가 없다
2. raw 수명주기 규칙 (90일 Nearline) — 지금은 푼돈이지만 설정 비용도 푼돈이다
3. 둘 다 안 할 거면 단일 버킷 + `raw/`, `normalized/` 프리픽스로 합치기.
   관리 대상을 하나 줄이는 게 이름만 나눠두는 것보다 낫다

## 6. 관련 설정

```
GCS_RAW_BUCKET          raw 버킷 이름   (shared/config.py)
GCS_NORMALIZED_BUCKET   normalized 버킷 이름
```

객체 키 규칙 (`shared/gcs.py`)

```
raw/{fileId}{.hwp|.hwpx}        원본
normalized/{fileId}.md          파싱·정규화된 본문 (RAG import 대상)
normalized/{fileId}.meta.md     바이너리용 경로·묶음 사이드카
normalized/{fileId}{.pdf|…}     변환 없이 복사된 원본 (RAG 가 직접 읽음)
```

`fileId` 는 Drive 파일 ID 다. 검색 결과에서 되돌리는 로직은
[`shared/search_postprocess.py`](../shared/search_postprocess.py) `extract_file_id`
에 있고, 크기 초과로 쪼갠 조각의 `.partN` 접미사도 여기서 떼어낸다.
