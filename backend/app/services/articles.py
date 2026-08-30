from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleAsset, ArticleRevision, ArticleSetting, ArticleTopic
from .llm import ExternalServiceError, llm_service, user_api_key
from .semi_kb import semi_kb


def _json(value: str | None, default):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


def _markdown_to_html(markdown: str) -> str:
    lines = []
    for raw in markdown.splitlines():
        line = html.escape(raw.strip())
        if not line:
            continue
        if line.startswith("### "):
            lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            lines.append(f"<li>{line[2:]}</li>")
        else:
            lines.append(f"<p>{line}</p>")
    return "\n".join(lines)


def validate_article(content: str, topic: ArticleTopic, evidence: list, title: str | None = None) -> dict:
    errors: list[str] = []
    text = content.strip()
    if len(text) < 500:
        errors.append("正文过短，至少需要 500 个字符")
    if text.count("客户痛点") == 0:
        errors.append("缺少客户痛点章节")
    if title and re.search(r"客户痛点|痛点分析|AI生成|AI 生成", title, re.I):
        errors.append("标题包含不适合公众号的模板化表述")
    if topic.pain_point:
        key_terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,6}", topic.pain_point)[:5] if len(term) >= 2]
        if key_terms and not any(term in text for term in key_terms):
            errors.append("正文未覆盖主题核心问题")
    if not evidence:
        errors.append("主题没有可追溯证据")
    if re.search(r"作为AI|作为一个AI|综上所述|希望这篇文章|本文将|值得注意的是", text, re.I):
        errors.append("检测到明显 AI 套话")
    if re.search(r"Bearer\s+\S+|授权码\s*[:：]|webhook/send\?key=", text, re.I):
        errors.append("正文疑似包含敏感凭据")
    tone_penalty = len(re.findall(r"首先|其次|最后|综上", text))
    ai_tone_score = max(0.0, min(1.0, 1 - tone_penalty / 10))
    factual_score = 1.0 if evidence and not errors else max(0.0, 1 - len(errors) / 8)
    return {"passed": not errors, "errors": errors, "word_count": len(text), "ai_tone_score": round(ai_tone_score, 3), "factual_score": round(factual_score, 3)}


def upsert_topic(db: Session, user_id: int, title: str, pain_point: str, source_ref: str, evidence: list, domain: str = "semiconductor") -> ArticleTopic:
    digest = hashlib.sha256((title.strip() + "\n" + pain_point.strip()).encode()).hexdigest()
    existing = db.scalar(select(ArticleTopic).where(ArticleTopic.user_id == user_id, ArticleTopic.source_ref == digest))
    if existing:
        return existing
    topic = ArticleTopic(user_id=user_id, title=title[:240], domain=domain, pain_point=pain_point, business_context="由知识库、经营模型和仿真结果共同验证", evidence_json=json.dumps(evidence, ensure_ascii=False), source_ref=digest, priority_score=min(1.0, 0.5 + len(evidence) * 0.1), novelty_score=0.8)
    db.add(topic); db.commit(); db.refresh(topic)
    return topic


def discover_topics(db: Session, user_id: int) -> int:
    """Build a small, deduplicated candidate pool from current scenario material."""
    content = semi_kb.article_text()
    if not content.strip():
        return 0
    chunks = [item.strip() for item in re.split(r"\n(?=#+\s)", content) if item.strip()]
    created = 0
    for chunk in chunks[:30]:
        heading = re.search(r"^#+\s+(.+)$", chunk, re.M)
        title = heading.group(1).strip() if heading else "半导体现场知识缺口与交付风险"
        pain = chunk[:1000]
        before = db.scalar(select(ArticleTopic.id).where(ArticleTopic.user_id == user_id, ArticleTopic.source_ref == hashlib.sha256((title + "\n" + pain).encode()).hexdigest()))
        upsert_topic(db, user_id, title, pain, "knowledge/articles/current-scenarios.md", [{"source": "knowledge/articles/current-scenarios.md", "excerpt": pain[:500]}])
        created += int(before is None)
    return created


def _safe_title(candidate: str, topic: ArticleTopic) -> str:
    title = re.sub(r"^#+\s*", "", candidate.strip()).strip("\"' ")
    if not title or re.search(r"客户痛点|痛点分析|AI生成|AI 生成", title, re.I):
        title = f"{topic.domain.upper()}现场的一个异常信号，为什么会拖慢交付？"
    return title[:120]


async def generate_article(db: Session, user_id: int, topic: ArticleTopic, model_id: str, approval_required: bool = True, auto_visuals: bool = True, image_model_id: str | None = None, image_count: int = 1, generation_date: str | None = None, sequence_no: int | None = None) -> Article:
    evidence = _json(topic.evidence_json, [])
    key = user_api_key(db, user_id)
    if not key:
        raise ExternalServiceError("未配置模型 API Key，无法生成场景文章")
    system = "你是半导体行业公众号主编。只写一个具体业务场景和一个核心问题，使用自然中文、短段落和小标题，深入解释症状、根因和经营影响，避免任何AI套话。不得编造数字；不确定处明确写待验证。第一行只输出一个吸引人的公众号标题（不要出现‘客户痛点’或‘痛点分析’），随后输出正文。正文必须包含：场景背景、现场表现、问题根因、经营影响、证据与仿真/校验依据、建议行动。"
    prompt = json.dumps({"title": topic.title, "domain": topic.domain, "pain_point": topic.pain_point, "business_context": topic.business_context, "evidence": evidence}, ensure_ascii=False)
    narrative = (await llm_service.complete(key, model_id, system, prompt)).strip()
    first_line, _, body = narrative.partition("\n")
    title = _safe_title(first_line, topic)
    body = body.strip() or narrative
    content = f"# {title}\n\n> 本文只讨论一个可验证的现场场景。\n\n{body}"
    validation = validate_article(content, topic, evidence, title)
    article = Article(user_id=user_id, topic_id=topic.id, title=title, subtitle=topic.pain_point[:200], status="waiting_approval" if validation["passed"] and approval_required else ("approved" if validation["passed"] else "blocked"), approval_required=approval_required, content_markdown=content, content_html=_markdown_to_html(content), validation_json=json.dumps(validation, ensure_ascii=False), metrics_snapshot_json=json.dumps(await semi_kb.metrics(db, user_id), ensure_ascii=False), word_count=validation["word_count"], ai_tone_score=validation["ai_tone_score"], factual_score=validation["factual_score"], article_model_id=model_id, image_model_id=image_model_id, generation_date=generation_date, sequence_no=sequence_no, cover_prompt=f"半导体工厂真实生产现场，{topic.domain} 场景，{topic.pain_point[:180]}，专业纪实风格，无文字水印", generated_at=datetime.now(timezone.utc))
    db.add(article); topic.used_at = datetime.now(timezone.utc); topic.status = "used"; db.commit(); db.refresh(article)
    article_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / f"article-{article.id}.md").write_text(content, encoding="utf-8")
    (article_dir / f"article-{article.id}.html").write_text(article.content_html, encoding="utf-8")
    if auto_visuals and image_count > 0:
        asset_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"; asset_dir.mkdir(parents=True, exist_ok=True)
        for index in range(image_count):
            path = asset_dir / f"article-{article.id}-{index + 1}.png"
            mime_type = "image/png"; source_type = "generated"
            try:
                if image_model_id:
                    image_bytes, extension = await llm_service.generate_image(key, image_model_id, article.cover_prompt or topic.title)
                    path = path.with_suffix(extension)
                    path.write_bytes(image_bytes)
                else:
                    raise ExternalServiceError("未指定图片模型")
            except ExternalServiceError:
                svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630'><rect width='100%' height='100%' fill='#0b1220'/><text x='60' y='180' fill='#fff' font-size='42'>{html.escape(title[:28])}</text><text x='60' y='260' fill='#9fb3c8' font-size='26'>SEMI-KB 场景洞察</text></svg>"
                path = path.with_suffix(".svg"); path.write_text(svg, encoding="utf-8"); mime_type = "image/svg+xml"; source_type = "fallback"
            db.add(ArticleAsset(article_id=article.id, asset_type="cover" if index == 0 else "illustration", file_path=str(path), mime_type=mime_type, caption=title, source_type=source_type, source_ref=image_model_id))
        db.commit()
    return article


async def generate_daily_batch(db: Session, user_id: int, setting: ArticleSetting, model_id: str, image_model_id: str | None, image_count: int, count: int, generation_date: str) -> list[Article]:
    """Generate the configured number of independent articles for one day."""
    discover_topics(db, user_id)
    topics = db.scalars(select(ArticleTopic).where(ArticleTopic.user_id == user_id, ArticleTopic.status == "qualified", ArticleTopic.used_at.is_(None)).order_by(ArticleTopic.priority_score.desc(), ArticleTopic.novelty_score.desc(), ArticleTopic.created_at.asc()).limit(count)).all()
    articles: list[Article] = []
    for index, topic in enumerate(topics, 1):
        try:
            article = await generate_article(db, user_id, topic, model_id, setting.approval_required, setting.auto_visuals, image_model_id, image_count, generation_date, index)
            articles.append(article)
        except Exception:
            topic.status = "blocked"
            db.commit()
    return articles
