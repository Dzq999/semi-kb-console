from __future__ import annotations

import json

import pytest

from app.services.reports import (
    augment_gap,
    compute_round_direction,
    fixed_metrics_markdown,
    validate_report,
)


def snapshot():
    totals = {"classes": 10, "properties": 20, "relations": 30, "individuals": 40, "axioms": 5, "rules": 6, "knowledge_entries": 7, "business_relations": 8, "simulation_scenarios": 9, "scenario_articles": 2, "vfab_state": "awaiting_source"}
    added = {key: 1 for key in totals if key != "vfab_state"}
    return {"totals": totals, "today_added": added}


def test_metrics_table_contains_today_and_total():
    content = fixed_metrics_markdown(snapshot())
    assert "今日新增" in content
    assert "当前总量" in content
    assert "| 类 Class | 1 | 10 |" in content
    assert "## 今日结果" in content
    assert "## 质量与验证" in content
    assert "## 明日计划" in content
    assert "## 今日关键结果" not in content
    assert "## 验证与需关注事项" not in content
    assert "## 明日重点" not in content
    assert "场景知识产物" in content
    assert "场景文章" not in content


def test_report_rejects_legacy_article_label_and_business_summary():
    content = fixed_metrics_markdown(snapshot()) + "\n## 业务进展摘要\n场景文章"
    result = validate_report(content, snapshot())
    assert not result["passed"]
    assert any("业务进展摘要" in item for item in result["errors"])
    assert any("场景文章" in item for item in result["errors"])


def test_report_blocks_false_vfab_pass():
    content = fixed_metrics_markdown(snapshot()) + "\n客户痛点 场景 交叉验证 经营 仿真 明日\nvFab 已验证通过"
    result = validate_report(content, snapshot())
    assert not result["passed"]
    assert any("vFab" in item for item in result["errors"])


def test_report_accepts_complete_content():
    content = fixed_metrics_markdown(snapshot()) + "\n场景正文 客户痛点 交叉验证 经营模型 仿真结果 明日重点"
    assert validate_report(content, snapshot())["passed"]


def test_internal_feature_reflects_alignment_not_a_constant():
    # 通过校验的轮次：内部特征与来源对齐都应显示真实“通过/已接入”，命中数一并展示。
    passed = snapshot()
    passed["latest_round"] = {"round_number": 7, "status": "completed", "validation": {"checks": {
        "source_alignment": {"passed": True},
        "candidate_source_alignment": {"internal_supported": 12, "terms_checked": 20},
        "full_publish_gate": {"passed": True},
        "business_simulation": {"passed": True},
    }}}
    content = fixed_metrics_markdown(passed)
    assert "内部特征：`已接入`" in content
    assert "来源对齐：`通过`" in content
    assert "暂无通过记录" not in content
    # 用户要求：日报不再出现单轮/最近轮次口径。
    assert "本轮" not in content
    assert "最近轮次" not in content


def test_internal_feature_not_faked_when_no_validated_round():
    # 复现被修的 bug：没有已完成校验的轮次时，内部特征不能再写死“已接入”。
    content = fixed_metrics_markdown(snapshot())  # snapshot() 无 latest_round
    assert "内部特征：`暂无通过记录`" in content
    assert "来源对齐：`暂无通过记录`" in content
    assert "已接入" not in content


def test_optimization_direction_reflects_feature_gap_and_auto_progress():
    # 反向特征缺口有数据时，“明日计划”应体现自动补齐趋势而非写死话术。
    snap = snapshot()
    snap["feature_gap"] = {
        "unmapped_total": 700, "business_relevant": 120,
        "by_theme": [{"theme": "运维域", "count": 30, "samples": ["巡检项"]}, {"theme": "组织域", "count": 12, "samples": ["班组"]}],
        "top_suspected": [],
    }
    snap["latest_round"] = {"round_number": 8, "status": "completed", "validation": {"accepted_candidates": {"mappings": 3}}}
    content = fixed_metrics_markdown(snap)
    assert "优化方向" in content
    assert "反向特征缺口 700 项" in content
    assert "业务相关 120" in content
    assert "今日自动补齐 3 条映射" in content
    assert "运维域(30)" in content
    assert "继续补齐 Fab、FAC、EQP" not in content


def test_optimization_direction_falls_back_without_feature_gap():
    # 没有 feature_gap 数据时回退为原静态话术，日报仍合法。
    content = fixed_metrics_markdown(snapshot())
    assert "继续补齐 Fab、FAC、EQP" in content
    assert "## 明日计划" in content


def test_augment_gap_injects_feature_gap_and_prior_plan(clean_db):
    # 闭环上下文：每轮的 gap 应被注入真实反向缺口 + 昨日日报『明日计划』。
    from app.db import SessionLocal
    from app.models import DailyReport, Run, User

    with SessionLocal() as db:
        user = User(username="closer", password_hash="x")
        db.add(user)
        db.flush()
        db.add(Run(id="run-closed-loop", user_id=user.id, model_id="gpt-test"))
        db.add(DailyReport(
            user_id=user.id, report_date="2000-01-01",
            content="# 日报\n\n## 明日计划\n- 优化方向：反向特征缺口 742 项，随本体逐轮成熟自动收敛。\n",
        ))
        db.commit()
        run_id, user_id = "run-closed-loop", user.id

    gap = augment_gap({}, run_id)
    assert gap["feature_gap"]["unmapped_total"] > 0
    assert "反向特征缺口 742 项" in gap["prior_report_plan"]
    assert "# 日报" not in gap["prior_report_plan"]  # 仅截取『明日计划』节


def test_augment_gap_is_defensive_on_unknown_run(clean_db):
    # run 不存在时不得抛错，且仍注入 feature_gap（反向缺口不依赖 DB）。
    gap = augment_gap({}, "run-does-not-exist")
    assert "feature_gap" in gap
    assert "prior_report_plan" not in gap


def test_cross_validation_section_reports_today_added_and_overall():
    # 领导视角小节：只写今日新增本体项 + 总体覆盖率/反向缺口 + 门禁，绝不写单轮命中数。
    snap = snapshot()
    snap["feature_gap"] = {
        "unmapped_total": 741, "business_relevant": 120, "coverage_percent": 21.8,
        "by_theme": [{"theme": "运维域", "count": 30}, {"theme": "组织域", "count": 12}],
    }
    snap["latest_round"] = {"round_number": 7, "status": "completed", "validation": {
        "checks": {
            "source_alignment": {"passed": True},
            "business_simulation": {"passed": True},
            "candidate_source_alignment": {"internal_supported": 12, "terms_checked": 20},
        },
        "accepted_candidates": {"mappings": 3},
    }}
    content = fixed_metrics_markdown(snap)
    assert "## 交叉验证结果" in content
    assert "今日新增本体项：类 +1" in content
    assert "本体-源覆盖率（总体）：`21.8%`" in content
    assert "反向特征缺口（总体）：741 项（业务相关 120）" in content
    assert "运维域(30)" in content
    assert "交叉验证门禁：来源对齐 `通过`；经营仿真 `通过`" in content
    # 单轮命中数不得出现在交叉验证结果里。
    assert "命中 12/20" not in content
    assert "本轮" not in content
    assert "最近轮次" not in content


def test_cross_validation_section_falls_back_without_round():
    # 无发布轮次 / 无 feature_gap 时：总体覆盖率回退 —、门禁回退中性，绝不写成“通过”。
    content = fixed_metrics_markdown(snapshot())
    assert "## 交叉验证结果" in content
    assert "本体-源覆盖率（总体）：`—`" in content
    assert "反向特征缺口（总体）：0 项（业务相关 0）" in content
    assert "交叉验证门禁：来源对齐 `暂无通过记录`；经营仿真 `暂无通过记录`" in content


# --------------------------------------------------------------------------- 闭环：每轮优化方向

def test_compute_round_direction_summarizes_gap_and_unpassed_gates():
    gap = {
        "unmapped_total": 700, "business_relevant": 120,
        "by_theme": [{"theme": "运维域"}, {"theme": "组织域"}, {"theme": "良率"}, {"theme": "被截断"}],
    }
    validation = {"checks": {
        "source_alignment": {"passed": True},
        "business_simulation": {"passed": False},
    }}
    direction = compute_round_direction(gap, validation, delta={"classes": 2, "relations": 0})
    assert direction["unmapped_total"] == 700 and direction["business_relevant"] == 120
    assert direction["focus_themes"] == ["运维域", "组织域", "良率"]  # 只取前 3
    assert direction["unpassed_gates"] == ["business_simulation"]
    assert direction["grew"] == {"classes": 2}  # 只记正增量
    assert "运维域" in direction["text"] and "business_simulation" in direction["text"]


def test_compute_round_direction_degrades_on_empty_inputs():
    direction = compute_round_direction({}, None, None)
    assert direction["focus_themes"] == [] and direction["unpassed_gates"] == []
    assert direction["text"]  # 兜底文案非空


def test_augment_gap_injects_prior_round_direction(clean_db):
    # 闭环：上一轮成功收尾写下 next_direction_json，下一轮 augment_gap 应读回注入。
    from app.db import SessionLocal
    from app.models import Run, RunRound, User

    with SessionLocal() as db:
        user = User(username="looper", password_hash="x")
        db.add(user)
        db.flush()
        db.add(Run(id="run-direction", user_id=user.id, model_id="gpt-test"))
        # 上一轮：已完成，带方向。当前 running 轮：默认 "{}"，不得被误读。
        db.add(RunRound(run_id="run-direction", round_number=1, status="completed",
                        next_direction_json=json.dumps({"text": "优先补齐热点主题「运维域」", "focus_themes": ["运维域"]}, ensure_ascii=False)))
        db.add(RunRound(run_id="run-direction", round_number=2, status="running", next_direction_json="{}"))
        db.commit()

    gap = augment_gap({}, "run-direction")
    assert gap["prior_round_direction"]["focus_themes"] == ["运维域"]
    assert "运维域" in gap["prior_round_direction"]["text"]


@pytest.mark.asyncio
async def test_generate_report_strips_round_words(clean_db, monkeypatch):
    # narrative 兜底：模型漏出「本轮/这一轮」等轮次口径 → 正文统一改写为「今日」。
    from app.db import SessionLocal
    from app.models import User
    from app.services import reports

    with SessionLocal() as db:
        db.add(User(id=1, username="editor", password_hash="x"))
        db.commit()

    async def fake_metrics(db=None, user_id=None):
        return snapshot()

    async def fake_complete(*args, **kwargs):
        return "- 本轮新增映射3条，这一轮门禁全通过；上一轮的缺口已收敛。"

    monkeypatch.setattr(reports.semi_kb, "metrics", fake_metrics)
    monkeypatch.setattr(reports.llm_service, "complete", fake_complete)
    monkeypatch.setattr(reports, "user_api_key", lambda db, user_id: "test-key")

    with SessionLocal() as db:
        report = await reports.generate_report(db, 1, "2026-09-01", "gpt-test")

    assert "本轮" not in report.content
    assert "这一轮" not in report.content
    assert "上一轮" not in report.content
    assert "今日" in report.content

