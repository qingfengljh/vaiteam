from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.core.database import get_session
from app.models import Project, Task, TaskComment, UploadedFile
from app.services import git_repo

router = APIRouter(prefix="/api", tags=["task-comments"])


class CommentCreate(BaseModel):
    author: str
    content: str
    comment_type: str = "discussion"
    attachments: list = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class TestFeedbackCreate(BaseModel):
    passed: bool
    feedback: str
    tester: str


class BugReportCreate(BaseModel):
    title: str
    description: str
    severity: str = "warning"  # critical / warning / minor
    reporter: str = ""


@router.get("/tasks/{task_id}/comments")
async def list_comments(task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    q = select(TaskComment).where(TaskComment.task_id == task_id).order_by(TaskComment.created_at)
    result = await session.execute(q)
    return [_comment_dict(c) for c in result.scalars()]


@router.post("/tasks/{task_id}/comments")
async def create_comment(task_id: str, body: CommentCreate, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)

    content = body.content
    attachments = list(body.attachments)

    if body.file_ids:
        for fid in body.file_ids:
            uf = await session.get(UploadedFile, fid)
            if uf:
                attachments.append({
                    "file_id": uf.id,
                    "name": uf.original_name,
                    "format": uf.format,
                    "size": uf.size,
                    "is_image": uf.is_image,
                })
                if uf.description:
                    content += f"\n\n[附件: {uf.original_name}]\n{uf.description}"

    comment = TaskComment(
        task_id=task_id,
        author=body.author,
        content=content,
        comment_type=body.comment_type,
        attachments=attachments,
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return _comment_dict(comment)


@router.delete("/tasks/{task_id}/comments/{comment_id}")
async def delete_comment(task_id: str, comment_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    comment = await session.get(TaskComment, comment_id)
    if not comment or comment.task_id != task_id:
        raise HTTPException(404)
    await session.delete(comment)
    await session.commit()
    return {"status": "ok"}


@router.post("/tasks/{task_id}/test-feedback")
async def submit_test_feedback(task_id: str, body: TestFeedbackCreate, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    author = f"human:{body.tester}"
    comment = TaskComment(
        task_id=task_id,
        author=author,
        content=body.feedback,
        comment_type="test_feedback",
    )
    session.add(comment)
    await session.flush()

    entry = {
        "type": "human_integration",
        "passed": body.passed,
        "feedback": body.feedback,
        "tester": body.tester,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    test_results = list(task.test_results or [])
    test_results.append(entry)
    task.test_results = test_results
    task.test_status = "accepted" if body.passed else "rejected"

    await session.commit()
    await session.refresh(comment)
    return {"comment": _comment_dict(comment), "test_status": task.test_status}


@router.post("/tasks/{task_id}/report-bug")
async def report_bug(task_id: str, body: BugReportCreate, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)

    project = await session.get(Project, task.project_id)
    if not project:
        raise HTTPException(404)

    project.task_seq = (project.task_seq or 0) + 1
    ref_id = f"TASK-{project.task_seq:03d}"
    git_branch = git_repo.task_branch_name(ref_id, body.title)
    if git_branch.startswith("task/"):
        git_branch = "fix/" + git_branch.removeprefix("task/")
    cfg = project.config or {}
    git_cfg = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    production_branch = (git_cfg.get("production_branch") or "main").strip() or "main"

    priority_map = {"critical": 3, "warning": 2, "minor": 1}

    fix_task = Task(
        project_id=task.project_id,
        iteration_id=task.iteration_id,
        parent_task_id=task_id,
        ref_id=ref_id,
        title=f"[BUG] {body.title}",
        description=f"来源: [{task.ref_id}] {task.title}\n\n{body.description}",
        type="bug",
        priority=priority_map.get(body.severity, 2),
        suggested_role=task.suggested_role,
        suggested_model=task.suggested_model or "sonnet",
        git_branch=git_branch,
        ref_docs=task.ref_docs or [],
        acceptance_criteria=[f"修复: {body.title}", "回归测试通过"],
        context={
            **(task.context or {}),
            "git_base_branch": production_branch,
            "git_merge_target": production_branch,
            "bug_source_task_id": task.id,
        },
    )
    session.add(fix_task)
    await session.flush()

    reporter = body.reporter or "human"
    comment = TaskComment(
        task_id=task_id,
        author=f"human:{reporter}",
        content=f"提交 Bug: **{body.title}** ({body.severity})\n\n{body.description}\n\n已创建修复任务: [{ref_id}]",
        comment_type="test_feedback",
    )
    session.add(comment)

    task.test_status = "rejected"

    await session.commit()
    await session.refresh(fix_task)

    return {
        "fix_task": {
            "id": fix_task.id,
            "ref_id": ref_id,
            "title": fix_task.title,
            "git_branch": git_branch,
        },
        "comment": _comment_dict(comment),
    }


def _comment_dict(c: TaskComment) -> dict:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "author": c.author,
        "content": c.content,
        "comment_type": c.comment_type,
        "attachments": c.attachments or [],
        "created_at": c.created_at.isoformat(),
    }
