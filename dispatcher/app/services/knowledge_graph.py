"""
知识图谱服务 - Leader 通过此模块查询代码结构和依赖关系

底层调用 codebase-memory-mcp 的 MCP 工具。
调度器不直接运行 MCP Server，而是通过 OpenClaw 的 API 间接调用，
或通过 subprocess 调用本地二进制（如果调度器容器也装了 codebase-memory-mcp）。
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def _call_mcp(db_path: str, tool: str, args: dict) -> dict | None:
    """通过 subprocess 调用 codebase-memory-mcp"""
    try:
        cmd = [
            "codebase-memory-mcp",
            "--db", db_path,
            "--tool", tool,
            "--args", json.dumps(args),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning(f"MCP tool {tool} failed: {result.stderr}")
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"MCP call failed: {e}")
        return None


def get_architecture(db_path: str) -> dict | None:
    """获取项目架构概览：语言、包、入口、热点、层次"""
    return _call_mcp(db_path, "get_architecture", {})


def get_dependencies(db_path: str, symbol: str) -> dict | None:
    """查询某个符号（函数/类/模块）依赖了什么"""
    return _call_mcp(db_path, "get_dependencies", {"symbol": symbol})


def get_dependents(db_path: str, symbol: str) -> dict | None:
    """查询谁依赖了某个符号"""
    return _call_mcp(db_path, "get_dependents", {"symbol": symbol})


def detect_changes(db_path: str) -> dict | None:
    """检测未提交的变更，返回影响范围和风险分级"""
    return _call_mcp(db_path, "detect_changes", {})


def trace_call_path(db_path: str, from_symbol: str, to_symbol: str) -> dict | None:
    """追踪两个符号之间的调用路径"""
    return _call_mcp(db_path, "trace_call_path", {"from": from_symbol, "to": to_symbol})


def search_symbol(db_path: str, query: str) -> dict | None:
    """搜索符号（函数/类/模块）"""
    return _call_mcp(db_path, "search_symbol", {"query": query})


# ── Leader 高级查询（组合基础工具） ──

def get_task_context(db_path: str, description: str, files: list[str] | None = None) -> str:
    """
    为任务分解/指令生成提供图谱上下文。
    返回可直接注入 prompt 的文本。
    """
    parts = []

    arch = get_architecture(db_path)
    if arch:
        parts.append(f"## 项目架构\n{json.dumps(arch, ensure_ascii=False, indent=2)}")

    if files:
        for f in files[:5]:
            deps = get_dependencies(db_path, f)
            if deps:
                parts.append(f"## {f} 的依赖\n{json.dumps(deps, ensure_ascii=False, indent=2)}")

    return "\n\n".join(parts) if parts else ""


def get_review_context(db_path: str) -> str:
    """
    为代码审查提供图谱上下文：变更影响范围 + 风险分级。
    """
    changes = detect_changes(db_path)
    if not changes:
        return ""
    return f"## 变更影响分析\n{json.dumps(changes, ensure_ascii=False, indent=2)}"
