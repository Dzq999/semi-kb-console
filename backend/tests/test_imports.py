"""素材导入通道测试：上传→分析(mock LLM)→校验(真门禁)→采纳(真引擎写入)+回滚+NDA 拦截。

不依赖真 LLM：monkeypatch llm_service.complete 返回罐装映射建议。校验/采纳走真 imported_ingest.py
子进程，对真引擎根 sources/internal/imported/ 有写入的用例在 finally 彻底清理。
"""
from __future__ import annotations

import json
import re
import shutil

from app.config import settings
from app.services import imports as imports_service
from app.services.llm import llm_service

CSV_BYTES = b"equipment,phase,value\nEQP1,etch,3\nEQP2,cvd,5\n"

_CLASS_RE = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+owl:Class")


def _gate_declared_classes() -> set[str]:
    """按 imported_ingest.py 门禁的同一规则扫 ontology/modules/*.ttl 收集已声明类。"""
    terms: set[str] = set()
    modules = settings.engine_root / "ontology" / "modules"
    for path in sorted(modules.glob("*.ttl")):
        terms.update("urn:pxai:semi:" + m for m in _CLASS_RE.findall(path.read_text(encoding="utf-8")))
    return terms


def _first_declared_class() -> str:
    """取既被 ontology_context 认可、又能过门禁正则的类，保证整条链路都放行。"""
    gate_terms = _gate_declared_classes()
    for cls in imports_service._declared_classes():
        if cls["iri"] in gate_terms:
            return cls["iri"]
    raise AssertionError("找不到同时满足 ontology_context 与门禁正则的已声明类")


def _cleanup_live() -> None:
    """删除本测试对真引擎根的所有写入：导入源目录 + 派生 catalog。"""
    shutil.rmtree(settings.engine_root / "sources" / "internal" / "imported", ignore_errors=True)
    (settings.engine_root / "build" / "source" / "imported-catalog.json").unlink(missing_ok=True)


def _upload(client, classification="internal_confidential"):
    return client.post(
        "/api/imports",
        files=[("files", ("sample.csv", CSV_BYTES, "text/csv"))],
        data={"classification": classification},
    )


def test_import_full_flow_upload_analyze_validate_adopt(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    target_class = _first_declared_class()

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "summary": "两台设备的异常样本",
            "files": [{"stored_name": "sample.csv", "target_class": target_class,
                       "entity_keys": ["equipment"], "time_field": None}],
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    job_id = ""
    try:
        upload = _upload(authenticated)
        assert upload.status_code == 200, upload.text
        job = upload.json()
        job_id = job["job_id"]
        assert job["status"] == "uploaded"
        assert job["uploaded"][0]["sha256"]  # 原件已 sha256 锁定

        analyzed = authenticated.post(f"/api/imports/{job_id}/analyze", json={"model_id": "gpt-test"}).json()
        assert analyzed["status"] == "analyzed"
        mapped = analyzed["manifest"]["files"][0]
        assert mapped["target_class"] == target_class
        assert mapped["target_class_valid"] is True
        assert mapped["entity_keys"] == ["equipment"]

        validated = authenticated.post(f"/api/imports/{job_id}/validate").json()
        assert validated["status"] == "validated", validated
        assert validated["validation"]["passed"] is True
        assert validated["validation"]["datasets"][0]["target_class"] == target_class

        adopted = authenticated.post(f"/api/imports/{job_id}/adopt")
        assert adopted.status_code == 200, adopted.text
        body = adopted.json()
        assert body["status"] == "adopted"
        assert body["adopted_paths"]["files"], "应记录入库文件路径"
        # 线上源目录已写入原件 + manifest；catalog 生成。
        live = settings.engine_root / "sources" / "internal" / "imported"
        assert (live / "manifest.json").is_file()
        assert list((live / "raw").glob("*.csv"))
        assert (settings.engine_root / "build" / "source" / "imported-catalog.json").is_file()
        # 采纳成功后 staging 目录被清理。
        assert not (settings.engine_root / "imports" / "drafts" / job_id).exists()
    finally:
        if job_id:
            shutil.rmtree(settings.engine_root / "imports" / "drafts" / job_id, ignore_errors=True)
        _cleanup_live()


def test_import_restricted_blocks_validate_and_adopt(authenticated, monkeypatch):
    """restricted 素材可上传暂存，但校验/采纳被 422 拦下，绝不入库。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    target_class = _first_declared_class()

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "summary": "受限素材",
            "files": [{"stored_name": "sample.csv", "target_class": target_class,
                       "entity_keys": ["equipment"], "time_field": None}],
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    job_id = ""
    try:
        job = _upload(authenticated, classification="restricted").json()
        job_id = job["job_id"]
        assert job["classification"] == "restricted"
        authenticated.post(f"/api/imports/{job_id}/analyze", json={"model_id": "gpt-test"})

        validate = authenticated.post(f"/api/imports/{job_id}/validate")
        assert validate.status_code == 422
        adopt = authenticated.post(f"/api/imports/{job_id}/adopt")
        assert adopt.status_code == 422
        # 绝无线上写入。
        assert not (settings.engine_root / "sources" / "internal" / "imported").exists()
    finally:
        if job_id:
            shutil.rmtree(settings.engine_root / "imports" / "drafts" / job_id, ignore_errors=True)
        _cleanup_live()


def test_import_undeclared_target_class_fails_validation(authenticated, monkeypatch):
    """模型建议了未声明的类 → 服务端留空 target_class → 校验因无可入库文件被 422。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "summary": "杜撰的类",
            "files": [{"stored_name": "sample.csv", "target_class": "urn:pxai:semi:TotallyMadeUpClass",
                       "entity_keys": ["equipment"], "time_field": None}],
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    job_id = ""
    try:
        job_id = _upload(authenticated).json()["job_id"]
        analyzed = authenticated.post(f"/api/imports/{job_id}/analyze", json={"model_id": "gpt-test"}).json()
        mapped = analyzed["manifest"]["files"][0]
        assert mapped["target_class"] == ""  # 未命中已声明类，服务端不采信
        assert mapped["target_class_valid"] is False

        validate = authenticated.post(f"/api/imports/{job_id}/validate")
        assert validate.status_code == 422  # 无可入库文件
        assert not (settings.engine_root / "sources" / "internal" / "imported").exists()
    finally:
        if job_id:
            shutil.rmtree(settings.engine_root / "imports" / "drafts" / job_id, ignore_errors=True)
        _cleanup_live()


def test_import_unsupported_format_rejected(authenticated):
    response = authenticated.post(
        "/api/imports",
        files=[("files", ("evil.exe", b"MZ\x00\x00", "application/octet-stream"))],
        data={"classification": "internal"},
    )
    assert response.status_code == 422
