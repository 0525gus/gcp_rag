# DocAI 폴백 경로 (미조치 — 검토 결과 기록)

> 2026-08-02 파이프라인 전수조사에서 확인. **코드는 그대로 두었다.**
> 조치할 때 이 문서를 근거로 쓰고, 반영 후 이 경고를 지울 것.

## 요약

`QG_MODE=fallback` + `ENABLE_DOCAI_FALLBACK=true` 조합은 **배포 이미지에서 구조적으로
동작할 수 없다.** 설정·문서·의존성은 모두 갖춰져 있는데 실행 파일 하나가 없다.

## 왜 동작할 수 없나

`services/parser/fallback_docai.py` 의 진입점은 HWP→PDF 변환이고, 그 변환은
LibreOffice 헤드리스에 의존한다.

```python
soffice = shutil.which("soffice") or shutil.which("libreoffice")
if not soffice:
    raise FallbackParseError("LibreOffice(soffice) not found in PATH")
```

`services/parser/Dockerfile` 이 설치하는 OS 패키지는 `libexpat1` 뿐이다. 이미지
어디에도 LibreOffice 가 없으므로 `hwp_to_pdf` 는 **항상 첫 줄에서** 예외를 던진다.
Document AI 는 호출조차 되지 않는다.

`requirements-parser.txt` 가 `google-cloud-documentai` 를 설치하고 있어 의존성만
보면 준비된 것처럼 보이는 점이 오진을 돕는다.

## 왜 눈에 안 띄었나

기본값이 이 경로를 막고 있다.

- `QG_MODE` 기본값 `log`
- `ENABLE_DOCAI_FALLBACK` 기본값 `false`
- `QG_MODE=fallback` 인데 `ENABLE_DOCAI_FALLBACK=false` 면 `services/parser/main.py`
  가 경고만 남기고 `log` 로 강등한다 — 폴백 코드에 진입하지 않는다

즉 **두 값을 동시에 켜야만** 드러난다. 그 전까지는 죽은 코드가 조용히 남아 있다.

## 켰을 때 벌어지는 일

1. 게이트에 걸린 문서마다 `FallbackParseError` → 파서가 422 `FALLBACK_FAILED`
2. sync 가 그대로 DLQ 로 이관
3. **전환 전보다 나빠진다.** `QG_MODE=log` 였다면 "품질은 낮지만 색인은 되던"
   문서들이 이제 전부 색인에서 빠진다 → 검색 결과가 눈에 띄게 준다
4. `retry-failed` 가 maxAttempts(기본 3)까지 같은 실패를 반복한 뒤 `parked` 로 보류
5. DLQ reason 이 `FALLBACK_FAILED` 라 "Document AI 문제"로 읽히지만, 실제로는
   Document AI 를 부르지도 못했다

## 선행 관계

구 G2(표 셀 실패율)·G3(이미지 면적비)와 **같은 부류**다 — 설정과 문서가 존재하는데
그 지표를 채우는 코드가 없어 구조적으로 발동할 수 없었던 게이트. 그쪽은 커밋
`6113864` 에서 제거했다. 이 항목은 아직 제거하지 않았다.

## 조치 선택지 (미결정)

| 방법 | 얻는 것 | 비용 |
|---|---|---|
| ① Dockerfile 에 LibreOffice 추가 | 폴백이 실제로 동작 | 이미지 ~1GB 증가, 콜드스타트 지연. HWP→PDF 변환 품질은 **미검증** |
| ② 폴백 경로 제거 | 죽은 코드·죽은 설정 소멸, documentai 의존성 제거 | `QG_MODE=fallback` 옵션 삭제 (README·config/common.yaml 동반 수정) |
| ③ 기동 시 가드 | 잘못된 설정으로 켜는 것을 막음 | `ENABLE_DOCAI_FALLBACK=true` 인데 `soffice` 가 없으면 파서가 기동 실패 또는 `/health` degraded — 코드 몇 줄 |

②를 택하면 선례(`6113864`)와 일관되고, ①을 택하면 변환 품질부터 실측해야 한다.
③은 ①·② 어느 쪽을 택하든 그 전까지의 안전장치로 단독 적용 가능하다.

## 지금 당장 필요한 것은 없다

기본값(`QG_MODE=log`)에서는 이 경로에 진입하지 않는다. **두 값을 함께 켜지만
않으면 된다.** 켜기 전에 이 문서를 볼 것.
