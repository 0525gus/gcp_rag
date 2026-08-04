# shellcheck shell=bash
# .env 를 셸 환경으로 올린다. 배포 스크립트가 source 해서 쓴다.
#
# 왜 필요한가 — deploy.sh 는 `--set-env-vars` 로 Cloud Run env 를 **통째로 치환**하고,
# 넘기지 않은 변수는 `${VAR:-기본값}` 으로 떨어진다. .env 에만 적어두고 export 를
# 안 하면 운영 값이 조용히 기본값으로 되돌아간다:
#   RAG_DELETE_CONCURRENCY  4 → 1     삭제가 5배 느려짐
#   SYNC_FOLDER_IDS         값 → ""   공유 드라이브 **전체**가 수집 대상
#
# 왜 `source .env` 가 아닌가 — source 는 무조건 대입이라 .env 가 셸을 이긴다.
# 그러면 학생용 MCP 배포처럼 값만 바꿔 두 번 도는 절차가 깨진다:
#   RAG_CORPUS_NAME="${RAG_CORPUS_NAME_STUDENT}" ./scripts/deploy_mcp.sh
# 앞에 붙인 값이 .env 의 교직원 코퍼스로 덮여 **학생 서비스에 교직원 자료가 실린다.**
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

    # 값 양끝 따옴표 제거
    if [[ ${#val} -ge 2 ]]; then
      case "$val" in
        \"*\") val="${val:1:${#val}-2}" ;;
        \'*\') val="${val:1:${#val}-2}" ;;
      esac
    fi

    export "$key=$val"
  done < "$file"
}
