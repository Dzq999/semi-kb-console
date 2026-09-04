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
from ..db import SessionLocal
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


# ── 分步真调用（完整档）用到的三段提示词 ───────────────────────────────────
# 规划：一次小调用，判定简单/复杂、给出子步骤与要引用的库内来源。产出 JSON。
_PLANNER_PROMPT = (
    "你是半导体经营知识库问答的【规划器】。基于用户问题与知识上下文，判断该问题是否需要分步分析，"
    "并输出计划。只输出一个合法 JSON 对象，不要 Markdown 代码围栏，结构：\n"
    '{"mode": "simple" | "complex",\n'
    ' "steps": [{"title": "该步要解决什么(简短)", "instruction": "该步的具体测算/分析指令"}],\n'
    ' "citations": [{"source": "经营模型/仿真场景/知识库来源族/本体", "ref": "文件路径或来源标识", "note": "支撑了什么"}],\n'
    ' "grounded": true/false}\n\n'
    "判定规则：\n"
    "1. 仅当问题涉及多环节推算/多因素对比/需要先取基线再算增益等，才判 complex，steps 拆 2-5 步、"
    "每步职责单一、可独立测算；简单事实/单点取值判 simple，steps 置为空数组[]。\n"
    "2. steps 只描述做什么，不要在此处写出答案或数字。\n"
    "3. citations 预判本次会用到的库内来源(business_models/simulation_scenarios/knowledge_sources/"
    "ontology_terms)；标 restricted 的来源(设备手册/NDA)只可引用标题与来源标识。\n"
    "4. 若库内确无相关来源可支撑，grounded=false 且 citations 可为空数组。绝不编造不存在的来源。"
)
# 单步测算：每步一次真调用，只产出该步结论(Markdown)，可引用前序步骤结果。
_STEP_PROMPT = (
    "你是半导体经营知识库问答的【分步测算器】，正在执行多步分析中的某一步。"
    "只输出这一步的结论正文(简洁 Markdown，可含要点/小表格/推算式)，不要输出 JSON、不要重复问题、"
    "不要写与本步无关的内容。严格依据知识上下文中的经营模型变量与取值；涉及数字必须给出推算依据，"
    "不得编造库内不存在的数值或变量。标 restricted 的来源只可引用标题与来源标识，禁止复制其正文。"
)
# 汇总：流式产出最终答案正文(纯 Markdown，不再包 JSON)，供逐字浮现。
_SYNTH_PROMPT = (
    "你是半导体智能制造知识库的问答助手。下面给出用户问题、知识上下文与各分步测算结果，"
    "请综合成面向经营的最终回答。只输出中文回答正文(Markdown，可含要点/表格/结论)，不要输出 JSON、"
    "不要罗列『步骤一/步骤二』的过程流水账，直接给出经过整合、可读的结论与依据。"
    "涉及数字时保留关键推算依据。若各步结果显示库内无支撑(grounded=false)，"
    "在开头用一句话标注『以下为非本库来源的通用参考，非经本项目数据验证』。"
    "标 restricted 的来源只可引用标题与来源标识，禁止复制其正文。"
)

_MAX_STEPS = 5  # 分步真调用步数上限，控制成本与失败面。


def _grounding_detail(context: dict) -> str:
    """把接地上下文的规模浓缩成一句可展示的进度描述。"""
    models = len(context.get("business_models") or [])
    scenarios = len(context.get("simulation_scenarios") or [])
    sources = context.get("knowledge_sources") or {}
    families = len(sources.get("families") or []) if isinstance(sources, dict) else 0
    return f"已汇集 {models} 个经营模型、{scenarios} 个仿真场景、{families} 类知识来源"


def _clean_citations(raw) -> list[dict]:
    """把模型给的 citations 清洗成统一结构，剔除非 dict 项、补齐缺省字段。"""
    if not isinstance(raw, list):
        return []
    return [
        {"source": str(c.get("source") or ""), "ref": str(c.get("ref") or ""), "note": str(c.get("note") or "")}
        for c in raw if isinstance(c, dict)
    ]


def _parse_plan(raw: str) -> dict:
    """解析规划器 JSON；容错：解析失败或结构不符时退化为 simple 单步计划。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"mode": "simple", "steps": [], "citations": [], "grounded": False}
    if not isinstance(data, dict):
        return {"mode": "simple", "steps": [], "citations": [], "grounded": False}
    steps = []
    for item in (data.get("steps") or []):
        if isinstance(item, dict) and (item.get("title") or item.get("instruction")):
            steps.append({
                "title": str(item.get("title") or "分析步骤").strip()[:60],
                "instruction": str(item.get("instruction") or item.get("title") or "").strip(),
            })
        if len(steps) >= _MAX_STEPS:
            break
    mode = "complex" if steps else "simple"
    return {
        "mode": mode,
        "steps": steps,
        "citations": _clean_citations(data.get("citations")),
        "grounded": bool(data.get("grounded")),
    }


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


async def answer_question_streamed(user_id: int, conversation_id: str, question: str, model_id: str):
    """分步真调用（完整档）+ 流式作答的异步生成器，逐个 yield 事件字典。

    事件类型：
      stage  —— 阶段进度({key,label,status,detail})，status ∈ started/done/error
      token  —— 汇总答案的增量文本({text})
      done   —— 落库后的完整助手消息({message})
      error  —— 失败({detail})
    落库在生成器内自开 SessionLocal（响应体已开始，请求级会话可能已关闭），与用户消息一并提交。
    """
    with SessionLocal() as db:
        user = db.get(User, user_id)
        conversation = db.get(QaConversation, conversation_id)
        if user is None or conversation is None:
            yield {"type": "error", "detail": "会话不存在"}
            return
        api_key = user_api_key(db, user_id)
        if not api_key:
            yield {"type": "error", "detail": "未配置模型 API Key，无法回答"}
            return
        endpoint = user_llm_endpoint(db, user_id)
        history = _history_messages(db, conversation_id)

        try:
            # 阶段 1：接地检索（无 LLM 调用）
            yield {"type": "stage", "key": "grounding", "label": "汇集库内知识", "status": "started"}
            context = build_grounding_context()
            yield {"type": "stage", "key": "grounding", "label": "汇集库内知识", "status": "done", "detail": _grounding_detail(context)}

            # 阶段 2：问题规划
            yield {"type": "stage", "key": "planning", "label": "拆解问题", "status": "started"}
            plan_payload = json.dumps({"question": question, "conversation_history": history, "knowledge_context": context}, ensure_ascii=False)
            plan_raw = await llm_service.complete(api_key, model_id, _PLANNER_PROMPT, plan_payload, temperature=0.0, max_tokens=1200, endpoint=endpoint)
            plan = _parse_plan(plan_raw)
            steps = plan["steps"]
            if steps:
                yield {"type": "stage", "key": "planning", "label": "拆解问题", "status": "done", "detail": f"拆解为 {len(steps)} 步", "steps": [s["title"] for s in steps]}
            else:
                yield {"type": "stage", "key": "planning", "label": "拆解问题", "status": "done", "detail": "问题较直接，直接作答"}

            # 阶段 3：逐步真调用（仅 complex；每步各调一次 LLM，把前序结果串进去）
            step_results: list[dict] = []
            for index, step in enumerate(steps):
                skey = f"step-{index}"
                yield {"type": "stage", "key": skey, "label": step["title"], "status": "started", "group": "step"}
                step_payload = json.dumps({
                    "question": question,
                    "current_step": step,
                    "previous_step_results": step_results,
                    "knowledge_context": context,
                }, ensure_ascii=False)
                try:
                    step_text = await llm_service.complete(api_key, model_id, _STEP_PROMPT, step_payload, temperature=0.1, max_tokens=1600, endpoint=endpoint)
                except ExternalServiceError as exc:
                    step_text = f"(本步测算未完成：{exc})"
                step_results.append({"title": step["title"], "result": step_text.strip()})
                yield {"type": "stage", "key": skey, "label": step["title"], "status": "done", "group": "step"}

            # 阶段 4：汇总作答（流式逐字浮现）
            yield {"type": "stage", "key": "synthesis", "label": "综合作答", "status": "started"}
            synth_payload = json.dumps({
                "question": question,
                "conversation_history": history,
                "knowledge_context": context,
                "step_results": step_results,
                "grounded": plan["grounded"],
            }, ensure_ascii=False)
            answer_parts: list[str] = []
            async for piece in llm_service.stream_complete(api_key, model_id, _SYNTH_PROMPT, synth_payload, temperature=0.2, endpoint=endpoint):
                answer_parts.append(piece)
                yield {"type": "token", "text": piece}
            answer = "".join(answer_parts).strip()
            if not answer:
                yield {"type": "error", "detail": "模型未返回有效答案"}
                return
            yield {"type": "stage", "key": "synthesis", "label": "综合作答", "status": "done"}
        except ExternalServiceError as exc:
            yield {"type": "error", "detail": str(exc)}
            return

        # 落库：用户提问 + 助手答案一并提交（引用/grounded 取自规划阶段）
        db.add(QaMessage(conversation_id=conversation_id, role="user", content=question))
        message = QaMessage(
            conversation_id=conversation_id, role="assistant",
            content=answer, citations_json=json.dumps(plan["citations"], ensure_ascii=False),
            model_id=model_id, grounded=plan["grounded"],
        )
        db.add(message)
        if conversation.title in ("", "新会话"):
            conversation.title = question.strip()[:40] or "新会话"
        db.commit()
        db.refresh(message)
        yield {"type": "done", "message": {
            "id": message.id, "role": "assistant", "content": message.content,
            "citations": plan["citations"], "model_id": message.model_id,
            "grounded": message.grounded, "created_at": message.created_at.isoformat(),
        }}
