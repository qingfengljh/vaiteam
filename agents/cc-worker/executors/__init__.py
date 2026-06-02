"""CC Worker 角色执行器工厂。

根据 AGENT_ROLE 选择对应的执行器实现，避免 if/else 判断。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 延迟导入避免循环依赖
def get_executor(agent_id: str, role: str, dispatcher_base: str):
    """根据角色返回对应的执行器实例。"""
    role_clean = (role or "mid").lower().strip()

    if role_clean == "archaeologist":
        from .archaeologist_executor import ArchaeologistExecutor
        return ArchaeologistExecutor(agent_id, role_clean, dispatcher_base)

    # 默认：编码角色（senior/mid/junior/architect/devops/tester）
    from .coding_executor import CodingExecutor
    return CodingExecutor(agent_id, role_clean, dispatcher_base)
