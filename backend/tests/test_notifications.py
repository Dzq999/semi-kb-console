from app.services.notifications import split_markdown_v2


def test_split_markdown_v2_respects_utf8_byte_limit_and_preserves_text():
    content = "# 日报\n\n" + ("场景知识产物与验证结果。\n" * 1000)
    chunks = split_markdown_v2(content, max_bytes=128)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 128 for chunk in chunks)
    assert "".join(chunks) == content


def test_split_markdown_v2_keeps_short_content_single_chunk():
    content = "## 今日结果\n| 指标 | 今日新增 | 当前总量 |"
    assert split_markdown_v2(content) == [content]
