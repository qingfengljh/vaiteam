from __future__ import annotations

import time
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent
from app.services import mq

logger = logging.getLogger(__name__)


async def notify_model_config_changed(
    session: AsyncSession,
    *,
    scope: str,
    project_id: str = "",
    module_task_id: str = "",
    reason: str = "",
) -> dict:
    """广播模型配置变更通知给受影响的 Agent。

    scope:
      - global_default: 全局默认配置变化
      - project: 项目级 role_model_map 变化
      - module: 模块级 role_model_map 变化
    """
    version = str(int(time.time()))
    stmt = select(Agent)
    if project_id:
        stmt = stmt.where(Agent.project_id == project_id)
    result = await session.execute(stmt)
    agents = list(result.scalars())

    delivered = 0
    failed = 0
    for agent in agents:
        try:
            await mq.ensure_inbox_group(agent.id)
            await mq.publish_to_inbox(
                agent.id,
                {
                    "msg_id": f"model-config-{version}-{agent.id[:8]}",
                    "task_id": "",
                    "project_id": agent.project_id,
                    "from": "dispatcher:model-config",
                    "to": agent.id,
                    "type": "model_config_changed",
                    "payload": {
                        "scope": scope,
                        "version": version,
                        "project_id": project_id or agent.project_id,
                        "module_task_id": module_task_id,
                        "reason": reason,
                    },
                },
            )
            delivered += 1
        except Exception as e:
            failed += 1
            logger.warning("model config notify failed for %s: %s", agent.id, e)

    return {
        "scope": scope,
        "version": version,
        "project_id": project_id,
        "module_task_id": module_task_id,
        "delivered": delivered,
        "failed": failed,
    }
