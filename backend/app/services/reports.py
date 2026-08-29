from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import DailyReport, ReportSetting, Run, RunRound
from .llm import ExternalServiceError, llm_service, user_api_key
from .notifications import NotificationError, send_wecom
from .semi_kb import semi_kb


METRIC_LABELS = [
    ("classes", "类 Class"),
    ("properties", "属性 Property"),
    ("relations", "关系 Relation"),
    ("individuals", "实例 Individual"),
    ("axioms", "公理 Axiom"),
    ("rules", "推理规则 Rule"),
    ("knowledge_entries", "知识条目"),
    ("business_relations", "经营模型关系"),
    ("simulation_scenarios", "仿真场景"),
    ("scenario_articles", "场景文章"),
]


def fixed_metrics_markdown(snapshot: dict) -> str:
    lines = ["## 一、今日核心数据", "", "| 指标 | 今日新增 | 当前总量 |", "|---|---:|---:|"]
    totals = snapshot["totals"]
    added = snapshot["today_added"]
    for key, label in METRIC_LABELS:
        lines.append(f"| {label} | {int(added.get(key, 0)):,} | {int(totals.get(key, 0)):,} |")
    latest = snapshot.get("latest_round") or {}
    checks = (latest.get("validation") or {}).get("checks") or {}
    lines.extend(["", "## 二、自动校验与交叉验证", "", f"- 内部特征：已接入；最近轮次来源对齐：`{'pass' if (checks.get('source_alignment') or {}).get('passed') else '无通过记录'}`。", f"- vFab：`{totals.get('vfab_state', 'awaiting_source')}`；未提供时不得标记通过。", f"- 最近轮次：`{latest.get('run_id', '无')}` / 第 `{latest.get('round_number', 0)}` 轮 / `{latest.get('status', '无')}`。", f"- OWL/SHACL/推理发布门禁：`{'pass' if (checks.get('full_publish_gate') or checks.get('semantic_precheck') or {}).get('passed') else '无通过记录'}`；经营仿真：`{'pass' if (checks.get('business_simulation') or {}).get('passed') else '无通过记录'}`。", ""])
    return "\n".join(lines)


def validate_report(content: str, snapshot: dict) -> dict:
    errors: list[str] = []
    for key, label in METRIC_LABELS:
        today = f"{int(snapshot['today_added'].get(key, 0)):,}"
        total = f"{int(snapshot['totals'].get(key, 0)):,}"
        if label not in content or today not in content or total not in content:
            errors.append(f"缺少或不一致：{label}")
    required = ["客户痛点", "场景", "交叉验证", "经营", "仿真", "明日"]
    errors.extend(f"缺少章节语义：{term}" for term in required if term not in content)
    if snapshot["totals"].get("vfab_state") == "awaiting_source" and re.search(r"vFab.{0,12}(通过|已验证|确认)", content, re.I):
        errors.append("vFab 未提供却被描述为通过")
    secret_patterns = [r"Bearer\s+[A-Za-z0-9._-]+", r"webhook/send\?key=", r"授权码\s*[:：]\s*\S+"]
    if any(re.search(pattern, content, re.I) for pattern in secret_patterns):
        errors.append("内容可能包含敏感凭据")
    return {"passed": not errors, "errors": errors}


async def generate_report(db: Session, user_id: int, report_date: str, model_id: str) -> DailyReport:
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user_id, DailyReport.report_date == report_date))
    settings_row = db.get(ReportSetting, user_id)
    if settings_row is None:
        settings_row = ReportSetting(user_id=user_id)
        db.add(settings_row)
        db.flush()
    if not report:
        report = DailyReport(user_id=user_id, report_date=report_date)
        db.add(report)
    report.status = "generating"
    report.approval_required = settings_row.approval_required
    db.commit()
    snapshot = await semi_kb.metrics(db, user_id)
    latest_round = db.scalar(select(RunRound).join(Run, Run.id == RunRound.run_id).where(Run.user_id == user_id).order_by(RunRound.completed_at.desc()).limit(1))
    if latest_round:
        snapshot["latest_round"] = {"run_id": latest_round.run_id, "round_number": latest_round.round_number, "status": latest_round.status, "validation": json.loads(latest_round.validation_json or "{}")}
    article = semi_kb.article_text()
    api_key = user_api_key(db, user_id)
    if not api_key:
        report.status = "send_blocked"
        report.validation_json = json.dumps({"passed": False, "errors": ["未配置模型 API Key"]}, ensure_ascii=False)
        db.commit()
        raise ExternalServiceError("未配置模型 API Key，无法生成日报")
    system = "你是半导体知识工程日报编辑。只根据提供的数据写中文Markdown，不改写或编造数字，不虚构vFab验证。场景部分必须包含约200至400字正文、客户痛点、影响、经营模型与仿真状态。"
    user = json.dumps({"date": report_date, "metrics": snapshot, "scenario_source": article[-12000:]}, ensure_ascii=False)
    narrative = await llm_service.complete(api_key, model_id, system, user)
    fixed = fixed_metrics_markdown(snapshot)
    content = f"# 半导体本体与知识库建设日报（{report_date}）\n\n{fixed}\n## 三、业务场景、正文与客户痛点\n\n{narrative.strip()}\n\n## 四、明日重点\n\n- 继续扩充 Fab、FAC、EQP 问题域，并优先处理未覆盖异常和校验失败项。\n"
    validation = validate_report(content, snapshot)
    report.metrics_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    report.model_draft = content
    report.content = content
    report.validation_json = json.dumps(validation, ensure_ascii=False)
    report.generated_at = datetime.now(timezone.utc)
    report.status = "waiting_approval" if validation["passed"] and report.approval_required else ("validating" if validation["passed"] else "send_blocked")
    db.commit()
    if validation["passed"] and not report.approval_required:
        try:
            response = await send_wecom(db, user_id, content, report.id)
        except NotificationError as exc:
            report.status = "send_failed"
            report.send_response_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
            db.commit()
            raise
        report.status = "sent"
        report.sent_at = datetime.now(timezone.utc)
        report.send_response_json = json.dumps(response, ensure_ascii=False)
        db.commit()
    return report


async def send_report(db: Session, user_id: int, report: DailyReport) -> dict:
    snapshot = json.loads(report.metrics_snapshot_json or "{}")
    validation = validate_report(report.content, snapshot)
    report.validation_json = json.dumps(validation, ensure_ascii=False)
    if not validation["passed"]:
        report.status = "send_blocked"
        db.commit()
        raise ValueError("日报校验未通过：" + "；".join(validation["errors"]))
    report.status = "sending"
    db.commit()
    try:
        response = await send_wecom(db, user_id, report.content, report.id)
    except NotificationError:
        report.status = "send_failed"
        db.commit()
        raise
    report.status = "sent"
    report.sent_at = datetime.now(timezone.utc)
    report.send_response_json = json.dumps(response, ensure_ascii=False)
    db.commit()
    return response
