"""CC Worker 基础执行器。

定义执行任务的骨架（模板方法模式）：
- 子类覆盖 _prepare / _post / _construct_prompt 实现角色差异
- 支持自纠错循环：执行失败后自动诊断并修复
- 支持动态工具权限：按任务类型分配 --allowedTools
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
SKIP_CLAUDE = os.environ.get("SKIP_CLAUDE", "0") == "1"
DRY_RUN = os.environ.get("CC_DRY_RUN", "0") == "1"
MAX_SELF_RETRY = int(os.environ.get("CC_MAX_SELF_RETRY", "2"))

# ── 动态工具权限配置 ──
# 按任务类型分配工具权限，高危操作限制工具范围
TOOL_PERMISSIONS = {
    "default": "Read,Bash,Edit,Write,Glob,Grep",
    "api_development": "Read,Bash,Edit,Write,Glob,Grep",
    "database_migration": "Read,Bash,Edit,Glob,Grep",  # 禁止 Write（防止随意创建文件）
    "refactoring": "Read,Edit,Glob,Grep",  # 禁止 Bash（防止跑测试/构建干扰）
    "testing": "Read,Bash,Glob,Grep",  # 禁止 Edit/Write（只读测试分析）
    "documentation": "Read,Edit,Write,Glob,Grep",
    "configuration": "Read,Edit,Glob,Grep",  # 禁止 Bash/Write（配置修改需谨慎）
    "hotfix": "Read,Bash,Edit,Write,Glob,Grep",  # 紧急修复全开
}


def get_tool_permissions(task_type: str = "", role: str = "") -> str:
    """获取任务的工具权限列表。

    优先级：task_type > role > default
    """
    # 先按任务类型匹配
    if task_type and task_type.lower() in TOOL_PERMISSIONS:
        return TOOL_PERMISSIONS[task_type.lower()]

    # 再按角色匹配（可扩展）
    role_tools = {
        "archaeologist": "Read,Bash,Glob,Grep",  # 只读角色
    }
    if role and role.lower() in role_tools:
        return role_tools[role.lower()]

    return TOOL_PERMISSIONS["default"]


def _restore_workspace_writable(workspace: str):
    """恢复 workspace 可写权限（上次执行残留的只读保护可能影响新任务）。"""
    try:
        import subprocess
        subprocess.run(["chmod", "-R", "u+w", workspace], capture_output=True, timeout=5)
    except Exception:
        pass


class BaseExecutor(ABC):
    """任务执行器基类。"""

    def __init__(self, agent_id: str, role: str, dispatcher_base: str):
        self.agent_id = agent_id
        self.role = role
        self.dispatcher_base = dispatcher_base.rstrip("/")

    def reply_to_chat(self, msg: dict) -> str | None:
        """生成群聊 @mention 的回复。子类可覆盖。

        使用 Claude Code 根据消息内容生成简短回复。
        返回 None 表示不回复。
        """
        content = msg.get("content", "")
        sender = msg.get("sender_id", "unknown")
        if not content.strip():
            return None

        import subprocess, tempfile
        prompt = f"""You are a {self.role} in a dev team. Someone (@{sender}) mentioned you:

"{content}"

Reply concisely in Chinese. Keep it under 100 characters. Be helpful and direct."""
        # 优先 root，回退 worker（容器不同用户场景兼容）
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            alt = Path("/home/worker/.claude/settings.json")
            if alt.exists():
                settings_path = alt
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--bare",
                 "--settings", str(settings_path),
                 "--model", os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                reply = result.stdout.strip()[:200]
                return reply
        except Exception:
            pass
        return None

    # ── 模板方法：执行骨架 ──

    def execute(self, task: dict, skill: dict) -> dict:
        """执行单个任务。

        Wrapper 负责：
        1. 任务级模型切换（从 task.model_config 读取）
        2. 调用 Agent 子进程（CC/Codex）
        3. 解析子进程输出：token 消耗、澄清请求
        4. 返回标准化结果
        """
        import time as _time
        t_start = _time.monotonic()

        task_id = task.get("id", task.get("task_id", ""))
        model_config = task.get("model_config", {})
        resolved_model = model_config.get("resolved_model", "") or task.get("suggested_model", "")

        result: dict = {
            "task_id": task_id,
            "status": "failed",
            "error": "",
            "commit": "",
            "model": resolved_model,
            "token_usage": None,
            "duration_ms": 0,
        }

        # 1. 检查是否为 human-only 任务
        if task.get("requires_human") or task.get("actor_type") == "human":
            result["error"] = "Task requires human execution, skipping"
            result["status"] = "cancelled"
            return result

        # 2. 环境自检
        from env_self_heal import self_heal
        env_report = self_heal(skill.get("toolchain", []))
        if env_report["overall"] != "ok":
            failed = ", ".join(f"{f['tool']}" for f in env_report["failed"])
            result["error"] = f"Environment check failed: {failed}"
            return result

        # 3. 设置工作区
        repo_url = task.get("git_repo", "")
        branch = task.get("git_branch", "")
        base_branch = task.get("git_base_branch", "develop")
        workspace = os.path.join(WORKSPACE_ROOT, f"{self.agent_id}_{task_id}")
        os.makedirs(workspace, exist_ok=True)

        # 4. 预执行准备（子类覆盖）
        prepare_ctx = self._prepare_execution(task, skill, workspace, repo_url, branch, base_branch)
        if prepare_ctx and prepare_ctx.get("error"):
            result["error"] = prepare_ctx["error"]
            return result

        # 5. 构造 prompt 并执行 CC（子类覆盖 prompt 构造）
        prompt = self._construct_prompt(task, skill)
        # 确保 workspace 可写（上次失败的 allowlist 可能残留只读权限）
        _restore_workspace_writable(workspace)
        prompt_file = Path(workspace) / ".cc_prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        self._report_progress(task_id, 10, f"开始执行 (模型: {resolved_model or '默认'})")

        # 获取工具权限
        task_type = task.get("task_type", "")
        tools = get_tool_permissions(task_type, self.role)

        # 6. 调用 Agent 子进程
        claude_r = self._run_claude(prompt_file, workspace, tools=tools, task=task)

        # 7. 自纠错循环
        if not claude_r["ok"]:
            claude_r = self._self_correct_loop(
                task, skill, workspace, prompt_file, claude_r, tools, task_id
            )

        # 8. 解析 token 消耗
        from report_result import parse_token_usage
        token_usage = parse_token_usage(claude_r.get("stdout", ""), claude_r.get("stderr", ""))
        result["token_usage"] = token_usage
        result["model"] = resolved_model
        result["duration_ms"] = int((_time.monotonic() - t_start) * 1000)

        self._report_progress(task_id, 80, self._progress_message())

        # 9. 后执行处理（子类覆盖）
        self._post_execution(task, skill, workspace, repo_url, claude_r, result)

        # 10. 解析 NEED_CLARIFICATION（仅记录，不阻塞任务）
        stdout_text = claude_r.get("stdout", "")
        questions = self._extract_clarification_questions(stdout_text)

        # 11. 判断结果——执行阶段完成即 completed，疑问写入 summary 供人类复查
        if result.get("status") == "blocked":
            result["status"] = "blocked"
            result["summary"] = stdout_text[-500:] if stdout_text else "blocked"
        elif claude_r["ok"]:
            result["status"] = "completed"
            result["summary"] = stdout_text[-500:] if stdout_text else "completed"
            if questions:
                result["summary"] = "[待确认] " + "; ".join(questions[:3]) + " | " + (result["summary"] or "")
        else:
            result["status"] = "failed"
            result["error"] = f"Claude exited {claude_r['exit_code']}: {claude_r['stderr'][:500]}"

        return result

    # ── 自纠错循环 ──

    def _self_correct_loop(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        prompt_file: Path,
        original_result: dict,
        tools: str,
        task_id: str,
    ) -> dict:
        """执行自纠错循环：诊断错误 → 生成修复 prompt → 重新执行。

        最多重试 MAX_SELF_RETRY 次。每次重试前运行诊断命令收集错误信息。
        """
        claude_r = original_result
        retry_count = 0

        while not claude_r["ok"] and retry_count < MAX_SELF_RETRY:
            retry_count += 1
            logger.info(f"Task {task_id}: self-correction attempt {retry_count}/{MAX_SELF_RETRY}")
            self._report_progress(task_id, 30 + retry_count * 15, f"自纠错第 {retry_count} 次尝试")

            # 诊断：收集错误信息
            diagnostics = self._run_diagnostics(workspace, task)
            error_context = self._build_error_context(claude_r, diagnostics, retry_count)

            # 生成修复 prompt
            fix_prompt = self._build_fix_prompt(task, skill, error_context)
            fix_file = Path(workspace) / f".cc_fix_{retry_count}.md"
            fix_file.write_text(fix_prompt, encoding="utf-8")

            # 重新执行
            claude_r = self._run_claude(fix_file, workspace, tools=tools, timeout=600, task=task)

        if claude_r["ok"]:
            logger.info(f"Task {task_id}: self-correction succeeded after {retry_count} attempt(s)")
        else:
            logger.warning(f"Task {task_id}: self-correction exhausted after {retry_count} attempt(s)")

        return claude_r

    def _run_diagnostics(self, workspace: str, task: dict) -> dict:
        """运行诊断命令收集错误信息。"""
        diagnostics: dict = {"errors": [], "tests": [], "lint": []}

        # 1. 尝试检测 Python 语法错误
        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", "."],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                diagnostics["errors"].append(f"Python syntax: {result.stderr[:500]}")
        except Exception:
            pass

        # 2. 尝试运行测试（如果任务要求）
        test_command = task.get("test_command", "")
        if test_command:
            try:
                result = subprocess.run(
                    test_command.split(),
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    diagnostics["tests"].append(f"Tests failed: {result.stderr[:800]}")
            except Exception:
                pass

        # 3. 检查 TypeScript 编译错误（如果存在 tsconfig.json）
        tsconfig_path = Path(workspace) / "tsconfig.json"
        if tsconfig_path.exists():
            try:
                result = subprocess.run(
                    ["npx", "tsc", "--noEmit"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    diagnostics["errors"].append(f"TypeScript: {result.stderr[:800]}")
            except Exception:
                pass

        # 4. 检查是否有 ImportError（Python）
        try:
            result = subprocess.run(
                ["python3", "-c", "import sys; sys.path.insert(0, '.'); __import__('main' if __import__('os').path.exists('main.py') else 'app')"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                diagnostics["errors"].append(f"Import: {result.stderr[:500]}")
        except Exception:
            pass

        return diagnostics

    def _build_error_context(self, claude_r: dict, diagnostics: dict, retry_count: int) -> str:
        """构建错误上下文文本，用于生成修复 prompt。"""
        parts = [f"## 第 {retry_count} 次自纠错诊断"]

        # Claude 输出中的错误
        stderr = claude_r.get("stderr", "")
        stdout = claude_r.get("stdout", "")
        if stderr:
            parts.append(f"\n### Claude 执行错误\n```\n{stderr[-1500:]}\n```")
        if stdout and len(stdout) > 1000:
            # 取 stdout 的最后部分，通常包含错误信息
            parts.append(f"\n### Claude 输出末尾\n```\n{stdout[-2000:]}\n```")

        # 诊断结果
        if diagnostics["errors"]:
            parts.append("\n### 编译/语法错误")
            for err in diagnostics["errors"]:
                parts.append(f"- {err[:500]}")

        if diagnostics["tests"]:
            parts.append("\n### 测试失败")
            for err in diagnostics["tests"]:
                parts.append(f"- {err[:500]}")

        if not diagnostics["errors"] and not diagnostics["tests"] and not stderr:
            parts.append("\n### 诊断结果\n未检测到具体的编译或测试错误。可能是逻辑问题或 Claude 执行异常。")

        return "\n".join(parts)

    def _build_fix_prompt(self, task: dict, skill: dict, error_context: str) -> str:
        """构建修复 prompt。子类可覆盖以添加角色特定的修复指导。"""
        instruction = task.get("instruction", task.get("description", ""))

        parts = [
            "## 修复任务",
            "",
            f"原始任务：{instruction}",
            "",
            "上一次执行遇到了问题。请根据以下诊断信息修复错误。",
            "",
            error_context,
            "",
            "## 修复要求",
            "1. 先分析错误的根本原因",
            "2. 只修改必要的文件，不要扩大范围",
            "3. 修复后执行测试/编译验证",
            "4. 如果无法确定根因，保留已有进展，标记 NEED_CLARIFICATION",
            "",
            "## 全自动模式",
            "你处于全自动模式，不能停下来提问。继续执行并尽可能修复问题。",
        ]

        return "\n".join(parts)

    # ── 子类可覆盖的钩子 ──

    def _prepare_execution(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        repo_url: str,
        branch: str,
        base_branch: str,
    ) -> dict | None:
        """预执行准备。返回 {"error": str} 表示失败，None 表示成功。"""
        return None

    def _post_execution(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        repo_url: str,
        claude_r: dict,
        result: dict,
    ) -> None:
        """后执行处理。可修改 result。"""
        pass

    @abstractmethod
    def _construct_prompt(self, task: dict, skill: dict) -> str:
        """构造 Claude Code 的输入提示词。子类必须实现。"""
        raise NotImplementedError

    def _progress_message(self) -> str:
        """执行到 80% 时的进度消息。"""
        return "执行完成，准备收尾"

    # ── 公共工具方法 ──

    def _run_claude(self, prompt_file: Path, workspace: str, tools: str = "", timeout: int = 900, task: dict | None = None) -> dict:
        """调用 Claude Code CLI。支持任务级模型配置。"""
        if SKIP_CLAUDE:
            logger.info("SKIP_CLAUDE=1, skipping claude execution")
            return {"ok": True, "exit_code": 0, "stdout": "dry-run", "stderr": ""}

        if DRY_RUN:
            logger.info("DRY_RUN=1, writing prompt to file instead of running claude")
            return {"ok": True, "exit_code": 0, "stdout": f"prompt written to {prompt_file}", "stderr": ""}

        if not tools:
            tools = TOOL_PERMISSIONS["default"]

        # --bare: 最小化模式，跳过 hooks/LSP/plugin-sync/auto-memory/keychain/CLAUDE.md 自动发现
        # --settings: 指定预配置，跳过首次运行交互
        # --dangerously-skip-permissions: 无人值守时绕过所有权限确认
        settings_file = Path.home() / ".claude" / "settings.json"
        cmd = [
            "claude",
            "-p",
            f"@{prompt_file}",
            "--bare",
            "--settings", str(settings_file),
            "--allowedTools", tools,
            "--dangerously-skip-permissions",
        ]

        # ── 任务级模型配置 ──
        env = dict(os.environ)
        model_config = (task or {}).get("model_config", {})
        resolved_model = model_config.get("resolved_model", "")
        if resolved_model:
            cmd.extend(["--model", resolved_model])
            env["ANTHROPIC_MODEL"] = resolved_model
            logger.info(f"Task-level model override: {resolved_model}")

        # 协议适配相关环境变量（由 entrypoint.sh 设置，但任务级可覆盖）
        protocol_adapter = model_config.get("protocol_adapter", "")
        if protocol_adapter:
            logger.info(f"Protocol adapter: {protocol_adapter}")

        logger.info(f"Running claude in {workspace} with tools={tools}")
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return {
                "ok": result.returncode == 0,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "claude timed out"}
        except FileNotFoundError:
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "claude command not found"}

    def _http_json(self, url: str, method: str = "GET", data: dict | None = None, timeout: int = 30) -> dict:
        """发送 HTTP 请求。"""
        api_token = os.environ.get("AGENT_API_TOKEN", "").strip()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8")}
        except HTTPError as e:
            err = e.read().decode("utf-8") if e.fp else ""
            return {"ok": False, "status": e.code, "error": err}
        except Exception as e:
            return {"ok": False, "status": 0, "error": str(e)}

    def _report_progress(self, task_id: str, progress: int, message: str) -> None:
        """上报任务进度。"""
        from report_result import report_progress
        report_progress(self.dispatcher_base, task_id, progress, message, agent_id=self.agent_id)

    @staticmethod
    def _extract_clarification_questions(stdout: str) -> list[str]:
        """从 AI 输出中提取 NEED_CLARIFICATION 标记后的疑问列表。"""
        if not stdout:
            return []

        import re

        match = re.search(
            r'NEED_CLARIFICATION[:：]\s*(.*?)(?:\n```|\n---|\Z)',
            stdout,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return []

        block = match.group(1).strip()
        if not block:
            return []

        questions = []
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            cleaned = re.sub(r'^(?:\d+[.．]\s*|[\-\*]\s*)', '', line)
            if cleaned:
                questions.append(cleaned)

        return questions
