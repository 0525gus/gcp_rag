# DocAI 폴백 (미조치)

> 2026-08-02 확인. 코드는 그대로 두었다. 조치 후 이 문서를 지울 것.

## 상태

`QG_MODE=fallback` + `ENABLE_DOCAI_FALLBACK=true` 는 **배포 이미지에서 동작하지 않는다.**

- 폴백 진입점은 HWP→PDF (`services/parser/fallback_docai.py`)
- 변환은 LibreOffice(`soffice`) 필요
- parser Dockerfile에는 LibreOffice가 없음 → 변환 전 예외, Document AI 미호출
- `google-cloud-documentai` 패키지만 있어 준비된 것처럼 보임

## 기본값에서는 안 탐

- `QG_MODE=log`, `ENABLE_DOCAI_FALLBACK=false`
- `fallback`만 켜고 DocAI가 꺼져 있으면 `log`로 강등
- **두 값을 같이 켜야** 이 경로에 진입함

## 같이 켜면

1. 게이트 문서마다 422 `FALLBACK_FAILED` → DLQ
2. `log` 때보다 나쁨 — 품질 낮은 문서도 색인에서 빠짐
3. retry 3회 후 `parked`
4. reason이 `FALLBACK_FAILED`여도 Document AI는 호출되지 않음

## 조치 (미결정)

| 방법 | 효과 | 비용 |
|---|---|---|
| Dockerfile에 LibreOffice 추가 | 폴백 동작 | 이미지 ~1GB, 콜드스타트, 변환 품질 미검증 |
| 폴백 경로 제거 혹은 DocAI(google) 생성 및 추가 | 죽은 코드·documentai 의존성 제거 | `QG_MODE=fallback` 삭제 (README·common.yaml 동반) |
| 기동 가드 | 잘못된 설정 차단 | `soffice` 없으면 기동 실패 또는 health degraded |

지금은 두 값을 함께 켜지 말 것.
