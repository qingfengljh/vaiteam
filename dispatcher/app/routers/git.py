"""Git 仓库管理 API"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import Project
from app.services import git_repo

router = APIRouter(prefix="/api/projects/{project_id}/git", tags=["git"])


class GitInitReq(BaseModel):
    git_repo: str = ""


@router.post("/clone")
async def clone_repo(project_id: str, body: GitInitReq, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    repo_url = body.git_repo or project.git_repo
    if not repo_url:
        raise HTTPException(400, "未配置 git_repo 地址")

    if body.git_repo and body.git_repo != project.git_repo:
        project.git_repo = body.git_repo
        await session.commit()

    return await git_repo.clone(project_id, repo_url)


@router.post("/pull")
async def pull_repo(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return await git_repo.pull(project_id)


@router.get("/commits")
async def get_commits(project_id: str, branch: str = "main", limit: int = 20, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return await git_repo.get_branch_commits(project_id, branch, limit)


class BranchReq(BaseModel):
    branch: str
    base: str = "main"


@router.post("/branches")
async def create_branch(project_id: str, body: BranchReq, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.git_repo:
        raise HTTPException(400, "未配置 git_repo")
    await git_repo.ensure_repo(project_id, project.git_repo)
    return await git_repo.create_branch(project_id, body.branch, body.base)


class MergeReq(BaseModel):
    branch: str
    target: str = "main"


@router.post("/merge")
async def merge_branch(project_id: str, body: MergeReq, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return await git_repo.merge_branch(project_id, body.branch, body.target)
