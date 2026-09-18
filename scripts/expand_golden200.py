"""Expand the audited Golden100 with 100 weakness-focused, source-grounded cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_e2e_golden import _compact, _evidence_units, _load_states
from scripts.dept_gui import _common, _provision_access_token
from scripts.mcp_registry import RegistryAdmin
from shared.lexical_rerank import normalize_title

GENERATOR_MODEL = "gemini-2.5-flash-lite"
AUDITOR_MODEL = "gemini-2.5-flash"
QUOTAS = {
    "period_conflict": 20,
    "sequence_conflict": 15,
    "revision_conflict": 15,
    "numeric_table": 15,
    "similar_title_bundle": 15,
    "multi_condition": 10,
    "distributed_evidence": 10,
}
SKIP_FILE_IDS = {
    # The source has only one usable fact and repeatedly fails the multi-condition audit.
    "1MRmJajW44aRKf8wHJbM4olstAyJZ3tgj",
    "1rfwqOTJLS-R0s2BImODhNwy-kNMj_idX",
}
LABEL_OVERRIDES = {
    "1PKcnDKiW84pngzIivKwcIII-CPXGRzVl": {
        "question": "학칙·학사규정 개정안에 의견이 있으면 언제까지 어느 부서로 보내야 하나요?",
        "expected_answer": (
            "2026년 2월 23일 월요일 오전 9시까지 의견제출서를 학사운영팀으로 "
            "공문 또는 전자우편으로 보내야 합니다."
        ),
        "expected_evidence": [
            "2. 붙임의 개정(안)을 참고하신 후 제시하실 의견이 있는 경우, 의견을 의견제출서 서식에 작성하시어 학사운영팀으로 공문 또는 전자우편(point0@tukorea.ac.kr) 송부하여 주시기 바랍니다.",
            "※ 의견 제출 기한: 2026. 2. 23.(월) 오전 9시 까지",
        ],
        "must_include": ["2026년 2월 23일 오전 9시", "학사운영팀", "공문 또는 전자우편"],
        "must_not_include": [],
        "expected_year_version": "2026",
        "reason": "짧은 원문에 대해 자동 생성이 과도하게 길어져 원문 그대로 수동 교정",
    },
    "1EPjob1S8JUiuKI6i_swKLu0vL65cV3nU": {
        "question": (
            "2026학년도 1학기 AI교육협력센터 수업배정 명단에서 강사와 겸임교원 "
            "연번 1번은 각각 누구인가요?"
        ),
        "expected_answer": "강사 연번 1번은 강민설이고, 겸임교원 연번 1번은 강수형입니다.",
        "expected_evidence": [
            "| 1 | B2096 | AI교육협력센터 | 강민설 |  |",
            "| 1 | B2128 | AI교육협력센터 | 강수형 |  |",
        ],
        "must_include": ["강사 강민설", "겸임교원 강수형"],
        "must_not_include": [],
        "expected_year_version": "2026학년도 1학기",
        "reason": "서로 떨어진 강사·겸임교원 표의 근거를 함께 사용하도록 수동 교정",
    },
}
KIND = {
    "period_conflict": "기간·버전",
    "sequence_conflict": "차수·회차",
    "revision_conflict": "개정·규정",
    "numeric_table": "수치·표",
    "similar_title_bundle": "유사 제목·자료묶음",
    "multi_condition": "다중 조건",
    "distributed_evidence": "분산 근거",
}
WEAKNESS_REQUIREMENT = {
    "period_conflict": "Question must explicitly identify a real year, semester, half, or month; never use 00 or a placeholder date.",
    "sequence_conflict": "Question must explicitly identify N차, 제N회, 붙임 N, 첨부 N, 서식 N, or 양식 N.",
    "revision_conflict": "Question must ask a changed, added, deleted, before/after, or explicitly revised rule.",
    "numeric_table": "Question must ask for a concrete amount, count, percentage, score, duration, credit, date, or other numeric table value.",
    "similar_title_bundle": "Question must include enough topic and period context to distinguish this source from sibling files or bundles.",
    "multi_condition": "Question must require at least two material conditions or two tightly connected answer facts.",
    "distributed_evidence": "Question must require at least two answer facts supported by separate source units.",
}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answerable": {"type": "BOOLEAN"},
        "question": {"type": "STRING"},
        "expected_answer": {"type": "STRING"},
        "evidence_ids": {
            "type": "ARRAY", "items": {"type": "INTEGER"}, "minItems": 1, "maxItems": 5,
        },
        "must_include": {
            "type": "ARRAY", "items": {"type": "STRING"}, "minItems": 1, "maxItems": 10,
        },
        "must_not_include": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 5},
        "expected_year_version": {"type": "STRING", "nullable": True},
        "reason": {"type": "STRING"},
    },
    "required": [
        "answerable", "question", "expected_answer", "evidence_ids", "must_include",
        "must_not_include", "expected_year_version", "reason",
    ],
}

GENERATOR_SYSTEM = """Create one difficult but natural Korean factual QA evaluation case
from the authoritative university document. Use only the supplied source units. Target the
requested weakness type. The question must be directly answerable, must not merely ask for
the document title, and must preserve every explicit period, sequence, negation, comparison,
and condition needed to identify the answer. Write the kind of short question a real staff
member would ask. Never state the answer, enumerate all answer candidates, or add irrelevant
premises inside the question. Ask for one fact or one tightly connected pair of facts. Do not
ask an unanswerable hypothetical or ask what happens when the source has no such rule. Prefer
facts likely to be confused with a nearby period, revision, amount, form, attachment, or
sibling document. Select 1-5 verbatim evidence unit IDs. The concise expected answer and every
must_include fact must be fully supported.
must_not_include should contain only concrete conflicting values visible in the source; it may
be empty. Do not invent a distractor. Return answerable=false if no strong case is possible."""

AUDITOR_SYSTEM = """Independently audit a Korean factual QA evaluation case against the full
authoritative source. Correct the question and labels when needed. It must directly test the
requested weakness type, be unambiguous, and be answerable only from this source. Do not ask
for a title unless the target is explicitly similar_title_bundle. Preserve necessary year,
semester, round, revision, comparison, and threshold conditions. Select 1-5 verbatim source
unit IDs and never rewrite evidence. Reject questions that reveal their own answer, list all
candidates before asking for one, contain irrelevant setup, or ask about a case the source
does not define. Prefer a concise real-user wording. For distributed_evidence require at least
two evidence units supporting distinct necessary answer facts. For revision_conflict prefer a
before/after change when both are present. Reject unsupported facts. Return answerable=false
if the source cannot support a useful case."""

_PERIOD = re.compile(
    r"(?:19|20)\d{2}|[12]\s*학기|상반기|하반기|(?:1[0-2]|0?[1-9])\s*월"
)
_SEQUENCE = re.compile(r"(?:\d{1,3}\s*차|제\s*\d{1,3}\s*회|(?:붙임|첨부|별첨|서식|양식)\s*[-_.]?\s*\d+)")
_REVISION = re.compile(r"개정|신구대비|일부개정|전부개정|시행세칙|규정|지침|변경")
_NUMERIC = re.compile(r"\d[\d,.]*\s*(?:원|천원|만원|억원|%|퍼센트|명|건|점|학점|시간|주|일)")
_CONDITION = re.compile(r"(?:이상|이하|초과|미만|경우|까지|동시에|및|또는|제외|않)")


def _model_url(project: str, model: str) -> str:
    return (
        f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/"
        f"publishers/google/models/{model}:generateContent"
    )


def _generate(
    session: AuthorizedSession,
    project: str,
    model: str,
    system: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    response = session.post(
        _model_url(project, model),
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{
                "role": "user",
                "parts": [{"text": json.dumps(request, ensure_ascii=False)}],
            }],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 3000,
                "thinkingConfig": {"thinkingBudget": 0},
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
            },
        },
        timeout=90,
    )
    response.raise_for_status()
    parts = response.json()["candidates"][0]["content"]["parts"]
    return json.loads("".join(part.get("text", "") for part in parts))


def _call_with_retry(
    session: AuthorizedSession,
    project: str,
    model: str,
    system: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    for attempt in range(5):
        try:
            return _generate(session, project, model, system, request)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(min(40, 5 * (attempt + 1)))
    raise AssertionError("unreachable")


def _list_objects(session: AuthorizedSession, bucket: str) -> set[str]:
    names: set[str] = set()
    token = ""
    while True:
        params = {"maxResults": 1000, "fields": "items/name,nextPageToken"}
        if token:
            params["pageToken"] = token
        response = session.get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o", params=params, timeout=60
        )
        if response.status_code == 404:
            return set()
        response.raise_for_status()
        body = response.json()
        names.update(item["name"] for item in body.get("items", []))
        token = body.get("nextPageToken", "")
        if not token:
            return names


def _fetch_source(session: AuthorizedSession, bucket: str, file_id: str) -> str:
    response = session.get(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(f'{file_id}.md', safe='')}",
        params={"alt": "media"}, timeout=60,
    )
    response.raise_for_status()
    return response.content.decode("utf-8")


def _usable_text(value: str) -> bool:
    compact = _compact(value)
    return (
        500 <= len(compact) <= 120_000
        and "동일 경로의 관련" not in compact
        and compact.count("�") <= 2
    )


def _features(state: dict[str, Any], text: str, sibling_count: int) -> set[str]:
    metadata = f"{state.get('name') or ''} {state.get('bundle') or ''}"
    sample = f"{metadata} {text[:8000]}"
    found = set()
    if len(_PERIOD.findall(sample)) >= 2:
        found.add("period_conflict")
    if _SEQUENCE.search(sample):
        found.add("sequence_conflict")
    if _REVISION.search(sample):
        found.add("revision_conflict")
    if len(_NUMERIC.findall(sample)) >= 2:
        found.add("numeric_table")
    if sibling_count >= 3:
        found.add("similar_title_bundle")
    if len(_CONDITION.findall(sample)) >= 5:
        found.add("multi_condition")
    if len(text) >= 5000 and len(_evidence_units(text[:12000])) >= 20:
        found.add("distributed_evidence")
    return found


def _assign(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assigned = []
    used: set[str] = set()
    for weakness, quota in QUOTAS.items():
        choices = [row for row in candidates if weakness in row["features"] and row["fileId"] not in used]
        choices.sort(
            key=lambda row: (
                -len(row["features"]),
                hashlib.sha256(row["fileId"].encode()).hexdigest(),
            )
        )
        if len(choices) < quota:
            raise RuntimeError(f"Only {len(choices)} candidates for {weakness}, need {quota}")
        for row in choices[:quota]:
            selected = dict(row)
            selected["weakness_type"] = weakness
            assigned.append(selected)
            used.add(row["fileId"])
    return assigned


def _materialize(label: dict[str, Any], units: list[str]) -> dict[str, Any] | None:
    ids = label.pop("evidence_ids", [])
    evidence = [units[i] for i in ids if isinstance(i, int) and 0 <= i < len(units)]
    if not (
        label.get("answerable")
        and label.get("question")
        and label.get("expected_answer")
        and label.get("must_include")
        and evidence
    ):
        return None
    label["expected_evidence"] = evidence
    return label


def _label_is_usable(label: dict[str, Any], weakness: str) -> bool:
    question = _compact(label.get("question") or label.get("query") or "")
    answer = _compact(label["expected_answer"])
    evidence = label["expected_evidence"]
    must_include = label.get("must_include", [])
    if len(question) > 150 or len(question) < 12:
        return False
    if re.search(
        r"(?<!\d)(?:00|○○|OO)\s*(?:년|월|일|학기|명|원|점)", f"{question} {answer}"
    ):
        return False
    if answer and len(answer) >= 8 and answer in question:
        return False
    if weakness == "period_conflict" and not _PERIOD.search(question):
        return False
    if weakness == "sequence_conflict" and not _SEQUENCE.search(question):
        return False
    if weakness == "revision_conflict" and not re.search(
        r"개정|변경|바뀌|신설|삭제|종전|기존|신구", question
    ):
        return False
    if weakness == "numeric_table" and not re.search(r"\d", answer):
        return False
    if weakness in {"multi_condition", "distributed_evidence"} and len(must_include) < 2:
        return False
    return not (weakness == "distributed_evidence" and len(evidence) < 2)


def _create_audited_label(
    session: AuthorizedSession,
    project: str,
    request: dict[str, Any],
    units: list[str],
    existing_questions: set[str],
) -> tuple[dict[str, Any], str]:
    last_question = ""
    for quality_attempt in range(4):
        attempt_request = dict(request)
        if last_question:
            attempt_request["avoid_question"] = last_question
        if quality_attempt:
            attempt_request["instruction"] = (
                "Try a different supported fact and make the question shorter and more natural."
            )
        generated = _materialize(_call_with_retry(
            session, project, GENERATOR_MODEL, GENERATOR_SYSTEM, attempt_request
        ), units)
        if generated is None:
            continue
        audit_request = {**attempt_request, "current_label": generated}
        try:
            audit_label = _call_with_retry(
                session, project, AUDITOR_MODEL, AUDITOR_SYSTEM, audit_request
            )
            audit_model = AUDITOR_MODEL
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            audit_label = _call_with_retry(
                session, project, GENERATOR_MODEL, AUDITOR_SYSTEM, audit_request
            )
            audit_model = GENERATOR_MODEL
        audited = _materialize(audit_label, units)
        if audited is None:
            continue
        last_question = audited["question"]
        if (
            _label_is_usable(audited, str(request["weakness_type"]))
            and _compact(audited["question"]) not in existing_questions
        ):
            return audited, audit_model
    raise RuntimeError(f"Could not create a usable label for {request['source_name']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden100", type=Path, default=ROOT / "tests/golden100.json")
    parser.add_argument("--source100", type=Path, default=ROOT / "tests/_bench_out/golden100_sources.json")
    parser.add_argument("--out", type=Path, default=ROOT / "tests/golden200.json")
    parser.add_argument("--source-out", type=Path, default=ROOT / "tests/_bench_out/golden200_sources.json")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "tests/_bench_out/golden200_new.json")
    args = parser.parse_args()

    base = json.loads(args.golden100.read_text(encoding="utf-8"))
    source_seed = args.source_out if args.source_out.exists() else args.source100
    sources = json.loads(source_seed.read_text(encoding="utf-8"))
    existing_files = {str(row["expected_file"]) for row in base}
    existing_bundles = {str(row.get("expected_bundle") or "") for row in base}
    existing_questions = {_compact(row["query"]) for row in base}

    common = _common()
    token = _provision_access_token()
    _, _, configs = RegistryAdmin(
        common["GCP_PROJECT_ID"], common["GCP_REGION"], common["FIRESTORE_DATABASE"], token
    ).read()
    buckets = sorted({
        str(config.get("buckets", {}).get("source"))
        for config in configs.values()
        if config.get("buckets", {}).get("source")
    })
    session = AuthorizedSession(Credentials(token))
    available: dict[str, str] = {}
    for bucket in buckets:
        for name in _list_objects(session, bucket):
            if name.endswith(".md"):
                available.setdefault(name[:-3], bucket)

    states = _load_states(
        session, common["GCP_PROJECT_ID"], common["FIRESTORE_DATABASE"],
        common["DOC_STATE_COLLECTION"],
    )
    bundle_counts = Counter(str(row.get("bundle") or "") for row in states if row.get("status") == "INDEXED")
    pool = []
    used_bundles = {normalize_title(value) for value in existing_bundles}
    used_names = {normalize_title(str(row.get("name") or "")) for row in base}
    for state in sorted(
        states,
        key=lambda row: hashlib.sha256(str(row.get("fileId") or "").encode()).hexdigest(),
    ):
        file_id = str(state.get("fileId") or "")
        bundle = str(state.get("bundle") or "")
        name = str(state.get("name") or "")
        if (
            state.get("status") != "INDEXED"
            or state.get("audience") not in (None, "STAFF")
            or not file_id or file_id in existing_files or file_id in SKIP_FILE_IDS
            or file_id not in available
            or not bundle or normalize_title(bundle) in used_bundles
            or not name or "�" in name
            or normalize_title(name) in used_names
        ):
            continue
        text = _fetch_source(session, available[file_id], file_id)
        if not _usable_text(text):
            continue
        features = _features(state, text, bundle_counts[bundle])
        if not features:
            continue
        pool.append({**state, "text": text, "features": features, "bucket": available[file_id]})
        used_bundles.add(normalize_title(bundle))
        used_names.add(normalize_title(name))
        if len(pool) >= 700:
            break
    selected = _assign(pool)
    selection_path = ROOT / "tests/_bench_out/golden200_selection_v2.json"
    selection_path.write_text(
        json.dumps([
            {
                "offset": offset,
                "fileId": row["fileId"],
                "name": row.get("name"),
                "bundle": row.get("bundle"),
                "weakness_type": row["weakness_type"],
            }
            for offset, row in enumerate(selected, start=101)
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"candidate_pool={len(pool)} selected={len(selected)}", flush=True)

    checkpoint = []
    if args.checkpoint.exists():
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
    valid_checkpoint = [
        row for row in checkpoint
        if _label_is_usable(row, str(row.get("weakness_type") or ""))
    ]
    completed = {str(row["expected_file"]): row for row in valid_checkpoint}
    existing_questions.update(_compact(row["query"]) for row in valid_checkpoint)
    output_new = []
    for offset, item in enumerate(selected, start=101):
        if item["fileId"] in completed:
            output_new.append(completed[item["fileId"]])
            sources[str(offset)] = {
                "source": {
                    key: item.get(key)
                    for key in (
                        "fileId", "name", "path", "bundle", "sourceUri", "modifiedTime"
                    )
                },
                "chunks": [{
                    "text": item["text"], "score": 0, "scoreType": "gold_source",
                }],
            }
            continue
        source_text = item["text"][:12000]
        units = _evidence_units(source_text)
        request = {
            "weakness_type": item["weakness_type"],
            "weakness_requirement": WEAKNESS_REQUIREMENT[item["weakness_type"]],
            "source_name": item.get("name"),
            "source_bundle": item.get("bundle"),
            "source_units": [{"id": i, "text": value} for i, value in enumerate(units)],
        }
        if item["fileId"] in LABEL_OVERRIDES:
            audited = dict(LABEL_OVERRIDES[item["fileId"]])
            audit_model = "manual-source-audit"
        else:
            audited, audit_model = _create_audited_label(
                session, common["GCP_PROJECT_ID"], request, units, existing_questions
            )
        question_key = _compact(audited["question"])
        existing_questions.add(question_key)
        row = {
            "n": offset,
            "query": audited["question"],
            "type": KIND[item["weakness_type"]],
            "weakness_type": item["weakness_type"],
            "expected_file": item["fileId"],
            "name": item.get("name"),
            "expected_bundle": item.get("bundle"),
            "expected_evidence": audited["expected_evidence"],
            "expected_answer": audited["expected_answer"],
            "must_include": audited["must_include"],
            "must_not_include": audited["must_not_include"],
            "expected_year_version": audited["expected_year_version"],
            "label_audit": {
                "generator": GENERATOR_MODEL,
                "auditor": audit_model,
                "reason": audited["reason"],
            },
        }
        output_new.append(row)
        sources[str(offset)] = {
            "source": {
                key: item.get(key)
                for key in ("fileId", "name", "path", "bundle", "sourceUri", "modifiedTime")
            },
            "chunks": [{"text": item["text"], "score": 0, "scoreType": "gold_source"}],
        }
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        args.checkpoint.write_text(json.dumps(output_new, ensure_ascii=False, indent=2), encoding="utf-8")
        args.source_out.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"case {offset}/200 {item['weakness_type']} evidence={len(row['expected_evidence'])}", flush=True)

    combined = base + output_new
    args.out.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    args.source_out.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(combined), "weaknesses": Counter(
        row["weakness_type"] for row in output_new
    )}, ensure_ascii=False, default=dict), flush=True)


if __name__ == "__main__":
    main()
