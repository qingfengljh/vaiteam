"""按 46 号协议回传任务结果到 Dispatcher —— Wrapper 标准化报告。

每个任务执行完毕后，Wrapper 负责：
1. 解析 Agent 子进程（CC/Codex）的输出，提取 token 消耗
2. 组装标准化报告（含 token_usage、model、duration、commit）
3. 上报 Dispatcher /webhook 端点
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)


# ── Token 消耗解析 ──

def parse_token_usage(stdout: str, stderr: str = "") -> dict | None:
    """从 Claude Code / Codex 子进程的输出中提取 token 用量。

    支持多种输出格式：
    - Claude Code JSON 格式（最新版）
    - Anthropic API usage 格式
    - OpenAI API usage 格式
    """
    combined = (stderr or "") + "\n" + (stdout or "")

    # 1. 尝试 JSON 行（Claude Code 新版输出）
    for line in combined.split("\n"):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            usage = obj.get("usage") or obj.get("token_usage")
            if usage and isinstance(usage, dict):
                return _normalize_usage(usage)
        except (json.JSONDecodeError, TypeError):
            continue

    # 2. 尝试提取 "Total tokens" 等文本模式
    input_tokens = _extract_int(combined, r"(?:Input|输入|input)[\s:]+tokens?[:\s]*(\d[\d,]*)")
    output_tokens = _extract_int(combined, r"(?:Output|输出|output)[\s:]+tokens?[:\s]*(\d[\d,]*)")
    cache_tokens = _extract_int(combined, r"(?:Cache|cache_read)[\s:]+tokens?[:\s]*(\d[\d,]*)")
    total_tokens = _extract_int(combined, r"(?:Total|总计|total)[\s:]+tokens?[:\s]*(\d[\d,]*)")

    if input_tokens or output_tokens:
        return {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cache_read_tokens": cache_tokens or 0,
            "total_tokens": total_tokens or 0,
        }

    # 3. 尝试提取 Anthropic SDK usage 日志格式
    input_tokens = _extract_int(combined, r"input_tokens[=:]\s*(\d+)")
    output_tokens = _extract_int(combined, r"output_tokens[=:]\s*(\d+)")
    if input_tokens or output_tokens:
        return {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cache_read_tokens": 0,
        }

    return None


def _extract_int(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _normalize_usage(usage: dict) -> dict:
    """标准化不同来源的 usage 字段名。"""
    return {
        "input_tokens": (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("inputTokenCount")
            or 0
        ),
        "output_tokens": (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("outputTokenCount")
            or 0
        ),
        "cache_read_tokens": (
            usage.get("cache_read_input_tokens")
            or usage.get("cache_read_tokens")
            or usage.get("cacheReadInputTokens")
            or 0
        ),
        "total_tokens": usage.get("total_tokens", 0),
    }


# ── 标准化报告 ──

def build_report(
    status: str,
    *,
    agent_id: str = "",
    summary: str = "",
    error: str = "",
    commit_hash: str = "",
    token_usage: dict | None = None,
    model: str = "",
    duration_ms: int = 0,
    clarification_questions: list[str] | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    """构建标准化任务执行报告。"""
    report = {
        "agent_id": agent_id or os.environ.get("AGENT_ID", ""),
        "status": status,
        "summary": summary[:2000] if summary else "",
        "error": error[:2000] if error else "",
        "commit_hash": commit_hash or "",
        "model": model or "",
        "duration_ms": duration_ms,
        "token_usage": token_usage,
        "timestamp": time.time(),
    }
    if clarification_questions:
        report["clarification_questions"] = clarification_questions
    if extra_metadata:
        report["metadata"] = extra_metadata
    return report


# ── HTTP 上报 ──

def _http_post(url: str, payload: dict, api_token: str = "", timeout: int = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    elif os.environ.get("AGENT_API_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['AGENT_API_TOKEN']}"

    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8")}
    except HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        return {"ok": False, "status": e.code, "error": err_body}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def report(
    dispatcher_base: str,
    task_id: str,
    status: str,
    *,
    agent_id: str = "",
    summary: str = "",
    error: str = "",
    commit_hash: str = "",
    token_usage: dict | None = None,
    model: str = "",
    duration_ms: int = 0,
    metadata: dict | None = None,
) -> dict:
    """回传标准化的任务完成/失败报告。"""
    base = (dispatcher_base or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "dispatcher_base empty"}

    endpoint = f"{base}/api/webhook/task-complete"
    if status == "failed":
        endpoint = f"{base}/api/webhook/task-failed"
    elif status == "cancelled":
        endpoint = f"{base}/api/webhook/task-cancelled"

    report = build_report(
        status,
        agent_id=agent_id,
        summary=summary,
        error=error,
        commit_hash=commit_hash,
        token_usage=token_usage,
        model=model,
        duration_ms=duration_ms,
        extra_metadata=metadata,
    )
    payload = {
        "task_id": task_id,
        "agent_id": report["agent_id"],
        "status": status,
        "summary": summary,
        "error": error,
        "commit_hash": commit_hash,
        "token_usage": token_usage,  # 顶层字段，dispatcher 直接读取
        "model": model,
        "duration_ms": duration_ms,
        "metadata": {
            "token_usage": token_usage,
            "model": model,
            "duration_ms": duration_ms,
            **(metadata or {}),
        },
    }

    return _http_post(endpoint, payload)


def report_progress(
    dispatcher_base: str,
    task_id: str,
    progress: int,
    message: str = "",
    *,
    agent_id: str = "",
) -> dict:
    """上报任务进度。"""
    base = (dispatcher_base or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "dispatcher_base empty"}

    return _http_post(
        f"{base}/api/webhook/task-update",
        {
            "task_id": task_id,
            "agent_id": agent_id or os.environ.get("AGENT_ID", ""),
            "status": "executing",
            "progress": max(0, min(100, progress)),
            "message": message,
        },
    )


def report_clarification(
    dispatcher_base: str,
    task_id: str,
    questions: list[str],
    *,
    context: str = "",
    agent_id: str = "",
    attempt_id: str = "",
) -> dict:
    """上报澄清请求。"""
    base = (dispatcher_base or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "dispatcher_base empty"}

    return _http_post(
        f"{base}/api/webhook/task-clarification",
        {
            "task_id": task_id,
            "agent_id": agent_id or os.environ.get("AGENT_ID", ""),
            "questions": questions,
            "context": context,
            "attempt_id": attempt_id,
        },
    )
