"""独立经营模型协作 Agent：人触发起草『完整三件套基线』(template+dataset+model)。

与全自动闭环的信任模型不同——闭环内 Agent 被 sanitizer 禁止自造 template/dataset，只能在
已批准的 (template,dataset) 组合上复用；本模块是另一种信任模型：人触发 LLM 起草完整基线 →
引擎真实门禁校验 → 人点『采纳为基线』才落盘（promote_business_draft）。这不绕过人工把关，
而是加速『给系统加基线/校验资料』这件人本该做的事，即 Palantir 式治理化本体演化
（提案→人工审批→晋升）。

草案落在 business/drafts/<id>/，对全库门禁 / 浏览路由 / 闭环 approved-pair 扫描天然不可见
（validate_project 只 glob business/models/*.yaml，非递归），隔离免费。
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BusinessDraft, User
from .llm import ExternalServiceError, llm_service, user_api_key
from .orchestrator import _json_object
from .semi_kb import SemiKbError, semi_kb


DRAFT_ID_RE = re.compile(r"^[0-9]+-[0-9a-f]{12}$")
_DRAFTS_ROOT = "business/drafts"

# 定时自动起草时，缺口不足或读取失败的兜底通用意图（覆盖成本/产能/利润三条主线）。
_FALLBACK_INTENTS = [
    "评估晶圆制造产线的成本-产能-利润基线，覆盖良率、固定成本与单位成本对利润的敏感性。",
    "评估设备综合效率(OEE)对有效产能与单位成本的影响基线，识别产能瓶颈环节。",
    "评估工艺良率波动对可售产出与收入的传导基线，量化良率改善的经营收益。",
]

# simulation_algorithms.py 的算法目录在后端做常量镜像（后端不能 in-process import 引擎模块），
# 与引擎有同步风险——新增/改算法时需同步这里。只用于给 LLM 起草时的可用算法说明。
ALGORITHMS_CATALOG = [
    ("yield.cascade", "各道良率(ratio, 每个∈[0,1])连乘得总良率(ratio)；≥1 个输入"),
    ("capacity.bottleneck", "多个产能取最小值(瓶颈)；≥1 个输入"),
    ("production.saleable_output", "inputs=[投入量, 良率] → 投入×良率=可售产出；顺序敏感"),
    ("production.oee_capacity", "inputs=[理论速率, 可用时间, 性能, 质量] → 四者相乘=有效产能"),
    ("finance.revenue", "inputs=[产量, 单价] → 乘积=收入"),
    ("cost.variable_total", "inputs=[产量, 单位可变成本] → 乘积=可变成本合计"),
    ("cost.unit", "inputs=[成本合计, 产量] → 成本合计/产量=单位成本；顺序敏感"),
    ("finance.roi", "inputs=[收益, 投入] → 收益/投入=ROI"),
    ("finance.payback", "inputs=[投入, 每期收益] → 投入/每期收益=回收期"),
]

# 引擎 SOURCES 白名单(simulate.py)。可核查来源(observed/internal_feature/vfab/web)需 source_ref，
# 起草阶段无现场数据，一律用 assumption；由人在审批时判断数值假设是否合理。
_ALLOWED_SOURCES = {"observed", "internal_feature", "vfab", "assumption", "model_prior", "web", "human"}
_DEFAULT_SOURCE = "assumption"

_EXAMPLE = {
    "summary": "某产线单月成本-产能-利润基线，用于评估良率与固定成本变化对利润的影响。",
    "template": {"name": "某产线经营骨架", "extends": ["business/templates/manufacturing.yaml"]},
    "dataset": {
        "description": "情景假设数据，替换为现场数据后方可用于决策。",
        "values": [
            {"id": "input_units", "value": 120000, "unit": "unit/month", "source": "assumption"},
            {"id": "capacity_limit", "value": 130000, "unit": "unit/month", "source": "assumption"},
            {"id": "process_yield", "value": 0.94, "unit": "ratio", "source": "assumption"},
            {"id": "selling_price", "value": 480, "unit": "CNY/unit", "source": "assumption"},
            {"id": "variable_unit_cost", "value": 300, "unit": "CNY/unit", "source": "assumption"},
            {"id": "fixed_cost", "value": 8000000, "unit": "CNY/month", "source": "assumption"},
            {"id": "intervention_opex", "value": 0, "unit": "CNY/month", "source": "assumption"},
        ],
    },
    "model": {
        "name": "某产线经营基线", "domain": "manufacturing", "period": "month", "currency": "CNY",
        "outputs": ["constrained_input", "saleable_units", "revenue", "variable_cost", "profit", "unit_cost"],
    },
}


def _draft_dir(draft_id: str) -> Path:
    if not DRAFT_ID_RE.match(draft_id or ""):
        raise SemiKbError(f"非法草案 ID：{draft_id}")
    return settings.engine_root / "business" / "drafts" / draft_id


def draft_dir_path(draft_id: str) -> Path:
    """校验 ID 后返回草案 staging 目录，且要求目录仍存在（供晋升前复核）。"""
    draft_dir = _draft_dir(draft_id)
    if not draft_dir.is_dir():
        raise SemiKbError(f"草案目录不存在：{draft_id}")
    return draft_dir


def _base_template_summary() -> str:
    """把线上 manufacturing 基座的变量/计算读出来给 LLM，作为最稳妥的 extends 目标。"""
    path = settings.engine_root / "business" / "templates" / "manufacturing.yaml"
    try:
        template = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("template") or {}
    except (OSError, yaml.YAMLError):
        return ""
    variables = "、".join(f"{v.get('id')}({v.get('unit')})" for v in template.get("variables") or [])
    calcs = "、".join(str(c.get("id")) for c in template.get("calculations") or [])
    return f"基座 business/templates/manufacturing.yaml 变量：{variables}；计算(可作 outputs)：{calcs}"


def _build_prompt(intent: str, domain: str) -> tuple[str, str]:
    algorithms = "\n".join(f"  - {name}：{desc}" for name, desc in ALGORITHMS_CATALOG)
    system = (
        "你是经营模型基线的起草助手。基于用户意图，产出一套【完整三件套基线】草案，"
        "只输出一个 JSON 对象，不要任何解释或 Markdown 代码块。JSON 结构：\n"
        '{"summary": "一句话说明这套基线刻画的经营场景",\n'
        ' "template": {"name": "...", "extends": ["business/templates/manufacturing.yaml"],'
        ' "variables": [可选，额外变量 {id, unit, required:false}],'
        ' "calculations": [可选，额外计算 {id, unit, algorithm 或 formula, inputs:[...]}]},\n'
        ' "dataset": {"description": "...", "values": [{id, value, unit, source}]},\n'
        ' "model": {"name": "...", "domain": "...", "period": "month", "currency": "CNY", "outputs": [已定义变量 id]}}\n\n'
        "硬规则(违反必被引擎门禁拒绝)：\n"
        "1. 最稳妥做法：template 仅 extends 上述基座、不新增 calculations；在 dataset 里给全基座"
        "的 7 个基础变量赋值(input_units, capacity_limit, process_yield, selling_price, "
        "variable_unit_cost, fixed_cost, intervention_opex)；outputs 从基座计算项中选。\n"
        "2. 每个 dataset value 必须含 id/value/unit/source；source 一律用 assumption(起草阶段无现场数据)。\n"
        "3. 单位需与模板一致：ratio 型取值必须∈[0,1]；非 ratio 取值≥0。\n"
        "4. outputs 里每一项都必须是已定义的变量 id(基座变量或计算，或你新增的变量/计算)。\n"
        "5. 【禁止】输出 ontology_ref 字段(乱填必挂)。\n"
        "6. 若新增 calculations，algorithm 只能取下列之一，inputs 顺序按说明：\n" + algorithms + "\n"
        "id 由服务端统一生成，你无需提供 template/dataset/model 的 id。"
    )
    user = json.dumps(
        {
            "intent": intent,
            "domain": domain,
            "base_template": _base_template_summary(),
            "example_output": _EXAMPLE,
        },
        ensure_ascii=False,
    )
    return system, user


def _normalize_values(values: object) -> list[dict]:
    result: list[dict] = []
    for item in values or []:
        if not isinstance(item, dict) or "id" not in item:
            continue
        entry = {"id": str(item["id"]), "value": item.get("value"), "unit": str(item.get("unit") or "")}
        source = str(item.get("source") or "").strip()
        entry["source"] = source if source in _ALLOWED_SOURCES else _DEFAULT_SOURCE
        result.append(entry)
    return result


def _assemble_documents(parsed: dict, draft_id: str, domain: str, token: str) -> dict:
    """把 LLM 输出组装成三份带 schema_version 的文档；id 由服务端强制生成、来源缺省 assumption。"""
    slug = (re.sub(r"[^a-z0-9]+", "-", (domain or "model").lower()).strip("-")[:24]) or "model"
    template_id = f"template.human.{slug}.{token}"
    dataset_id = f"dataset.human.{slug}.{token}"
    model_id = f"business.human.{slug}.{token}"

    raw_template = parsed.get("template") if isinstance(parsed.get("template"), dict) else {}
    template_body: dict = {
        "id": template_id,
        "name": str(raw_template.get("name") or f"{slug} 经营骨架"),
        "extends": raw_template.get("extends") or ["business/templates/manufacturing.yaml"],
    }
    if raw_template.get("variables"):
        template_body["variables"] = raw_template["variables"]
    if raw_template.get("calculations"):
        template_body["calculations"] = raw_template["calculations"]

    raw_dataset = parsed.get("dataset") if isinstance(parsed.get("dataset"), dict) else {}
    dataset_body = {
        "id": dataset_id,
        "description": str(raw_dataset.get("description") or "情景假设数据，替换为现场数据后方可用于决策。"),
        "values": _normalize_values(raw_dataset.get("values")),
    }

    raw_model = parsed.get("model") if isinstance(parsed.get("model"), dict) else {}
    model_body = {
        "id": model_id,
        "name": str(raw_model.get("name") or f"{slug} 经营基线"),
        "domain": str(raw_model.get("domain") or domain or "manufacturing"),
        "period": str(raw_model.get("period") or "month"),
        "currency": str(raw_model.get("currency") or "CNY"),
        "template_ref": f"{_DRAFTS_ROOT}/{draft_id}/template.yaml",
        "dataset_ref": f"{_DRAFTS_ROOT}/{draft_id}/dataset.yaml",
        "outputs": [str(item) for item in (raw_model.get("outputs") or [])],
    }
    return {
        "template": {"schema_version": "2.0", "template": template_body},
        "dataset": {"schema_version": "2.0", "dataset": dataset_body},
        "model": {"schema_version": "2.0", "model": model_body},
    }


async def draft_business_baseline(db: Session, user: User, intent: str, domain: str, model_id: str) -> dict:
    """LLM 起草三件套 → 落 staging → 引擎门禁校验 → 存 DB 行，返回完整响应。"""
    api_key = user_api_key(db, user.id)
    if not api_key:
        raise ExternalServiceError("未配置模型 API Key，无法起草经营基线")
    system, user_prompt = _build_prompt(intent, domain)
    raw = await llm_service.complete(api_key, model_id, system, user_prompt, temperature=0.2)
    parsed = _json_object(raw)

    draft_id = f"{user.id}-{uuid.uuid4().hex[:12]}"
    token = uuid.uuid4().hex[:8]
    documents = _assemble_documents(parsed, draft_id, domain, token)
    summary = str(parsed.get("summary") or "")[:1000]

    draft_dir = _draft_dir(draft_id)
    draft_dir.mkdir(parents=True, exist_ok=True)
    for name, key in (("template.yaml", "template"), ("dataset.yaml", "dataset"), ("model.yaml", "model")):
        (draft_dir / name).write_text(
            yaml.safe_dump(documents[key], allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    meta = {
        "draft_id": draft_id, "user_id": user.id, "intent": intent, "domain": domain,
        "llm_model_id": model_id, "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (draft_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    validation = await semi_kb.validate_business_draft(draft_dir / "model.yaml")
    status = "validated" if validation.get("passed") else "invalid"

    row = BusinessDraft(
        id=draft_id, user_id=user.id, status=status, intent=intent, domain=domain,
        llm_model_id=model_id, summary=summary, validation_json=json.dumps(validation, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    return {
        "draft_id": draft_id, "status": status, "summary": summary,
        "template": documents["template"], "dataset": documents["dataset"], "model": documents["model"],
        "validation": validation, "created_at": meta["created_at"],
    }


def load_draft_documents(draft_id: str) -> dict:
    """详情视图：从磁盘重读三件套原文 + meta（引擎真源）。"""
    draft_dir = _draft_dir(draft_id)
    if not draft_dir.is_dir():
        raise SemiKbError(f"草案目录不存在：{draft_id}")

    def _read_yaml(name: str) -> dict:
        path = draft_dir / name
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}

    meta_path = draft_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    return {
        "draft_id": draft_id, "meta": meta,
        "template": _read_yaml("template.yaml"),
        "dataset": _read_yaml("dataset.yaml"),
        "model": _read_yaml("model.yaml"),
    }


def discard_draft_files(draft_id: str) -> None:
    """丢弃草案：删除 staging 目录（幂等）。"""
    draft_dir = _draft_dir(draft_id)
    if draft_dir.is_dir():
        shutil.rmtree(draft_dir, ignore_errors=True)


def derive_intents_from_gap(count: int) -> list[tuple[str, str]]:
    """从反向特征缺口的热点主题派生 count 条 (intent, domain)，供定时自动起草使用。

    读 semi_kb.feature_gap() 的 by_theme（形如 [{theme, count, samples}]，与
    reports._optimization_direction 同一读法）：取前 count 个热点主题，把主题名与示例
    特征拼进意图，让 LLM 起草时聚焦该域尚未映射的业务相关缺口。domain 一律返回
    "manufacturing"——线上唯一的经营基座是 business/templates/manufacturing.yaml，
    _build_prompt 的硬规则也围绕它展开；主题只驱动 intent 文案，不改 domain 以免 slug
    漂移或触发门禁。热点不足或读取失败时用 _FALLBACK_INTENTS 通用意图补足。只读、不抛。
    """
    count = max(1, min(10, int(count)))
    intents: list[tuple[str, str]] = []
    try:
        by_theme = (semi_kb.feature_gap() or {}).get("by_theme") or []
    except Exception:
        by_theme = []
    for item in by_theme:
        if len(intents) >= count:
            break
        theme = str((item or {}).get("theme") or "").strip()
        if not theme:
            continue
        samples = "、".join(str(s) for s in ((item or {}).get("samples") or [])[:5] if s)
        sample_hint = f"（示例待映射特征：{samples}）" if samples else ""
        intent = (
            f"评估「{theme}」域的成本/产能/利润经营基线，"
            f"覆盖该域尚未映射到本体的业务相关特征缺口{sample_hint}。"
        )
        intents.append((intent, "manufacturing"))
    fallback = iter(_FALLBACK_INTENTS)
    while len(intents) < count:
        try:
            intents.append((next(fallback), "manufacturing"))
        except StopIteration:
            intents.append((_FALLBACK_INTENTS[len(intents) % len(_FALLBACK_INTENTS)], "manufacturing"))
    return intents[:count]


async def generate_baseline_batch(db: Session, user_id: int, count: int, model_id: str) -> list[dict]:
    """定时批量起草 count 份经营基线草案（逐个隔离，单个失败不影响其余）。

    意图取自 derive_intents_from_gap（缺口热点驱动）；每份复用人触发路径同一个
    draft_business_baseline —— 产出 BusinessDraft 行(status=validated|invalid)并落 staging，
    但【不 promote】。落盘仍由人在待采纳列表批量采纳，治理边界不破。
    """
    user = db.get(User, user_id)
    if not user:
        return []
    results: list[dict] = []
    for intent, domain in derive_intents_from_gap(count):
        try:
            results.append(await draft_business_baseline(db, user, intent, domain, model_id))
        except (ExternalServiceError, ValueError, SemiKbError, OSError) as exc:
            # 单份失败（模型抖动/门禁异常）不应中断整批；记录后继续下一份。
            db.rollback()
            results.append({"status": "error", "intent": intent, "error": f"{type(exc).__name__}: {exc}"})
    return results
