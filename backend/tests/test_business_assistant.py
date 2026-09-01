"""独立经营模型协作 Agent 的测试：草案引擎校验 / 晋升落盘 / 回滚 / 端点状态流转。

分三层：
- Tier1：把手写草案落到真引擎根的 business/drafts/ 就地直调 validate_business_draft（真子进程）。
- Tier2：copytree 出隔离引擎根，用独立 SemiKbAdapter 直测 promote：三件套落线上 + 全库门禁通过、
  同名碰撞被拒、以及『单体过但全库挂』时只回滚本次写入的三文件。
- 端点级：monkeypatch llm_service.complete 返回罐装三件套，走 draft→approve 断言状态流转。

不依赖真 LLM。对真引擎根有写入的测试都在 finally 清理。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from app.config import settings
from app.models import User
from app.services.llm import llm_service
from app.services.semi_kb import SemiKbAdapter, SemiKbError, semi_kb

BASE_VALUES = [
    {"id": "input_units", "value": 120000, "unit": "unit/month", "source": "assumption"},
    {"id": "capacity_limit", "value": 130000, "unit": "unit/month", "source": "assumption"},
    {"id": "process_yield", "value": 0.94, "unit": "ratio", "source": "assumption"},
    {"id": "selling_price", "value": 480, "unit": "CNY/unit", "source": "assumption"},
    {"id": "variable_unit_cost", "value": 300, "unit": "CNY/unit", "source": "assumption"},
    {"id": "fixed_cost", "value": 8000000, "unit": "CNY/month", "source": "assumption"},
    {"id": "intervention_opex", "value": 0, "unit": "CNY/month", "source": "assumption"},
]
BASE_OUTPUTS = ["constrained_input", "saleable_units", "revenue", "variable_cost", "profit", "unit_cost"]


def _docs(draft_id: str, token: str, *, values=None, outputs=None) -> dict:
    """按 business_assistant._assemble_documents 的结构手写三件套（id 服务端式命名，ref 自指 drafts/）。"""
    slug = "manufacturing"
    return {
        "template.yaml": {
            "schema_version": "2.0",
            "template": {
                "id": f"template.human.{slug}.{token}",
                "name": "测试经营骨架",
                "extends": ["business/templates/manufacturing.yaml"],
            },
        },
        "dataset.yaml": {
            "schema_version": "2.0",
            "dataset": {
                "id": f"dataset.human.{slug}.{token}",
                "description": "测试情景假设数据",
                "values": BASE_VALUES if values is None else values,
            },
        },
        "model.yaml": {
            "schema_version": "2.0",
            "model": {
                "id": f"business.human.{slug}.{token}",
                "name": "测试经营基线",
                "domain": "manufacturing",
                "period": "month",
                "currency": "CNY",
                "template_ref": f"business/drafts/{draft_id}/template.yaml",
                "dataset_ref": f"business/drafts/{draft_id}/dataset.yaml",
                "outputs": BASE_OUTPUTS if outputs is None else outputs,
            },
        },
    }


def _write_draft(root: Path, draft_id: str, docs: dict) -> Path:
    draft_dir = root / "business" / "drafts" / draft_id
    draft_dir.mkdir(parents=True, exist_ok=True)
    for name, body in docs.items():
        (draft_dir / name).write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return draft_dir


# --------------------------------------------------------------------------- Tier1

@pytest.mark.asyncio
async def test_validate_business_draft_accepts_valid_baseline():
    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    draft_dir = _write_draft(settings.engine_root, draft_id, _docs(draft_id, token))
    try:
        result = await semi_kb.validate_business_draft(draft_dir / "model.yaml")
        assert result["passed"] is True
        assert result["errors"] == []
        assert set(BASE_OUTPUTS) <= set(result["outputs"])
        assert result["outputs"]["profit"]["unit"] == "CNY/month"
    finally:
        shutil.rmtree(draft_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_validate_business_draft_rejects_ratio_out_of_range():
    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    values = [dict(v, value=2.0) if v["id"] == "process_yield" else dict(v) for v in BASE_VALUES]
    draft_dir = _write_draft(settings.engine_root, draft_id, _docs(draft_id, token, values=values))
    try:
        result = await semi_kb.validate_business_draft(draft_dir / "model.yaml")
        assert result["passed"] is False
        assert any("比例" in error for error in result["errors"])
    finally:
        shutil.rmtree(draft_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_validate_business_draft_rejects_undefined_output():
    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    outputs = BASE_OUTPUTS + ["does_not_exist"]
    draft_dir = _write_draft(settings.engine_root, draft_id, _docs(draft_id, token, outputs=outputs))
    try:
        result = await semi_kb.validate_business_draft(draft_dir / "model.yaml")
        assert result["passed"] is False
        assert any("does_not_exist" in error for error in result["errors"])
    finally:
        shutil.rmtree(draft_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_validate_business_draft_rejects_missing_input():
    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    values = [dict(v) for v in BASE_VALUES if v["id"] != "fixed_cost"]
    draft_dir = _write_draft(settings.engine_root, draft_id, _docs(draft_id, token, values=values))
    try:
        result = await semi_kb.validate_business_draft(draft_dir / "model.yaml")
        assert result["passed"] is False
        assert any("fixed_cost" in error for error in result["errors"])
    finally:
        shutil.rmtree(draft_dir, ignore_errors=True)


# --------------------------------------------------------------------------- Tier2

def _isolated_engine(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    shutil.copytree(
        settings.engine_root, engine,
        ignore=shutil.ignore_patterns("knowledge", "build", ".git", "drafts"),
    )
    return engine


@pytest.mark.asyncio
async def test_promote_business_draft_lands_live_and_passes_gate(tmp_path):
    engine = _isolated_engine(tmp_path)
    adapter = SemiKbAdapter(root=engine)
    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    draft_dir = _write_draft(engine, draft_id, _docs(draft_id, token))

    validation = await adapter.validate_business_draft(draft_dir / "model.yaml")
    assert validation["passed"] is True

    promoted = await adapter.promote_business_draft(draft_dir)
    template_path = engine / promoted["template_path"]
    dataset_path = engine / promoted["dataset_path"]
    model_path = engine / promoted["model_path"]
    assert template_path.is_file() and dataset_path.is_file() and model_path.is_file()
    assert promoted["template_path"] == f"business/templates/template.human.manufacturing.{token}.yaml"
    assert promoted["model_path"] == f"business/models/business.human.manufacturing.{token}.yaml"

    # 晋升时 model 的 ref 被改写为线上文件（不再指向 drafts/）。
    live_model = yaml.safe_load(model_path.read_text(encoding="utf-8"))["model"]
    assert live_model["template_ref"] == promoted["template_path"]
    assert live_model["dataset_ref"] == promoted["dataset_path"]

    # 同一草案二次晋升：目标已存在 → 碰撞被拒。
    with pytest.raises(SemiKbError):
        await adapter.promote_business_draft(draft_dir)


@pytest.mark.asyncio
async def test_promote_rolls_back_when_whole_library_gate_fails(tmp_path):
    engine = _isolated_engine(tmp_path)
    adapter = SemiKbAdapter(root=engine)

    # 预埋一个『全库挂』的兄弟模型（ref 指向不存在文件），让 simulate_check.py 必然失败，
    # 而本次晋升的草案本身『单体过』——用以验证门禁失败时只回滚本次写入的三文件。
    broken = engine / "business" / "models" / "zzz_broken.yaml"
    broken.write_text(
        yaml.safe_dump(
            {"schema_version": "2.0", "model": {
                "id": "business.broken.test", "period": "month", "currency": "CNY",
                "template_ref": "business/templates/__missing__.yaml",
                "dataset_ref": "business/datasets/__missing__.yaml",
                "outputs": ["profit"],
            }},
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )

    draft_id = f"1-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    draft_dir = _write_draft(engine, draft_id, _docs(draft_id, token))
    assert (await adapter.validate_business_draft(draft_dir / "model.yaml"))["passed"] is True

    with pytest.raises(SemiKbError):
        await adapter.promote_business_draft(draft_dir)

    # 回滚：本次写入的三文件都不得残留；预埋的坏文件不受影响。
    assert not (engine / "business" / "templates" / f"template.human.manufacturing.{token}.yaml").exists()
    assert not (engine / "business" / "datasets" / f"dataset.human.manufacturing.{token}.yaml").exists()
    assert not (engine / "business" / "models" / f"business.human.manufacturing.{token}.yaml").exists()
    assert broken.is_file()


# --------------------------------------------------------------------------- 端点级

def _fake_llm_payload() -> str:
    return json.dumps({
        "summary": "某产线单月成本-产能-利润基线",
        "template": {"name": "端点测试骨架", "extends": ["business/templates/manufacturing.yaml"]},
        "dataset": {"description": "端点测试假设数据", "values": BASE_VALUES},
        "model": {"name": "端点测试基线", "domain": "manufacturing", "period": "month",
                  "currency": "CNY", "outputs": BASE_OUTPUTS},
    }, ensure_ascii=False)


def test_draft_then_approve_endpoint_flow(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_complete(*args, **kwargs):
        return _fake_llm_payload()

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    promoted_paths: dict = {}
    draft_id = ""
    try:
        response = authenticated.post(
            "/api/business-models/draft",
            json={"intent": "评估良率提升与固定成本变化对单月利润的影响", "domain": "manufacturing", "model_id": "gpt-test"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        draft_id = body["draft_id"]
        assert body["status"] == "validated"
        assert body["validation"]["passed"] is True

        listed = authenticated.get("/api/business-models/drafts").json()["items"]
        assert any(item["draft_id"] == draft_id for item in listed)

        detail = authenticated.get(f"/api/business-models/drafts/{draft_id}").json()
        assert detail["documents"]["model"]["model"]["id"].startswith("business.human.")

        approved = authenticated.post(f"/api/business-models/drafts/{draft_id}/approve")
        assert approved.status_code == 200, approved.text
        approved_body = approved.json()
        assert approved_body["status"] == "approved"
        promoted_paths = approved_body["promoted_paths"]
        assert {"template_path", "dataset_path", "model_path"} <= set(promoted_paths)
        for path in ("template_path", "dataset_path", "model_path"):
            assert (settings.engine_root / promoted_paths[path]).is_file()

        # 晋升成功后草案 staging 目录被清理。
        assert not (settings.engine_root / "business" / "drafts" / draft_id).exists()
    finally:
        for path in promoted_paths.values():
            (settings.engine_root / path).unlink(missing_ok=True)
        if draft_id:
            shutil.rmtree(settings.engine_root / "business" / "drafts" / draft_id, ignore_errors=True)


def test_approve_rejects_unvalidated_draft(authenticated, monkeypatch):
    """LLM 产出畸形草案（ratio 越界）→ 状态 invalid → approve 被 422 拦下、不落盘。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    bad_values = [dict(v, value=2.0) if v["id"] == "process_yield" else dict(v) for v in BASE_VALUES]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "summary": "越界草案",
            "template": {"name": "坏骨架", "extends": ["business/templates/manufacturing.yaml"]},
            "dataset": {"description": "坏数据", "values": bad_values},
            "model": {"name": "坏基线", "domain": "manufacturing", "period": "month",
                      "currency": "CNY", "outputs": BASE_OUTPUTS},
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    draft_id = ""
    try:
        body = authenticated.post(
            "/api/business-models/draft",
            json={"intent": "构造一个不合法的比例假设", "domain": "manufacturing", "model_id": "gpt-test"},
        ).json()
        draft_id = body["draft_id"]
        assert body["status"] == "invalid"
        assert body["validation"]["passed"] is False

        approved = authenticated.post(f"/api/business-models/drafts/{draft_id}/approve")
        assert approved.status_code == 422
    finally:
        if draft_id:
            shutil.rmtree(settings.engine_root / "business" / "drafts" / draft_id, ignore_errors=True)


# --------------------------------------------------------------------------- 定时自动起草

def test_derive_intents_from_gap_uses_hot_themes(monkeypatch):
    from app.services import business_assistant

    fake_gap = {"by_theme": [
        {"theme": "设备可用性", "count": 12, "samples": ["mtbf", "mttr"]},
        {"theme": "良率", "count": 8, "samples": ["defect_density"]},
    ]}
    monkeypatch.setattr(business_assistant.semi_kb, "feature_gap", lambda: fake_gap)

    intents = business_assistant.derive_intents_from_gap(3)
    assert len(intents) == 3
    assert "设备可用性" in intents[0][0] and "mtbf" in intents[0][0]
    assert "良率" in intents[1][0]
    # 热点只有 2 个，第 3 条用通用意图补足；domain 一律 manufacturing（线上唯一基座）。
    assert all(domain == "manufacturing" for _, domain in intents)
    assert intents[2][0] in business_assistant._FALLBACK_INTENTS


def test_derive_intents_from_gap_falls_back_when_gap_unavailable(monkeypatch):
    from app.services import business_assistant

    def boom():
        raise RuntimeError("gap unavailable")

    monkeypatch.setattr(business_assistant.semi_kb, "feature_gap", boom)
    intents = business_assistant.derive_intents_from_gap(2)
    assert len(intents) == 2
    assert all(text in business_assistant._FALLBACK_INTENTS for text, _ in intents)


@pytest.mark.asyncio
async def test_generate_baseline_batch_isolates_failures(monkeypatch):
    from app.db import SessionLocal
    from app.services import business_assistant

    monkeypatch.setattr(
        business_assistant, "derive_intents_from_gap",
        lambda count: [(f"意图{i}", "manufacturing") for i in range(count)],
    )

    calls = {"n": 0}

    async def flaky_draft(db, user, intent, domain, model_id):
        calls["n"] += 1
        if intent == "意图1":
            raise business_assistant.ExternalServiceError("模型抖动")
        return {"draft_id": f"1-{calls['n']:012d}", "status": "validated", "intent": intent}

    monkeypatch.setattr(business_assistant, "draft_business_baseline", flaky_draft)

    with SessionLocal() as db:
        db.add(User(id=1, username="u", password_hash="x"))
        db.commit()
        results = await business_assistant.generate_baseline_batch(db, 1, 3, "gpt-test")

    assert calls["n"] == 3  # 单份失败不中断整批
    statuses = [item["status"] for item in results]
    assert statuses.count("validated") == 2 and statuses.count("error") == 1


def test_approve_batch_endpoint_partial_success(authenticated, monkeypatch):
    """批量采纳：一份通过校验→晋升，一份未通过→拒；串行、部分成功。"""
    import json as _json

    from app.db import SessionLocal
    from app.models import BusinessDraft

    with SessionLocal() as db:
        db.add(BusinessDraft(id="1-aaaaaaaaaaaa", user_id=1, status="validated",
                             validation_json=_json.dumps({"passed": True})))
        db.add(BusinessDraft(id="1-bbbbbbbbbbbb", user_id=1, status="invalid",
                             validation_json=_json.dumps({"passed": False, "errors": ["坏"]})))
        db.commit()

    async def fake_promote(draft_dir):
        return {"template_path": "t", "dataset_path": "d", "model_path": "m"}

    monkeypatch.setattr(semi_kb, "promote_business_draft", fake_promote)
    monkeypatch.setattr("app.main.draft_dir_path", lambda draft_id: Path(draft_id))
    monkeypatch.setattr("app.main.discard_draft_files", lambda draft_id: None)

    response = authenticated.post(
        "/api/business-models/drafts/approve-batch",
        json={"draft_ids": ["1-aaaaaaaaaaaa", "1-bbbbbbbbbbbb"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] == 1
    by_id = {item["draft_id"]: item for item in body["results"]}
    assert by_id["1-aaaaaaaaaaaa"]["ok"] is True
    assert by_id["1-bbbbbbbbbbbb"]["ok"] is False


def test_baseline_schedule_roundtrip(authenticated):
    default = authenticated.get("/api/business-models/schedule").json()
    assert default["enabled"] is False and default["generate_time"] == "03:00"

    updated = authenticated.put(
        "/api/business-models/schedule",
        json={"enabled": True, "generate_time": "3:30", "daily_count": 5, "domain_strategy": "gap_hotspot"},
    ).json()
    assert updated["enabled"] is True
    assert updated["generate_time"] == "03:30"  # HH:MM 归一化
    assert updated["daily_count"] == 5

    assert authenticated.get("/api/business-models/schedule").json()["daily_count"] == 5
