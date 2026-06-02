"""模型输出中的工具泄漏/过程性前言清洗（无 FastAPI 依赖，供路由与单测共用）。"""

from __future__ import annotations

import json
import re

_FSWRITE_MARKERS = ('{"name": "fsWrite"', '{"name":"fsWrite"')


def extract_fs_write_markdown(s: str) -> str | None:
    """OpenAI/Cursor 式工具调用被代理当正文返回时，从 arguments.content 取出真实 Markdown。"""
    best: str | None = None
    best_len = 0
    for marker in _FSWRITE_MARKERS:
        start = 0
        while True:
            idx = s.find(marker, start)
            if idx < 0:
                break
            try:
                obj, end = json.JSONDecoder().raw_decode(s, idx)
            except json.JSONDecodeError:
                start = idx + len(marker)
                continue
            if not isinstance(obj, dict) or obj.get("name") != "fsWrite":
                start = idx + 1
                continue
            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            if isinstance(args, dict):
                body = args.get("content")
                if isinstance(body, str):
                    t = body.strip()
                    if t.startswith("#") and len(t) > best_len:
                        best, best_len = t, len(t)
            start = end
    return best


def strip_trailing_cn_tool_meta(s: str) -> str:
    """去掉极少数情况下仍夹在正文末尾的「文档已生成至 …」式工具说明（不误删章节结语）。"""
    lines = s.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    tail_pat = re.compile(r"^(文档已(生成至|写入|保存)|已写入至|输出完成|文件已保存至)")
    while lines:
        t = lines[-1].strip()
        if tail_pat.match(t) or ("已生成至" in t and ".md" in t and len(t) < 200):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def sanitize_llm_markdown(text: str) -> str:
    """清理模型误输出的工具调用片段，保留最终 Markdown 正文。"""
    if not text:
        return ""
    s = text.strip()

    fs_body = extract_fs_write_markdown(s)
    if fs_body:
        return strip_trailing_cn_tool_meta(fs_body)

    if "<tool_use>" in s and '"content"' in s:
        args_blocks = re.findall(r"<arguments>\s*(\{.*?\})\s*</arguments>", s, flags=re.DOTALL)
        for raw in reversed(args_blocks):
            try:
                payload = json.loads(raw)
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            except Exception:
                continue

    s = re.sub(r"<tool_use>.*?</tool_use>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"</?(server_name|tool_name|arguments)>", "", s, flags=re.IGNORECASE)

    filtered = []
    chatter_cn = (
        "我需要先", "让我先", "现在我已经有足够", "我先", "我将先", "我会先", "首先我会", "接下来我会",
    )
    for line in s.splitlines():
        l = line.strip()
        lower = l.lower()
        if lower.startswith("let me ") or lower.startswith("now let me ") or lower.startswith("now i have enough context"):
            continue
        if l.startswith(chatter_cn):
            continue
        if lower.startswith("i will first") or lower.startswith("i'll first") or lower.startswith("first, i will"):
            continue
        if lower.startswith("the assistant ") or lower.startswith("the user ") or lower.startswith("covers "):
            continue
        if lower.startswith("since the user") or lower.startswith("i need to document"):
            continue
        if "read_file" in lower or "create_file" in lower:
            continue
        if '"name": "fswrite"' in lower or '{"name":' in l and "fswrite" in lower:
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered).strip()
    if not cleaned:
        return cleaned

    lines = cleaned.splitlines()
    body_idx = -1
    for i, line in enumerate(lines):
        t = line.strip()
        if not t:
            continue
        if t.startswith(("#", "##", "###", "|", "```", "1.", "2.", "3.", "一、", "二、", "三、")):
            body_idx = i
            break
    if body_idx > 0:
        head = "\n".join(lines[:body_idx]).strip().lower()
        conversational_markers = (
            "let me", "i will first", "now i", "根据以上讨论", "基于以上讨论",
            "我先", "我会先", "下面是", "以下是", "本次讨论", "会话",
            "the assistant", "the user", "covers operations", "i need to document",
            "didn't confirm", "pending confirmation", "since the user", "key architectural",
        )
        if head and any(m in head for m in conversational_markers):
            return strip_trailing_cn_tool_meta("\n".join(lines[body_idx:]).strip())
        head_lines = [ln.strip() for ln in lines[:body_idx] if ln.strip()]
        if 0 < len(head_lines) <= 3 and all(not ln.startswith("#") for ln in head_lines):
            return strip_trailing_cn_tool_meta("\n".join(lines[body_idx:]).strip())
    return strip_trailing_cn_tool_meta(cleaned)


def has_tool_artifacts(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    markers = (
        "<tool_use>", "</tool_use>", "<server_name>", "<tool_name>", "<arguments>",
        "read_file", "create_file", "filesystem",
        '"name": "fswrite"',
        '{"name":"fswrite"',
    )
    return any(m in lower for m in markers)
