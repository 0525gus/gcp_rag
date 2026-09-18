"""Conservative Vertex Gemini query rewriting for retrieval only."""

from __future__ import annotations

import logging
import re

import google.auth
from google.auth.transport.requests import AuthorizedSession

from shared.config import Settings

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_NUMBER = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_PLACEHOLDER = re.compile(r"(?:OO|○○|XX|□□)\s*(?:대학교|대학|기관|회사)", re.IGNORECASE)
_QUESTION_FORM = re.compile(
    r"[?？]|(?:요|까|나요|인가요|어떻게|언제|어디|누구|무엇|얼마|알려|보여)$"
)
_CONTEXT_REFERENCES = (
    "그거",
    "그것",
    "그때",
    "그쪽",
    "그곳",
    "저번",
    "거기",
    "이거",
    "이것",
    "저것",
)
# These are intentionally conservative. A false rejection only loses the expansion;
# accepting a dropped negation, date, or comparison changes what the user asked for.
_REQUIRED_MARKERS = ("아닌", "제외", "이전", "이후", "초과", "미만", "이상", "이하", "앞당")

PROMPT = """# 역할

너는 사용자의 질의를 벡터/하이브리드 검색 엔진에 최적화된 독립형 검색 쿼리로 재작성하는 전문 엔진이다.

# 핵심 규칙

1. 문맥 해결 (대명사 복원): 이전 대화의 지시대명사(그거, 저번에 말한 것, 거기 등)를 명확한 고유명사나 구체적 대상 명사로 치환한다. 이전 대화가 제공되지 않은 경우에는 추측하지 않는다.
2. 독립형 쿼리화: 대화 기록 없이 현재 쿼리 하나만 보고도 검색 엔진이 문서 의미를 파악할 수 있도록 완성형 문장/구문으로 구성한다.
3. 잡음 제거: 감정 표현, 인사말, 질문 형태의 군더더기(\"알려줘\", \"궁금해\", \"~인 것 같은데 맞니?\")를 제거하고 핵심 검색 의도 위주로 재구성한다.
4. 사실 왜곡 금지: 대화 맥락에 없는 정보를 임의로 지어내지 않는다. 원문의 날짜·숫자·학기·부정·제외·비교 조건을 반드시 보존한다.

# 출력 규칙

- 부가 설명, 인사말, 인용부호 없이 재작성된 검색 쿼리 단 1줄만 출력할 것."""


def _normalized(value: str) -> str:
    return _SPACE.sub("", value or "")


def should_rewrite_query(query: str) -> bool:
    """Skip obvious standalone keyword searches before paying for an LLM call."""
    text = (query or "").strip()
    if not text:
        return False
    if any(reference in text for reference in _CONTEXT_REFERENCES):
        return True
    words = text.split()
    # Short noun phrases such as "2026학년도 학사일정" already make strong
    # retrieval queries. Questions and longer requests still get an expansion.
    return not (len(words) <= 4 and not _QUESTION_FORM.search(text))


def safe_rewrite(original: str, rewritten: str) -> str | None:
    """Return a usable expansion only when simple loss checks pass."""
    candidate = " ".join((rewritten or "").splitlines()).strip().strip("\"'")
    if not candidate or _normalized(candidate) == _normalized(original):
        return None
    if _PLACEHOLDER.search(candidate):
        return None
    original_numbers = set(_NUMBER.findall(original))
    candidate_numbers = set(_NUMBER.findall(candidate))
    if not original_numbers.issuperset(candidate_numbers) or not candidate_numbers.issuperset(
        original_numbers
    ):
        return None
    for marker in _REQUIRED_MARKERS:
        if marker in original and marker not in candidate:
            return None
    return candidate


def rewrite_query(query: str, settings: Settings) -> str | None:
    """Generate a validated search expansion; failure never blocks raw retrieval."""
    if (
        not settings.search_rewrite_enabled
        or not query.strip()
        or not settings.search_rewrite_model
    ):
        return None
    try:
        credentials, _ = google.auth.default(scopes=[_SCOPE])
        session = AuthorizedSession(credentials)
        url = (
            "https://aiplatform.googleapis.com/v1/projects/"
            f"{settings.gcp_project_id}/locations/global/publishers/google/models/"
            f"{settings.search_rewrite_model}:generateContent"
        )
        response = session.post(
            url,
            json={
                "systemInstruction": {"parts": [{"text": PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": query}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 128,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=settings.search_rewrite_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        parts = body["candidates"][0]["content"]["parts"]
        generated = "".join(str(part.get("text") or "") for part in parts)
        return safe_rewrite(query, generated)
    except Exception:
        logger.warning("search query rewrite unavailable; using raw query", exc_info=True)
        return None
