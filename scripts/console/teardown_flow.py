from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import dept_config, gcp_cli
from scripts.console import cloud_registry, core
from scripts.run_store import RunStore

router = APIRouter()


_TEARDOWNS = RunStore(60 * 60, "targets")


def _teardown_target(
    key: str,
    kind: str,
    label: str,
    name: str,
    shared: list[str],
    *,
    note: str = "",
    meta: dict[str, Any] | None = None,
    selected: bool = True,
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "label": label,
        "name": name,
        "note": note,
        "meta": meta or {},
        "sharedWith": shared,
        "selectable": not shared,
        "selected": bool(selected) and not shared,
        "skipped": bool(shared),
        "status": "PENDING",
        "detail": (
            f"다른 학과가 사용 중이라 남깁니다: {', '.join(shared)}" if shared else "삭제 대기"
        ),
    }


def department_teardown_plan(code: str) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    configs = cloud_registry._cloud_department_configs()
    if normalised not in configs:
        raise FileNotFoundError(normalised)
    config = configs[normalised]
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    corpora = config.get("corpora") or {}
    buckets = config.get("buckets") or {}
    corpus_usage: dict[str, list[str]] = {}
    bucket_usage: dict[str, list[str]] = {}
    drive_usage: dict[str, list[str]] = {}
    for department_code, deployed in configs.items():
        for value in (deployed.get("corpora") or {}).values():
            corpus_usage.setdefault(str(value).strip(), []).append(department_code)
        for value in (deployed.get("buckets") or {}).values():
            bucket_usage.setdefault(str(value).removeprefix("gs://").strip(), []).append(department_code)
        for value in core._normalise_ids((deployed.get("drive") or {}).get("driveIds")):
            drive_usage.setdefault(value, []).append(department_code)

    targets: list[dict[str, Any]] = []
    audiences = ["staff", "student"] if str(corpora.get("student") or "").strip() else ["staff"]
    disabled_audiences = set(config.get("mcpDisabledAudiences") or [])
    for audience in audiences:
        already_disabled = audience in disabled_audiences
        targets.append(
            _teardown_target(
                f"mcp-{audience}",
                "mcpRoute",
                f"MCP 접근 중지 ({audience})",
                f"{normalised}/{audience}",
                [],
                note=(
                    "이미 접근이 중지되어 있어 Cloud 등록부를 다시 쓰지 않습니다."
                    if already_disabled
                    else "이 학과·대상의 인증 키 접근을 중지합니다. 공통 MCP 서비스는 유지됩니다."
                ),
                meta={"audience": audience, "alreadyDisabled": already_disabled},
            )
        )
    targets.append(
        _teardown_target(
            "sync-env",
            "syncEnv",
            "이 학과 동기화 중지",
            f"{core.SYNC_SERVICE} · DEPARTMENTS_JSON",
            [],
            note="학과를 동기화 대상에서 제외하고 중지 상태를 Cloud 등록부에 저장합니다.",
        )
    )
    for audience in ("staff", "student"):
        name = str(corpora.get(audience) or "").strip()
        if not name:
            continue
        shared = [item for item in corpus_usage.get(name, []) if item != normalised]
        targets.append(
            _teardown_target(
                f"corpus-{audience}",
                "corpus",
                f"RAG 코퍼스 ({audience})",
                name,
                shared,
                note="색인된 파일까지 force 로 함께 지웁니다. 다시 만들면 전체 재색인이 필요합니다.",
            )
        )
    for slot, label in (("hwpOriginal", "원본 HWP 버킷"), ("source", "Source 버킷")):
        name = str(buckets.get(slot) or "").removeprefix("gs://").strip()
        if not name:
            continue
        shared = [item for item in bucket_usage.get(name, []) if item != normalised]
        targets.append(
            _teardown_target(
                f"bucket-{slot}",
                "bucket",
                label,
                name,
                shared,
                note="객체를 모두 지운 뒤 버킷을 삭제합니다. Drive 원본은 남습니다.",
            )
        )

    drive_ids = core._normalise_ids((config.get("drive") or {}).get("driveIds"))
    if drive_ids:
        shared_drives = sorted(
            {
                item
                for drive_id in drive_ids
                for item in drive_usage.get(drive_id, [])
                if item != normalised
            }
        )
        targets.append(
            _teardown_target(
                "firestore-state",
                "firestoreState",
                "동기화 이력 (Firestore)",
                f"{core._firestore_database_name()} · 공유드라이브 {len(drive_ids)}개",
                shared_drives,
                note="doc_state·rag_files·sync_tokens·DLQ·분할 큐에서 이 학과 드라이브 문서를 지웁니다.",
                meta={"driveIds": drive_ids},
            )
        )

    metadata_bucket = str(common.get("RAG_METADATA_BUCKET") or "").removeprefix("gs://").strip()
    if metadata_bucket:
        for audience in ("staff", "student"):
            corpus = str(corpora.get(audience) or "").strip()
            if not corpus:
                continue
            corpus_id = corpus.rstrip("/").rsplit("/", 1)[-1]
            shared = [item for item in corpus_usage.get(corpus, []) if item != normalised]
            targets.append(
                _teardown_target(
                    f"metadata-{audience}",
                    "metadataObjects",
                    f"import 결과 객체 ({audience})",
                    f"gs://{metadata_bucket}/import-results/{corpus_id}/",
                    shared,
                    note="공용 메타데이터 버킷은 남기고 이 코퍼스 prefix 만 비웁니다.",
                )
            )

    targets.append(_teardown_target(
        "registry-config", "mcpRegistryConfig", "학과 Cloud 등록 설정 삭제", normalised, [],
        note="접근·동기화를 중지하고 선택 가능한 관련 리소스를 모두 정리한 후에만 삭제합니다.",
    ))

    return {
        "kind": "department",
        "code": normalised,
        "name": str(config.get("name") or normalised),
        "projectId": project,
        "region": region,
        "confirmWord": normalised,
        "lastDepartment": len(configs) == 1,
        "unifiedMcp": True,
        "targets": targets,
    }


def common_runtime_teardown_plan() -> dict[str, Any]:
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    if not project:
        raise ValueError("공통 환경의 GCP 프로젝트가 설정되어 있지 않습니다.")
    remaining = sorted(cloud_registry._cloud_department_configs())
    # Scheduler → Workflow → Cloud Run 순. 거꾸로 지우면 살아 있는 잡이 없어진
    # 워크플로를 호출해 실패 이력만 쌓는다.
    targets = [
        _teardown_target(
            "scheduler",
            "scheduler",
            "Cloud Scheduler 작업",
            "rag-daily-sync",
            [],
            note="정기 실행만 멈춥니다. 수동 동기화는 계속 됩니다.",
        ),
        _teardown_target(
            "workflow",
            "workflow",
            "Workflow",
            "rag-daily-sync",
            [],
            note="동기화 오케스트레이션이 사라집니다. 수동 실행도 멈춥니다.",
        ),
        _teardown_target(
            "sync",
            "cloudRun",
            "Cloud Run",
            "rag-sync",
            [],
            note="Drive 수집·색인 런타임입니다. 학과 라우팅 맵(DEPARTMENTS_JSON)도 함께 사라집니다.",
        ),
        _teardown_target(
            "parser",
            "cloudRun",
            "Cloud Run",
            "rag-parser",
            [],
            note="HWP·HWPX 파싱 런타임입니다. rag-sync 가 이 서비스를 호출합니다.",
        ),
    ]
    return {
        "kind": "commonRuntime",
        "code": "",
        "name": "공통 런타임",
        "projectId": project,
        "region": region,
        "confirmWord": project,
        "remainingDepartments": remaining,
        "targets": targets,
    }


def _delete_cloud_run_service(name: str, project: str, region: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "run",
            "services",
            "delete",
            name,
            f"--region={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=300,
    )
    if ok:
        return "삭제 완료"
    if core._missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Cloud Run 서비스 삭제에 실패했습니다.")[-400:])


def _delete_workflow_resource(name: str, project: str, region: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "workflows",
            "delete",
            name,
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if ok:
        return "삭제 완료"
    if core._missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Workflow 삭제에 실패했습니다.")[-400:])


def _delete_scheduler_job(name: str, project: str, region: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "scheduler",
            "jobs",
            "delete",
            name,
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if ok:
        return "삭제 완료"
    if core._missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Scheduler 작업 삭제에 실패했습니다.")[-400:])


def _delete_rag_corpus(name: str, region: str, token: str) -> str:
    """코퍼스는 force=true 로 지운다 — 안 그러면 파일이 남았다며 거부한다."""
    url = f"https://{region}-aiplatform.googleapis.com/v1/{name}?force=true"
    status, body, _ = gcp_cli.http_delete_json(url, token, timeout=30)
    if status == 404:
        return "이미 없습니다"
    if status not in {200, 202}:
        message = (
            str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        )
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"코퍼스 삭제 실패 (HTTP {status or 'timeout'}){detail}")
    operation = str(body.get("name") or "") if isinstance(body, dict) else ""
    if not operation or (isinstance(body, dict) and body.get("done")):
        return "삭제 완료"
    operation_url = f"https://{region}-aiplatform.googleapis.com/v1/{operation}"
    deadline = time.time() + 300
    while time.time() < deadline:
        poll_status, payload, _ = gcp_cli.http_json(operation_url, token, timeout=20)
        if poll_status == 200 and isinstance(payload, dict) and payload.get("done"):
            if payload.get("error"):
                raise RuntimeError(
                    str((payload.get("error") or {}).get("message") or "")[:400]
                    or "코퍼스 삭제 작업이 실패했습니다."
                )
            return "삭제 완료"
        time.sleep(3)
    return "삭제 진행 중 (백그라운드)"


def _delete_bucket_resource(name: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [gcloud, "storage", "rm", "--recursive", f"gs://{name}/**", "--quiet"],
        timeout=1800,
    )
    lowered = str(output or "").lower()
    if not ok and not core._missing_resource_output(output) and "matched no objects" not in lowered:
        raise RuntimeError((output or "버킷 객체 삭제에 실패했습니다.")[-400:])
    ok, output = gcp_cli.run_command(
        [gcloud, "storage", "buckets", "delete", f"gs://{name}", "--quiet"], timeout=300
    )
    if ok:
        return "삭제 완료"
    if core._missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "버킷 삭제에 실패했습니다.")[-400:])


_FIRESTORE_DELETE_BATCH = 200


def _delete_department_firestore_state(
    drive_ids: list[str],
    project: str,
    *,
    progress: Callable[[str], None] | None = None,
) -> str:
    if not drive_ids:
        return "대상 없음"
    from google.cloud.firestore_v1.base_query import FieldFilter

    client = core._firestore_client(project)
    names = core._firestore_collection_names()
    docs = client.collection(names["docState"])
    deleted_docs = 0
    deleted_mappings = 0
    deleted_queue_refs = 0
    deleted_tokens = 0
    batch = client.batch()
    pending_writes = 0

    def detail(*, running: bool = False) -> str:
        prefix = "Firestore 배치 삭제 중 · " if running else ""
        return (
            f"{prefix}문서 {deleted_docs}건 · 매핑 {deleted_mappings}건 · "
            f"큐 참조 {deleted_queue_refs}건 · 페이지 토큰 {deleted_tokens}건 삭제"
        )

    def flush() -> None:
        nonlocal batch, pending_writes
        if not pending_writes:
            return
        batch.commit()
        pending_writes = 0
        if progress is not None:
            progress(detail(running=True))
        batch = client.batch()

    def queue_delete(reference: Any) -> None:
        nonlocal pending_writes
        batch.delete(reference)
        pending_writes += 1
        if pending_writes >= _FIRESTORE_DELETE_BATCH:
            flush()

    for start in range(0, len(drive_ids), 30):
        chunk = drive_ids[start : start + 30]
        for snapshot in docs.where(
            filter=FieldFilter("driveId", "in", chunk)
        ).stream():
            file_id = snapshot.id
            for mapping in snapshot.reference.collection("rag_files").list_documents():
                deleted_mappings += 1
                queue_delete(mapping)
            for collection in (names["dlq"], names["splitQueue"]):
                reference = client.collection(collection).document(file_id)
                # Firestore delete는 문서가 없어도 성공한다. 존재 확인용 get을 없애
                # 문서마다 두 번씩 발생하던 불필요한 네트워크 왕복을 피한다.
                deleted_queue_refs += 1
                queue_delete(reference)
            deleted_docs += 1
            queue_delete(snapshot.reference)

    tokens = client.collection(names["syncTokens"])
    for drive_id in drive_ids:
        reference = tokens.document(drive_id)
        deleted_tokens += 1
        queue_delete(reference)

    flush()

    return detail()


def _delete_gcs_prefix(uri: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    target = uri.rstrip("/") + "/**"
    ok, output = gcp_cli.run_command(
        [gcloud, "storage", "rm", "--recursive", target, "--quiet"], timeout=900
    )
    if ok:
        return "삭제 완료"
    lowered = str(output or "").lower()
    if core._missing_resource_output(output) or "matched no objects" in lowered:
        return "지울 객체가 없습니다"
    raise RuntimeError((output or "객체 삭제에 실패했습니다.")[-400:])


def _require_sync_removed() -> None:
    common = core._common()
    ok, result = gcp_cli.gcloud_json([
        "run", "services", "describe", core.SYNC_SERVICE,
        f"--project={common.get('GCP_PROJECT_ID', '')}",
        f"--region={common['GCP_REGION']}",
    ], timeout=30)
    if not ok and core._missing_resource_output(str(result)):
        return
    raise ValueError("마지막 학과 삭제 전 운영 환경에서 Sync 런타임을 삭제해 주세요. 빈 학과 맵은 현재 런타임에서 지원하지 않습니다.")


def _refresh_cloud_teardown_routing(remove_code: str) -> str:
    configs = cloud_registry._cloud_department_configs()
    remaining = {code: config for code, config in configs.items()
                 if code != remove_code and not config.get("syncDisabled")}
    if not remaining:
        _require_sync_removed()
    if remove_code and remove_code in configs:
        configs[remove_code]["syncDisabled"] = True
        cloud_registry._mcp_registry_mutate("update_department", remove_code, configs[remove_code])
    configs = {code: config for code, config in configs.items() if not config.get("syncDisabled")}
    if remove_code:
        configs.pop(remove_code, None)
    if not configs:
        _require_sync_removed()
        return "Sync 런타임이 없어 라우팅 갱신이 필요하지 않습니다"
    try:
        expected = dept_config.departments_json_from_configs(configs)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    return f"남은 학과: {cloud_registry.update_sync_department_map(expected=expected) or '없음'}"


def _execute_teardown_run(run_id: str) -> None:
    with _TEARDOWNS.lock:
        run = copy.deepcopy(_TEARDOWNS.runs[run_id])
    project = run["projectId"]
    region = run["region"]
    token = ""
    failed = 0
    for target in run["targets"]:
        key = target["key"]
        kind = target["kind"]
        name = target["name"]
        if target.get("skipped"):
            _TEARDOWNS.update_item(run_id, key, status="SKIPPED")
            continue
        if run["kind"] == "department" and failed and (
            key in {"sync-env", "mcp-staff"} or (run.get("unifiedMcp") and kind != "mcpRoute")
        ):
            _TEARDOWNS.update_item(
                run_id, key, status="SKIPPED", detail="앞선 작업이 실패하여 설정과 라우팅을 유지합니다"
            )
            continue
        _TEARDOWNS.update_item(
            run_id, key, status="RUNNING", detail="갱신 중" if kind == "syncEnv" else "삭제 중"
        )
        try:
            if kind == "cloudRun":
                if run["kind"] == "department" and name == "rag-mcp":
                    raise ValueError("공통 MCP는 학과 삭제 대상이 될 수 없습니다.")
                detail = _delete_cloud_run_service(name, project, region)
            elif kind == "mcpRoute":
                if target["meta"].get("alreadyDisabled"):
                    detail = "이미 접근 중지됨 · Cloud 등록부 변경 없음 · 공통 MCP 유지"
                else:
                    cloud_registry._mcp_registry_mutate(
                        "disable_audience", run["code"], target["meta"]["audience"]
                    )
                    detail = "해당 학과·대상 접근 중지 완료 · 공통 MCP 유지"
            elif kind == "mcpRegistryConfig":
                cloud_registry._mcp_registry_mutate("remove_department", run["code"])
                detail = "학과 Cloud 등록 설정 삭제 완료 · 공통 MCP 유지"
            elif kind == "workflow":
                detail = _delete_workflow_resource(name, project, region)
            elif kind == "scheduler":
                detail = _delete_scheduler_job(name, project, region)
            elif kind == "corpus":
                token = token or gcp_cli.access_token()
                detail = _delete_rag_corpus(name, region, token)
            elif kind == "bucket":
                detail = _delete_bucket_resource(name)
            elif kind == "firestoreState":
                detail = _delete_department_firestore_state(
                    list((target.get("meta") or {}).get("driveIds") or []),
                    project,
                    progress=lambda current, target_key=key: _TEARDOWNS.update_item(
                        run_id, target_key, status="RUNNING", detail=current
                    ),
                )
            elif kind == "metadataObjects":
                detail = _delete_gcs_prefix(name)
            elif kind == "syncEnv":
                remove_department = run.get("unifiedMcp") or any(item["key"] == "mcp-staff" and not item.get("skipped") for item in run["targets"])
                detail = _refresh_cloud_teardown_routing(run["code"] if remove_department else "")
            else:
                raise ValueError(f"지원하지 않는 삭제 대상: {kind}")
            _TEARDOWNS.update_item(run_id, key, status="COMPLETE", detail=detail)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _TEARDOWNS.update_item(
                run_id,
                key,
                status="FAILED",
                detail=f"{type(exc).__name__}: {exc}"[:400],
            )

    with _TEARDOWNS.lock:
        current = _TEARDOWNS.runs.get(run_id)
        if not current:
            return
        statuses = [item["status"] for item in current["targets"]]
        if any(status == "FAILED" for status in statuses):
            current["status"] = "PARTIAL" if "COMPLETE" in statuses else "FAILED"
        else:
            current["status"] = "COMPLETED"
        current["finishedEpoch"] = time.time()


def apply_teardown_selection(
    plan: dict[str, Any], selected: list[str] | None
) -> dict[str, Any]:
    plan = copy.deepcopy(plan)
    if selected is None:
        chosen = {target["key"] for target in plan["targets"] if target.get("selected")}
    else:
        chosen = {str(key) for key in selected}
    if plan.get("unifiedMcp"):
        available = {target["key"] for target in plan["targets"] if target.get("selectable")}
        if "registry-config" in chosen and available - chosen:
            raise ValueError("학과 등록 설정 삭제 시 선택 가능한 접근·동기화·리소스 정리 항목을 모두 선택해 주세요.")
        data_targets = {target["key"] for target in plan["targets"]
                        if target["kind"] in {"corpus", "bucket", "firestoreState", "metadataObjects"} and target["key"] in chosen and target.get("selectable")}
        if data_targets:
            required = {key for key in available if key.startswith("mcp-")} | {"sync-env"}
            if required - chosen:
                raise ValueError("데이터 삭제 전 이 학과의 MCP 접근 중지와 동기화 중지를 모두 선택해 주세요.")
    elif plan.get("kind") == "department" and "mcp-staff" in chosen and "sync-env" not in chosen:
        raise ValueError("교직원 MCP 삭제 시 학과 라우팅 갱신도 선택해 주세요.")
    for target in plan["targets"]:
        if target.get("sharedWith"):
            continue
        picked = target["key"] in chosen
        target["selected"] = picked
        if not picked:
            target["skipped"] = True
            target["detail"] = "선택하지 않아 남깁니다"
    if not any(target.get("selected") for target in plan["targets"]):
        raise ValueError("지울 대상을 하나 이상 선택해 주세요.")
    return plan


def teardown_selection_warnings(plan: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    chosen = {target["key"] for target in plan["targets"] if target.get("selected")}
    if not chosen:
        return warnings
    if plan.get("kind") != "department":
        return warnings
    if plan.get("unifiedMcp"):
        return ["공통 MCP는 유지됩니다. 중지한 범위는 키로 접근할 수 없으며, Cloud 등록 설정을 남기면 정리 작업을 재시도할 수 있습니다."]
    orphans = [
        target["label"]
        for target in plan["targets"]
        if target.get("selectable")
        and not target.get("selected")
        and target["kind"] in {"cloudRun", "corpus", "bucket", "metadataObjects"}
    ]
    if "mcp-staff" in chosen and orphans:
        warnings.append(
            "Cloud 설정을 담은 MCP를 지우면 남긴 리소스는 콘솔에서 다시 찾을 수 없습니다: "
            + ", ".join(orphans)
        )
    if "mcp-staff" in chosen and "sync-env" not in chosen:
        warnings.append(
            "rag-sync 라우팅을 갱신하지 않으면 없어진 버킷으로 계속 동기화를 시도합니다."
        )
    if "firestore-state" not in chosen and any(
        key.startswith(("corpus-", "bucket-")) for key in chosen
    ):
        warnings.append(
            "동기화 이력을 남기면 같은 공유드라이브를 다시 등록해도 문서가 재색인되지 않습니다."
        )
    return warnings


def start_teardown_run(
    plan: dict[str, Any], confirm: str, selected: list[str] | None = None
) -> dict[str, Any]:
    expected = str(plan.get("confirmWord") or "")
    if str(confirm or "").strip() != expected:
        raise PermissionError(expected)
    plan = apply_teardown_selection(plan, selected)
    if plan.get("unifiedMcp") and any(target["key"] == "sync-env" and target.get("selected") for target in plan["targets"]):
        remaining = {code: config for code, config in cloud_registry._cloud_department_configs().items()
                     if code != plan["code"] and not config.get("syncDisabled")}
        if not remaining:
            _require_sync_removed()
    removal_key = "sync-env" if plan.get("unifiedMcp") else "mcp-staff"
    if plan.get("lastDepartment") and any(target["key"] == removal_key and target.get("selected") for target in plan["targets"]):
        _require_sync_removed()
    with _TEARDOWNS.lock:
        _TEARDOWNS.cleanup()
        active = next(
            (item for item in _TEARDOWNS.runs.values() if item["status"] == "RUNNING"), None
        )
        if active:
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "kind": plan["kind"],
            "unifiedMcp": bool(plan.get("unifiedMcp")),
            "code": plan.get("code", ""),
            "name": plan.get("name", ""),
            "projectId": plan["projectId"],
            "region": plan["region"],
            "status": "RUNNING",
            "targets": copy.deepcopy(plan["targets"]),
            "warnings": teardown_selection_warnings(plan),
            "createdEpoch": time.time(),
        }
        _TEARDOWNS.runs[run_id] = run
    threading.Thread(
        target=_execute_teardown_run,
        args=(run_id,),
        name=f"teardown-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


@router.get("/api/v1/departments/{code}/teardown-plan")
def department_teardown_plan_endpoint(code: str) -> JSONResponse:
    try:
        return JSONResponse(department_teardown_plan(code))
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )


@router.get("/api/v1/common-runtime/teardown-plan")
def common_runtime_teardown_plan_endpoint() -> JSONResponse:
    try:
        return JSONResponse(common_runtime_teardown_plan())
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )


def _teardown_selection(payload: dict[str, Any]) -> list[str] | None:
    raw = payload.get("targets")
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("targets 는 문자열 목록이어야 합니다.")
    return [item for item in raw]


def _teardown_response(
    plan: dict[str, Any], confirm: str, selected: list[str] | None = None
) -> JSONResponse:
    try:
        run = start_teardown_run(plan, confirm, selected)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_SELECTION_EMPTY", "message": str(exc)[:200]}},
            status_code=422,
        )
    except PermissionError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "TEARDOWN_CONFIRM_MISMATCH",
                    "message": f"확인 문구가 다릅니다. '{exc}' 를 그대로 입력해 주세요.",
                }
            },
            status_code=422,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "TEARDOWN_RUNNING",
                    "message": "이미 진행 중인 삭제 작업이 있습니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@router.post("/api/v1/departments/{code}/teardown")
async def create_department_teardown(code: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "본문이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = department_teardown_plan(code)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    try:
        selected = _teardown_selection(payload)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": str(exc)}}, status_code=400
        )
    return _teardown_response(plan, str(payload.get("confirm") or ""), selected)


@router.post("/api/v1/common-runtime/teardown")
async def create_common_runtime_teardown(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "본문이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = common_runtime_teardown_plan()
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    try:
        selected = _teardown_selection(payload)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": str(exc)}}, status_code=400
        )
    return _teardown_response(plan, str(payload.get("confirm") or ""), selected)


@router.get("/api/v1/teardowns/{run_id}")
def teardown_run(run_id: str) -> JSONResponse:
    with _TEARDOWNS.lock:
        _TEARDOWNS.cleanup()
        run = _TEARDOWNS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "삭제 작업을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))
