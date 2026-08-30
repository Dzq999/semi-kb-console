from app.models import ArticleAsset
from app.services.articles import _markdown_to_html, _markdown_to_wechat_html, _select_image_anchors


def test_image_anchors_are_spread_across_long_paragraphs():
    content = "# 标题\n\n## 背景\n\n" + "背景段落。" * 30 + "\n\n## 影响\n\n" + "影响段落。" * 30
    anchors = _select_image_anchors(content, 2)
    assert len(anchors) == 2
    assert anchors[0][0] < anchors[1][0]


def test_markdown_image_is_rendered_inline_with_asset_route():
    asset = ArticleAsset(id=7, article_id=3, file_path="D:/articles/generated/article-3-1.png", asset_type="cover", mime_type="image/png", caption="背景")
    html = _markdown_to_html("正文第一段。\n\n![背景](generated/article-3-1.png)\n\n正文第二段。", 3, [asset])
    assert 'class="article-inline-image"' in html
    assert '/api/articles/3/assets/7' in html


def test_wechat_renderer_uses_graphite_layout_and_leaf_wrappers():
    content = """# 标题

> 数据进入决策链，现场才会真正变得可控。

## 场景背景

这是一个足够长的中文正文段落，用于验证公众号排版中的关键词强调、章节层级和安全文本节点。

### 现场表现

设备告警在交接班后才被发现，维护人员需要重新确认。

![现场](generated/article-3-1.png)

1. 建立统一的状态口径
2. 追踪异常到处置闭环
"""
    html = _markdown_to_wechat_html(content, 3, [ArticleAsset(id=7, article_id=3, file_path="D:/articles/generated/article-3-1.png", asset_type="cover", mime_type="image/png", caption="现场")])
    assert html.startswith('<section style="max-width:677px')
    assert 'class=' not in html and '<div' not in html and '<style' not in html
    assert 'border-bottom:2px solid #52525B' in html
    assert '/api/articles/3/assets/7' in html
    assert '<span leaf="">' in html
