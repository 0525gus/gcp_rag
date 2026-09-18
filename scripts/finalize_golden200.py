"""Merge Golden200 corrections, add verified weakness tags, and validate all sources."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.expand_golden200 import _PERIOD, _SEQUENCE, KIND

FINAL_CORRECTIONS = {
    130: {
        "query": "원격수업 운영 규정 개정안에서 외국인 유학생의 원격수업 취득 학점 한도는 어떻게 신설되나요?",
        "expected_answer": (
            "국내 체류 외국인 유학생은 학년별 취득 학점의 30% 이내로 제한하며, "
            "계약학과 재학생은 별도 규정을 따릅니다."
        ),
        "expected_evidence": [
            "| 2p. <신설> | 2p. 제8조의2(외국인 유학생의 원격수업 학점취득) 국내 체류중인 외국인 유학생의 원격수업으로 취득할 수 있는 학점은 학년별 취득 학점의 30% 이내로 한다. 단, 계약학과에 재학 중인 외국인 유학생에 대한 사항은 별도의 규정을 따른다.   |",
        ],
        "must_include": ["학년별 취득 학점의 30% 이내", "계약학과는 별도 규정"],
        "must_not_include": ["제한 없음"],
        "expected_year_version": "2026년 개정안",
    },
    142: {
        "query": "산학협력단 규정입안 요청서에서 규정 입안사유를 작성하는 항목은 몇 번이며 무엇을 적어야 하나요?",
        "expected_answer": (
            "3번 '입안요청 사유' 항목에 관련 법령 개정이나 제도개선 등 규정 입안사유를 "
            "상세히 작성해야 합니다."
        ),
        "expected_evidence": [
            "| 규정입안 요청서  1. 입안요청 부서명 :  2. 규정 명칭 :   3. 입안요청 사유 (관련법령 개정, 제도개선 등 규정 입안사유를 상세하게 작성)   ◦     -   ◦     -   ◦     -  4. 관련부서(학과) 협의사항 (유관부서 협의, 의견수렴 경과 등을 상세하게 작성)   ◦     -  5. 주요 내용   ◦     -    ◦      -   ◦     -  5. 요청한 규정입안 승인일자(부서에서 필",
        ],
        "must_include": ["3번", "입안요청 사유", "관련법령 개정 또는 제도개선", "상세 작성"],
        "must_not_include": [],
        "expected_year_version": None,
    },
}


def _tags(row: dict[str, Any]) -> list[str]:
    question = row["query"]
    answer = row["expected_answer"]
    tags = []
    if _PERIOD.search(question):
        tags.append("period_conflict")
    if _SEQUENCE.search(question):
        tags.append("sequence_conflict")
    if re.search(r"개정|변경|바뀌|신설|삭제|종전|기존|신구", question):
        tags.append("revision_conflict")
    if re.search(r"\d", answer) and re.search(
        r"얼마|몇|언제|기간|기한|시점|비율|금액|학점|시간|인원|건수|횟수|날짜", question
    ):
        tags.append("numeric_table")
    if row.get("weakness_type") == "similar_title_bundle":
        tags.append("similar_title_bundle")
    if len(row.get("must_include", [])) >= 2:
        tags.append("multi_condition")
    if len(row.get("expected_evidence", [])) >= 2 and len(row.get("must_include", [])) >= 2:
        tags.append("distributed_evidence")
    return list(dict.fromkeys(tags or [row["weakness_type"]]))


def main() -> None:
    draft = json.loads((ROOT / "tests/golden200_v3.json").read_text(encoding="utf-8"))
    corrections = {
        int(row["n"]): row
        for row in json.loads(
            (ROOT / "tests/_bench_out/golden200_new_v3.json").read_text(encoding="utf-8")
        )
    }
    rows = [corrections.get(int(row["n"]), row) if int(row["n"]) > 100 else row for row in draft]
    for row in rows:
        if int(row["n"]) in FINAL_CORRECTIONS:
            row.update(FINAL_CORRECTIONS[int(row["n"])])
            row["label_audit"] = {
                "auditor": "manual-source-audit",
                "reason": "placeholder 값을 묻지 않도록 확정된 원문 사실로 교정",
            }
    sources = json.loads(
        (ROOT / "tests/_bench_out/golden200_sources_v3.json").read_text(encoding="utf-8")
    )
    problems = []
    for row in rows:
        n = int(row["n"])
        required = (
            "query", "expected_file", "expected_bundle", "expected_answer",
            "expected_evidence", "must_include", "must_not_include",
        )
        if any(key not in row or row[key] in (None, "") for key in required):
            problems.append(f"missing field: {n}")
            continue
        if not 1 <= len(row["expected_evidence"]) <= 5 or not row["must_include"]:
            problems.append(f"invalid label cardinality: {n}")
        source = sources.get(str(n))
        if not source or source.get("source", {}).get("fileId") != row["expected_file"]:
            problems.append(f"source mismatch: {n}")
            continue
        source_text = "\n".join(chunk.get("text", "") for chunk in source.get("chunks", []))
        if any(value not in source_text for value in row["expected_evidence"]):
            problems.append(f"evidence absent: {n}")
        if re.search(
            r"(?<!\d)(?:00|○○|OO)\s*(?:년|월|일|학기|명|원|점)",
            f"{row['query']} {row['expected_answer']}",
        ):
            problems.append(f"placeholder label: {n}")
        if n > 100:
            row["generation_target"] = row.pop("weakness_type")
            row["weakness_tags"] = _tags({**row, "weakness_type": row["generation_target"]})
            row["type"] = " · ".join(KIND[tag] for tag in row["weakness_tags"])
    if problems:
        raise RuntimeError("; ".join(problems[:20]))
    if len(rows) != 200 or len({row["n"] for row in rows}) != 200:
        raise RuntimeError("Golden200 must contain 200 unique sequence numbers")
    if len({row["query"] for row in rows}) != 200:
        raise RuntimeError("Golden200 contains duplicate questions")
    if len({str(row["expected_file"]) for row in rows}) != 200:
        raise RuntimeError("Golden200 contains duplicate files")

    (ROOT / "tests/golden200.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ROOT / "tests/_bench_out/golden200_sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    new_rows = rows[100:]
    print(json.dumps({
        "rows": len(rows),
        "unique_files": len({row["expected_file"] for row in rows}),
        "generation_targets": Counter(row["generation_target"] for row in new_rows),
        "weakness_tags": Counter(tag for row in new_rows for tag in row["weakness_tags"]),
    }, ensure_ascii=False, default=dict), flush=True)


if __name__ == "__main__":
    main()
