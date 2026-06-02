"""文档落盘前的 sanitize：兼容代理把 fsWrite 当正文返回的情况。"""

import json

from app.services.doc_llm_sanitize import extract_fs_write_markdown, has_tool_artifacts, sanitize_llm_markdown


def test_extract_fs_write_returns_inner_markdown():
    blob = {
        "name": "fsWrite",
        "arguments": {"path": "0070.md", "content": "# 0070 标题\n\n正文段落。"},
    }
    raw = "Covers operations\n\n" + json.dumps(blob, ensure_ascii=False) + "\n\n文档已生成至 docs/0070.md。"
    out = extract_fs_write_markdown(raw)
    assert out is not None
    assert out.startswith("# 0070")
    assert "正文段落" in out
    assert "文档已生成" not in out


def test_sanitize_prefers_fs_write_body():
    blob = {
        "name": "fsWrite",
        "arguments": {"path": "x.md", "content": "# 0010 产品\n\n- a\n- b"},
    }
    raw = "The user didn't confirm.\n\n" + json.dumps(blob, ensure_ascii=False)
    out = sanitize_llm_markdown(raw)
    assert out.startswith("# 0010")
    assert "The user" not in out


def test_has_tool_artifacts_detects_fs_write():
    assert has_tool_artifacts('x {"name": "fsWrite"')
    assert not has_tool_artifacts("# 仅标题\n\n正文")


def test_sanitize_strips_english_preamble_before_heading():
    raw = """The assistant proposed choices.

# 0040 技术方案

## 1. 总述

内容。
"""
    out = sanitize_llm_markdown(raw)
    assert out.startswith("# 0040")
    assert "The assistant" not in out
