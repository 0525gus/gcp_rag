"""Read-before-create setup for the two supported pretrained Document AI processors."""
from __future__ import annotations

import re
import threading
import time
import uuid
from urllib.parse import urlencode

KINDS = {
    "fallback": ("LAYOUT_PARSER_PROCESSOR", "DOCAI_PROCESSOR_ID", "DOCAI_LOCATION"),
    "ocr": ("OCR_PROCESSOR", "DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION"),
}
LOCATIONS = ("us", "eu", "asia-south1", "asia-southeast1", "australia-southeast1",
             "europe-west2", "europe-west3", "northamerica-northeast1")


def selection_fields(selections):
    if not isinstance(selections, dict) or set(selections) - set(KINDS):
        raise ValueError("Document AI 설정 형식이 올바르지 않습니다.")
    fields = {}
    for kind, selection in selections.items():
        if not isinstance(selection, dict) or set(selection) != {"processorId", "location"}:
            raise ValueError("프로세서 ID와 리전을 함께 지정해 주세요.")
        proc, location = selection["processorId"], selection["location"]
        if not isinstance(proc, str) or not isinstance(location, str):
            raise ValueError("프로세서 ID와 리전은 문자열이어야 합니다.")  # noqa: TRY004 -- invalid setup payload
        if proc and (not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", proc) or location not in LOCATIONS):
            raise ValueError("프로세서 ID 또는 Document AI 리전을 확인해 주세요.")
        if not proc and location:
            raise ValueError("리전만 저장할 수 없습니다. 프로세서를 선택해 주세요.")
        _, id_key, location_key = KINDS[kind]
        fields.update({id_key: proc, location_key: location})
    return fields


class ProcessorSetup:
    def __init__(self, get, post, token):
        self.get, self.post, self.token = get, post, token
        self.plans = {}
        self.lock = threading.Lock()

    def base(self, project, kind, location):
        if not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
            raise ValueError("GCP 프로젝트 ID를 확인해 주세요.")
        if kind not in KINDS or location not in LOCATIONS:
            raise ValueError("프로세서 종류와 Document AI 리전을 선택해 주세요.")
        return f"https://{location}-documentai.googleapis.com/v1/projects/{project}/locations/{location}"

    @staticmethod
    def checked(status, body):
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"Document AI 조회/생성 실패 (HTTP {status or 'timeout'}). API 활성화와 IAM 권한을 확인하고 다시 조회해 주세요.")
        return body

    def processors(self, base, kind, token):
        rows, page, seen = [], "", set()
        while True:
            query = urlencode({"pageSize": 100, **({"pageToken": page} if page else {})})
            status, body, _ = self.get(f"{base}/processors?{query}", token, timeout=20)
            body = self.checked(status, body)
            for row in body.get("processors", []):
                if row.get("type") == KINDS[kind][0]:
                    rows.append({"id": row["name"].rsplit("/", 1)[-1], "name": row.get("displayName", ""),
                                 "state": row.get("state", "UNKNOWN"), "type": row["type"]})
            page = body.get("nextPageToken", "")
            if not page:
                return rows
            if page in seen or len(seen) >= 100:
                raise RuntimeError("프로세서 목록을 끝까지 확인하지 못했습니다. 생성하지 않습니다.")
            seen.add(page)

    def lookup(self, project, kind, location):
        base = self.base(project, kind, location)
        token = self.token()
        rows = self.processors(base, kind, token)
        can_create, reason = False, "기존 프로세서를 선택해 주세요." if rows else ""
        if not rows:
            status, body, _ = self.get(f"{base}/processorTypes/{KINDS[kind][0]}", token, timeout=20)
            body = self.checked(status, body)
            can_create = body.get("allowCreation") is True and any(
                row.get("locationId") == location for row in body.get("availableLocations", []))
            reason = "새 프로세서를 만들 수 있습니다." if can_create else "이 프로젝트·리전에서는 해당 종류를 생성할 수 없습니다."
        plan_id = ""
        if can_create:
            with self.lock:
                self.plans = {k: v for k, v in self.plans.items() if time.time() - v["time"] < 600}
                plan_id = uuid.uuid4().hex
                self.plans[plan_id] = {"project": project, "kind": kind, "location": location, "time": time.time()}
        return {"processors": rows, "canCreate": can_create, "reason": reason, "planId": plan_id,
                "projectId": project, "kind": kind, "location": location,
                "displayName": f"rag-{kind}"}

    def verify(self, project, kind, selection):
        selection_fields({kind: selection})
        if not selection["processorId"]:
            return
        base = self.base(project, kind, selection["location"])
        status, body, _ = self.get(f"{base}/processors/{selection['processorId']}", self.token(), timeout=20)
        body = self.checked(status, body)
        if body.get("type") != KINDS[kind][0] or body.get("state") != "ENABLED":
            raise ValueError("종류가 일치하고 ENABLED 상태인 프로세서만 저장할 수 있습니다.")

    def create(self, plan_id):
        # Serialise creation, re-list immediately before POST, and never retry an ambiguous POST.
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan or time.time() - plan["time"] >= 600:
                raise ValueError("생성 계획이 만료됐습니다. 기존 프로세서를 다시 조회해 주세요.")
            if plan.get("attempted"):
                raise ValueError("이미 생성 요청한 계획입니다. 결과를 다시 조회해 주세요.")
            base = self.base(plan["project"], plan["kind"], plan["location"])
            token = self.token()
            if self.processors(base, plan["kind"], token):
                raise ValueError("기존 프로세서가 발견됐습니다. 다시 조회해서 선택해 주세요.")
            # Availability can change after the plan was prepared.
            status, body, _ = self.get(f"{base}/processorTypes/{KINDS[plan['kind']][0]}", token, timeout=20)
            body = self.checked(status, body)
            if body.get("allowCreation") is not True or not any(
                row.get("locationId") == plan["location"] for row in body.get("availableLocations", [])):
                raise ValueError("현재 이 리전에서는 생성할 수 없습니다. 다시 조회해 주세요.")
            plan["attempted"] = True
            status, body, _ = self.post(f"{base}/processors", {
                "type": KINDS[plan["kind"]][0], "displayName": f"rag-{plan['kind']}",
            }, token, timeout=60)
            body = self.checked(status, body)
            proc = str(body.get("name", "")).rsplit("/", 1)[-1]
            selection = {"processorId": proc, "location": plan["location"]}
            if not proc:
                raise RuntimeError("생성 결과 ID를 확인하지 못했습니다. 재생성 전에 다시 조회해 주세요.")
            self.verify(plan["project"], plan["kind"], selection)
            return {**selection, "projectId": plan["project"], "kind": plan["kind"]}
