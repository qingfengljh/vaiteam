"""
OpenClaw 通信层

通过 Webhook API 与 OpenClaw 实例交互。
每个方法职责单一，不混入业务逻辑。
"""

import logging
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.OPENCLAW_HOOK_TOKEN}", "Content-Type": "application/json"}


async def _post(path: str, payload: dict, timeout: int = 30, base_url: str = "") -> dict:
    url = f"{base_url or settings.OPENCLAW_GATEWAY_URL}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"OpenClaw POST {url} failed: {e}")
            return {"error": str(e)}


async def send_task(
    agent_id: str,
    instruction: str,
    metadata: dict | None = None,
    model: str | None = None,
    gateway_url: str = "",
) -> dict:
    payload: dict = {
        "agentId": agent_id,
        "message": instruction,
        "wakeMode": "now",
    }
    if model:
        payload["model"] = model
    if metadata:
        payload["metadata"] = metadata
    return await _post("/hooks/agent", payload, base_url=gateway_url)


async def send_message(agent_id: str, message: str, model: str | None = None, gateway_url: str = "") -> dict:
    payload: dict = {"agentId": agent_id, "message": message, "wakeMode": "now"}
    if model:
        payload["model"] = model
    return await _post("/hooks/agent", payload, base_url=gateway_url)


async def health_check() -> bool:
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{settings.OPENCLAW_GATEWAY_URL}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


# ── 容器操作（备份/恢复用） ──

async def exec_in_agent(container_id: str, command: str) -> dict:
    """在 agent 容器内执行命令"""
    return await _post("/hooks/exec", {"containerId": container_id, "command": command}, timeout=120)


async def copy_from_agent(container_id: str, src: str, dest: str) -> dict:
    """从 agent 容器复制文件到宿主机"""
    return await _post("/hooks/copy-from", {"containerId": container_id, "src": src, "dest": dest}, timeout=300)


async def copy_to_agent(container_id: str, src: str, dest: str) -> dict:
    """从宿主机复制文件到 agent 容器"""
    return await _post("/hooks/copy-to", {"containerId": container_id, "src": src, "dest": dest}, timeout=300)
