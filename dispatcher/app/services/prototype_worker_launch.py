"""在项目执行环境组节点上通过 SSH+rsync+docker compose 拉起原型 CC Worker 容器。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import textwrap
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import claw_deployer
from app.services.deploy_manager import DISPATCHER_SHARED_SSH_KEY, _resolve_node_ssh_key, get_infra_node

logger = logging.getLogger(__name__)


def _ssh_key(node) -> str:
    k = _resolve_node_ssh_key(node)
    if k and os.path.exists(os.path.expanduser(k)):
        return k
    if os.path.exists(DISPATCHER_SHARED_SSH_KEY):
        return DISPATCHER_SHARED_SSH_KEY
    return ""


async def _ssh_exec(host: str, port: int, user: str, key_file: str, remote_cmd: str, timeout: int = 180) -> dict[str, Any]:
    resolved = os.path.expanduser((key_file or "").strip())
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        resolved,
        "-p",
        str(port),
        f"{user}@{host}",
        remote_cmd,
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"exit_code": -1, "stderr": "ssh timeout", "stdout": ""}


async def launch_prototype_cc_worker(
    session: AsyncSession,
    *,
    project_id: str,
    run_id: str,
    run_secret: str,
) -> dict[str, Any]:
    """在 infra 节点推送 compose 并 `docker compose up -d`。失败返回 ok=False（run 仍可人工 wrapper）。"""
    # 清理旧的原型 nginx 容器（只保留最新的）
    import asyncio as _asyncio
    try:
        cleanup = await _asyncio.create_subprocess_exec(
            "sh", "-c",
            "docker ps -aq --filter 'name=proto-nginx-' | xargs -r docker rm -f 2>/dev/null; "
            "docker ps -aq --filter 'name=proto-cc-' | grep -v $(docker ps -aq --filter 'name=proto-cc-' | head -1) | xargs -r docker rm -f 2>/dev/null; "
            "true",
            stdout=_asyncio.DEVNULL, stderr=_asyncio.DEVNULL,
        )
        await cleanup.wait()
    except Exception:
        pass

    base = (settings.DISPATCHER_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "error": "未配置 DISPATCHER_PUBLIC_BASE_URL，远程容器无法访问 Dispatcher"}

    node = await get_infra_node(session, project_id, role="agent")
    if not node:
        # 无 infra 节点时尝试本地 Docker
        logger.info("No infra node available, trying local Docker for prototype worker")
        return await _launch_local_docker(
            project_id=project_id, run_id=run_id, run_secret=run_secret,
        )

    key_file = _ssh_key(node)
    if not key_file:
        logger.info("SSH key unavailable, trying local Docker for prototype worker")
        return await _launch_local_docker(
            project_id=project_id, run_id=run_id, run_secret=run_secret,
        )

    image = (settings.PROTOTYPE_CC_WORKER_IMAGE or "").strip() or "openclaw/prototype-cc-worker:latest"

    # 从 Agent Provider Pool 获取 API Key 注入原型容器
    from app.services import agent_provider_pool
    provider = agent_provider_pool.resolve_agent_provider("architect", "claude_code")
    cc_api_key = (provider or {}).get("api_key", "")
    cc_api_base = (provider or {}).get("api_base", "")

    import asyncio as _asyncio, random as _random
    container_name = f"proto-cc-{run_id[:12]}"
    nginx_name = f"proto-nginx-{run_id[:12]}"
    volume_name = f"proto-out-{run_id[:12]}"
    preview_port = str(_random.randint(32000, 32999))

    # 创建共享卷
    await _asyncio.create_subprocess_exec("docker", "volume", "create", volume_name,
        stdout=_asyncio.subprocess.DEVNULL, stderr=_asyncio.subprocess.DEVNULL)

    # 启动 CC Worker
    cc_proc = await _asyncio.create_subprocess_exec(
        "docker", "run", "-d", "--name", container_name,
        "--restart", "no",
        "-v", f"{volume_name}:/workspace",
        "-e", f"VAI_DISPATCHER_BASE={base}",
        "-e", f"VAI_PROJECT_ID={project_id}",
        "-e", f"VAI_RUN_ID={run_id}",
        "-e", f"VAI_RUN_SECRET={run_secret}",
        "-e", "VAI_PACK_USE_SECRET=1",
        "-e", "VAI_SKIP_CLAUDE=0",
        "-e", "VAI_WORKDIR=/workspace",
        "-e", "VAI_KEEP_WORKDIR=1",
        "-e", f"CC_ANTHROPIC_API_KEY={cc_api_key}",
        "-e", f"CC_ANTHROPIC_BASE_URL={cc_api_base}",
        "--entrypoint", "/opt/vaiteam-cc-worker/run_cc_wrapper.sh",
        image,
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
    )
    await cc_proc.communicate()

    # 启动 nginx 预览
    await _asyncio.create_subprocess_exec(
        "docker", "run", "-d", "--name", nginx_name,
        "--restart", "no",
        "-p", f"{preview_port}:80",
        "-v", f"{volume_name}:/usr/share/nginx/html:ro",
        "nginx:alpine",
        stdout=_asyncio.subprocess.DEVNULL, stderr=_asyncio.subprocess.DEVNULL,
    )
    preview_url = f"http://DISPATCHER_HOST_PLACEHOLDER:{preview_port}"

    return {
        "ok": True,
        "host": "DISPATCHER_HOST_PLACEHOLDER",
        "image": image,
        "dispatcher_base": base,
        "preview_url": preview_url,
        "container_name": container_name,
        "nginx_name": nginx_name,
    }


async def _launch_local_docker(
    *,
    project_id: str,
    run_id: str,
    run_secret: str,
) -> dict[str, Any]:
    """SSH 不可用时，直接在本地 Docker 启动原型 CC Worker 容器。"""
    base = (settings.DISPATCHER_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "error": "未配置 DISPATCHER_PUBLIC_BASE_URL"}

    image = (settings.PROTOTYPE_CC_WORKER_IMAGE or "").strip() or "openclaw/prototype-cc-worker:latest"
    container_name = f"prototype-cc-run-{run_id[:12]}"

    from app.services import agent_provider_pool
    provider = agent_provider_pool.resolve_agent_provider("architect", "claude_code")
    cc_api_key = (provider or {}).get("api_key", "")
    cc_api_base = (provider or {}).get("api_base", "")

    import asyncio
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "-d", "--name", container_name,
        "--restart", "no",
        "--entrypoint", "/opt/vaiteam-cc-worker/run_cc_wrapper.sh",
        "-e", f"VAI_DISPATCHER_BASE={base}",
        "-e", f"VAI_PROJECT_ID={project_id}",
        "-e", f"VAI_RUN_ID={run_id}",
        "-e", f"VAI_RUN_SECRET={run_secret}",
        "-e", "VAI_PACK_USE_SECRET=1",
        "-e", "VAI_SKIP_CLAUDE=0",
        "-e", "VAI_WORKDIR=/workspace/out",
        "-e", "VAI_KEEP_WORKDIR=1",
        "-e", f"CC_ANTHROPIC_API_KEY={cc_api_key}",
        "-e", f"CC_ANTHROPIC_BASE_URL={cc_api_base}",
        image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"ok": False, "error": f"docker run failed: {stderr.decode()[:500]}"}

    cid = stdout.decode().strip()
    logger.info(f"Prototype CC Worker launched locally: {container_name} ({cid[:12]})")

    # 获取 nginx 预览端口
    import asyncio as _asyncio
    port_proc = await _asyncio.create_subprocess_exec(
        "docker", "port", container_name, "80",
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
    )
    port_out, _ = await port_proc.communicate()
    preview_port = port_out.decode().strip().split(":")[-1] if port_proc.returncode == 0 else ""
    preview_url = f"http://localhost:{preview_port}" if preview_port else ""

    return {"ok": True, "container_id": cid, "container_name": container_name, "image": image, "dispatcher_base": base, "preview_url": preview_url}
