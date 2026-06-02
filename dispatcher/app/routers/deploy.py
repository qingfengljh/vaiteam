from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import Project
from app.services import deploy_manager
from app.services.project_access import raise_if_expired_for_write

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


class DeployRequest(BaseModel):
    project_id: str
    role: str
    model_provider: str = "147api"
    model_id: str = ""
    port: int = 0  # 0 = 自动分配
    api_key: str = ""
    api_base: str = "https://api.147api.com"
    proxy_env: dict | None = None


class DeployResult(BaseModel):
    agent_id: str
    deploy_dir: str
    gateway_token: str
    compose_file: str
    openclaw_config: dict
    pushed_to: str | None = None
    push_warning: str | None = None


async def _generate_deploy_config(
    body: DeployRequest,
    session: AsyncSession,
    allow_push_failure: bool = False,
):
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    raise_if_expired_for_write(project)
    data = await deploy_manager.generate_agent_deploy(
        session,
        project_id=body.project_id,
        role=body.role,
        model_provider=body.model_provider,
        model_id=body.model_id,
        port=body.port,
        api_key=body.api_key,
        api_base=body.api_base,
        proxy_env=body.proxy_env,
        allow_push_failure=allow_push_failure,
    )
    return DeployResult(**data)


async def _get_infra_node(session: AsyncSession, project_id: str, role: str = "agent"):
    return await deploy_manager.get_infra_node(session, project_id, role)


@router.post("/generate", response_model=DeployResult)
async def generate_deploy_config(
    body: DeployRequest,
    session: AsyncSession = Depends(get_session),
    allow_push_failure: bool = False,
):
    return await _generate_deploy_config(body, session, allow_push_failure=allow_push_failure)


class BatchDeployRequest(BaseModel):
    project_id: str
    roles: list[str] = ["architect", "senior", "mid", "junior"]
    model_provider: str = "147api"
    architect_model: str = ""
    engineer_model: str = ""
    api_key: str = ""
    api_base: str = "https://api.147api.com"


@router.post("/generate-team")
async def generate_team_config(body: BatchDeployRequest, session: AsyncSession = Depends(get_session)):
    """批量生成项目团队的部署配置并推送到远程节点"""
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    raise_if_expired_for_write(project)

    results = []
    for role in body.roles:
        model_id = body.architect_model if role == "architect" else body.engineer_model
        req = DeployRequest(
            project_id=body.project_id,
            role=role,
            model_provider=body.model_provider,
            model_id=model_id,
            api_key=body.api_key,
            api_base=body.api_base,
        )
        result = await generate_deploy_config(req, session)
        results.append(result)

    return {
        "project_id": body.project_id,
        "agents": [
            {
                "agent_id": r.agent_id,
                "role": body.roles[i],
                "deploy_dir": r.deploy_dir,
                "pushed_to": r.pushed_to,
            }
            for i, r in enumerate(results)
        ],
    }
