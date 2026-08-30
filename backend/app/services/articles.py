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


def validate_article(content: str, topic: ArticleTopic, evidence: list) -> dict:
    errors: list[str] = []
    text = content.strip()
    if len(text) < 500:
        errors.append("正文过短，至少需要 500 个字符")
    if text.count("客户痛点") == 0:
        errors.append("缺少客户痛点章节")
    if topic.title and topic.title not in text:
        errors.append("正文未明确对应唯一主题")
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


async def generate_article(db: Session, user_id: int, topic: ArticleTopic, model_id: str, approval_required: bool = True, auto_visuals: bool = True) -> Article:
    evidence = _json(topic.evidence_json, [])
    key = user_api_key(db, user_id)
    if not key:
        raise ExternalServiceError("未配置模型 API Key，无法生成场景文章")
    system = "你是半导体行业公众号主编。只写一个具体业务场景和一个客户痛点，使用自然中文、短段落和小标题，避免AI套话。不得编造数字；不确定处明确写待验证。必须包含：场景背景、客户痛点、经营影响、仿真/校验依据、建议行动。"
    prompt = json.dumps({"title": topic.title, "domain": topic.domain, "pain_point": topic.pain_point, "business_context": topic.business_context, "evidence": evidence}, ensure_ascii=False)
    narrative = (await llm_service.complete(key, model_id, system, prompt)).strip()
    content = f"# {topic.title}\n\n> 本文聚焦一个现场场景与一个核心痛点。\n\n{narrative}"
    validation = validate_article(content, topic, evidence)
    article = Article(user_id=user_id, topic_id=topic.id, title=topic.title, subtitle=topic.pain_point[:200], status="waiting_approval" if validation["passed"] and approval_required else ("approved" if validation["passed"] else "blocked"), approval_required=approval_required, content_markdown=content, content_html=_markdown_to_html(content), validation_json=json.dumps(validation, ensure_ascii=False), metrics_snapshot_json=json.dumps(await semi_kb.metrics(db, user_id), ensure_ascii=False), word_count=validation["word_count"], ai_tone_score=validation["ai_tone_score"], factual_score=validation["factual_score"], generated_at=datetime.now(timezone.utc))
    db.add(article); topic.used_at = datetime.now(timezone.utc); topic.status = "used"; db.commit(); db.refresh(article)
    article_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / f"article-{article.id}.md").write_text(content, encoding="utf-8")
    (article_dir / f"article-{article.id}.html").write_text(article.content_html, encoding="utf-8")
    if auto_visuals:
        asset_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"; asset_dir.mkdir(parents=True, exist_ok=True)
        svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630'><rect width='100%' height='100%' fill='#0b1220'/><text x='60' y='180' fill='#fff' font-size='42'>{html.escape(topic.title[:28])}</text><text x='60' y='260' fill='#9fb3c8' font-size='26'>SEMI-KB 场景洞察</text></svg>"
        path = asset_dir / f"article-{article.id}.svg"; path.write_text(svg, encoding="utf-8")
        db.add(ArticleAsset(article_id=article.id, asset_type="cover", file_path=str(path), mime_type="image/svg+xml", caption=topic.title, source_type="generated")); db.commit()
    return article
