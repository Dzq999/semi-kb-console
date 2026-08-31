from __future__ import annotations

from app.services.reports import fixed_metrics_markdown, validate_report


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
    assert "内部特征：`已接入（本轮命中 12/20）`" in content
    assert "来源对齐：`通过`" in content
    assert "暂无通过记录" not in content


def test_internal_feature_not_faked_when_no_validated_round():
    # 复现被修的 bug：没有已完成校验的轮次时，内部特征不能再写死“已接入”。
    content = fixed_metrics_markdown(snapshot())  # snapshot() 无 latest_round
    assert "内部特征：`暂无通过记录`" in content
    assert "来源对齐：`暂无通过记录`" in content
    assert "已接入" not in content
