from __future__ import annotations

import json
import re
import time
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import dept_config, gcp_cli
from scripts.console import cloud_registry, core
from shared.post_metadata import parse_post_metadata
from shared.search_postprocess import extract_file_id
from shared.source_links import citation_view_uri

router = APIRouter()


CORPUS_CHAT_MODEL = "gemini-2.5-flash-lite"


CORPUS_CHAT_LOCATION = "global"


def _corpus_post_metadata(raw_contexts, config, common, audience):
    buckets = set((config.get("buckets") or {}).values()) - {None, ""}
    ids = set()
    for context in raw_contexts:
        uri = str(context.get("sourceUri") or "")
        if uri.startswith("gs://") and uri[5:].split("/", 1)[0] in buckets:
            file_id = extract_file_id("", uri)
            if re.fullmatch(r"[A-Za-z0-9_-]{20,200}", file_id):
                ids.add(file_id)
    if not ids:
        return {}
    try:
        db = core._firestore_client(common["GCP_PROJECT_ID"])
        collection = db.collection(core._firestore_collection_names()["docState"])
        allowed_drives = set((config.get("drive") or {}).get("driveIds") or [])
        result = {}
        for snap in db.get_all([collection.document(fid) for fid in ids], timeout=10):
            data = snap.to_dict() or {}
            if not snap.exists or data.get("driveId") not in allowed_drives:
                continue
            if audience == "student" and data.get("audience") != "STUDENT":
                continue
            try:
                post = parse_post_metadata(json.dumps({"title": data.get("sourceTitle") or "",
                                                       "source_url": data.get("websiteUrl") or ""}).encode())
            except ValueError:
                continue
            result[snap.id] = {"title": post.title, "websiteUrl": post.source_url,
                               "driveUri": data.get("sourceUri"), "fileName": data.get("name")}
        return result
    except Exception:
        return {}


def retrieve_department_corpus(
    code: str, audience: str, query: str, top_k: int = 5, *, generate: bool = False
) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    if audience not in dept_config.AUDIENCES:
        raise ValueError("교직원 또는 학생 코퍼스를 선택해 주세요.")
    text = str(query or "").strip()
    if not text or len(text) > 1000:
        raise ValueError("질문은 1~1000자로 입력해 주세요.")
    limit = max(1, min(int(top_k), 10))

    started = time.perf_counter()
    config = cloud_registry.cloud_department_public_config(normalised)
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    corpus = str((config.get("corpora") or {}).get(audience) or "")
    match = core.CORPUS_RE.fullmatch(corpus)
    if not match or match.group(1) != project or match.group(2) != region:
        raise ValueError("학과 코퍼스가 현재 프로젝트·리전과 일치하지 않습니다.")

    token = gcp_cli.access_token()
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}:retrieveContexts"
    )
    status, body, _ = gcp_cli.http_post_json(
        url,
        {
            "vertexRagStore": {"ragResources": [{"ragCorpus": corpus}]},
            "query": {"text": text, "ragRetrievalConfig": {"topK": limit}},
        },
        token,
        timeout=45,
    )
    if status != 200 or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"코퍼스 조회 실패 (HTTP {status or 'timeout'}){detail}")

    raw_contexts = (body.get("contexts") or {}).get("contexts") or []
    post_metadata = _corpus_post_metadata(raw_contexts[:limit], config, common, audience)
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(raw_contexts[:limit], start=1):
        if not isinstance(item, dict):
            continue
        chunk = item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
        context_text = str(item.get("text") or chunk.get("text") or "")[:12000]
        contexts.append(
            {
                **post_metadata.get(extract_file_id("", str(item.get("sourceUri") or "")), {}),
                "rank": index,
                "text": context_text,
                "sourceDisplayName": str(item.get("sourceDisplayName") or ""),
                "sourceUri": citation_view_uri(
                    str(item.get("sourceUri") or ""),
                    str(item.get("sourceDisplayName") or ""),
                ),
                "score": item.get("score"),
            }
        )
    answer = _corpus_answer_fields(text, contexts, project, token) if generate else {}
    return {
        "code": normalised,
        "departmentName": config["name"],
        "audience": audience,
        "corpus": corpus,
        "query": text,
        "contexts": contexts,
        "latencyMs": round((time.perf_counter() - started) * 1000),
        **answer,
    }


def _gemini_text(body: dict[str, Any]) -> str:
    blocked = str(((body.get("promptFeedback") or {}).get("blockReason")) or "")
    if blocked:
        raise RuntimeError(f"답변 생성이 차단되었습니다: {blocked}")
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    return "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    ).strip()


def _vertex_generate_url(project: str) -> str:
    """생성은 코퍼스 리전과 분리한다. Seoul에는 Gemini publisher model이 없다."""
    location = CORPUS_CHAT_LOCATION
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{CORPUS_CHAT_MODEL}:generateContent"
    )


def _generate_corpus_answer(
    query: str,
    contexts: list[dict[str, Any]],
    project: str,
    token: str,
) -> tuple[str, int]:
    blocks: list[str] = []
    used = 0
    for item in contexts:
        title = str(item.get("sourceDisplayName") or f"결과 {item.get('rank')}")
        block = f"[{item.get('rank')}] {title}\n{item.get('text') or ''}"
        if used + len(block) > 24000:
            break
        blocks.append(block)
        used += len(block)
    prompt = (
        "다음은 Vertex RAG에서 검색한 문서 조각입니다. 이 내용만 근거로 질문에 한국어로 답하세요.\n"
        "근거에 없으면 확인되지 않는다고 답하고, 지어내지 마세요.\n"
        "찾은 자료에 대해 제목 : {제목} 이렇게 표기하세요\n"
        "가능하면 [번호]로 출처를 표시하세요.\n\n"
        f"질문: {query}\n\n검색 결과:\n"
        + ("\n\n".join(blocks) if blocks else "(검색 결과 없음)")
    )
    url = _vertex_generate_url(project)
    status, body, latency = gcp_cli.http_post_json(
        url,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        },
        token,
        timeout=60,
    )
    if status != 200 or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"답변 생성 실패 (HTTP {status or 'timeout'}){detail}")
    answer = _gemini_text(body)
    if not answer:
        raise RuntimeError("모델이 빈 답변을 반환했습니다.")
    return answer, latency


def _corpus_answer_fields(
    query: str,
    contexts: list[dict[str, Any]],
    project: str,
    token: str,
) -> dict[str, Any]:
    fields = {"answer": "", "answerModel": CORPUS_CHAT_MODEL, "answerError": ""}
    if not contexts:
        return fields
    try:
        answer, _latency = _generate_corpus_answer(query, contexts, project, token)
        fields["answer"] = answer
    except RuntimeError as exc:
        fields["answerError"] = str(exc)[:400]
    return fields


@router.post("/api/v1/corpus-query")
async def corpus_query(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = retrieve_department_corpus(
            str(payload.get("code") or ""),
            str(payload.get("audience") or ""),
            str(payload.get("query") or ""),
            int(payload.get("topK") or 5),
            generate=bool(payload.get("generate")),
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_CORPUS_QUERY", "message": str(exc)}},
            status_code=422,
        )
    except (OSError, RuntimeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CORPUS_QUERY_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )
    return JSONResponse(result)
