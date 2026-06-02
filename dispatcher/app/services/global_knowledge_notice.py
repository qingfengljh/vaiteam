from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Project, TeamChat, new_id
from app.services import global_knowledge, mq


def _default_notice_summary() -> str:
    return "全局规范已更新，请全员补训并按新规则执行。"


async def ensure_entry_submitted(session: AsyncSession, project: Project, sender_id: str) -> str:
    cfg = dict(project.config or {})
    text = (cfg.get("global_knowledge_content") or "").strip()
    if text:
        return text

    text = (global_knowledge.read_entry_text(project.id) or "").strip()
    if not text:
        text = (
            f"# {project.name} 全局知识入口\n\n"
            "## 当前约束\n"
            "- 以任务流转为中心推进开发\n"
            "- 所有成员按职责协作，优先保证可交付\n"
            "- 重大变更须同步到项目文档\n"
        )

    cfg["global_knowledge_content"] = text
    cfg["global_knowledge_entry_path"] = global_knowledge.GLOBAL_KNOWLEDGE_ENTRY
    cfg["global_knowledge_updated_at"] = datetime.now(timezone.utc).timestamp()
    cfg["global_knowledge_updated_by"] = sender_id or "architect"
    cfg["global_knowledge_content_hash"] = global_knowledge.calc_version(text)
    if global_knowledge.to_revision(cfg.get("global_knowledge_revision")) <= 0:
        cfg["global_knowledge_revision"] = 1
        cfg["global_knowledge_version"] = global_knowledge.format_revision(1)
    project.config = cfg
    await session.flush()
    return text


async def notify_project_agents(
    session: AsyncSession,
    project_id: str,
    sender_id: str = "human",
    summary: str = "",
) -> dict:
    project = await session.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    text = await ensure_entry_submitted(session, project, sender_id=sender_id)
    if not text:
        raise ValueError("全局知识公告板未初始化，请先提交公告板内容")

    content_hash = global_knowledge.calc_version(text)
    refs = global_knowledge.extract_local_refs(text)[:8]
    project_cfg = dict(project.config or {})
    prev_hash = project_cfg.get("global_knowledge_content_hash") or ""
    prev_revision = global_knowledge.to_revision(project_cfg.get("global_knowledge_revision"))
    version_changed = prev_hash != content_hash
    revision = prev_revision + 1 if version_changed else prev_revision
    if revision <= 0:
        revision = 1
    version = global_knowledge.format_revision(revision)

    notice_summary = (summary or _default_notice_summary()).strip()
    refs_text = "\n".join(f"- {r}" for r in refs) if refs else "- 无额外引用"
    content = (
        "【全局变更通知】\n\n"
        f"项目：{project.name}\n"
        f"入口：{global_knowledge.GLOBAL_KNOWLEDGE_ENTRY}\n"
        f"版本：{version}\n"
        f"内容摘要哈希：{content_hash}\n\n"
        f"变更说明：{notice_summary}\n\n"
        f"引用文档：\n{refs_text}"
    )

    q = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = list(q.scalars())
    pending_count = 0
    for agent in agents:
        cfg = dict(agent.config or {})
        ack_revision = global_knowledge.to_revision(cfg.get("global_knowledge_ack_revision"))
        cfg["global_knowledge_last_notified_version"] = version
        cfg["global_knowledge_last_notified_revision"] = revision
        if ack_revision < revision:
            cfg["global_knowledge_pending_version"] = version
            cfg["global_knowledge_pending_revision"] = revision
            pending_count += 1
        else:
            cfg.pop("global_knowledge_pending_version", None)
            cfg.pop("global_knowledge_pending_revision", None)
        agent.config = cfg

    project_cfg["global_knowledge_version"] = version
    project_cfg["global_knowledge_revision"] = revision
    project_cfg["global_knowledge_prev_revision"] = prev_revision
    project_cfg["global_knowledge_content_hash"] = content_hash
    project_cfg["global_knowledge_prev_content_hash"] = prev_hash
    project_cfg["global_knowledge_version_changed"] = version_changed
    project_cfg["global_knowledge_content"] = text
    project_cfg["global_knowledge_entry_path"] = global_knowledge.GLOBAL_KNOWLEDGE_ENTRY
    project_cfg["global_knowledge_updated_at"] = datetime.now(timezone.utc).timestamp()
    project_cfg["global_knowledge_updated_by"] = sender_id or "human"
    project.config = project_cfg

    mention_ids = [a.id for a in agents]
    chat = TeamChat(
        id=new_id(),
        project_id=project_id,
        sender_type="human",
        sender_id=sender_id or "human",
        mentions=mention_ids,
        task_ref="",
        content=content,
        status="sent",
        metadata_={
            "kind": "global_knowledge_notify",
            "version": version,
            "revision": revision,
            "content_hash": content_hash,
            "entry": global_knowledge.GLOBAL_KNOWLEDGE_ENTRY,
        },
    )
    session.add(chat)
    await session.flush()

    delivered = False
    for agent in agents:
        try:
            await mq.ensure_inbox_group(agent.id)
            await mq.publish_to_inbox(agent.id, {
                "msg_id": chat.id,
                "task_id": "",
                "project_id": project_id,
                "from": f"human:{sender_id or 'human'}",
                "to": agent.id,
                "type": "human_message",
                "payload": {
                    "kind": "global_knowledge_notify",
                    "version": version,
                    "revision": revision,
                    "content": content,
                    "task_ref": "",
                    "chat_msg_id": chat.id,
                },
            })
            delivered = True
        except Exception:
            continue

    chat.status = "delivered" if delivered else "failed"
    await session.flush()
    return {
        "status": chat.status,
        "version": version,
        "revision": revision,
        "content_hash": content_hash,
        "mentions": len(mention_ids),
        "chat_id": chat.id,
        "version_changed": version_changed,
        "pending_ack_agents": pending_count,
    }
