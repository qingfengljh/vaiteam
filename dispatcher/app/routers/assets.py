"""项目资料管理：上传代码 zip / API 规范文档，AI 分析"""

import asyncio
import os
import re
import json
import shutil
import zipfile
import tarfile
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session, async_session
from app.core.config import settings
from app.models import Project, ProjectAsset, GenerationTask, Document, Message
from app.services import ai_leader, token_tracker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/assets", tags=["assets"])

IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
    "build", ".idea", ".vscode", ".DS_Store", ".next", ".nuxt",
    "target", "vendor", ".gradle", ".mvn",
}
IGNORE_EXTS = {".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe", ".jar", ".war"}
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".java", ".go", ".rs",
    ".rb", ".php", ".c", ".cpp", ".h", ".cs", ".swift", ".kt",
    ".html", ".css", ".scss", ".less", ".sql", ".sh", ".bash",
    ".yaml", ".yml", ".json", ".toml", ".xml", ".md", ".txt",
    ".dockerfile", ".conf", ".ini", ".env.example", ".gitignore",
}
MAX_FILE_SIZE_FOR_READ = 100 * 1024  # 100KB

ENTRY_PATTERNS = [
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
    "index.ts", "index.js", "main.ts", "main.js", "app.ts", "app.js",
    "server.ts", "server.js", "server.go", "main.go", "cmd/main.go",
    "Application.java", "App.java",
    "Program.cs", "Startup.cs",
]
ENTRY_DIR_PATTERNS = ["src/", "app/", "lib/", "api/", "routes/", "routers/", "controllers/", "views/", "services/"]
CONFIG_PATTERNS = [
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "go.mod", "pom.xml", "build.gradle", "Cargo.toml", "Gemfile",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "README.md", "readme.md",
]

# import/require 正则
_IMPORT_PATTERNS = [
    re.compile(r'^\s*(?:from|import)\s+([a-zA-Z0-9_.]+)', re.MULTILINE),                    # Python
    re.compile(r'''(?:import|require)\s*\(\s*['"]([^'"]+)['"]\s*\)''', re.MULTILINE),        # JS/TS require/import()
    re.compile(r'''import\s+.*?\s+from\s+['"]([^'"]+)['"]''', re.MULTILINE),                 # JS/TS import from
    re.compile(r'''import\s+['"]([^'"]+)['"]''', re.MULTILINE),                               # Go / JS side-effect import
]


def _projects_dir(project_id: str) -> Path:
    return Path(settings.PROJECTS_DIR) / project_id


def _scan_tree(root: Path, max_depth: int = 5) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        if depth > max_depth:
            dirnames.clear()
            continue
        for f in filenames:
            ext = Path(f).suffix.lower()
            if ext in IGNORE_EXTS:
                continue
            files.append(str(rel / f))
    return sorted(files)


def _read_file_safe(path: Path) -> str | None:
    if path.stat().st_size > MAX_FILE_SIZE_FOR_READ:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


# ── 入口追踪：静态分析活跃文件 ──

def _find_entry_files(files: list[str]) -> list[str]:
    """从文件列表中识别入口文件"""
    entries = []
    for f in files:
        basename = Path(f).name
        if basename in ENTRY_PATTERNS:
            entries.append(f)
            continue
        if basename in CONFIG_PATTERNS:
            entries.append(f)
    return entries


def _extract_imports(content: str) -> list[str]:
    """从文件内容中提取 import/require 的模块名"""
    imports = set()
    for pattern in _IMPORT_PATTERNS:
        for match in pattern.finditer(content):
            imports.add(match.group(1))
    return list(imports)


def _resolve_import(module_name: str, source_file: str, all_files: list[str]) -> list[str]:
    """尝试将 import 名解析为实际文件路径"""
    resolved = []
    source_dir = str(Path(source_file).parent)

    # 相对路径 ./xxx ../xxx
    if module_name.startswith("."):
        base = Path(source_dir) / module_name
        candidates = [
            str(base), str(base) + ".py", str(base) + ".ts", str(base) + ".js",
            str(base) + ".tsx", str(base) + ".jsx", str(base) + ".vue",
            str(base / "index.ts"), str(base / "index.js"), str(base / "__init__.py"),
        ]
        for c in candidates:
            normed = os.path.normpath(c)
            if normed in all_files:
                resolved.append(normed)
        return resolved

    # Python 风格: app.models -> app/models.py 或 app/models/__init__.py
    parts = module_name.replace(".", "/")
    candidates = [
        parts + ".py", parts + ".ts", parts + ".js",
        parts + "/index.ts", parts + "/index.js", parts + "/__init__.py",
        parts + ".go",
    ]
    for c in candidates:
        if c in all_files:
            resolved.append(c)

    # 模糊匹配：文件名包含模块名
    if not resolved:
        mod_base = module_name.split(".")[-1].split("/")[-1].lower()
        for f in all_files:
            if Path(f).stem.lower() == mod_base and Path(f).suffix.lower() in CODE_EXTS:
                resolved.append(f)
                if len(resolved) >= 3:
                    break

    return resolved


def _trace_active_files(root: Path, files: list[str], max_depth: int = 8) -> dict:
    """从入口文件出发，沿 import 链追踪活跃文件，返回分析结果"""
    file_set = set(files)
    entries = _find_entry_files(files)

    if not entries:
        for f in files:
            for pattern in ENTRY_DIR_PATTERNS:
                if f.startswith(pattern):
                    entries.append(f)
                    break
            if len(entries) >= 10:
                break

    active = set(entries)
    queue = list(entries)
    visited = set()
    dep_graph = defaultdict(set)

    for _ in range(max_depth):
        next_queue = []
        for f in queue:
            if f in visited:
                continue
            visited.add(f)
            full_path = root / f
            if not full_path.is_file():
                continue
            content = _read_file_safe(full_path)
            if not content:
                continue
            imports = _extract_imports(content)
            for imp in imports:
                resolved = _resolve_import(imp, f, files)
                for r in resolved:
                    dep_graph[f].add(r)
                    if r not in active:
                        active.add(r)
                        next_queue.append(r)
        queue = next_queue
        if not queue:
            break

    inactive = file_set - active
    code_inactive = [f for f in inactive if Path(f).suffix.lower() in CODE_EXTS]

    return {
        "entries": entries,
        "active_files": sorted(active),
        "inactive_files": sorted(code_inactive),
        "total_files": len(files),
        "active_count": len(active),
        "inactive_count": len(code_inactive),
        "dep_graph_size": sum(len(v) for v in dep_graph.values()),
    }


def _build_stack_prompt_hints(files: list[str]) -> str:
    """根据仓库文件特征生成技术栈补充检查项（通用模板上的增强约束）"""
    file_set = set(files)
    suffixes = {Path(f).suffix.lower() for f in files}
    hints: list[str] = []

    def has_name(name: str) -> bool:
        return any(Path(f).name.lower() == name.lower() for f in file_set)

    # Python
    if ".py" in suffixes or has_name("requirements.txt") or has_name("pyproject.toml"):
        hints.append(
            "- Python 项目补充：检查依赖声明完整性（requirements/pyproject 与实际 import 是否一致）、"
            "阻塞 I/O 与 async 混用风险、配置加载与环境变量覆盖链路。"
        )

    # Node / TS / Frontend
    if has_name("package.json") or {".js", ".ts", ".jsx", ".tsx", ".vue"} & suffixes:
        hints.append(
            "- Node/前端项目补充：检查 package 依赖与锁文件一致性、构建产物目录与源码边界、"
            "API 调用层封装一致性与错误处理约定。"
        )

    # Java
    if ".java" in suffixes or has_name("pom.xml") or has_name("build.gradle"):
        hints.append(
            "- Java 项目补充：检查分层边界（controller/service/repository）是否被绕过、"
            "事务边界与异常传播、ORM 查询热点与 N+1 风险。"
        )

    # Go
    if ".go" in suffixes or has_name("go.mod"):
        hints.append(
            "- Go 项目补充：检查 context 传递完整性、goroutine 生命周期与泄漏风险、"
            "错误返回处理一致性。"
        )

    # .NET
    if ".cs" in suffixes or any(f.lower().endswith(".csproj") for f in file_set):
        hints.append(
            "- .NET 项目补充：检查 DI 生命周期配置、异步调用链异常处理、"
            "配置分层（appsettings.* 与环境变量）覆盖关系。"
        )

    # Rust
    if ".rs" in suffixes or has_name("cargo.toml"):
        hints.append(
            "- Rust 项目补充：检查错误类型建模（Result/thiserror/anyhow）一致性、"
            "并发共享状态与所有权边界、unsafe 使用范围。"
        )

    # PHP
    if ".php" in suffixes or has_name("composer.json"):
        hints.append(
            "- PHP 项目补充：检查自动加载规则、配置与密钥管理、"
            "数据库访问层与输入校验策略。"
        )

    if not hints:
        hints.append("- 未识别明显单一技术栈：按通用维护分析模板执行，并在报告中明确技术栈识别不确定性。")

    return "\n".join(hints)


# ── 上传 ──

@router.post("/upload")
async def upload_asset(
    project_id: str,
    asset_type: str = Form(...),
    purpose: str = Form(""),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if asset_type not in ("code", "api_spec"):
        raise HTTPException(400, "asset_type 必须是 code 或 api_spec")
    if asset_type == "code" and purpose not in ("maintain", "learn_style", "legacy_rewrite"):
        raise HTTPException(400, "代码资料必须指定 purpose: maintain, learn_style 或 legacy_rewrite")

    existing = await session.execute(
        select(ProjectAsset).where(
            ProjectAsset.project_id == project_id,
            ProjectAsset.asset_type == asset_type,
        )
    )
    old = existing.scalar_one_or_none()

    base_dir = _projects_dir(project_id)
    if asset_type == "code":
        store_dir = base_dir / "code"
    else:
        store_dir = base_dir / "specs"

    if store_dir.exists():
        shutil.rmtree(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    file_size = len(content)

    if asset_type == "code":
        fname = file.filename.lower()
        archive_path = store_dir / file.filename
        archive_path.write_bytes(content)
        try:
            if fname.endswith(".zip"):
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(store_dir)
            elif fname.endswith(".tar.gz") or fname.endswith(".tgz"):
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(store_dir, filter="data")
            elif fname.endswith(".tar"):
                with tarfile.open(archive_path, "r:") as tf:
                    tf.extractall(store_dir, filter="data")
            elif fname.endswith(".rar"):
                try:
                    import rarfile
                    with rarfile.RarFile(archive_path, "r") as rf:
                        rf.extractall(store_dir)
                except ImportError:
                    shutil.rmtree(store_dir)
                    raise HTTPException(400, "服务器未安装 rar 解压支持")
            else:
                shutil.rmtree(store_dir)
                raise HTTPException(400, "支持的压缩格式：zip、tar.gz、rar")
            archive_path.unlink(missing_ok=True)
        except (zipfile.BadZipFile, tarfile.TarError) as e:
            shutil.rmtree(store_dir)
            raise HTTPException(400, f"无效的压缩文件: {e}")
    else:
        dest = store_dir / file.filename
        dest.write_bytes(content)

    if old:
        old.filename = file.filename
        old.file_path = str(store_dir)
        old.file_size = file_size
        old.purpose = purpose
        old.summary = ""
        old.status = "uploaded"
        asset = old
    else:
        asset = ProjectAsset(
            project_id=project_id,
            asset_type=asset_type,
            purpose=purpose,
            filename=file.filename,
            file_path=str(store_dir),
            file_size=file_size,
        )
        session.add(asset)

    await session.commit()
    await session.refresh(asset)
    return _asset_to_dict(asset)


# ── 从 Git 仓库导入代码 ──

class GitCloneReq(BaseModel):
    git_url: str
    branch: str = ""
    purpose: str = "maintain"
    token: str = ""

@router.post("/clone-git")
async def clone_git_as_asset(
    project_id: str,
    body: GitCloneReq,
    session: AsyncSession = Depends(get_session),
):
    """从 Git 仓库 clone 代码作为项目资料（替代上传压缩包）"""
    import asyncio
    from urllib.parse import urlparse, urlunparse

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if body.purpose not in ("maintain", "learn_style", "legacy_rewrite"):
        raise HTTPException(400, "purpose 必须是 maintain, learn_style 或 legacy_rewrite")

    clone_url = body.git_url
    if body.token and clone_url.startswith("http"):
        parsed = urlparse(clone_url)
        clone_url = urlunparse(parsed._replace(netloc=f"oauth2:{body.token}@{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")))

    existing = await session.execute(
        select(ProjectAsset).where(
            ProjectAsset.project_id == project_id,
            ProjectAsset.asset_type == "code",
        )
    )
    old = existing.scalar_one_or_none()

    store_dir = _projects_dir(project_id) / "code"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1"]
    if body.branch:
        cmd += ["-b", body.branch]
    cmd += [clone_url, str(store_dir)]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        shutil.rmtree(store_dir, ignore_errors=True)
        err_msg = stderr.decode(errors="replace").strip()[-500:]
        raise HTTPException(400, f"Git clone 失败: {err_msg}")

    git_dir = store_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    total_size = sum(f.stat().st_size for f in store_dir.rglob("*") if f.is_file())
    repo_name = body.git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    if old:
        old.filename = f"git:{repo_name}"
        old.file_path = str(store_dir)
        old.file_size = total_size
        old.purpose = body.purpose
        old.summary = ""
        old.status = "uploaded"
        asset = old
    else:
        asset = ProjectAsset(
            project_id=project_id,
            asset_type="code",
            purpose=body.purpose,
            filename=f"git:{repo_name}",
            file_path=str(store_dir),
            file_size=total_size,
        )
        session.add(asset)

    # 记录 Stage0 分析源码库地址，便于后续删除/重试分析时复用
    cfg = dict(project.config or {})
    cfg["stage0_analysis_git_url"] = body.git_url
    cfg["stage0_analysis_git_branch"] = body.branch or ""
    cfg["stage0_analysis_git_purpose"] = body.purpose
    project.config = cfg

    await session.commit()
    await session.refresh(asset)
    return _asset_to_dict(asset)


# ── 列表 ──

@router.get("")
async def list_assets(project_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(ProjectAsset).where(ProjectAsset.project_id == project_id).order_by(ProjectAsset.created_at)
    )
    return [_asset_to_dict(a) for a in q.scalars()]


# ── 查询最近一次代码分析任务（用于刷新后恢复状态） ──

@router.get("/analyze/latest")
async def latest_analyze_task(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.iteration_id == project.current_iteration_id,
            GenerationTask.stage == 0,
            GenerationTask.doc_title == "代码分析",
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    task = q.scalar_one_or_none()
    if not task:
        return {"task": None}

    return {"task": {
        "id": task.id,
        "status": task.status,
        "progress": task.progress,
        "steps": task.steps,
        "error_message": task.error_message or "",
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }}


# ── 删除 ──

@router.delete("/{asset_id}")
async def delete_asset(project_id: str, asset_id: str, session: AsyncSession = Depends(get_session)):
    asset = await session.get(ProjectAsset, asset_id)
    if not asset or asset.project_id != project_id:
        raise HTTPException(404, "Asset not found")

    if asset.file_path and Path(asset.file_path).exists():
        shutil.rmtree(asset.file_path, ignore_errors=True)

    await session.delete(asset)
    await session.commit()
    return {"deleted": True}


# ── AI 分析 ──

class AnalyzeRequest(BaseModel):
    model: str | None = None


@router.post("/{asset_id}/analyze")
async def analyze_asset(
    project_id: str, asset_id: str,
    bg: BackgroundTasks,
    body: AnalyzeRequest = AnalyzeRequest(),
    session: AsyncSession = Depends(get_session),
):
    asset = await session.get(ProjectAsset, asset_id)
    if not asset or asset.project_id != project_id:
        raise HTTPException(404, "Asset not found")

    store_dir = Path(asset.file_path)
    if not store_dir.exists():
        raise HTTPException(400, "资料文件不存在，请重新上传")

    project = await session.get(Project, project_id)
    project_type = project.project_type if project else "new"
    rewrite_reason = project.rewrite_reason if project else ""

    from app.services import model_pool
    if body.model:
        chosen_model = body.model
    else:
        chosen_model = model_pool.resolve_model("architect", project_map=project.role_model_map if project else None)

    is_code = asset.asset_type == "code"
    purpose = asset.purpose
    if is_code and project_type == "legacy_rewrite" and purpose != "learn_style":
        purpose = "legacy_rewrite"

    # maintain 场景要求先在 Stage 0 进行用户对话，再执行代码分析
    if is_code and purpose == "maintain":
        iter_id = project.current_iteration_id if project else None
        if not iter_id:
            raise HTTPException(400, "请先进入 Stage 0 开始对话，再执行代码分析")
        has_user_msg = await session.execute(
            select(Message.id).where(
                Message.project_id == project_id,
                Message.iteration_id == iter_id,
                Message.stage == 0,
                Message.role == "user",
            ).limit(1)
        )
        if not has_user_msg.scalar_one_or_none():
            raise HTTPException(400, "请先在 Stage 0 对话中提供项目背景和关注点，再执行代码分析")

    if is_code and purpose in ("maintain", "legacy_rewrite"):
        steps = [
            {"name": "扫描文件结构与入口追踪", "status": "pending"},
            {"name": "AI 选择关键文件", "status": "pending"},
            {"name": "读取代码内容", "status": "pending"},
            {"name": "AI 生成分析报告", "status": "pending"},
            {"name": "AI 提取 API 规范", "status": "pending"},
            {"name": "AI 提取代码风格", "status": "pending"},
        ]
    elif is_code:
        steps = [
            {"name": "扫描文件结构与入口追踪", "status": "pending"},
            {"name": "AI 选择关键文件", "status": "pending"},
            {"name": "读取代码内容", "status": "pending"},
            {"name": "AI 生成分析报告", "status": "pending"},
        ]
    else:
        steps = [
            {"name": "读取规范文档", "status": "pending"},
            {"name": "AI 分析规范", "status": "pending"},
        ]

    task = GenerationTask(
        project_id=project_id,
        iteration_id=project.current_iteration_id if project else None,
        stage=0,
        doc_title="代码分析" if is_code else "API 规范分析",
        status="running", progress=0, steps=steps,
    )
    session.add(task)
    await session.commit()

    bg.add_task(
        _run_analyze, task.id, asset_id, str(store_dir),
        is_code, purpose, rewrite_reason, chosen_model,
    )
    return {"task_id": task.id}


class TaskCancelled(Exception):
    pass


async def _update_analyze_step(session: AsyncSession, task: GenerationTask, step_idx: int, status: str):
    await session.refresh(task)
    if task.status == "cancelled":
        raise TaskCancelled("Task cancelled by user")
    task.steps[step_idx]["status"] = status
    task.progress = int((sum(1 for s in task.steps if s["status"] == "completed") / len(task.steps)) * 100)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "steps")
    await session.commit()


async def _run_analyze(
    task_id: str, asset_id: str, store_dir_str: str,
    is_code: bool, purpose: str, rewrite_reason: str, model: str | None,
):
    store_dir = Path(store_dir_str)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        asset = await session.get(ProjectAsset, asset_id)
        if not task or not asset:
            return
        token_tracker.set_context(project_id=task.project_id)
        try:
            # 读取 Stage 0 对话历史作为人类指导上下文（固定使用任务所属迭代）
            human_guidance = ""
            if is_code and purpose in ("maintain", "legacy_rewrite"):
                iter_id = task.iteration_id
                msgs_q = await session.execute(
                    select(Message).where(
                        Message.project_id == task.project_id,
                        Message.iteration_id == iter_id,
                        Message.stage == 0,
                    ).order_by(Message.created_at)
                )
                chat_msgs = list(msgs_q.scalars())
                if chat_msgs:
                    compact_msgs = [
                        m for m in chat_msgs
                        if m.role == "system" and isinstance(m.metadata_, dict) and m.metadata_.get("kind") == "chat_compaction"
                    ]
                    if compact_msgs:
                        latest_compact = compact_msgs[-1]
                        source_last_id = (latest_compact.metadata_ or {}).get("source_last_id")
                        recent_user_lines = []
                        for m in chat_msgs:
                            if m.role != "user":
                                continue
                            if isinstance(source_last_id, int) and m.id <= source_last_id:
                                continue
                            recent_user_lines.append(f"**用户补充**: {m.content}")
                        human_guidance = (
                            "### 会话整理摘要（优先）\n"
                            f"{latest_compact.content.strip()}\n\n"
                            "### 整理后新增用户输入\n"
                            + ("\n\n".join(recent_user_lines[-8:]) if recent_user_lines else "（无）")
                        )
                    else:
                        lines = []
                        for m in chat_msgs:
                            if m.role != "user":
                                continue
                            lines.append(f"**用户**: {m.content}")
                        human_guidance = "\n\n".join(lines[-20:])

            if is_code:
                result = await _analyze_code_with_progress(session, task, store_dir, purpose, rewrite_reason, model, human_guidance)
                summary = result["summary"]
            else:
                await _update_analyze_step(session, task, 0, "running")
                summary = await _analyze_api_spec(store_dir, model=model)
                result = {"summary": summary}
                await _update_analyze_step(session, task, 0, "completed")
                await _update_analyze_step(session, task, 1, "completed")

            asset.summary = summary
            asset.status = "analyzed"
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)

            iter_id = task.iteration_id
            if is_code and purpose == "maintain":
                primary_title = "项目维护分析报告"
            elif is_code and purpose == "legacy_rewrite":
                primary_title = "旧系统审计与业务逻辑报告"
            elif is_code and purpose == "learn_style":
                primary_title = "代码风格分析报告"
            else:
                primary_title = "代码分析报告" if is_code else "API 规范分析报告"

            docs_to_save = [(primary_title, summary)]
            if result.get("api_spec"):
                docs_to_save.append(("API 接口规范", result["api_spec"]))
            if result.get("code_style"):
                docs_to_save.append(("代码风格规范", result["code_style"]))

            _TITLE_META = {
                "项目维护分析报告": ("analysis", ["维护分析", "代码分析", "架构"]),
                "旧系统审计与业务逻辑报告": ("analysis", ["旧系统审计", "业务逻辑", "代码分析"]),
                "代码风格分析报告": ("analysis", ["代码风格", "分析"]),
                "代码分析报告": ("analysis", ["代码分析", "架构"]),
                "API 接口规范": ("spec",     ["API", "接口", "规范"]),
                "代码风格规范": ("spec",     ["代码风格", "规范", "lint"]),
            }
            for doc_title, doc_content in docs_to_save:
                existing = await session.execute(
                    select(Document).where(
                        Document.project_id == task.project_id,
                        Document.stage == 0,
                        Document.title == doc_title,
                        Document.iteration_id == iter_id,
                    )
                )
                old_doc = existing.scalar_one_or_none()
                cat, doc_tags = _TITLE_META.get(doc_title, ("general", []))
                if old_doc:
                    old_doc.content = doc_content
                    old_doc.version += 1
                    old_doc.category = cat
                    old_doc.tags = doc_tags
                else:
                    session.add(Document(
                        project_id=task.project_id,
                        iteration_id=iter_id,
                        stage=0,
                        title=doc_title,
                        content=doc_content,
                        status="draft",
                        category=cat,
                        tags=doc_tags,
                    ))

            await session.commit()

        except TaskCancelled:
            logger.info(f"Analyze task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"Analyze task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


async def _analyze_code_with_progress(
    session: AsyncSession, task: GenerationTask,
    store_dir: Path, purpose: str, rewrite_reason: str = "", model: str | None = None,
    human_guidance: str = "",
) -> dict:
    """带进度汇报的代码分析，返回 {"summary": ..., "api_spec"?: ..., "code_style"?: ...}"""
    # Step 0: 扫描文件结构与入口追踪
    await _update_analyze_step(session, task, 0, "running")
    files = _scan_tree(store_dir)
    total_steps = len(task.steps)
    if not files:
        await _update_analyze_step(session, task, 0, "completed")
        for i in range(1, total_steps):
            await _update_analyze_step(session, task, i, "completed")
        return {"summary": "目录为空，无文件可分析"}

    tree_text = "\n".join(files[:500])
    trace = _trace_active_files(store_dir, files)
    active_files = trace["active_files"]
    inactive_files = trace["inactive_files"]
    entries = trace["entries"]
    trace_summary = (
        f"入口文件: {', '.join(entries[:10])}\n"
        f"活跃文件: {trace['active_count']} / {trace['total_files']}\n"
        f"疑似未引用文件: {trace['inactive_count']}"
    )
    logger.info(f"Code trace: {trace_summary}")
    await _update_analyze_step(session, task, 0, "completed")

    # Step 1: AI 选择关键文件
    await _update_analyze_step(session, task, 1, "running")
    priority_files = active_files if active_files else files
    selected_files = await _select_files(files, priority_files, inactive_files, entries, tree_text, purpose, model)
    await _update_analyze_step(session, task, 1, "completed")

    # Step 2: 读取代码内容
    await _update_analyze_step(session, task, 2, "running")
    all_content = _read_selected_files(store_dir, selected_files, priority_files, purpose)
    await _update_analyze_step(session, task, 2, "completed")

    # Step 3: AI 生成分析报告
    await _update_analyze_step(session, task, 3, "running")
    stack_hints = _build_stack_prompt_hints(files)
    summary = await _generate_analysis(
        all_content,
        tree_text,
        trace,
        trace_summary,
        purpose,
        rewrite_reason,
        model,
        human_guidance,
        stack_hints,
    )
    await _update_analyze_step(session, task, 3, "completed")

    result = {"summary": summary}

    if purpose in ("maintain", "legacy_rewrite"):
        # Step 4: AI 提取 API 规范
        await _update_analyze_step(session, task, 4, "running")
        try:
            api_doc = await _generate_api_doc(all_content, tree_text, model)
            result["api_spec"] = api_doc
            await _update_analyze_step(session, task, 4, "completed")
        except Exception as e:
            logger.warning(f"API spec extraction failed (non-blocking): {type(e).__name__}: {e}")
            await _update_analyze_step(session, task, 4, "completed")
            result.setdefault("warnings", []).append(
                f"API 规范提取失败（已跳过，不影响主报告）: {type(e).__name__}: {e}"
            )

        # Step 5: AI 提取代码风格
        await _update_analyze_step(session, task, 5, "running")
        try:
            style_doc = await _generate_style_analysis(all_content, tree_text, model)
            result["code_style"] = style_doc
            await _update_analyze_step(session, task, 5, "completed")
        except Exception as e:
            logger.warning(f"Code style extraction failed (non-blocking): {type(e).__name__}: {e}")
            await _update_analyze_step(session, task, 5, "completed")
            result.setdefault("warnings", []).append(
                f"代码风格提取失败（已跳过，不影响主报告）: {type(e).__name__}: {e}"
            )

    return result


async def _select_files(files, priority_files, inactive_files, entries, tree_text, purpose, model):
    """AI 选择需要深度阅读的文件"""
    if purpose == "learn_style":
        select_prompt = f"""以下是一个项目的文件列表。我只需要学习它的代码风格（命名规范、目录结构、编码习惯）。请选出最有代表性的文件。

文件列表：
{tree_text}

请输出你需要阅读的文件路径列表，每行一个，不要其他内容。选择有代表性的：入口文件、配置文件、1-2个典型业务模块、1个测试文件（如有）。最多选 20 个文件。"""
    else:
        active_text = "\n".join(priority_files[:200])
        inactive_text = "\n".join(inactive_files[:100]) if inactive_files else "无"
        select_prompt = f"""以下是一个项目的文件分析结果。

入口文件（已通过静态分析识别）：
{chr(10).join(entries[:10])}

活跃文件（从入口沿 import 链追踪到的）：
{active_text}

疑似未引用文件（未被任何入口直接或间接引用）：
{inactive_text}

完整文件树：
{tree_text}

请从活跃文件中选出需要深度阅读的文件。优先选择：入口文件、配置文件、核心业务模块、数据模型、路由/API。
如果活跃文件中有遗漏（静态分析可能漏掉动态加载的模块），也可以从完整文件树中补充。
每行输出一个文件路径，不要其他内容。最多选 60 个文件。"""

    selected_text = await ai_leader._call(
        "你是一个代码分析专家。根据要求选择需要阅读的文件。",
        select_prompt, max_tokens=2048, model=model,
    )
    selected = []
    for line in selected_text.strip().split("\n"):
        line = line.strip().lstrip("- ").strip("`")
        if line in files:
            selected.append(line)
    if not selected:
        selected = [f for f in priority_files if Path(f).suffix.lower() in CODE_EXTS][:15]
    return selected


def _read_selected_files(store_dir, selected_files, priority_files, purpose):
    """读取选中文件的内容"""
    file_contents = []
    total_chars = 0
    char_limit = 200_000 if purpose in ("maintain", "legacy_rewrite") else 80_000
    for rel in selected_files:
        full = store_dir / rel
        if not full.is_file():
            continue
        text = _read_file_safe(full)
        if text is None:
            file_contents.append(f"=== {rel} === (文件过大，跳过)")
            continue
        if total_chars + len(text) > char_limit:
            file_contents.append(f"=== {rel} === (达到字符上限，跳过)")
            break
        file_contents.append(f"=== {rel} ===\n{text}")
        total_chars += len(text)
    return "\n\n".join(file_contents)


async def _generate_analysis(
    all_content,
    tree_text,
    trace,
    trace_summary,
    purpose,
    rewrite_reason,
    model,
    human_guidance="",
    stack_hints="",
):
    """根据 purpose 调用 LLM 生成分析报告"""
    if purpose == "legacy_rewrite":
        return await _generate_legacy_analysis(all_content, tree_text, trace, trace_summary, rewrite_reason, model)
    elif purpose == "maintain":
        return await _generate_maintain_analysis(
            all_content,
            tree_text,
            trace_summary,
            model,
            human_guidance,
            stack_hints,
        )
    else:
        return await _generate_style_analysis(all_content, tree_text, model)


async def _generate_legacy_analysis(all_content, tree_text, trace, trace_summary, rewrite_reason, model):
    rewrite_ctx = ""
    if rewrite_reason:
        rewrite_ctx = f"\n用户提出的重写原因和痛点：\n{rewrite_reason}\n\n请重点验证和分析这些问题，给出量化评估。\n"
    analyze_prompt = f"""以下是一个需要完全重写的旧系统代码。请进行深度审计，输出两部分内容：

知识来源约束（必须遵守）：
1) 项目背景知识只能来自两类输入：A. 本次提供的项目源码/源码内文档；B. 用户会话中提供的信息
2) 不得引入任何外部资料、通用行业经验或你自身记忆中的项目背景作为事实
3) 若某结论缺少 A/B 证据，必须标记为“推断”或“未知”

## 第一部分：缺陷分析报告
{rewrite_ctx}
1. **性能瓶颈**：并发处理能力、数据库查询效率、资源利用率、是否有明显的 N+1 查询、阻塞操作等
2. **技术债务**：过时的依赖和框架版本、不规范的代码模式、硬编码、魔法数字、重复代码
3. **可维护性问题**：代码耦合度、模块划分合理性、文档缺失程度、测试覆盖率
4. **AI 协作友好度**：代码是否结构清晰到 AI 能理解和修改、函数粒度是否合理
5. **安全隐患**：SQL 注入、XSS、不安全的认证/授权、敏感信息泄露
6. **扩展性限制**：架构是否支持水平扩展、是否有单点瓶颈
7. **废代码统计**：疑似未引用的文件 {trace['inactive_count']} 个（占比 {trace['inactive_count']*100//max(trace['total_files'],1)}%），列出主要的废代码文件

## 第二部分：业务逻辑文档

1. **系统概述**：项目类型、核心功能、技术栈
2. **核心业务流程**：从代码中逆向提取的主要业务流程（用流程图或步骤描述）
3. **数据模型**：关键实体、表结构、实体关系
4. **API/接口清单**：所有对外接口的路径、方法、用途
5. **业务规则**：从代码中提取的所有业务规则和约束
6. **数据迁移要点**：数据量级估算、数据转换需求、迁移风险

静态分析摘要：
{trace_summary}

项目文件树：
{tree_text[:3000]}

代码内容：
{all_content}"""
    result = await ai_leader._call(
        "你是一个资深代码审计专家。对旧系统进行深度审计，输出结构化的 Markdown 报告。"
        "只基于源码/源码文档与用户会话，不引入外部知识。报告要具体、有数据支撑，不要泛泛而谈。",
        analyze_prompt, max_tokens=16384, model=model, auto_continue=True, temperature=0.3,
    )
    cleaned = _sanitize_llm_output(result)
    return await _enforce_source_restriction(cleaned, model)


async def _generate_maintain_analysis(
    all_content,
    tree_text,
    trace_summary,
    model,
    human_guidance="",
    stack_hints="",
):
    guidance_section = ""
    if human_guidance:
        guidance_section = f"""## 用户提供的项目背景与关注点（重要，优先参考）

以下是用户在对话中提供的项目信息和分析指导，请在分析中优先考虑用户关注的方向：

{human_guidance}

---

"""
    stack_section = ""
    if stack_hints:
        stack_section = f"""## 技术栈适配补充检查项（自动识别）

{stack_hints}

---

"""

    analyze_prompt = f"""{guidance_section}{stack_section}## 分析对象

静态分析摘要：
{trace_summary}

项目文件树：
{tree_text[:3000]}

代码内容：
{all_content}

---

请按照以下结构输出《项目维护分析报告》：
注意：不要输出任何过程性话术（例如“我先查看/Let me ...”），直接给最终报告正文。

知识来源约束（必须遵守）：
1) 项目背景知识只能来自两类输入：A. 本次提供的项目源码/源码内文档；B. 用户会话中提供的信息
2) 不得引入任何外部资料、通用行业经验或你自身记忆中的项目背景作为事实
3) 若某结论缺少 A/B 证据，必须标记为“推断”或“未知”，并放入“缺失信息与待确认问题”

输出硬约束：
1) 每个关键结论都要给“证据锚点”（文件路径/目录），格式示例：`证据: app/analyzer/particle_analyzer.py, app/service/monitor_service.py`
2) 对无法确认的信息，必须标注为“推断”并写明依据，不得当作已确认事实
3) 在报告末尾追加“分析覆盖度声明”：已读取文件规模、未读取但关键的文件类别、结论置信度（高/中/低）
4) 在“建议”中给出可量化的维护基线指标建议（如响应时间、误报率、恢复时间、缺陷回归率等），即便当前缺少实测数据，也要明确“建议采集口径”

### 1. 项目概览
- 项目用途/业务目标
- 系统形态判断（单体/前后端分离/微服务等）
- 技术栈概览
- 当前仓库包含的内容概述

### 2. 项目资产盘点
**2.1 代码资产**：主要模块/目录及职责、入口点、核心组件、关键依赖
**2.2 文档资产**：重要文档清单（主题、作用、位置），对理解项目最关键的资料

### 3. 现有结构与关键约定
- 模块职责划分与层次结构
- 核心调用链/核心流程
- 代码中的约定（命名规范、错误处理方式、设计模式）
- 文档中定义的关键规则或术语

### 4. 维护风险与注意事项
- 高风险模块（高耦合、影响面大）
- 不宜贸然改动的位置及原因
- 隐式依赖与配置风险
- 历史兼容逻辑
- 缺乏测试保护的核心路径
- 知识断层风险（文档缺失导致的理解障碍）

### 5. 缺失信息与待确认问题
逐条列出（每条包含：缺失项、怀疑依据、对维护的影响、需要补充的资料）

### 6. 给后续维护者的建议
- 推荐优先阅读的内容
- 建议先理解的模块
- 修改前应先确认的约束
- 后续阶段可继续深入的方向

### 7. 分析覆盖度声明
- 本次分析已覆盖（按目录/模块）
- 本次未覆盖但影响重大的文件或模块
- 结论置信度（高/中/低）及原因
- 建议补充采集的数据与验证动作"""

    system = """你是一个资深技术分析师，正在对一个需要维护的项目进行全面的维护性分析。

核心原则：
- 现状优先：以项目实际代码、配置、文档为准，不以理想架构代替现状
- 维护导向：重点关注"如何理解、接手、安全修改"，不优先讨论重构
- 文档代码同等：代码和文档都是重要依据
- 来源受限：项目背景仅可基于“源码/源码文档 + 用户会话”形成，不得扩展到外部知识
- 最小假设：没有明确证据的信息，不做过度脑补
- 完整性检查：不仅看有什么，还要判断本应有什么但缺失
- 谨慎评价：不因项目老旧就下负面结论，先解释现状和维护影响

质量要求：
- 结论有依据，区分"已确认"和"推断"
- 覆盖全面，不遗漏重要文档和关键代码
- 不把缺失当不存在
- 面向维护者，重点突出"可维护性理解"而非"理想化设计评审"
- 不要在本次输出中向用户反问；缺失信息直接写入“缺失信息与待确认问题”，并继续完成报告
- 禁止输出思考过程、执行步骤、工具调用痕迹或前置寒暄

输出结构化的 Markdown 报告。"""

    result = await ai_leader._call(
        system,
        analyze_prompt, max_tokens=16384, model=model, auto_continue=True, temperature=0.3,
    )
    cleaned = _sanitize_llm_output(result)
    return await _enforce_source_restriction(cleaned, model)


async def _generate_style_analysis(all_content, tree_text, model):
    analyze_prompt = f"""以下是一个项目的代码。请提取代码风格特征：

1. **技术栈**：语言、框架、工具
2. **命名规范**：变量/函数/类/文件的命名风格
3. **目录结构**：组织方式
4. **编码习惯**：缩进、注释风格、错误处理方式、导入顺序
5. **设计模式**：使用的架构和设计模式
6. **API 风格**：接口设计规范（如有）
7. **质量门禁建议**：最小可执行规范（lint/format/type-check/test）

输出要求（重要）：
- 如果项目同时包含前端与后端，请分别输出“前端代码风格规范”和“后端代码风格规范”两个小节
- 分别说明二者命名、目录组织、错误处理、测试约定上的差异，不要混写
- 若仅有单端代码，明确写“仅检测到单端代码”
- 输出应是“规范与建议”，不是 Stage0 需求文档，不需要业务痛点/需求边界/KPI 验收项

项目文件树：
{tree_text[:2000]}

代码内容：
{all_content}"""
    result = await ai_leader._call(
        "你是一个资深代码分析专家。输出结构化的 Markdown 分析报告。",
        analyze_prompt, max_tokens=16384, model=model, auto_continue=True, temperature=0.3,
    )
    return _sanitize_llm_output(result)


async def _generate_api_doc(all_content, tree_text, model):
    analyze_prompt = f"""以下是一个项目的代码。请从代码中提取**可直接用于开发联调**的 API/接口规范文档。

硬约束（必须遵守）：
1) 不要输出“接口目录”式文档，必须输出“接口规范”文档
2) 每个接口都要包含：路径、方法、鉴权要求、Path/Query/Header 参数、请求体、响应体、状态码映射、业务说明
3) 每个接口至少给 1 个请求示例和 1 个响应示例（JSON）
4) 如果某项在代码中无法确认，明确写“未确认（需补充）”，不要编造
5) 所有统计数字必须精确，不允许使用“约”“大概”等措辞
6) 仅基于源码/源码文档输出，不引入外部常识

请按以下结构输出：

## 1. 接口概览
- 精确接口数量（REST / WebSocket / 其他）
- 基础路径与版本策略（若无版本，明确写“未实现版本化”）
- 认证与鉴权（Token 位置、格式、有效期/刷新机制是否可确认）

## 2. 统一约定
- 统一响应格式（成功/失败）
- 错误码规范（通用码 + 业务码，未确认项需标注）
- 分页/排序/过滤约定（若缺失需指出）

## 3. 接口规范（逐接口）
对每个接口使用固定模板：
- 接口标识：`METHOD PATH`
- 鉴权：必需/可选/无
- 参数：
  - Path 参数（名称、类型、必填、说明）
  - Query 参数（名称、类型、必填、默认值、说明）
  - Header 参数（名称、类型、必填、说明）
  - Body（字段、类型、必填、约束）
- 成功响应：字段结构 + 示例
- 错误响应：状态码 + code + message + 触发条件
- 业务规则与边界条件

## 4. WebSocket/HLS（如有）
- 连接地址、鉴权方式、订阅/推送方向、心跳机制、断线重连约定
- 消息类型定义（客户端->服务端 与 服务端->客户端）

## 5. 数据模型
- 请求/响应核心模型（字段、类型、含义、是否可为空）
- 模型间关系与 ID 映射关系（如 line_id/source_id/device_id）

## 6. 风险与待确认项
- 路径冲突、鉴权不完整、敏感字段泄露等风险
- 明确列出“未确认（需补充）”项

项目文件树：
{tree_text[:2000]}

代码内容：
{all_content}"""
    result = await ai_leader._call(
        "你是一个资深 API 设计专家。从代码中逆向提取接口规范，输出结构化的 Markdown API 文档。"
        "禁止输出任何工具调用、XML/HTML 标签、思考过程或伪代码执行步骤（如 <tool_use>、read_file、create_file）。"
        "只输出最终文档正文。",
        analyze_prompt, max_tokens=16384, model=model, auto_continue=True, temperature=0.3,
    )
    return _sanitize_llm_output(result)


def _sanitize_llm_output(text: str) -> str:
    """清理模型误输出的工具调用片段，尽量恢复最终正文。"""
    if not text:
        return ""

    s = text.strip()

    # 某些模型会把 create_file 的 arguments JSON 输出出来，优先提取 content 正文
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

    # 常规清理：移除工具调用标签块
    s = re.sub(r"<tool_use>.*?</tool_use>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"</?(server_name|tool_name|arguments)>", "", s, flags=re.IGNORECASE)

    filtered = []
    chatter_cn = (
        "我需要先", "让我先", "现在我已经有足够", "我先", "我将先", "我会先", "首先我会", "接下来我会",
    )
    for line in s.splitlines():
        l = line.strip()
        if not l:
            filtered.append(line)
            continue
        lower = l.lower()
        if lower.startswith("let me ") or lower.startswith("now let me ") or lower.startswith("now i have enough context"):
            continue
        if l.startswith(chatter_cn):
            continue
        if lower.startswith("i will first") or lower.startswith("i'll first") or lower.startswith("first, i will"):
            continue
        if "read_file" in lower or "create_file" in lower:
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered).strip()
    if not cleaned:
        return cleaned

    # 如果前面仍有“对话式前言”，从第一个结构化正文段开始截断
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
        head = "\n".join(lines[:body_idx]).lower()
        if "let me" in head or "i will first" in head or "我先" in head or "我会先" in head:
            return "\n".join(lines[body_idx:]).strip()
    return cleaned


def _has_external_knowledge_markers(text: str) -> bool:
    """检测是否包含明显的外部知识/经验化表达。"""
    if not text:
        return False
    lower = text.lower()
    markers = (
        "行业经验",
        "业内通常",
        "通常做法",
        "最佳实践",
        "best practice",
        "in general",
        "generally speaking",
        "根据公开资料",
        "根据网上资料",
        "according to public",
        "according to online",
    )
    return any(m in lower for m in markers)


async def _enforce_source_restriction(text: str, model: str | None) -> str:
    """如命中外部知识措辞，自动重写为仅基于源码与会话的版本。"""
    cleaned = _sanitize_llm_output(text)
    if not _has_external_knowledge_markers(cleaned):
        return cleaned

    repair_prompt = f"""请将下面这份《项目维护分析报告》改写为“来源合规版本”。

强制要求：
1) 只能保留可由“源码/源码文档 + 用户会话”支持的内容
2) 删除或改写任何外部经验化表达（如“行业经验/通常做法/最佳实践/according to ...”）
3) 无法由给定信息支撑的结论，改写为“推断”或“未知”，并放入“缺失信息与待确认问题”
4) 保留原有报告结构和主要信息密度，不要加入无关新内容
5) 只输出最终 Markdown 正文

待改写内容：
{cleaned}
"""
    rewritten = await ai_leader._call(
        "你是一个文档合规清洗器。只做来源合规改写，不扩写新事实。",
        repair_prompt,
        max_tokens=16384,
        model=model,
        auto_continue=True,
        temperature=0.2,
    )
    repaired = _sanitize_llm_output(rewritten)
    if _has_external_knowledge_markers(repaired):
        logger.warning("Maintain report still contains external-knowledge markers after auto-repair")
    return repaired


async def _analyze_code(store_dir: Path, purpose: str, rewrite_reason: str = "", model: str | None = None, **_kw) -> str:
    """无进度汇报版本，复用拆分后的函数"""
    files = _scan_tree(store_dir)
    if not files:
        return "目录为空，无文件可分析"

    tree_text = "\n".join(files[:500])
    trace = _trace_active_files(store_dir, files)
    priority_files = trace["active_files"] if trace["active_files"] else files
    trace_summary = (
        f"入口文件: {', '.join(trace['entries'][:10])}\n"
        f"活跃文件: {trace['active_count']} / {trace['total_files']}\n"
        f"疑似未引用文件: {trace['inactive_count']}"
    )
    logger.info(f"Code trace: {trace_summary}")

    selected_files = await _select_files(files, priority_files, trace["inactive_files"], trace["entries"], tree_text, purpose, model)
    all_content = _read_selected_files(store_dir, selected_files, priority_files, purpose)
    stack_hints = _build_stack_prompt_hints(files)
    return await _generate_analysis(
        all_content,
        tree_text,
        trace,
        trace_summary,
        purpose,
        rewrite_reason,
        model,
        stack_hints=stack_hints,
    )


async def _analyze_api_spec(store_dir: Path, model: str | None = None) -> str:
    """分析 API 规范文档"""
    contents = []
    for f in store_dir.iterdir():
        if f.is_file():
            text = _read_file_safe(f)
            if text:
                contents.append(f"=== {f.name} ===\n{text}")

    if not contents:
        return "未找到可读取的规范文档"

    all_content = "\n\n".join(contents)

    return await ai_leader._call(
        "你是一个资深 API 设计专家。审核 API 规范文档，给出分析和改进建议。",
        f"""请审核以下 API 规范文档：

{all_content}

请输出：
1. **规范概述**：覆盖的接口数量、风格（RESTful/GraphQL 等）
2. **优点**：做得好的地方
3. **问题和建议**：不足之处和具体改进建议
4. **风格总结**：提取 API 设计风格特征（URL 命名、请求/响应格式、错误处理、认证方式等）

注意：这些建议仅供参考，用户可以选择采纳或保持原貌。""",
        max_tokens=4096,
        model=model,
    )


def _asset_to_dict(asset: ProjectAsset) -> dict:
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "asset_type": asset.asset_type,
        "purpose": asset.purpose,
        "filename": asset.filename,
        "file_size": asset.file_size,
        "summary": asset.summary,
        "status": asset.status,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
    }
