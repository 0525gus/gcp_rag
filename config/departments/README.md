# 학과 설정

- 파일 하나 = 학과 하나
- 파일 이름(`cs.yaml`)의 `cs` 가 학과 코드
- **키가 평문으로 들어간다.** 그래서 실값 파일은 커밋하지 않는다

## 커밋 여부

| 파일 | 커밋 | 담는 것 |
|---|---|---|
| `dept.yaml.example` | O | 템플릿 (플레이스홀더만) |
| `<학과>.yaml` | **X** — `.gitignore` | 코퍼스 ID, **MCP 키**, 버킷, 폴더 ID |
| `../common.yaml` | O | 학과 무관 공통값 |

`ALLOW_UNAUTH=true` 라 MCP 엔드포인트가 공개다. URL 과 키만 맞으면 인터넷
누구나 호출된다 — GCP 프로젝트 권한은 여기에 영향이 없다(그건 컨트롤 플레인,
이건 데이터 플레인). git 은 키를 **회전해도 이력에 영구히 남기는** 유일한
저장소라 실값 파일을 넣지 않는다.

대신 학과 목록·코퍼스 ID 도 git 밖에 있다. **백업은 각자 책임.**

## 저장하는 것 / 안 하는 것

저장 — 파생 불가능한 값만:

- 코퍼스 ID (GCP 발급)
- MCP 키
- 버킷 이름
- 폴더 ID
- 상주 인스턴스 수

저장 안 함 — 규칙으로 만든다 (두 곳에 적힌 값은 갈라진다):

| 값 | 규칙 |
|---|---|
| Cloud Run 서비스 | `rag-mcp-{학과}-{staff\|student}` |

## 학과 추가

```powershell
# 1. 코퍼스 2개 + 버킷 2개 생성
gcloud storage buckets create gs://ee-rag-hwp-PROJECT --location=asia-northeast3
gcloud storage buckets create gs://ee-rag-source-PROJECT --location=asia-northeast3

# 2. 템플릿 복사 후 채우기 (커밋하지 않는다)
cp config/departments/dept.yaml.example config/departments/ee.yaml

# 3. 키 생성해서 keys 에 기입
python -c "import secrets;print(secrets.token_urlsafe(32))"

# 4. 배포
.\scripts\deploy_mcp.ps1 -Dept ee
```

## 배포 전 자동 검사

거부:

- `CHANGE_ME` 잔존
- staff·student **코퍼스**가 같음 (학생이 교직원 전량을 보게 됨)
- staff·student **키**가 같음
- **학과 간** 키 중복 (배포 시작 전 전량 대조)
- `buckets` 를 한쪽만 기입 (원본은 학과 버킷, 산출물은 공용으로 갈라짐)

경고만 (막지 않음):

- 키가 24자 미만이거나 사전 단어 포함

## 버킷

- 학과마다 따로 만든다. 생략하면 `common.yaml` 의 공용 버킷을 상속
- 객체 키가 Drive fileId 라 공용이어도 **충돌은 없다.** 나누는 이유는 격리:
  학과에 GCS 권한을 줘도 남의 원본이 안 열리고, 학과 이탈 시 버킷 삭제로 끝난다
- **나중에 나누면 비싸다** — 코퍼스가 옛 `gs://` URI 를 참조하므로 전량 삭제 후
  재import 가 따라온다. 처음부터 따로 둘 것
- 이름은 GCS **전역** 유일. 프로젝트명을 접미사로 붙인다
- rag-sync 만 쓴다. 파서는 `gcsUri` 를 요청으로 받고, MCP 는 GCS 를 안 만진다

## 전 학과 배포

```powershell
.\scripts\deploy_mcp.ps1 -All            # 키는 가려짐
.\scripts\deploy_mcp.ps1 -All -ShowKeys  # 요약표에 키 노출
```

이미지는 **한 번만** 빌드하고 같은 digest 를 전 학과에 배포한다 — 학과끼리
다른 코드가 도는 일이 없다.

## 주의

- 설정 원본은 `config/` 하나뿐이다. `-Dept` 또는 `-All` 이 **필수**
- 실값 파일은 gitignore 대상이라 VS Code 탐색기에서 숨겨질 수 있다
  (`explorer.excludeGitIgnore`). `.vscode/settings.json` 에서 꺼 뒀다
- git 으로 복구할 수 없으니 **에디터의 오래된 탭이 덮어쓰지 않도록** 주의
