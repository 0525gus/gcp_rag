# shellcheck shell=bash
# .env 를 셸 환경으로 올린다. 배포 스크립트가 source 해서 쓴다.
#
# 왜 필요한가 — 배포 스크립트는 `--set-env-vars` 로 Cloud Run env 를 **통째로 치환**하고,
# 넘기지 않은 변수는 `${VAR:-기본값}` 으로 떨어진다. .env 에만 적어두고 export 를
# 안 하면 운영 값이 조용히 기본값으로 되돌아간다:
#   RAG_DELETE_CONCURRENCY  4 → 1     삭제가 5배 느려짐
#   SYNC_FOLDER_IDS         값 → ""   공유 드라이브 **전체**가 수집 대상
#
# 왜 `source .env` 가 아닌가 — source 는 무조건 대입이라 .env 가 셸을 이긴다.
# 앞에 붙인 일회성 값이 파일로 덮이면 학생 MCP 에 교직원 코퍼스가 실린다.
# 그래서 여기서는 **이미 셸에 있는 변수는 건드리지 않는다.**
load_dotenv() {
  local file="${1:-.env}"
  [[ -f "$file" ]] || return 0

  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Windows 에서 편집하면 CRLF 가 붙는다. 안 떼면 값 끝에 \r 이 남아
    # 코퍼스 이름·버킷명이 조용히 어긋난다.
    line="${line%$'\r'}"

    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" != *=* ]] && continue

    key="${line%%=*}"
    val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#export }"

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # 셸에 이미 있으면 그쪽이 이긴다 (일회성 오버라이드 허용)
    [[ -n "${!key+x}" ]] && continue

    # 값 양끝 공백 제거. 편집하다 줄 끝에 스페이스 하나가 남는 일이 실제로
    # 있었고(RAG_CORPUS_NAME), 그대로 --set-env-vars 로 들어가면 코퍼스 이름이
    # 어긋나 조회가 통째로 실패한다. 눈에 안 보이는 문자라 추적도 어렵다.
    # scripts/_env.py(파이썬 로더)는 이미 같은 처리를 한다 — 정책을 맞춘다.
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"

    # 값 양끝 따옴표 제거. 공백보다 나중에 떼야 따옴표 안의 의도적 공백이 산다
    if [[ ${#val} -ge 2 ]]; then
      case "$val" in
        \"*\") val="${val:1:${#val}-2}" ;;
        \'*\') val="${val:1:${#val}-2}" ;;
      esac
    fi

    export "$key=$val"
  done < "$file"
}

# 예제 값이면 배포를 막는다. 안 막으면 Cloud Run 이 뜨고 런타임에서 죽는다.
# .env.example 의 자리표시자·중괄호 템플릿({project} 등)을 본다.
_is_placeholder() {
  local val="$1"
  [[ -z "$val" ]] && return 0
  [[ "$val" == *'{'* ]] && return 0
  case "$val" in
    your-project-id|change-me-to-a-long-random-secret|shared-drive-id-1|shared-drive-id-2|shared-drive-id-1,shared-drive-id-2)
      return 0 ;;
  esac
  return 1
}

_require_add() {
  local name="$1" hint="$2"
  local val="${!name-}"
  if [[ -z "$val" ]]; then
    _REQUIRE_ERRS+=("${name}: empty${hint:+ (${hint})}")
  elif _is_placeholder "$val"; then
    _REQUIRE_ERRS+=("${name}: example value (${val})")
  fi
}

_require_flush() {
  if ((${#_REQUIRE_ERRS[@]} == 0)); then
    return 0
  fi
  echo "== .env check failed ==" >&2
  local e
  for e in "${_REQUIRE_ERRS[@]}"; do
    echo "- ${e}" >&2
  done
  echo "fix .env and retry" >&2
  return 1
}

# deploy.ps1 과 같은 규칙 (pytest). 버킷·Drive 가 비면 색인이 빈 채로 돈다.
require_full_deploy_env() {
  _REQUIRE_ERRS=()
  _require_add GCP_PROJECT_ID ""
  _require_add GCS_RAW_BUCKET ""
  _require_add GCS_NORMALIZED_BUCKET ""
  _require_add RAG_CORPUS_NAME "Vertex RAG corpus path"
  _require_add DRIVE_IDS "shared drive id"
  _require_add MCP_API_KEY "set MCP_API_KEY_STAFF"

  local student_corpus="${RAG_CORPUS_NAME_STUDENT:-}"
  local student_folders="${STUDENT_FOLDER_IDS:-}"
  if [[ -n "$student_corpus" && -z "$student_folders" ]]; then
    _REQUIRE_ERRS+=("STUDENT_FOLDER_IDS: required when RAG_CORPUS_NAME_STUDENT is set")
  elif [[ -z "$student_corpus" && -n "$student_folders" ]]; then
    _REQUIRE_ERRS+=("RAG_CORPUS_NAME_STUDENT: required when STUDENT_FOLDER_IDS is set")
  fi
  if [[ -n "$student_corpus" ]] && _is_placeholder "$student_corpus"; then
    _REQUIRE_ERRS+=("RAG_CORPUS_NAME_STUDENT: example value (${student_corpus})")
  fi
  if [[ -n "${MCP_API_KEY_STUDENT:-}" && -n "${MCP_API_KEY:-}" && "${MCP_API_KEY_STUDENT}" == "${MCP_API_KEY}" ]]; then
    _REQUIRE_ERRS+=("MCP_API_KEY_STUDENT: must differ from MCP_API_KEY_STAFF")
  fi
  _require_flush
}

_mcp_staff_name() { echo "${MCP_SERVICE_NAME_STAFF:-rag-mcp}"; }
_mcp_student_name() { echo "${MCP_SERVICE_NAME_STUDENT:-rag-mcp-student}"; }

_mcp_deploy_name() {
  if [[ -n "${MCP_SERVICE_NAME:-}" ]]; then
    echo "${MCP_SERVICE_NAME}"
    return
  fi
  if [[ "${MCP_AUDIENCE:-}" == *student* ]]; then
    _mcp_student_name
    return
  fi
  _mcp_staff_name
}

_mcp_is_student() {
  local s="$1"
  [[ "${MCP_AUDIENCE:-}" == *student* ]] && return 0
  [[ "$s" == "$(_mcp_student_name)" ]] && return 0
  [[ "$s" == *student* ]] && return 0
  return 1
}

# deploy_mcp.ps1 과 같은 규칙 (테스트용). 버킷은 unused 기본값 허용.
require_mcp_deploy_env() {
  _REQUIRE_ERRS=()
  _require_add GCP_PROJECT_ID ""
  _require_add RAG_CORPUS_NAME "Vertex RAG corpus path"
  _require_add MCP_API_KEY "set MCP_API_KEY_STAFF (or MCP_API_KEY for student)"

  local service
  service="$(_mcp_deploy_name)"
  if _mcp_is_student "$service"; then
    if [[ -n "${MCP_API_KEY_STAFF:-}" && "${MCP_API_KEY}" == "${MCP_API_KEY_STAFF}" ]]; then
      _REQUIRE_ERRS+=("MCP_API_KEY: student service must not reuse MCP_API_KEY_STAFF")
    fi
    if [[ -n "${RAG_CORPUS_NAME_STUDENT:-}" && "${RAG_CORPUS_NAME}" != "${RAG_CORPUS_NAME_STUDENT}" ]]; then
      _REQUIRE_ERRS+=("RAG_CORPUS_NAME: student deploy must use RAG_CORPUS_NAME_STUDENT")
    fi
  elif [[ -n "${RAG_CORPUS_NAME_STUDENT:-}" && "${RAG_CORPUS_NAME}" == "${RAG_CORPUS_NAME_STUDENT}" ]]; then
    _REQUIRE_ERRS+=("MCP_AUDIENCE: student corpus on staff service ${service} — set MCP_AUDIENCE=student")
  fi
  _require_flush
}
