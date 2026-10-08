"""학과 설정이 실제 Parser 호출까지 격리되고 이전 호출은 호환되는지 검증한다."""

import json
import subprocess
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scripts import dept_config
from services.parser import fallback_docai
from services.parser import main as parser
from services.parser.quality_gate import ParseMetrics
from services.parser.rhwp_parser import ParseOutput
from services.sync import main as sync
from shared.config import Settings, _departments_from_json
from shared.path_context import build_path_context


def test_department_choice_round_trips_without_inheriting_global_flag(tmp_path, monkeypatch):
    (tmp_path / "common.yaml").write_text("GCP_PROJECT_ID: p\n", encoding="utf-8")
    monkeypatch.setattr(dept_config, "CONFIG_DIR", tmp_path)
    configs = {
        code: {
            "corpora": {"staff": f"corpus-{code}"},
            "keys": {"staff": code * 32},
            "buckets": {"hwpOriginal": f"raw-{code}", "source": f"source-{code}"},
            "drive": {"driveIds": [code], "syncFolderIds": [f"folder-{code}"]},
        }
        for code in ("cs", "ee", "ai")
    }
    configs["cs"]["enableDocaiFallback"] = True
    configs["ee"]["enableDocaiFallback"] = False
    mapping = dept_config.departments_map_from_configs(configs)
    settings = Settings(
        gcp_project_id="p", enable_docai_fallback=True,
        departments=_departments_from_json(json.dumps(mapping)),
    )
    assert settings.for_drive("cs").enable_docai_fallback is True
    assert settings.for_drive("ee").enable_docai_fallback is False
    assert settings.for_drive("ai").enable_docai_fallback is False
    assert settings.for_drive("cs").for_drive("ee").enable_docai_fallback is False
    mapping["cs"]["enableDocaiFallback"] = "false"
    with pytest.raises(ValueError, match="enableDocaiFallback"):
        _departments_from_json(json.dumps(mapping))
    configs["cs"]["enableDocaiFallback"] = "false"
    with pytest.raises(SystemExit, match="enableDocaiFallback"):
        dept_config.departments_map_from_configs(configs)


@pytest.fixture
def parser_client(monkeypatch):
    settings = Settings(gcp_project_id="p", qg_mode="log", enable_docai_fallback=False)
    monkeypatch.setattr(parser, "get_settings", lambda: settings)
    monkeypatch.setattr(parser, "can_parse", lambda _: True)
    gcs = MagicMock()
    gcs._client.bucket.return_value.get_blob.return_value.size = 100
    gcs.download_bytes.return_value = b"source"
    gcs.upload_source_md.return_value = "gs://source/doc.md"
    monkeypatch.setattr(parser, "GcsClient", lambda _: gcs)
    monkeypatch.setattr(parser, "parse_document_bytes", lambda *a, **kw: ParseOutput(
        markdown="본문 텍스트입니다. " * 20,
        metrics=ParseMetrics(text_length=200, table_count=10, source_bytes=100), engine="rhwp",
    ))
    fallback = MagicMock(return_value=(
        "복구한 본문입니다. " * 20, ParseMetrics(text_length=200, source_bytes=100),
    ))
    monkeypatch.setattr(fallback_docai, "fallback_parse", fallback)
    return TestClient(parser.app), fallback, settings


def test_parser_choice_is_per_request_and_omitted_requests_keep_old_behavior(parser_client):
    client, fallback, settings = parser_client
    body = {"gcsUri": "gs://raw/doc.hwp", "mimeType": "application/x-hwp", "fileId": "doc"}
    for enabled, status in [(True, "fallback_ok"), (False, "logged"), (None, "logged")]:
        payload = body if enabled is None else {**body, "enableDocaiFallback": enabled}
        response = client.post("/parse", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["qualityGate"]["status"] == status
    fallback.assert_called_once()
    assert fallback.call_args.kwargs["settings"].enable_docai_fallback is True
    assert settings.enable_docai_fallback is False
    assert settings.qg_mode == "log"


def test_explicit_false_overrides_global_fallback_but_omission_preserves_it(parser_client, monkeypatch):
    client, fallback, settings = parser_client
    monkeypatch.setattr(parser, "get_settings", lambda: replace(
        settings, qg_mode="fallback", enable_docai_fallback=True,
    ))
    body = {"gcsUri": "gs://raw/doc.hwp", "mimeType": "application/x-hwp", "fileId": "doc"}
    response = client.post("/parse", json={**body, "enableDocaiFallback": False})
    assert response.json()["qualityGate"]["mode"] == "log"
    fallback.assert_not_called()
    response = client.post("/parse", json=body)
    assert response.json()["qualityGate"]["status"] == "fallback_ok"
    fallback.assert_called_once()
    assert client.post("/parse", json={**body, "enableDocaiFallback": "false"}).status_code == 422


@pytest.mark.parametrize("enabled", [True, False])
def test_sync_sends_department_choice_to_parser(enabled, monkeypatch):
    settings = Settings(gcp_project_id="p", enable_docai_fallback=enabled)
    monkeypatch.setattr(sync, "_resolve_path_ctx", lambda *a: build_path_context([], "doc.hwp"))
    monkeypatch.setattr(sync, "_resolve_audience", lambda *a: "staff")
    monkeypatch.setattr(sync, "_cloud_run_auth_headers", lambda *a: {})
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.status_code = 422
    client.post.return_value.headers = {"content-type": "application/json"}
    client.post.return_value.json.return_value = {"detail": {"error": "QUALITY_GATE"}}
    monkeypatch.setattr(sync.httpx, "Client", lambda **kw: client)
    gcs, drive = MagicMock(), MagicMock()
    gcs.upload_hwp_original.return_value = "gs://raw/doc.hwp"
    drive.download_file.return_value = b"source"
    sync._ingest_hwp(sync.IngestBody(
        fileId="doc", driveId="cs", name="doc.hwp", mimeType="application/x-hwp",
        parserUrl="https://parser.example",
    ), MagicMock(), gcs, drive, settings)
    assert client.post.call_args.kwargs["json"]["enableDocaiFallback"] is enabled


def test_conversion_timeout_is_a_fallback_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback_docai.shutil, "which", lambda _: "soffice")
    def timeout(command, **kwargs):
        assert any(arg.startswith("-env:UserInstallation=file:") for arg in command)
        raise subprocess.TimeoutExpired(command, 300)
    monkeypatch.setattr(fallback_docai.subprocess, "run", timeout)
    with pytest.raises(fallback_docai.FallbackParseError, match="LibreOffice convert failed"):
        fallback_docai.hwp_to_pdf(tmp_path / "input.hwp", tmp_path)


@pytest.mark.parametrize("audience,expected", [("STAFF",False),("STUDENT",True),("unknown",False)])
def test_scoped_fallback_reaches_parser(audience,expected,monkeypatch):
    settings = Settings(gcp_project_id="p", enable_docai_fallback=True,
                        enable_docai_fallback_staff=False, enable_docai_fallback_student=True)
    monkeypatch.setattr(sync,"_resolve_path_ctx",lambda *a:build_path_context([],"doc.hwp"))
    monkeypatch.setattr(sync,"_resolve_audience",lambda *a:audience)
    monkeypatch.setattr(sync,"_cloud_run_auth_headers",lambda *a:{})
    client=MagicMock(); client.__enter__.return_value=client
    client.post.return_value.status_code=422
    client.post.return_value.headers={"content-type":"application/json"}
    client.post.return_value.json.return_value={"detail":{"error":"QUALITY_GATE"}}
    monkeypatch.setattr(sync.httpx,"Client",lambda **kw:client)
    gcs,drive=MagicMock(),MagicMock();gcs.upload_hwp_original.return_value="gs://raw/doc.hwp"
    drive.download_file.return_value=b"source"
    sync._ingest_hwp(sync.IngestBody(fileId="doc",driveId="cs",name="doc.hwp",mimeType="application/x-hwp",parserUrl="https://parser"),MagicMock(),gcs,drive,settings)
    assert client.post.call_args.kwargs["json"]["enableDocaiFallback"] is expected


def test_audience_overrides_survive_mapping_and_do_not_leak_between_departments(tmp_path,monkeypatch):
    (tmp_path/"common.yaml").write_text("GCP_PROJECT_ID: p\n")
    monkeypatch.setattr(dept_config,"CONFIG_DIR",tmp_path)
    configs={code:{"corpora":{"staff":code},"keys":{"staff":code*32},"buckets":{"hwpOriginal":"raw","source":"source"},"drive":{"driveIds":[code],"syncFolderIds":[code]}} for code in ["cs","ee"]}
    configs["cs"].update(enableDocaiFallbackStaff=False,enableDocaiFallbackStudent=True,enableImageOcrStaff=False,enableImageOcrStudent=True)
    mapping=dept_config.departments_map_from_configs(configs)
    cs=Settings(gcp_project_id="p",departments=_departments_from_json(json.dumps(mapping))).for_drive("cs")
    assert cs.enable_image_ocr  # changes/backfill must include possible student images
    for option in ["enable_docai_fallback","enable_image_ocr"]:
        assert not cs.parser_option_for(option,"STAFF")
        assert cs.parser_option_for(option,"STUDENT")
        assert not cs.for_drive("ee").parser_option_for(option,"STUDENT")
    mapping["cs"]["enableImageOcrStaff"]="false"
    with pytest.raises(ValueError,match="enableImageOcrStaff"):
        _departments_from_json(json.dumps(mapping))
