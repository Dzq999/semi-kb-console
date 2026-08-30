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
