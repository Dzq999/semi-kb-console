"""知识库问答服务：综合经营模型/仿真场景/项目知识库(含 vFab 可引用来源)接地作答。

治理边界：本服务【只读】现有引擎知识，不写本体、不落推理层、不改经营基线。
取材策略（用户已确认「严格接地 + 通用兜底」）：
  优先用库内经营模型/仿真/知识作答并标注来源；库内无覆盖时才用模型通用知识，
  且必须显式标注「非本库来源、仅供参考」，绝不编造库内数字。
  vFab restricted 来源（设备手册/NDA）只可引用标题与来源标识，禁止复制其正文。
"""

from __future__ import annotations

import json

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import QaConversation, QaMessage, User
from .llm import ExternalServiceError, llm_service, user_api_key, user_llm_endpoint
from .semi_kb import semi_kb

# 单次问答带入的历史轮数上限（一问一答算两条），控制 prompt 体积。
_HISTORY_LIMIT = 12


def _simulation_scenarios(limit: int = 8) -> list[dict]:
    """读 simulation/scenarios/*.yaml 的场景名与关键变量，作为仿真侧接地线索。"""
    root = settings.engine_root
    scenarios: list[dict] = []
    for path in sorted((root / "simulation" / "scenarios").glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        scenarios.append({
            "path": path.relative_to(root).as_posix(),
            "name": document.get("name") or path.stem,
            "model": document.get("model") or document.get("base_model"),
            "scenarios": [s.get("name") or s.get("id") for s in (document.get("scenarios") or [])][:6],
        })
        if len(scenarios) >= limit:
            break
    return scenarios


def build_grounding_context() -> dict:
    """把可作答的库内知识聚合成接地上下文（全程只读，任一源失败都降级为空、不抛出）。"""
    context: dict = {}
    try:
        business = semi_kb.business_context()
        # 只取经营模型正文与少量示例场景，控制体积。
        context["business_models"] = [
            {"path": item["path"], "document": item["document"]}
            for item in (business.get("models") or [])
        ]
    except Exception:
        context["business_models"] = []
    try:
        context["simulation_scenarios"] = _simulation_scenarios()
    except Exception:
        context["simulation_scenarios"] = []
    try:
        # vFab 可引用来源族：restricted 只给标题+来源标识（NDA 安全）。
        context["knowledge_sources"] = semi_kb.vfab_knowledge_context()
    except Exception:
        context["knowledge_sources"] = {}
    try:
        context["ontology_terms"] = semi_kb.ontology_context(limit=200)
    except Exception:
        context["ontology_terms"] = {}
    return context


_SYSTEM_PROMPT = (
    "你是半导体智能制造知识库的问答助手，面向经营与运营问题（如成本预测、产能、良率影响等）。"
    "只输出一个合法 JSON 对象，不要 Markdown 代码围栏，结构：\n"
    '{"answer": "中文回答正文(Markdown, 可含要点/表格/推算步骤)",\n'
    ' "citations": [{"source": "经营模型/仿真场景/知识库来源族/本体", "ref": "文件路径或来源标识", "note": "该来源支撑了什么"}],\n'
    ' "grounded": true/false}\n\n'
    "取材规则（严格接地 + 通用兜底）：\n"
    "1. 优先使用 business_models(经营模型三件套变量/计算/取值) 与 simulation_scenarios 作答；"
    "涉及成本/产能/良率等数值推算时，必须引用经营模型里的变量与取值，展示推算过程，grounded=true。\n"
    "2. knowledge_sources 列出已入库的 SEMI 标准与设备手册来源族；引用时 ref 用其句柄，勿编造。"
    "标注 restricted 的来源(设备手册/NDA)只可引用标题与来源标识，禁止在 answer 中复制或臆测其正文内容。\n"
    "3. 若库内确无相关经营模型/仿真/知识可支撑，才使用你的通用知识作答，此时 grounded=false，"
    "且必须在 answer 开头用一句话显式标注『以下为非本库来源的通用参考，非经本项目数据验证』。\n"
    "4. 绝不编造库内不存在的数字、变量或来源；不确定就说明缺口并给出需要补充的数据项。\n"
    "5. citations 只列真实用到的来源；纯通用知识作答时 citations 可为空数组。"
)


def _history_messages(db: Session, conversation_id: str) -> list[dict]:
    rows = db.scalars(
        select(QaMessage).where(QaMessage.conversation_id == conversation_id)
        .order_by(QaMessage.created_at.desc(), QaMessage.id.desc()).limit(_HISTORY_LIMIT)
    ).all()
    return [{"role": row.role, "content": row.content} for row in reversed(rows)]


def _parse_answer(raw: str) -> dict:
    """解析模型 JSON 输出；容错：非 JSON 时整体当作 answer、grounded 未知按 false 处理。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "answer" in data:
            citations = data.get("citations")
            if not isinstance(citations, list):
                citations = []
            clean_citations = [
                {"source": str(c.get("source") or ""), "ref": str(c.get("ref") or ""), "note": str(c.get("note") or "")}
                for c in citations if isinstance(c, dict)
            ]
            return {"answer": str(data.get("answer") or "").strip(),
                    "citations": clean_citations,
                    "grounded": bool(data.get("grounded"))}
    except (json.JSONDecodeError, ValueError):
        pass
    return {"answer": raw.strip(), "citations": [], "grounded": False}


async def answer_question(db: Session, user: User, conversation: QaConversation, question: str, model_id: str) -> QaMessage:
    """记录用户提问 → 综合库内知识调用 LLM 作答 → 落库助手消息(含引用来源)。"""
    api_key = user_api_key(db, user.id)
    if not api_key:
        raise ExternalServiceError("未配置模型 API Key，无法回答")
    # 先落用户消息，历史里能带上本次提问。
    db.add(QaMessage(conversation_id=conversation.id, role="user", content=question))
    db.flush()
    history = _history_messages(db, conversation.id)
    context = build_grounding_context()
    user_payload = json.dumps({
        "question": question,
        "conversation_history": history,
        "knowledge_context": context,
    }, ensure_ascii=False)
    raw = await llm_service.complete(api_key, model_id, _SYSTEM_PROMPT, user_payload, temperature=0.2, endpoint=user_llm_endpoint(db, user.id))
    parsed = _parse_answer(raw)
    message = QaMessage(
        conversation_id=conversation.id, role="assistant",
        content=parsed["answer"], citations_json=json.dumps(parsed["citations"], ensure_ascii=False),
        model_id=model_id, grounded=parsed["grounded"],
    )
    db.add(message)
    # 首次问答用问题前 40 字作为会话标题。
    if conversation.title in ("", "新会话"):
        conversation.title = question.strip()[:40] or "新会话"
    db.commit()
    db.refresh(message)
    return message
