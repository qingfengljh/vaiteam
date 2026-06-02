from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import init_db, engine, async_session
from app.core.auth import AuthMiddleware
from app.core.project_access_middleware import ProjectAccessMiddleware
from app.core.migration import run_migrations
from app.core import openclaw_config, redis as redis_pool
from app.services import model_pool, token_tracker, infra, mq_worker, heartbeat, scheduler_loop, backup, review_lock
from app.routers import projects, stages, tasks, task_comments, task_documents, agents, webhook, providers, deploy, experiences, documents, auth, assets, iterations, analytics, infra_nodes, change_requests, help, git, messages, chat, uploads, conversations, private_runtime, portal_internal, prototype_workshop, worker, agent_providers
from app.core.config import settings
from app.services.auth_bootstrap import bootstrap_initial_auth_from_env


@asynccontextmanager
async def lifespan(app: FastAPI):
    openclaw_config.load()
    await init_db()
    await run_migrations(engine)
    await bootstrap_initial_auth_from_env()
    async with async_session() as session:
        await model_pool.load_providers(session)
        await token_tracker.load_prices(session)
        from app.services import agent_provider_pool
        await agent_provider_pool.load_agent_providers(session)
        from app.routers.providers import restore_ssh_keys_from_db
        await restore_ssh_keys_from_db(session)
    await _cleanup_stale_tasks()
    await _discover_ollama()
    infra.init()
    await mq_worker.start()
    await heartbeat.start()
    await review_lock.start()
    await scheduler_loop.start()
    await backup.start()
    yield
    await backup.stop()
    await scheduler_loop.stop()
    await review_lock.stop()
    await heartbeat.stop()
    await mq_worker.stop()
    await redis_pool.close()


app = FastAPI(title="AI Dev Team Dispatcher", lifespan=lifespan)

app.add_middleware(ProjectAccessMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(portal_internal.router)
app.include_router(projects.router)
app.include_router(stages.router)
app.include_router(tasks.router)
app.include_router(task_comments.router)
app.include_router(task_documents.router)
app.include_router(agents.router)
app.include_router(providers.router)
app.include_router(deploy.router)
app.include_router(experiences.router)
app.include_router(documents.router)
app.include_router(prototype_workshop.router)
app.include_router(prototype_workshop.worker_router)
app.include_router(assets.router)
app.include_router(iterations.router)
app.include_router(analytics.router)
app.include_router(infra_nodes.router)
app.include_router(change_requests.router)
app.include_router(help.router)
app.include_router(git.router)
app.include_router(messages.router)
app.include_router(chat.router)
app.include_router(uploads.router)
app.include_router(conversations.router)
app.include_router(private_runtime.router)
app.include_router(agent_providers.router)
app.include_router(worker.router)
app.include_router(webhook.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "release_version": settings.VAITEAM_RELEASE_VERSION or None,
        "git_sha": settings.VAITEAM_GIT_SHA or None,
    }


async def _discover_ollama():
    """启动时按 OLLAMA 角色 key 查找节点，自动配置 OLLAMA_BASE_URL"""
    import logging
    from sqlalchemy import select
    from app.models import InfraNode
    from app.core.config import settings
    from app.core.constants import INFRA_ROLE_HEALTH
    logger = logging.getLogger(__name__)
    try:
        health_cfg = INFRA_ROLE_HEALTH.get("OLLAMA", {})
        health_path = health_cfg.get("health_path", "/api/tags")
        default_port = health_cfg.get("default_port", 11434)

        async with async_session() as session:
            q = await session.execute(select(InfraNode))
            for node in q.scalars():
                if "OLLAMA" not in {r.upper() for r in (node.roles or [])}:
                    continue
                svc_url = node.config.get("service_url", f"http://{node.host}:{default_port}")
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.get(f"{svc_url}{health_path}")
                        if resp.status_code == 200:
                            models = [m.get("name", "") for m in resp.json().get("models", [])]
                            settings.OLLAMA_BASE_URL = svc_url
                            settings.OLLAMA_ENABLED = True
                            logger.info(f"Ollama discovered: {svc_url} models={models}")
                            return
                except Exception:
                    continue
        logger.info(f"No reachable Ollama node found, using default: {settings.OLLAMA_BASE_URL}")
    except Exception as e:
        logger.warning(f"Ollama discovery failed (non-blocking): {e}")


async def _cleanup_stale_tasks():
    """启动时将所有 running/pending 的 GenerationTask 标记为 failed（上次重启后的僵尸任务）"""
    import logging
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from app.models import GenerationTask
    logger = logging.getLogger(__name__)
    try:
        async with async_session() as session:
            q = await session.execute(
                select(GenerationTask).where(GenerationTask.status.in_(["running", "pending"]))
            )
            stale = q.scalars().all()
            if not stale:
                return
            await session.execute(
                update(GenerationTask)
                .where(GenerationTask.status.in_(["running", "pending"]))
                .values(status="failed", error_message="服务重启，任务中断", completed_at=datetime.now(timezone.utc))
            )
            await session.commit()
            logger.info(f"Cleaned up {len(stale)} stale generation tasks")
    except Exception as e:
        logger.warning(f"Stale task cleanup failed (non-blocking): {e}")
