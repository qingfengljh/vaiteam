"""规范模板加载服务：按技术栈匹配并注入代码风格 / API 规范模板"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GUIDES_DIR = Path(__file__).resolve().parent.parent / "style_guides"
_MAX_TOTAL_CHARS = 8000

_PYTHON_GUIDE = "server/python.md"
_JAVA_GUIDE = "server/java.md"

_STACK_MAP: list[tuple[str, str]] = [
    ("vue", "client/vue.md"),
    ("react", "client/react.md"),
    ("flutter", "client/flutter.md"),
    ("python", _PYTHON_GUIDE),
    ("fastapi", _PYTHON_GUIDE),
    ("django", _PYTHON_GUIDE),
    ("flask", _PYTHON_GUIDE),
    ("java", _JAVA_GUIDE),
    ("springboot", _JAVA_GUIDE),
    ("go", "server/go.md"),
]


def _load(rel_path: str) -> str | None:
    fp = _GUIDES_DIR / rel_path
    if not fp.is_file():
        return None
    try:
        return fp.read_text(encoding="utf-8")
    except OSError:
        logger.warning(f"Failed to read style guide: {fp}")
        return None


def match_guides(tech_stack: str, stage: int, project_type: str = "new") -> str:  # noqa: ARG001
    """根据 tech_stack 关键词 + 阶段匹配规范模板，返回拼接文本。

    - Stage 3（技术方案）始终注入 API 规范
    - Stage 0 maintain/legacy_rewrite 注入代码风格模板（作为分析参照）
    - 其他阶段按需注入
    """
    parts: list[str] = []
    total = 0
    stack_lower = (tech_stack or "").lower()
    seen: set[str] = set()

    if stage == 3:
        api = _load("api/api_convention.md")
        if api:
            parts.append(f"## 参考规范：API 对接规范\n\n{api}")
            total += len(api)
            seen.add("api/api_convention.md")

    for keyword, path in _STACK_MAP:
        if keyword in stack_lower and path not in seen:
            content = _load(path)
            if content and total + len(content) <= _MAX_TOTAL_CHARS:
                parts.append(f"## 参考规范：{Path(path).stem} 代码风格\n\n{content}")
                total += len(content)
                seen.add(path)

    if not parts:
        return ""

    header = (
        "以下是项目遵循的编码规范和 API 约定，生成文档时请参照这些规范。"
        "规范中的 ✅/❌ 示例表示推荐和禁止的写法。\n\n"
    )
    return header + "\n\n---\n\n".join(parts)


def match_code_style(tech_stack: str) -> str:
    """仅加载代码风格模板（不含 API 规范），用于旧代码分析时作为参照。"""
    parts: list[str] = []
    total = 0
    stack_lower = (tech_stack or "").lower()
    seen: set[str] = set()

    for keyword, path in _STACK_MAP:
        if keyword in stack_lower and path not in seen:
            content = _load(path)
            if content and total + len(content) <= _MAX_TOTAL_CHARS:
                parts.append(content)
                total += len(content)
                seen.add(path)

    if not parts:
        return ""
    return "## 项目目标代码风格（分析时的参照基准）\n\n" + "\n\n---\n\n".join(parts)
