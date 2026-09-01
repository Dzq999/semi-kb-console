from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
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
    ("scenario_articles", "场景知识产物"),
]


def _load_prior_report_plan(db: Session, user_id: int) -> str:
    """取该用户最近一份【早于今天】的日报『明日计划』正文，作为下一轮闭环上下文。"""
    today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    report = db.scalar(
        select(DailyReport).where(DailyReport.user_id == user_id, DailyReport.report_date < today)
        .order_by(DailyReport.report_date.desc()).limit(1)
    )
    if not report or not report.content:
        return ""
    match = re.search(r"##\s*明日计划\s*\n(.+?)(?=\n##\s|\Z)", report.content, re.S)
    return (match.group(1).strip() if match else "")[:2000]


def augment_gap(gap: dict, run_id: str) -> dict:
    """给每轮的 gap 注入反向特征缺口(feature_gap)与昨日日报『明日计划』，形成日–日闭环上下文。

    gap 会随后进入每个子 Agent 的 prompt（且幂等 hash 已 fold gap），从而让模型看到
    昨天的缺口并逐轮自动补齐。读文件/DB 失败都吞掉，绝不影响主流程。
    """
    try:
        gap["feature_gap"] = semi_kb.feature_gap()
    except Exception:
        pass
    try:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                plan = _load_prior_report_plan(db, run.user_id)
                if plan:
                    gap["prior_report_plan"] = plan
            # 上一轮成功收尾写下的结构化优化方向：只有 completed* 轮次才有非空 next_direction_json，
            # 当前 running 轮仍是默认 "{}"，故按 round_number 倒序取到的即上一轮方向，形成显式闭环。
            prior = db.scalar(
                select(RunRound).where(
                    RunRound.run_id == run_id,
                    RunRound.next_direction_json.is_not(None),
                    RunRound.next_direction_json != "{}",
                ).order_by(RunRound.round_number.desc()).limit(1)
            )
            if prior:
                try:
                    direction = json.loads(prior.next_direction_json or "{}")
                except json.JSONDecodeError:
                    direction = {}
                if direction:
                    gap["prior_round_direction"] = direction
    except Exception:
        pass
    return gap


def compute_round_direction(gap: dict, validation: dict | None = None, delta: dict | None = None) -> dict:
    """一轮成功收尾 → 产出面向下一轮的结构化优化方向（确定性启发式，非 LLM 反思）。

    输入：本轮的 feature_gap（缺口/热点）、validation.checks（哪些门禁未通过）、指标增量 delta。
    输出：结构化 dict（含人类可读 text），写入 RunRound.next_direction_json，
    由下一轮 augment_gap 读回注入 gap["prior_round_direction"]，进入每个子 Agent 的 prompt。
    纯函数、不触库、不抛异常——坏输入一律降级为通用方向。
    """
    gap = gap or {}
    validation = validation or {}
    delta = delta or {}
    total = int(gap.get("unmapped_total") or 0)
    business = int(gap.get("business_relevant") or 0)
    themes = [str(item.get("theme")) for item in (gap.get("by_theme") or [])[:3] if item.get("theme")]
    checks = validation.get("checks") or {}
    unpassed = [
        name for name in ("source_alignment", "business_simulation", "semantic_precheck", "full_publish_gate")
        if name in checks and not (checks.get(name) or {}).get("passed")
    ]
    grew = {key: int(value) for key, value in delta.items() if isinstance(value, (int, float)) and value > 0}
    bits: list[str] = []
    if total:
        bits.append(f"反向特征缺口 {total} 项（业务相关 {business}）待收敛")
    if themes:
        bits.append("优先补齐热点主题「" + "、".join(themes) + "」")
    if unpassed:
        bits.append("下一轮需复跑未通过门禁：" + "、".join(unpassed))
    text = "；".join(bits) if bits else "本体缺口已收敛，下一轮维持既有映射并扩充经营基线覆盖。"
    return {
        "unmapped_total": total,
        "business_relevant": business,
        "focus_themes": themes,
        "unpassed_gates": unpassed,
        "grew": grew,
        "text": text,
    }


def _optimization_direction(snapshot: dict) -> str:
    """『明日计划』动态优化方向：体现反向特征缺口的自动下降趋势，无数据时回退静态话。"""
    gap = snapshot.get("feature_gap") or {}
    total = int(gap.get("unmapped_total") or 0)
    if not total:
        return "- 继续补齐 Fab、FAC、EQP 的高价值缺口，并优先处理未通过校验项。"
    business = int(gap.get("business_relevant") or 0)
    latest = snapshot.get("latest_round") or {}
    accepted = ((latest.get("validation") or {}).get("accepted_candidates") or {}).get("mappings")
    added = f"，今日自动补齐 {int(accepted)} 条映射" if isinstance(accepted, int) and accepted > 0 else ""
    themes = "、".join(f"{item.get('theme')}({item.get('count')})" for item in (gap.get("by_theme") or [])[:2] if item.get("theme"))
    hot = f"；热点主题「{themes}」将在后续轮次继续自动补全" if themes else ""
    return f"- 优化方向：反向特征缺口 {total} 项（业务相关 {business}）{added}{hot}，随本体逐轮成熟自动收敛。"


def _cross_validation_section(snapshot: dict) -> str:
    """『交叉验证结果』小节：面向领导，只写【今日新增】与【总体】的命中/覆盖，不写单轮数据。

    今日新增取自 today_added（当日跨轮汇总），总体命中/覆盖取自 feature_gap（累计），
    门禁状态取自 latest_round.validation.checks 但只呈现当前是否通过、不带轮次标签。
    全程只读、无副作用；缺数据时回退中性文案，绝不把缺失写成“通过”。
    """
    gap = snapshot.get("feature_gap") or {}
    added = snapshot.get("today_added") or {}
    checks = ((snapshot.get("latest_round") or {}).get("validation") or {}).get("checks") or {}
    # 今日新增本体项：当日汇总、非单轮；本体为跳变式指标，多数日零新增属正常。
    dims = [("classes", "类"), ("properties", "属性"), ("relations", "关系"),
            ("individuals", "实例"), ("axioms", "公理"), ("rules", "推理规则")]
    grew = [f"{label} +{int(added.get(key, 0))}" for key, label in dims if int(added.get(key, 0)) > 0]
    today_line = "、".join(grew) if grew else "无（本体为跳变式指标，多数日零新增属正常）"
    # 总体命中/覆盖：property_mapping_coverage 即源特征映射到本体的累计命中率。
    coverage = gap.get("coverage_percent")
    cov = f"{coverage}%" if isinstance(coverage, (int, float)) else "—"
    total = int(gap.get("unmapped_total") or 0)
    business = int(gap.get("business_relevant") or 0)
    themes = "、".join(f"{item.get('theme')}({item.get('count')})" for item in (gap.get("by_theme") or [])[:2] if item.get("theme"))
    hot = f"，热点主题「{themes}」" if themes else ""
    src = "通过" if (checks.get("source_alignment") or {}).get("passed") else "暂无通过记录"
    sim = "通过" if (checks.get("business_simulation") or {}).get("passed") else "暂无通过记录"
    return "\n".join([
        "## 交叉验证结果",
        f"- 今日新增本体项：{today_line}（当日经引擎门禁交叉验证后纳入）。",
        f"- 本体-源覆盖率（总体）：`{cov}`（源特征映射到本体的累计命中率）。",
        f"- 反向特征缺口（总体）：{total} 项（业务相关 {business}）{hot}。",
        f"- 交叉验证门禁：来源对齐 `{src}`；经营仿真 `{sim}`。",
    ])


def fixed_metrics_markdown(snapshot: dict) -> str:
    lines = ["## 今日结果", "", "| 指标 | 今日新增 | 当前总量 |", "|---|---:|---:|"]
    totals = snapshot["totals"]
    added = snapshot["today_added"]
    for key, label in METRIC_LABELS:
        lines.append(f"| {label} | {int(added.get(key, 0)):,} | {int(totals.get(key, 0)):,} |")
    latest = snapshot.get("latest_round") or {}
    checks = (latest.get("validation") or {}).get("checks") or {}
    # 内部特征状态不再写死。align_sources.py 通过即代表内部特征模型已接入并映射到本体；
    # candidate_source_alignment（发布轮次才有）能进一步给出命中数，有就一并展示。
    aligned = bool((checks.get("source_alignment") or {}).get("passed"))
    feature_state = "已接入" if aligned else "暂无通过记录"
    gate_passed = (checks.get("full_publish_gate") or checks.get("semantic_precheck") or {}).get("passed")
    lines.extend(["", _cross_validation_section(snapshot), "", "## 质量与验证", f"- 内部特征：`{feature_state}`；来源对齐：`{'通过' if aligned else '暂无通过记录'}`。", f"- vFab：`{totals.get('vfab_state', 'awaiting_source')}`（未提供时不标记为通过）。", f"- 校验门禁：OWL/SHACL/推理 `{'通过' if gate_passed else '暂无通过记录'}`；经营仿真 `{'通过' if (checks.get('business_simulation') or {}).get('passed') else '暂无通过记录'}`。", "", "## 明日计划", _optimization_direction(snapshot)])
    return "\n".join(lines)


def validate_report(content: str, snapshot: dict) -> dict:
    errors: list[str] = []
    for key, label in METRIC_LABELS:
        today = f"{int(snapshot['today_added'].get(key, 0)):,}"
        total = f"{int(snapshot['totals'].get(key, 0)):,}"
        if label not in content or today not in content or total not in content:
            errors.append(f"缺少或不一致：{label}")
    required = ["今日结果", "交叉验证", "质量与验证", "明日计划", "经营", "仿真"]
    errors.extend(f"缺少章节语义：{term}" for term in required if term not in content)
    if "业务进展摘要" in content:
        errors.append("不应包含业务进展摘要章节")
    if "场景文章" in content:
        errors.append("请将场景文章统一称为场景知识产物")
    if snapshot["totals"].get("vfab_state") == "awaiting_source" and re.search(r"vFab.{0,12}(通过|已验证|确认)", content, re.I):
        errors.append("vFab 未提供却被描述为通过")
    secret_patterns = [r"Bearer\s+[A-Za-z0-9._-]+", r"webhook/send\?key=", r"授权码\s*[:：]\s*\S+"]
    if any(re.search(pattern, content, re.I) for pattern in secret_patterns):
        errors.append("内容可能包含敏感凭据")
    if len(content) > 4000:
        errors.append("日报过长，应控制为领导结果摘要")
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
    # 只挑真正跑过校验的轮次。running/cancelled/failed 轮次的 validation_json 是空校验，
    # 若按 completed_at 直接取最新，会被这些空轮次盖掉，导致“来源对齐/OWL/仿真”全部
    # 误报“暂无通过记录”——与 metrics() 里“今日新增”采用的同一批收尾状态保持一致。
    latest_round = db.scalar(
        select(RunRound).join(Run, Run.id == RunRound.run_id)
        .where(Run.user_id == user_id, RunRound.status.in_(("completed", "completed_partial", "completed_no_change")))
        .order_by(RunRound.completed_at.desc()).limit(1)
    )
    if latest_round:
        snapshot["latest_round"] = {"run_id": latest_round.run_id, "round_number": latest_round.round_number, "status": latest_round.status, "validation": json.loads(latest_round.validation_json or "{}")}
    api_key = user_api_key(db, user_id)
    if not api_key:
        report.status = "send_blocked"
        report.validation_json = json.dumps({"passed": False, "errors": ["未配置模型 API Key"]}, ensure_ascii=False)
        db.commit()
        raise ExternalServiceError("未配置模型 API Key，无法生成日报")
    system = (
        "你是给公司领导写日报结论的编辑。只输出不超过180字的中文Markdown项目符号，最多3条，补充今日结果中的关键风险或结论。"
        "不要输出任何章节标题，不要复述指标表，不复述技术过程，不编造数字，不虚构vFab验证。"
        "公理、经营模型关系等属跳变式指标：仅在新增本体公理或发布新经营基线时才增长，绝大多数轮次零新增属正常，"
        "禁止把‘零新增’写成风险、问题或需要改进项。覆盖率只在低于100%时才作为缺口提示，达到100%视为达标、不必强调。"
        "全文禁止使用‘场景文章’和‘业务进展摘要’，统一使用‘场景知识产物’。"
        "禁止使用‘本轮/这一轮/上一轮/每轮’等轮次措辞——一天可能跑多轮，面向领导只用‘今日/当前/总体’口径，领导视角不关心单轮。"
    )
    user = json.dumps({"date": report_date, "metrics": snapshot}, ensure_ascii=False)
    narrative = await llm_service.complete(api_key, model_id, system, user)
    narrative = narrative.replace("场景文章", "场景知识产物").replace("业务进展摘要", "")
    # 兜底：模型偶尔仍漏出轮次口径，统一改写为“今日”，与上面的措辞替换并列。
    narrative = re.sub(r"本轮|这一轮|上一轮|每一轮|每轮", "今日", narrative)
    narrative = re.sub(r"^\s*#+\s*.*$", "", narrative, flags=re.MULTILINE).strip()
    fixed = fixed_metrics_markdown(snapshot)
    if narrative:
        fixed = fixed.replace("\n## 交叉验证结果\n", f"\n{narrative}\n\n## 交叉验证结果\n", 1)
    content = f"# SEMI-KB 日报｜{report_date}\n\n{fixed}\n"
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
