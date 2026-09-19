# GUI Cloud 설정 저장 방식

## 결정

학과 설정의 유일한 원본은 Cloud 등록부다.

- 설정 본문과 API 키: Secret Manager `rag-mcp-departments`
- 활성 revision과 라우팅 스냅샷: Firestore `mcp_registry/current`
- 검색 런타임: API 키 원문 대신 SHA-256 해시 라우팅만 사용

로컬 학과 YAML이나 Cloud Run 메타데이터 주석은 설정 원본으로 사용하지 않는다.

## 사용 흐름

```text
학과 설정 생성·수정
   ↓
Secret Manager에 불변 revision 기록
   ↓
Firestore 포인터와 해시 라우팅 원자적 갱신
   ↓
다른 관리자 PC에서 GCP 로그인
   ↓
Cloud 등록부에서 같은 revision 조회
```

## 운영 기준

등록부에는 `keys.staff`와 `keys.student`가 포함되므로 다음 기준을 지킨다.

- Secret Manager secret accessor와 Firestore 쓰기 권한은 관리자에게만 부여한다.
- GUI 서버는 `127.0.0.1`에만 바인딩한다.
- Secret 원문과 복원된 키를 로그 또는 브라우저 저장소에 남기지 않는다.
- 키 교체는 새 Secret revision과 Firestore 라우팅을 함께 갱신한다.

## 현재 범위

GUI는 신규 학과를 등록부에 직접 만들고 기존 키와 향후 추가 필드를 보존한 채 공통 MCP 및
sync 라우팅을 갱신한다. 동시 수정은 revision 비교로 감지한다.
