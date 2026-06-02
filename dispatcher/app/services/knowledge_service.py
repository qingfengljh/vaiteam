"""
KnowledgeService — 知识服务抽象层

统一所有知识源的检索、索引和管理，隔离存储实现细节。
业务代码通过此服务访问知识库，未来替换存储后端（PostgreSQL → Milvus/Weaviate）
时无需修改业务代码。

当前实现委托给现有的 knowledge_search / project_context / experience 模块，
后续可渐进式迁移到独立存储实现。
"""

import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import knowledge_search, project_context, experience, knowledge_maintenance, knowledge_review

logger = logging.getLogger(__name__)


# 角色到知识访问级别的映射（Phase 3-6）
# 0=公开(所有角色) | 1=内部(senior+) | 2=敏感(architect+) | 3=机密(architect+human)
ROLE_ACCESS_LEVEL = {
    "architect": 3,
    "senior": 2,
    "mid": 1,
    "junior": 0,
    "devops": 1,
    "tester": 1,
    "leader": 3,
    "human": 3,
}


class KnowledgeService:
    """知识服务：统一检索入口 + 知识块管理 + 经验操作"""

    # ── 统一检索 ──

    async def query(
        self,
        session: AsyncSession,
        query: str,
        *,
        project_id: str | None = None,
        sources: list[str] | None = None,
        mode: str = "auto",
        limit: int = 10,
    ) -> list[knowledge_search.SearchResult]:
        """统一知识检索入口。mode="auto" 时使用 Hybrid Search (RRF 融合)。"""
        return await knowledge_search.search(
            session, query, project_id=project_id, sources=sources, mode=mode, limit=limit,
        )

    async def search_for_context(
        self,
        session: AsyncSession,
        query: str,
        project_id: str,
        limit: int = 5,
    ) -> str:
        """检索后格式化为可注入 prompt 的文本"""
        return await knowledge_search.search_for_context(session, query, project_id, limit=limit)

    # ── 知识块按需加载 ──

    async def get_snippets(
        self,
        session: AsyncSession,
        keys: list[str],
        project_id: str,
        active_stage: int | None = None,
    ) -> str:
        """按 key 列表加载知识块原文，拼接为可注入 prompt 的文本"""
        if not keys:
            return ""
        parts: list[str] = []
        for key in keys:
            try:
                snippet = await project_context.load_knowledge_block(
                    session, project_id, key, active_stage=active_stage,
                )
                if snippet and not snippet.startswith(("未找到", "未知的知识块")):
                    parts.append(f"## {key}\n{snippet}")
            except Exception as e:
                logger.debug(f"KnowledgeService: failed to load block {key}: {e}")
        return "\n\n".join(parts) if parts else ""

    async def build_knowledge_index(
        self,
        session: AsyncSession,
        project_id: str,
        include_experiences: bool = True,
        active_stage: int | None = None,
    ) -> str:
        """构建轻量级知识索引目录"""
        return await project_context.build_knowledge_index(
            session, project_id, include_experiences=include_experiences, active_stage=active_stage,
        )

    # ── 经验库操作 ──

    async def find_relevant_experiences(
        self,
        session: AsyncSession,
        *,
        task_type: str = "",
        tech_stack: list[str] | None = None,
        keywords: list[str] | None = None,
        domain: str = "",
        type: str = "",
        scope: str = "",
        freshness: str = "",
        role: str = "",
        limit: int = 5,
    ) -> list:
        """查找与任务相关的经验，支持分类体系过滤和角色权限"""
        access_level_max = ROLE_ACCESS_LEVEL.get(role.lower(), 3)
        return await experience.find_relevant(
            session,
            task_type=task_type,
            tech_stack=tech_stack,
            keywords=keywords,
            domain=domain,
            type=type,
            scope=scope,
            freshness=freshness,
            access_level_max=access_level_max,
            limit=limit,
        )

    def format_experiences(self, experiences: list, max_chars: int = 3000) -> str:
        """将经验列表格式化为可注入 prompt 的文本"""
        return experience.format_for_context(experiences, max_chars=max_chars)

    async def record_experience_use(self, session: AsyncSession, exp_id: str) -> None:
        """记录经验被引用"""
        await experience.record_use(session, exp_id)

    async def extract_experience_from_retry(
        self,
        session: AsyncSession,
        task_title: str,
        task_description: str,
        error_history: list[str],
        final_result: str,
        retry_count: int,
        used_model: str = "",
        project_name: str = "",
        task_id: str | None = None,
        project_tech_stack: list[str] | None = None,
    ):
        """从重试成功后的任务中提取经验"""
        return await experience.extract_from_retry(
            session,
            task_title=task_title,
            task_description=task_description,
            error_history=error_history,
            final_result=final_result,
            retry_count=retry_count,
            used_model=used_model,
            project_name=project_name,
            task_id=task_id,
            project_tech_stack=project_tech_stack,
        )

    # ── 知识维护 ──

    async def auto_deprecate(self, session: AsyncSession) -> dict:
        """自动降级过期知识"""
        return await knowledge_maintenance.auto_deprecate(session)

    async def generate_audit_report(self, session: AsyncSession) -> dict:
        """生成知识库健康度审计报告"""
        return await knowledge_maintenance.generate_audit_report(session)

    async def record_experience_outcome(
        self, session: AsyncSession, exp_id: str, success: bool,
    ) -> None:
        """记录经验的引用结果，用于计算成功率"""
        await knowledge_maintenance.record_experience_outcome(session, exp_id, success)

    async def extract_failure_pattern_from_retry(
        self,
        session: AsyncSession,
        task_title: str,
        task_description: str,
        error_history: list[str],
        final_result: str,
        retry_count: int,
        used_model: str = "",
        project_name: str = "",
        task_id: str | None = None,
        source_experience_id: str | None = None,
        project_tech_stack: list[str] | None = None,
    ):
        """从 retry 失败历史中自动提取失败模式（负样本）"""
        return await experience.extract_failure_pattern_from_retry(
            session,
            task_title=task_title,
            task_description=task_description,
            error_history=error_history,
            final_result=final_result,
            retry_count=retry_count,
            used_model=used_model,
            project_name=project_name,
            task_id=task_id,
            source_experience_id=source_experience_id,
            project_tech_stack=project_tech_stack,
        )

    # ── 知识审核 ──

    async def review_experience(
        self,
        session: AsyncSession,
        exp_id: str,
        stage: str = "all",
    ) -> dict:
        """审核经验记录（self/expert/all）"""
        return await knowledge_review.review_experience(session, exp_id, stage=stage)

    async def batch_review_experiences(
        self,
        session: AsyncSession,
        stage: str = "all",
        limit: int = 50,
    ) -> dict:
        """批量审核待处理的经验"""
        return await knowledge_review.batch_review(session, stage=stage, limit=limit)

    # ── 冲突检测与缺口分析 ──

    async def detect_conflicts(
        self,
        session: AsyncSession,
        new_exp_id: str,
        similarity_threshold: float = 0.75,
    ) -> list[dict]:
        """检测新经验是否与已有 published 经验存在语义冲突"""
        return await knowledge_maintenance.detect_conflicts(
            session, new_exp_id, similarity_threshold=similarity_threshold,
        )

    async def analyze_knowledge_gaps(
        self,
        session: AsyncSession,
        project_id: str | None = None,
        min_search_count: int = 3,
    ) -> list[dict]:
        """分析知识缺口：哪些主题被频繁搜索但没有足够经验覆盖"""
        return await knowledge_maintenance.analyze_knowledge_gaps(
            session, project_id=project_id, min_search_count=min_search_count,
        )

    # ── 经验关联图谱（Phase 4-5）──

    async def link_experiences(
        self, session: AsyncSession, exp_id_a: str, exp_id_b: str, relation: str = "related",
    ) -> bool:
        """双向关联两条经验"""
        return await knowledge_maintenance.link_experiences(session, exp_id_a, exp_id_b, relation)

    async def find_related_experiences(
        self, session: AsyncSession, exp_id: str, include_similar: bool = True, similarity_limit: int = 3,
    ) -> list:
        """查找与指定经验相关的其他经验"""
        return await knowledge_maintenance.find_related_experiences(
            session, exp_id, include_similar=include_similar, similarity_limit=similarity_limit,
        )

    async def auto_discover_associations(
        self, session: AsyncSession, similarity_threshold: float = 0.80, limit: int = 50,
    ) -> int:
        """自动发现经验之间的关联关系"""
        return await knowledge_maintenance.auto_discover_associations(
            session, similarity_threshold=similarity_threshold, limit=limit,
        )

    # ── 工具方法 ──

    def build_context_keys(
        self,
        experiences: list,
        has_parent_module: bool = False,
    ) -> list[str]:
        """根据相关经验构建 context_keys 列表"""
        keys = ["project_info"]
        for exp in experiences:
            keys.append(f"exp_{exp.id}")
        if has_parent_module:
            keys.append("doc_s4")
        return keys


# 全局单例
knowledge_svc = KnowledgeService()
