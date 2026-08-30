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


def _markdown_to_html(markdown: str, article_id: int | None = None, assets: list[ArticleAsset] | None = None) -> str:
    assets_by_name = {Path(asset.file_path).name: asset for asset in (assets or [])}
    lines = []
    for raw in markdown.splitlines():
        raw = raw.strip()
        image = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", raw)
        if image:
            alt, source = image.groups()
            asset = assets_by_name.get(Path(source).name)
            if article_id and asset:
                src = f"/api/articles/{article_id}/assets/{asset.id}"
                lines.append(f'<figure class="article-inline-image"><img src="{src}" alt="{html.escape(alt, quote=True)}" loading="lazy" /><figcaption>{html.escape(alt)}</figcaption></figure>')
            continue
        line = html.escape(raw)
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


_WECHAT_FONT = "-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif"


def _leaf(text: str) -> str:
    """Wrap visible text as required by the WeChat editor compatibility rules."""
    return f'<span leaf="">{html.escape(text)}</span>'


def _inline_wechat(text: str, auto_emphasis: bool = False) -> str:
    """Render a deliberately small, safe subset of Markdown inline syntax."""
    text = text.strip()
    if not text:
        return ""
    # Protect supported inline constructs while escaping all user/model HTML.
    tokens: dict[str, str] = {}

    def token(value: str) -> str:
        key = f"\x00{len(tokens)}\x00"
        tokens[key] = value
        return key

    text = re.sub(r"`([^`]+)`", lambda m: token(
        f'<span style="background:#F1F5F9;color:#52525B;padding:1px 6px;border-radius:4px;font-family:\'SF Mono\',Consolas,Monaco,monospace;font-size:14px;">{_leaf(m.group(1))}</span>'
    ), text)
    text = re.sub(r"\*\*([^*]+)\*\*", lambda m: token(
        f'<strong style="color:#27272A;font-weight:700;">{_leaf(m.group(1))}</strong>'
    ), text)
    text = re.sub(r"==(.*?)==", lambda m: token(
        f'<span style="background:#F4F4F5;color:#27272A;padding:1px 4px;">{_leaf(m.group(1))}</span>'
    ), text)
    text = re.sub(r"(?:\+\+|<u>)(.*?)(?:\+\+|</u>)", lambda m: token(
        f'<span style="border-bottom:2px solid #52525B;font-weight:600;color:#27272A;">{_leaf(m.group(1))}</span>'
    ), text)
    text = re.sub(r"~~(.*?)~~", lambda m: token(
        f'<span style="color:#A1A1AA;text-decoration:line-through;">{_leaf(m.group(1))}</span>'
    ), text)
    if auto_emphasis and not tokens and len(re.findall(r"[\u4e00-\u9fff]", text)) >= 16:
        # A restrained first-clause marker keeps long model paragraphs scannable.
        match = re.match(r"([\u4e00-\u9fff]{4,10})", text)
        if match:
            rest = text[match.end():]
            return (f'<span style="border-bottom:2px solid #52525B;font-weight:600;color:#27272A;">{_leaf(match.group(1))}</span>'
                    f'{_leaf(rest)}').replace("\n", "<br>")
    escaped = html.escape(text)
    parts = re.split(r"(\x00\d+\x00)", escaped)
    rendered: list[str] = []
    for part in parts:
        if part in tokens:
            rendered.append(tokens[part])
        elif part:
            rendered.append(_leaf(html.unescape(part)))
    # Newlines in a paragraph are intentional soft breaks, not raw text nodes.
    return "".join(rendered).replace("\n", "<br>")


def _wechat_image_html(src: str, alt: str) -> str:
    return (
        '<section style="border:1px solid #E4E4E7;padding:4px;margin:0 10px 8px;">'
        '<section style="margin:0;overflow:hidden;">'
        f'<span leaf=""><img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}" style="max-width:100%;height:auto;display:block;margin:0 auto;"></span>'
        '</section></section>'
        f'<p style="font-size:12px;color:#A1A1AA;text-align:center;margin:0 10px 28px;letter-spacing:0.5px;">{_leaf("— " + alt)}</p>'
    )


def _markdown_to_wechat_html(markdown: str, article_id: int | None = None, assets: list[ArticleAsset] | None = None, author: str = "SEMI-KB") -> str:
    """Render an article as WeChat-editor-safe Graphite Minimal HTML.

    This is intentionally separate from the browser renderer: WeChat strips
    classes, style blocks and several CSS features during draft creation.
    """
    assets_by_name = {Path(asset.file_path).name: asset for asset in (assets or [])}
    lines = [line.rstrip() for line in markdown.splitlines()]
    out: list[str] = [f'<section style="max-width:677px;margin:0 auto;background:#FFFFFF;font-family:{_WECHAT_FONT};color:#52525B;line-height:1.8;letter-spacing:0.3px;overflow-x:hidden;">']
    heading_no = 0
    in_chapter = False
    intro_done = False
    index = 0
    first_h1_skipped = False

    def close_chapter() -> None:
        nonlocal in_chapter
        if in_chapter:
            out.append('</section>')
            in_chapter = False

    while index < len(lines):
        raw = lines[index].strip()
        if not raw or raw == "---":
            if raw == "---" and in_chapter:
                out.append('<section style="padding:0 10px;margin:28px 0;"><section style="height:1px;background:#E4E4E7;margin:0;"><span leaf=""><br></span></section></section>')
            index += 1
            continue
        if raw.startswith("# ") and not first_h1_skipped:
            first_h1_skipped = True
            index += 1
            continue
        if raw.startswith("## "):
            close_chapter()
            heading_no += 1
            title = raw[3:].strip()
            english = ["CONTEXT", "SIGNAL", "ROOT CAUSE", "IMPACT", "ACTION"][min(heading_no - 1, 4)]
            out.append(f'<section style="margin-top:{16 if heading_no == 1 else 56}px;margin-bottom:32px;padding:0 10px;">')
            out.append('<section style="padding-bottom:20px;border-bottom:1px solid #E4E4E7;">')
            out.append(f'<p style="font-size:48px;font-weight:900;color:#E4E4E7;margin:0;line-height:1;letter-spacing:-2px;">{_leaf(f"{heading_no:02d}")}</p>')
            out.append(f'<p style="font-size:10px;color:#A1A1AA;font-weight:500;letter-spacing:3px;margin:0 0 6px;text-transform:uppercase;">{_leaf(english)}</p>')
            out.append(f'<h3 style="font-size:20px;font-weight:800;color:#27272A;margin:0;letter-spacing:0.5px;line-height:1.4;">{_leaf(title)}</h3>')
            out.append('</section>')
            in_chapter = True
            index += 1
            continue
        if raw.startswith("### "):
            out.append(f'<p style="font-size:15px;font-weight:800;color:#27272A;margin:28px 10px 14px;padding-left:12px;border-left:3px solid #52525B;line-height:1.4;">{_leaf(raw[4:].strip())}</p>')
            index += 1
            continue
        image = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", raw)
        if image:
            alt, source = image.groups()
            asset = assets_by_name.get(Path(source).name)
            src = f"/api/articles/{article_id}/assets/{asset.id}" if article_id and asset else source
            out.append(_wechat_image_html(src, alt or "场景配图"))
            index += 1
            continue
        if raw.startswith(">"):
            quote = re.sub(r"^>+\s?", "", raw).strip()
            if not intro_done and heading_no == 0:
                out.append(f'<section style="margin:10px 10px 40px;padding:32px 24px 24px;border-top:1px solid #E4E4E7;border-bottom:1px solid #E4E4E7;background:#FFFFFF;"><p style="font-size:11px;color:#A1A1AA;letter-spacing:2px;margin:0 0 18px;font-weight:400;">{_leaf("QUOTE")}</p><p style="font-size:18px;font-weight:700;color:#27272A;margin:0;line-height:1.7;letter-spacing:0.5px;"><span style="border-bottom:2px solid #52525B;">{_leaf(quote)}</span></p></section>')
                intro_done = True
            else:
                out.append(f'<section style="border-left:3px solid #52525B;padding:16px 0 16px 24px;margin:0 10px 28px;"><p style="font-size:16px;font-weight:700;color:#27272A;margin:0;line-height:1.7;letter-spacing:0.5px;">{_inline_wechat(quote)}</p></section>')
            index += 1
            continue
        if raw.startswith("```"):
            language = raw[3:].strip() or "CODE"
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code = ''.join(f'<p style="margin:0;font-family:\'SF Mono\',Consolas,Monaco,monospace;font-size:13px;line-height:1.6;color:#E2E8F0;">{_leaf(line or " ")}</p>' for line in code_lines)
            out.append(f'<section style="margin:0 10px 20px;border-radius:8px;overflow:hidden;background:#1E293B;box-shadow:0 4px 16px -8px rgba(15,23,42,0.4);"><section style="padding:9px 14px;background:#0F172A;"><span style="font-size:12px;color:#64748B;font-family:Consolas,Monaco,monospace;letter-spacing:1px;">{_leaf(language)}</span></section><section style="padding:11px 14px;">{code}</section></section>')
            continue
        if re.match(r"^(?:[-*])\s+", raw):
            items: list[str] = []
            while index < len(lines) and re.match(r"^(?:[-*])\s+", lines[index].strip()):
                items.append(re.sub(r"^(?:[-*])\s+", "", lines[index].strip())); index += 1
            out.append('<section style="margin:0 10px 24px;padding-left:20px;">' + ''.join(f'<p style="font-size:15px;color:#52525B;margin:0 0 8px;line-height:1.8;">{_leaf("• ")}{_inline_wechat(item)}</p>' for item in items) + '</section>')
            continue
        if re.match(r"^\d+[.)]\s+", raw):
            items: list[tuple[str, str]] = []
            while index < len(lines) and re.match(r"^\d+[.)]\s+", lines[index].strip()):
                match = re.match(r"^(\d+)[.)]\s+(.*)$", lines[index].strip()); items.append((match.group(1), match.group(2))); index += 1
            out.append('<section style="margin:0 10px 24px;padding-left:4px;">' + ''.join(f'<p style="font-size:15px;color:#52525B;margin:0 0 8px;line-height:1.8;"><span style="display:inline-block;color:#A1A1AA;font-weight:700;margin-right:8px;">{_leaf(num)}</span>{_inline_wechat(item)}</p>' for num, item in items) + '</section>')
            continue
        if raw.startswith("|") and raw.endswith("|"):
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells): rows.append(cells)
                index += 1
            if rows:
                head = rows[0]; body = rows[1:]
                out.append('<section style="margin:0 10px 24px;overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:14px;"><thead><tr>' + ''.join(f'<th style="background:#27272A;color:#FFFFFF;font-weight:700;padding:8px 12px;text-align:left;">{_leaf(c)}</th>' for c in head) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td style="padding:8px 12px;border-bottom:1px solid #E4E4E7;color:#52525B;">{_inline_wechat(c)}</td>' for c in row) + '</tr>' for row in body) + '</tbody></table></section>')
            continue
        paragraph: list[str] = [raw]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(r"^(#{1,3}\s|>|!\[|```|[-*]\s+|\d+[.)]\s+|\|)", lines[index].strip()):
            paragraph.append(lines[index].strip()); index += 1
        out.append(f'<p style="margin:0 10px 22px;font-size:15px;line-height:1.8;text-align:justify;color:#52525B;letter-spacing:0.3px;">{_inline_wechat(" ".join(paragraph), auto_emphasis=True)}</p>')

    close_chapter()
    out.append('<section style="padding:0 10px;"><section style="text-align:center;margin:0 0 36px;"><section style="display:flex;align-items:center;justify-content:center;"><span style="height:1px;width:48px;background:#E4E4E7;margin-right:16px;"><span leaf=""><br></span></span><span style="font-size:10px;color:#A1A1AA;letter-spacing:4px;font-weight:500;">' + _leaf("END") + '</span><span style="height:1px;width:48px;background:#E4E4E7;margin-left:16px;"><span leaf=""><br></span></span></section></section></section>')
    out.append(f'<section style="padding:0 10px 24px;"><section style="border-top:1px solid #E4E4E7;padding-top:28px;"><p style="margin:0;font-size:12px;color:#A1A1AA;text-align:right;letter-spacing:1px;">{_leaf(author)}</p></section></section>')
    out.append('</section>')
    return "".join(out)


def _image_anchor_candidates(content: str) -> list[tuple[int, str, str]]:
    """Return paragraph boundaries and context suitable for inline illustrations."""
    lines = content.splitlines()
    heading = ""
    candidates: list[tuple[int, str, str]] = []
    for index, raw in enumerate(lines):
        text = raw.strip()
        if re.match(r"^#{2,3}\s+", text):
            heading = re.sub(r"^#{2,3}\s+", "", text)
            continue
        if len(text) < 80 or text.startswith(("#", ">", "- ", "* ", "|", "!")):
            continue
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if next_line:
            continue
        candidates.append((index + 1, heading or "现场环节", text[:320]))
    return candidates


def _select_image_anchors(content: str, count: int) -> list[tuple[int, str, str]]:
    candidates = _image_anchor_candidates(content)
    if count <= 0 or not candidates:
        return []
    count = min(count, len(candidates))
    if count == 1:
        return [candidates[len(candidates) // 2]]
    indexes = [round(index * (len(candidates) - 1) / (count - 1)) for index in range(count)]
    return [candidates[index] for index in indexes]


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


async def generate_article(db: Session, user_id: int, topic: ArticleTopic, model_id: str, approval_required: bool = True, auto_visuals: bool = True, image_model_id: str | None = None, image_count: int = 1, generation_date: str | None = None, sequence_no: int | None = None, auto_repair: bool = True, max_repair_attempts: int | None = None) -> Article:
    evidence = _json(topic.evidence_json, [])
    key = user_api_key(db, user_id)
    if not key:
        raise ExternalServiceError("未配置模型 API Key，无法生成场景文章")
    article = Article(user_id=user_id, topic_id=topic.id, title=topic.title[:240], subtitle=topic.pain_point[:200], status="generating", generation_stage="正文生成中", generation_progress=5, approval_required=approval_required, article_model_id=model_id, image_model_id=image_model_id, generation_date=generation_date, sequence_no=sequence_no, generated_at=datetime.now(timezone.utc))
    db.add(article); db.commit(); db.refresh(article)
    system = "你是半导体行业公众号主编。只写一个具体业务场景和一个核心问题，使用自然中文、短段落和小标题，深入解释症状、根因和经营影响，避免任何AI套话。不得编造数字；不确定处明确写待验证。第一行只输出一个吸引人的公众号标题（不要出现‘客户痛点’或‘痛点分析’），随后输出正文。正文必须包含：场景背景、现场表现、客户痛点、问题根因、经营影响、证据与仿真/校验依据、建议行动。"
    prompt = json.dumps({"title": topic.title, "domain": topic.domain, "pain_point": topic.pain_point, "business_context": topic.business_context, "evidence": evidence}, ensure_ascii=False)
    narrative = (await llm_service.complete(key, model_id, system, prompt)).strip()

    def compose(candidate: str) -> tuple[str, str, str]:
        first_line, _, body = candidate.partition("\n")
        title = _safe_title(first_line, topic)
        body = body.strip() or candidate
        return title, body, f"# {title}\n\n> 本文只讨论一个可验证的现场场景。\n\n{body}"

    title, body, content = compose(narrative)
    validation = validate_article(content, topic, evidence, title)
    # Repair the draft using the concrete gate failures. The limit is capped in
    # settings so a malformed model response cannot cause an unbounded loop.
    max_repairs = min(3, max(0, settings.article_repair_attempts if max_repair_attempts is None else max_repair_attempts)) if auto_repair else 0
    for attempt in range(1, max_repairs + 1):
        if validation["passed"]:
            break
        article.repair_attempts = attempt
        article.generation_stage = f"校验未通过，自动返修 {attempt}/{max_repairs}"
        article.generation_progress = min(18, 5 + attempt * 4)
        article.validation_json = json.dumps({**validation, "repair_attempts": attempt}, ensure_ascii=False)
        db.commit()
        repair_prompt = json.dumps({
            "原始标题": title,
            "原始正文": content,
            "校验失败原因": validation["errors"],
            "修订要求": "逐项修复所有失败原因，只输出第一行标题和随后完整正文，不要解释修改过程；保留可核验事实，不要编造数字。",
            "主题": topic.title,
            "证据": evidence,
        }, ensure_ascii=False)
        repaired = (await llm_service.complete(key, model_id, system, repair_prompt)).strip()
        title, body, content = compose(repaired)
        validation = validate_article(content, topic, evidence, title)
        article.title = title
        article.content_markdown = content
        article.content_html = _markdown_to_wechat_html(content)
        article.word_count = validation["word_count"]
        article.validation_json = json.dumps({**validation, "repair_attempts": attempt, "repair_limit": max_repairs}, ensure_ascii=False)
        db.commit()
    validation = {**validation, "repair_attempts": article.repair_attempts, "repair_limit": max_repairs}
    article.title = title
    article.content_markdown = content
    article.content_html = _markdown_to_wechat_html(content)
    article.validation_json = json.dumps(validation, ensure_ascii=False)
    article.metrics_snapshot_json = json.dumps(await semi_kb.metrics(db, user_id), ensure_ascii=False)
    article.word_count = validation["word_count"]
    article.ai_tone_score = validation["ai_tone_score"]
    article.factual_score = validation["factual_score"]
    article.cover_prompt = f"半导体生产现场纪实插图，{topic.domain} 场景，{topic.pain_point[:180]}，专业写实风格，无文字水印"
    article.generation_stage = "正文已生成，准备配图"
    article.generation_progress = 20
    db.commit(); db.refresh(article)
    article_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"
    article_dir.mkdir(parents=True, exist_ok=True)
    assets: list[ArticleAsset] = []
    if auto_visuals and image_count > 0:
        asset_dir = settings.semi_kb_root / "knowledge" / "articles" / "generated"
        asset_dir.mkdir(parents=True, exist_ok=True)
        anchors = _select_image_anchors(content, image_count)
        for index, (_, heading, excerpt) in enumerate(anchors):
            path = asset_dir / f"article-{article.id}-{index + 1}.png"
            mime_type = "image/png"; source_type = "generated"
            prompt = f"半导体生产现场纪实插图，环节：{heading}；对应正文：{excerpt}。画面必须与该段内容一致，专业写实风格，无文字水印。"
            try:
                if image_model_id:
                    image_bytes, extension = await llm_service.generate_image(key, image_model_id, prompt)
                    path = path.with_suffix(extension)
                    path.write_bytes(image_bytes)
                else:
                    raise ExternalServiceError("未指定图片模型")
            except ExternalServiceError:
                svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630'><rect width='100%' height='100%' fill='#0b1220'/><text x='60' y='180' fill='#fff' font-size='42'>{html.escape(title[:28])}</text><text x='60' y='260' fill='#9fb3c8' font-size='26'>SEMI-KB 场景洞察</text></svg>"
                path = path.with_suffix(".svg"); path.write_text(svg, encoding="utf-8"); mime_type = "image/svg+xml"; source_type = "fallback"
            asset = ArticleAsset(article_id=article.id, asset_type="cover" if index == 0 else "illustration", file_path=str(path), mime_type=mime_type, caption=heading or title, source_type=source_type, source_ref=image_model_id)
            db.add(asset); assets.append(asset)
            article.generation_stage = f"配图生成 {index + 1}/{len(anchors)}"
            article.generation_progress = 20 + round((index + 1) / max(1, len(anchors)) * 60)
            db.commit()
        db.flush()
        lines = content.splitlines()
        for (line_index, heading, _), asset in sorted(zip(anchors, assets), key=lambda pair: pair[0][0], reverse=True):
            alt = (heading or title).replace("]", "")
            lines.insert(line_index, f"![{alt}](generated/{Path(asset.file_path).name})")
        content = "\n".join(lines)
        article.content_markdown = content
        article.content_html = _markdown_to_wechat_html(content, article.id, assets)
        final_validation = {**validate_article(content, topic, evidence, title), "repair_attempts": article.repair_attempts, "repair_limit": max_repairs}
        article.word_count = final_validation["word_count"]
        article.validation_json = json.dumps(final_validation, ensure_ascii=False)
        article.ai_tone_score = final_validation["ai_tone_score"]
        article.factual_score = final_validation["factual_score"]
        article.status = "waiting_approval" if final_validation["passed"] and approval_required else ("approved" if final_validation["passed"] else "blocked")
        article.generation_stage = "已完成" if final_validation["passed"] else "需用户修改（自动返修未通过）"
        article.generation_progress = 100
        topic.used_at = datetime.now(timezone.utc); topic.status = "used"
        db.commit()
    else:
        article.content_html = _markdown_to_wechat_html(content, article.id, assets)
        article.status = "waiting_approval" if validation["passed"] and approval_required else ("approved" if validation["passed"] else "blocked")
        article.generation_stage = "已完成" if validation["passed"] else "需用户修改（自动返修未通过）"
        article.generation_progress = 100
        db.commit()
    (article_dir / f"article-{article.id}.md").write_text(article.content_markdown, encoding="utf-8")
    (article_dir / f"article-{article.id}.html").write_text(article.content_html, encoding="utf-8")
    return article


async def generate_daily_batch(db: Session, user_id: int, setting: ArticleSetting, model_id: str, image_model_id: str | None, image_count: int, count: int, generation_date: str) -> list[Article]:
    """Generate the configured number of independent articles for one day."""
    discover_topics(db, user_id)
    topics = db.scalars(select(ArticleTopic).where(ArticleTopic.user_id == user_id, ArticleTopic.status == "qualified", ArticleTopic.used_at.is_(None)).order_by(ArticleTopic.priority_score.desc(), ArticleTopic.novelty_score.desc(), ArticleTopic.created_at.asc()).limit(count)).all()
    articles: list[Article] = []
    for index, topic in enumerate(topics, 1):
        try:
            article = await generate_article(db, user_id, topic, model_id, setting.approval_required, setting.auto_visuals, image_model_id, image_count, generation_date, index, setting.auto_repair, setting.max_repair_attempts)
            articles.append(article)
        except Exception:
            topic.status = "blocked"
            db.commit()
    return articles
