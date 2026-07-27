"""벡터 순위 + 어휘 순위를 RRF 로 합치는 재정렬.

배경: 이 저장소는 한 번 "추측 기반 재정렬"을 걷어낸 적이 있다(83563ad).
score 가 거리인지 유사도인지 모르는 채 변환했다가 순위가 뒤집힌 사고였다.
그래서 여기서는 **점수를 일절 쓰지 않고 순위만** 쓴다(RRF). 점수 의미를
몰라도 안전하고, 벡터가 놓친 정확한 표현 일치를 어휘 쪽이 보완한다.

적용 범위는 이미 받아온 후보 청크뿐이다(재검색 아님). 따라서 recall 은
그대로고 상위 순서만 바뀐다.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# 한글/영문/숫자 덩어리만 남긴다 (조사·기호는 아래 bigram 이 흡수)
_TOKEN = re.compile(r"[가-힣]+|[a-zA-Z]+|\d+")
_RRF_K = 60.0


def _tokens(text: str) -> list[str]:
    """어휘 매칭용 토큰. 한글은 2-gram 까지 만들어 어미 변화를 흡수한다.

    '학사일정이' 와 '학사일정' 은 토큰으로는 다르지만 bigram 을 공유한다.
    """
    out: list[str] = []
    for m in _TOKEN.findall((text or "").lower()):
        out.append(m)
        if len(m) > 1 and "가" <= m[0] <= "힣":
            out.extend(m[i : i + 2] for i in range(len(m) - 1))
    return out


def _bm25_scores(query: str, docs: list[str]) -> list[float]:
    """후보 집합 안에서의 BM25. 코퍼스 전체가 아니라 후보만 대상이다."""
    k1, b = 1.5, 0.75
    doc_tokens = [_tokens(d) for d in docs]
    lengths = [len(t) or 1 for t in doc_tokens]
    avg_len = sum(lengths) / len(lengths)
    n = len(docs)

    df: Counter[str] = Counter()
    for toks in doc_tokens:
        df.update(set(toks))

    q_terms = set(_tokens(query))
    scores = []
    for toks, length in zip(doc_tokens, lengths):
        tf = Counter(toks)
        s = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * length / avg_len))
        scores.append(s)
    return scores


def rrf_rerank(query: str, texts: list[str]) -> list[int]:
    """벡터 순위(입력 순서)와 어휘 순위를 RRF 로 합친 인덱스 순서를 돌려준다.

    반환값은 texts 에 대한 인덱스 리스트(가장 관련 높은 것부터).
    """
    n = len(texts)
    if n <= 1:
        return list(range(n))

    # 입력 순서가 곧 벡터 순위 (Vertex 가 관련도 순으로 준다)
    vec_rank = {i: i for i in range(n)}

    lex = _bm25_scores(query, texts)
    # 점수 0 은 질의어가 하나도 안 걸린 것 — 순위는 매기되 뒤로 간다
    lex_order = sorted(range(n), key=lambda i: (-lex[i], i))
    lex_rank = {idx: pos for pos, idx in enumerate(lex_order)}

    def fused(i: int) -> float:
        return 1.0 / (_RRF_K + vec_rank[i]) + 1.0 / (_RRF_K + lex_rank[i])

    return sorted(range(n), key=lambda i: (-fused(i), i))
