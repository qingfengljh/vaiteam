"""
Lightweight DB migration: runs after create_all to add new columns and data.
Each migration function is idempotent.
"""

import logging
import uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def run_migrations(engine: AsyncEngine):
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Running migrations...")
    await _ensure_pgvector(engine)
    await _add_columns(engine)
    await _backfill_project_access_until(engine)
    await _migrate_project_codes(engine)
    await _create_default_iterations(engine)
    await _migrate_iteration_status(engine)
    await _migrate_model_configs(engine)
    await _migrate_agent_teams(engine)
    await _migrate_agent_messages(engine)
    await _migrate_node_types(engine)
    await _migrate_agent_recovery_tables(engine)
    await _ensure_prototype_runs_table(engine)
    await _migrate_experience_governance_fields(engine)
    await _create_failure_patterns_table(engine)
    await _migrate_document_vector_fields(engine)
    logger.info("Migrations complete.")


async def _backfill_project_access_until(engine: AsyncEngine):
    """历史项目：access_until 为空时补为 created_at + 30 天。"""
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='projects' AND column_name='access_until'"
        ))
        if r.scalar() is None:
            return
        await conn.execute(text(
            "UPDATE projects SET access_until = created_at + interval '30 days' "
            "WHERE access_until IS NULL"
        ))
    logger.info("  Backfilled projects.access_until where null")


async def _ensure_pgvector(engine: AsyncEngine):
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("  pgvector extension ensured")


async def _col_exists(conn, table, column):
    r = await conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table, "column": column})
    return r.scalar() is not None


async def _add_col(conn, table, column, col_type, default=""):
    if await _col_exists(conn, table, column):
        return
    dflt = f" DEFAULT {default}" if default else ""
    await conn.execute(text(
        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{dflt}"
    ))
    logger.info(f"  Added column {table}.{column}")


async def _add_columns(engine: AsyncEngine):
    async with engine.begin() as conn:
        await _add_col(conn, "projects", "current_iteration_id", "VARCHAR(32)")
        await _add_col(conn, "projects", "task_seq", "INTEGER", "0")
        await _add_col(conn, "projects", "project_type", "VARCHAR(32)", "'new'")
        await _add_col(conn, "projects", "rewrite_reason", "TEXT", "''")
        await _add_col(conn, "projects", "target_tech_stack", "TEXT", "''")
        await _add_col(conn, "projects", "git_web_url", "VARCHAR(500)", "''")
        await _add_col(conn, "projects", "port_range_start", "INTEGER")

        await _add_col(conn, "stage_progress", "iteration_id", "VARCHAR(32)")
        await _add_col(conn, "documents", "iteration_id", "VARCHAR(32)")
        await _add_col(conn, "documents", "category", "VARCHAR(64)", "'general'")
        await _add_col(conn, "documents", "tags", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "messages", "iteration_id", "VARCHAR(32)")
        await _add_col(conn, "generation_tasks", "iteration_id", "VARCHAR(32)")

        await _add_col(conn, "tasks", "iteration_id", "VARCHAR(32)")
        await _add_col(conn, "tasks", "ref_id", "VARCHAR(32)", "''")
        await _add_col(conn, "tasks", "ref_docs", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "tasks", "git_branch", "VARCHAR(200)", "''")
        await _add_col(conn, "tasks", "git_commits", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "tasks", "merge_status", "VARCHAR(32)", "'pending'")
        await _add_col(conn, "tasks", "merge_commit", "VARCHAR(64)", "''")
        await _add_col(conn, "tasks", "test_status", "VARCHAR(32)", "'pending'")
        await _add_col(conn, "tasks", "test_results", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "tasks", "max_retries", "INTEGER", "2")
        await _add_col(conn, "tasks", "escalation_level", "INTEGER", "0")
        await _add_col(conn, "tasks", "escalation_history", "JSONB", "'[]'::jsonb")

        await _add_col(conn, "model_providers", "input_price_per_mtok", "DOUBLE PRECISION", "0.0")
        await _add_col(conn, "model_providers", "output_price_per_mtok", "DOUBLE PRECISION", "0.0")
        await _add_col(conn, "model_providers", "cache_read_price_per_mtok", "DOUBLE PRECISION", "0.0")
        await _add_col(conn, "model_providers", "credential_source", "VARCHAR(16)", "'byok'")
        await _add_col(conn, "model_providers", "model_prices", "JSONB", "'{}'::jsonb")
        await _add_col(conn, "model_providers", "model_params", "JSONB", "'{}'::jsonb")

        await _add_col(conn, "model_configs", "capability_tier", "INTEGER", "3")
        await _add_col(conn, "model_configs", "cache_read_price", "DOUBLE PRECISION", "0.0")

        await _add_col(conn, "tasks", "superseded_by", "VARCHAR(32)")
        await _add_col(conn, "tasks", "supersedes", "VARCHAR(32)")

        await _add_col(conn, "agents", "supervisor_id", "VARCHAR(64)")

        await _add_col(conn, "projects", "infra_group_id", "VARCHAR(32)")
        await _add_col(conn, "projects", "role_model_map", "JSONB")
        await _add_col(conn, "agents", "last_heartbeat_status", "VARCHAR(32)", "'offline'")

        await _add_col(conn, "documents", "generated_model", "VARCHAR(128)", "''")
        await _add_col(conn, "documents", "git_path", "VARCHAR(500)", "''")
        await _add_col(conn, "tasks", "min_tier", "INTEGER", "0")
        await _add_col(conn, "tasks", "complexity", "VARCHAR(16)", "'medium'")

        await _add_col(conn, "infra_nodes", "roles", "JSONB", "'[\"agent\"]'::jsonb")
        await _add_col(conn, "infra_group_nodes", "roles", "JSONB", "'[\"agent\"]'::jsonb")

        await _add_col(conn, "experiences", "keywords", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "experiences", "tsv", "TSVECTOR")
        await _add_col(conn, "experiences", "embedding", "vector(1536)")

        await _add_col(conn, "agents", "module_task_id", "VARCHAR(32)")
        await _add_col(conn, "agents", "last_started_at", "TIMESTAMPTZ")
        await _add_col(conn, "agents", "auto_restart_count", "INTEGER", "0")
        await _add_col(conn, "infra_groups", "purpose", "VARCHAR(32)", "'agent'")
        await _add_col(conn, "infra_nodes", "last_metrics", "JSONB")
        await _add_col(conn, "projects", "access_until", "TIMESTAMPTZ")

        await _add_col(conn, "tasks", "requires_design_review", "BOOLEAN", "false")
        await _add_col(conn, "tasks", "design_conversation_id", "VARCHAR(32)")
        await _add_col(conn, "tasks", "design_approved", "BOOLEAN", "false")
        await _add_col(conn, "tasks", "design_approved_by", "VARCHAR(100)", "''")
        await _add_col(conn, "tasks", "design_approved_at", "TIMESTAMPTZ")

        await _safe_reindex(
            conn, "stage_progress",
            "idx_stage_project_stage", "idx_stage_project_iter_stage",
            "project_id, iteration_id, stage", unique=True)
        await _safe_reindex(
            conn, "documents",
            "idx_doc_project_stage", "idx_doc_project_iter_stage",
            "project_id, iteration_id, stage")
        await _safe_reindex(
            conn, "messages",
            "idx_messages_project_stage", "idx_messages_project_iter_stage",
            "project_id, iteration_id, stage")
        await _safe_reindex(
            conn, "generation_tasks",
            "idx_gentask_project_stage", "idx_gentask_project_iter_stage",
            "project_id, iteration_id, stage")


async def _safe_reindex(conn, table, old_idx, new_idx, columns, unique=False):
    r = await conn.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname = :idx"
    ), {"idx": new_idx})
    if r.scalar() is not None:
        return
    await conn.execute(text(f"DROP INDEX IF EXISTS {old_idx}"))
    u = "UNIQUE " if unique else ""
    await conn.execute(text(f"CREATE {u}INDEX {new_idx} ON {table} ({columns})"))
    logger.info(f"  Reindexed {table}: {old_idx} -> {new_idx}")


async def _create_default_iterations(engine: AsyncEngine):
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'iterations'"
        ))
        if r.scalar() is None:
            return

        projects = await conn.execute(text(
            "SELECT p.id, p.current_stage FROM projects p "
            "WHERE p.current_iteration_id IS NULL"
        ))
        rows = projects.fetchall()
        if not rows:
            return

        for row in rows:
            pid, cs = row[0], row[1]

            q = await conn.execute(text(
                "SELECT id FROM iterations WHERE project_id = :pid ORDER BY seq LIMIT 1"
            ), {"pid": pid})
            iter_id = q.scalar()

            if iter_id:
                await conn.execute(text(
                    "UPDATE projects SET current_iteration_id = :iid WHERE id = :pid"
                ), {"iid": iter_id, "pid": pid})
                logger.info(f"  Linked existing iteration {iter_id} for project {pid}")
                continue

            iter_id = str(uuid.uuid4())[:8]
            await conn.execute(text(
                "INSERT INTO iterations "
                "(id, project_id, seq, title, description, "
                "start_stage, current_stage, status, "
                "release_branch, release_tag, release_status, "
                "created_at, updated_at) "
                "VALUES (:id, :pid, 1, 'v1.0', 'initial', "
                "0, :cs, 'active', '', '', 'pending', NOW(), NOW()) "
                "ON CONFLICT (project_id, seq) DO NOTHING"
            ), {"id": iter_id, "pid": pid, "cs": cs})

            chk = await conn.execute(text(
                "SELECT id FROM iterations "
                "WHERE project_id = :pid ORDER BY seq LIMIT 1"
            ), {"pid": pid})
            found = chk.scalar()
            if found:
                iter_id = found

            await conn.execute(text(
                "UPDATE projects SET current_iteration_id = :iid WHERE id = :pid"
            ), {"iid": iter_id, "pid": pid})

            for tbl in ["stage_progress", "documents", "messages",
                        "generation_tasks", "tasks"]:
                await conn.execute(text(
                    f"UPDATE {tbl} SET iteration_id = :iid "
                    f"WHERE project_id = :pid AND iteration_id IS NULL"
                ), {"iid": iter_id, "pid": pid})

            logger.info(f"  Created default iteration {iter_id} for project {pid}")


async def _migrate_iteration_status(engine: AsyncEngine):
    """将旧的 draft 状态迁移为 planning"""
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'iterations'"
        ))
        if r.scalar() is None:
            return
        result = await conn.execute(text(
            "UPDATE iterations SET status = 'planning' WHERE status = 'draft'"
        ))
        if result.rowcount > 0:
            logger.info(f"  Migrated {result.rowcount} iterations from 'draft' to 'planning'")


async def _migrate_model_configs(engine: AsyncEngine):
    """从 model_providers 的 JSONB 字段迁移数据到 model_configs 表"""
    import json as _json

    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'model_configs'"
        ))
        if r.scalar() is None:
            return

        existing = await conn.execute(text("SELECT COUNT(*) FROM model_configs"))
        if existing.scalar() > 0:
            return

        providers = await conn.execute(text(
            "SELECT id, models, model_prices, model_params, "
            "input_price_per_mtok, output_price_per_mtok FROM model_providers"
        ))
        count = 0
        for row in providers.fetchall():
            pid = row[0]
            models = row[1] or []
            prices = row[2] or {}
            params = row[3] or {}
            default_input = row[4] or 0.0
            default_output = row[5] or 0.0

            if isinstance(models, str):
                models = _json.loads(models)
            if isinstance(prices, str):
                prices = _json.loads(prices)
            if isinstance(params, str):
                params = _json.loads(params)

            for model in models:
                mp = prices.get(model, {})
                pp = params.get(model, {})
                mid = str(uuid.uuid4())[:8]
                await conn.execute(text(
                    "INSERT INTO model_configs "
                    "(id, provider_id, model_name, input_price, output_price, "
                    "context_window, max_output_tokens, supports_vision, vision_fallback, "
                    "enabled, extra) "
                    "VALUES (:id, :pid, :name, :ip, :op, :cw, :mo, :sv, :vf, true, '{}'::jsonb) "
                    "ON CONFLICT (provider_id, model_name) DO NOTHING"
                ), {
                    "id": mid, "pid": pid, "name": model,
                    "ip": mp.get("input", default_input),
                    "op": mp.get("output", default_output),
                    "cw": pp.get("context_window", 128000),
                    "mo": pp.get("max_output_tokens", 4096),
                    "sv": bool(pp.get("supports_vision", False)),
                    "vf": pp.get("vision_fallback", ""),
                })
                count += 1

        if count:
            logger.info(f"  Migrated {count} model configs from JSONB to model_configs table")


async def _table_exists(conn, table):
    r = await conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :table"
    ), {"table": table})
    return r.scalar() is not None


async def _migrate_project_codes(engine: AsyncEngine):
    """历史库补 projects.code；新库 create_all 已含列时仅保证索引。"""
    async with engine.begin() as conn:
        if not await _table_exists(conn, "projects"):
            return
        if not await _col_exists(conn, "projects", "code"):
            await conn.execute(text("ALTER TABLE projects ADD COLUMN code VARCHAR(64)"))
            logger.info("  Added column projects.code")
        await conn.execute(text("""
            UPDATE projects SET code = 'p-' || id
            WHERE code IS NULL OR btrim(code) = ''
        """))
        await conn.execute(text("ALTER TABLE projects ALTER COLUMN code SET NOT NULL"))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_code_lower ON projects (lower(code))"
        ))
        logger.info("  projects.code backfill + unique index ensured")


async def _migrate_agent_teams(engine: AsyncEngine):
    """agent_teams 表迁移：agents.module_task_id -> agents.team_id"""
    async with engine.begin() as conn:
        if not await _table_exists(conn, "agents"):
            return
        if await _table_exists(conn, "agent_teams"):
            await _add_col(conn, "agent_teams", "default_review_policy", "JSONB", "'{}'::jsonb")
            await conn.execute(text(
                "UPDATE agent_teams SET default_review_policy = CAST(:p AS jsonb) "
                "WHERE default_review_policy IS NULL OR default_review_policy = '{}'::jsonb"
            ), {"p": '{"auto_review_enabled": true, "require_human_review_complexities": ["critical"], "require_human_review_task_types": []}'})

        # 添加 team_id 列（如果不存在）
        await _add_col(conn, "agents", "team_id", "VARCHAR(32)")

        # 如果 module_task_id 列还在，执行数据迁移
        if not await _col_exists(conn, "agents", "module_task_id"):
            return

        if not await _table_exists(conn, "agent_teams"):
            return

        # 迁移：为每个有 module_task_id 的 Agent 组创建 team
        rows = await conn.execute(text(
            "SELECT DISTINCT project_id, module_task_id FROM agents "
            "WHERE module_task_id IS NOT NULL AND team_id IS NULL"
        ))
        migrated = 0
        for row in rows.fetchall():
            pid, mid = row[0], row[1]
            tid = str(uuid.uuid4())[:8]
            await conn.execute(text(
                "INSERT INTO agent_teams (id, project_id, name, is_default, module_task_ids, default_review_policy, created_at) "
                "VALUES (:id, :pid, :name, false, :mids, :policy, NOW()) "
                "ON CONFLICT DO NOTHING"
            ), {
                "id": tid, "pid": pid, "name": f"小组-{mid[:6]}", "mids": f'["{mid}"]',
                "policy": '{"auto_review_enabled": true, "require_human_review_complexities": ["critical"], "require_human_review_task_types": []}',
            })
            await conn.execute(text(
                "UPDATE agents SET team_id = :tid WHERE module_task_id = :mid AND team_id IS NULL"
            ), {"tid": tid, "mid": mid})
            migrated += 1

        # 为没有 team 的 Agent 创建默认小组
        orphans = await conn.execute(text(
            "SELECT DISTINCT project_id FROM agents WHERE team_id IS NULL"
        ))
        for row in orphans.fetchall():
            pid = row[0]
            # 检查是否已有默认小组
            existing = await conn.execute(text(
                "SELECT id FROM agent_teams WHERE project_id = :pid AND is_default = true LIMIT 1"
            ), {"pid": pid})
            tid = existing.scalar()
            if not tid:
                tid = str(uuid.uuid4())[:8]
                await conn.execute(text(
                    "INSERT INTO agent_teams (id, project_id, name, is_default, module_task_ids, default_review_policy, created_at) "
                    "VALUES (:id, :pid, '默认团队', true, '[]'::jsonb, :policy, NOW())"
                ), {
                    "id": tid, "pid": pid,
                    "policy": '{"auto_review_enabled": true, "require_human_review_complexities": ["critical"], "require_human_review_task_types": []}',
                })
            await conn.execute(text(
                "UPDATE agents SET team_id = :tid WHERE project_id = :pid AND team_id IS NULL"
            ), {"tid": tid, "pid": pid})
            migrated += 1

        if migrated:
            logger.info(f"  Migrated {migrated} agent groups to agent_teams")


async def _migrate_agent_messages(engine: AsyncEngine):
    """确保 agent_messages 表存在"""
    async with engine.begin() as conn:
        if await _table_exists(conn, "agent_messages"):
            return
        await conn.execute(text("""
            CREATE TABLE agent_messages (
                id VARCHAR(32) PRIMARY KEY,
                task_id VARCHAR(32) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                project_id VARCHAR(32) NOT NULL,
                from_id VARCHAR(64) NOT NULL,
                to_id VARCHAR(64) NOT NULL,
                msg_type VARCHAR(32) NOT NULL,
                payload JSONB DEFAULT '{}'::jsonb,
                ref_msg_id VARCHAR(32),
                status VARCHAR(16) DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                replied_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("CREATE INDEX idx_amsg_task ON agent_messages (task_id)"))
        await conn.execute(text("CREATE INDEX idx_amsg_project ON agent_messages (project_id)"))
        await conn.execute(text("CREATE INDEX idx_amsg_to ON agent_messages (to_id)"))
        await conn.execute(text("CREATE INDEX idx_amsg_ref ON agent_messages (ref_msg_id)"))
        await conn.execute(text("CREATE INDEX idx_amsg_status ON agent_messages (status)"))
        logger.info("  Created agent_messages table")


async def _migrate_node_types(engine: AsyncEngine):
    async with engine.begin() as conn:
        if not await _table_exists(conn, "infra_nodes"):
            return
        result = await conn.execute(text(
            "UPDATE infra_nodes SET type = 'linux' WHERE type IN ('vm', 'docker')"
        ))
        if result.rowcount > 0:
            logger.info(f"  Migrated {result.rowcount} infra nodes from vm/docker to linux")
        # roles 统一大写（agent→AGENT, deploy→DEPLOY, ollama→OLLAMA）
        result = await conn.execute(text("""
            UPDATE infra_nodes
            SET roles = (
                SELECT jsonb_agg(upper(elem))
                FROM jsonb_array_elements_text(roles) elem
            )
            WHERE roles IS NOT NULL
              AND roles::text <> (
                SELECT coalesce(jsonb_agg(upper(elem))::text, '[]')
                FROM jsonb_array_elements_text(roles) elem
              )
        """))
        if result.rowcount > 0:
            logger.info(f"  Migrated {result.rowcount} infra nodes roles to uppercase")
        if await _table_exists(conn, "infra_group_nodes"):
            result = await conn.execute(text("""
                UPDATE infra_group_nodes
                SET roles = (
                    SELECT jsonb_agg(upper(elem))
                    FROM jsonb_array_elements_text(roles) elem
                )
                WHERE roles IS NOT NULL
                  AND roles::text <> (
                    SELECT coalesce(jsonb_agg(upper(elem))::text, '[]')
                    FROM jsonb_array_elements_text(roles) elem
                  )
            """))
            if result.rowcount > 0:
                logger.info(f"  Migrated {result.rowcount} group-node roles to uppercase")


async def _migrate_agent_recovery_tables(engine: AsyncEngine):
    async with engine.begin() as conn:
        if not await _table_exists(conn, "agent_boot_reports"):
            await conn.execute(text("""
                CREATE TABLE agent_boot_reports (
                    id SERIAL PRIMARY KEY,
                    agent_id VARCHAR(64) NOT NULL,
                    project_id VARCHAR(32) NOT NULL,
                    boot_id VARCHAR(128) DEFAULT '',
                    session_fingerprint VARCHAR(256) DEFAULT '',
                    recovery_mode VARCHAR(32) DEFAULT 'fast_resume',
                    retriever_ready BOOLEAN DEFAULT true,
                    context_versions JSONB DEFAULT '{}'::jsonb,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("CREATE INDEX idx_boot_reports_agent ON agent_boot_reports (agent_id)"))
            await conn.execute(text("CREATE INDEX idx_boot_reports_project ON agent_boot_reports (project_id)"))
            await conn.execute(text("CREATE INDEX idx_boot_reports_created ON agent_boot_reports (created_at)"))
            logger.info("  Created agent_boot_reports table")

        if not await _table_exists(conn, "agent_rehydration_jobs"):
            await conn.execute(text("""
                CREATE TABLE agent_rehydration_jobs (
                    id VARCHAR(32) PRIMARY KEY,
                    agent_id VARCHAR(64) NOT NULL,
                    project_id VARCHAR(32) NOT NULL,
                    mode VARCHAR(32) DEFAULT 'partial_rehydrate',
                    reason VARCHAR(500) DEFAULT '',
                    status VARCHAR(32) DEFAULT 'pending',
                    snapshot JSONB DEFAULT '{}'::jsonb,
                    result JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
            """))
            await conn.execute(text("CREATE INDEX idx_rehydrate_agent ON agent_rehydration_jobs (agent_id)"))
            await conn.execute(text("CREATE INDEX idx_rehydrate_project ON agent_rehydration_jobs (project_id)"))
            await conn.execute(text("CREATE INDEX idx_rehydrate_status ON agent_rehydration_jobs (status)"))
            await conn.execute(text("CREATE INDEX idx_rehydrate_created ON agent_rehydration_jobs (created_at)"))
            logger.info("  Created agent_rehydration_jobs table")


async def _ensure_prototype_runs_table(engine: AsyncEngine):
    """原型工坊 CC 运行记录（与 docs/PROTOTYPE_CC_RUN_PIPELINE.md 一致）。"""
    async with engine.begin() as conn:
        if await _table_exists(conn, "prototype_runs"):
            return
        await conn.execute(text("""
            CREATE TABLE prototype_runs (
                id VARCHAR(32) PRIMARY KEY,
                project_id VARCHAR(32) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                iteration_id VARCHAR(32),
                status VARCHAR(32) NOT NULL DEFAULT 'running',
                prototype_document_id VARCHAR(32),
                technical_document_id VARCHAR(32),
                secret_hash VARCHAR(64) NOT NULL,
                snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                result JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_message TEXT NOT NULL DEFAULT '',
                exit_code INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("CREATE INDEX idx_proto_runs_project ON prototype_runs (project_id)"))
        await conn.execute(text("CREATE INDEX idx_proto_runs_status ON prototype_runs (status)"))
        await conn.execute(text("CREATE INDEX idx_proto_runs_created ON prototype_runs (created_at)"))
        logger.info("  Created prototype_runs table")


async def _migrate_experience_governance_fields(engine: AsyncEngine):
    """Phase 3: 经验库治理字段迁移（status, domain, type, scope, freshness, version_range, valid_until, related_exp_ids）"""
    async with engine.begin() as conn:
        if not await _table_exists(conn, "experiences"):
            return
        await _add_col(conn, "experiences", "status", "VARCHAR(32)", "'published'")
        await _add_col(conn, "experiences", "domain", "VARCHAR(64)", "''")
        await _add_col(conn, "experiences", "type", "VARCHAR(64)", "'experience'")
        await _add_col(conn, "experiences", "scope", "VARCHAR(64)", "'global'")
        await _add_col(conn, "experiences", "freshness", "VARCHAR(32)", "'permanent'")
        await _add_col(conn, "experiences", "version_range", "VARCHAR(200)", "''")
        await _add_col(conn, "experiences", "valid_until", "TIMESTAMPTZ")
        await _add_col(conn, "experiences", "related_exp_ids", "JSONB", "'[]'::jsonb")
        await _add_col(conn, "experiences", "access_level", "INTEGER", "0")
        # 治理索引
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences (status)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_domain ON experiences (domain)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_type ON experiences (type)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_scope ON experiences (scope)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_freshness ON experiences (freshness)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_access_level ON experiences (access_level)"))
        logger.info("  Experience governance fields migrated")


async def _create_failure_patterns_table(engine: AsyncEngine):
    """Phase 4-2: 创建失败模式（负样本）表"""
    async with engine.begin() as conn:
        if await _table_exists(conn, "failure_patterns"):
            return
        await conn.execute(text("""
            CREATE TABLE failure_patterns (
                id VARCHAR(32) PRIMARY KEY,
                project_id VARCHAR(32),
                task_id VARCHAR(32),
                pattern_type VARCHAR(64) DEFAULT '',
                tech_stack JSONB DEFAULT '[]'::jsonb,
                tags JSONB DEFAULT '[]'::jsonb,
                failure_symptom TEXT DEFAULT '',
                root_cause TEXT DEFAULT '',
                failed_approach TEXT DEFAULT '',
                successful_approach TEXT DEFAULT '',
                keywords JSONB DEFAULT '[]'::jsonb,
                tsv TSVECTOR,
                embedding vector(1536),
                status VARCHAR(32) DEFAULT 'published',
                use_count INTEGER DEFAULT 0,
                quality_score DOUBLE PRECISION DEFAULT 0.0,
                source_experience_id VARCHAR(32),
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("CREATE INDEX idx_fp_project ON failure_patterns (project_id)"))
        await conn.execute(text("CREATE INDEX idx_fp_type ON failure_patterns (pattern_type)"))
        await conn.execute(text("CREATE INDEX idx_fp_tech_stack ON failure_patterns USING GIN (tech_stack)"))
        await conn.execute(text("CREATE INDEX idx_fp_tags ON failure_patterns USING GIN (tags)"))
        await conn.execute(text("CREATE INDEX idx_fp_keywords ON failure_patterns USING GIN (keywords)"))
        await conn.execute(text("CREATE INDEX idx_fp_tsv ON failure_patterns USING GIN (tsv)"))
        await conn.execute(text("CREATE INDEX idx_fp_status ON failure_patterns (status)"))
        logger.info("  Created failure_patterns table")


async def _migrate_document_vector_fields(engine: AsyncEngine):
    """Document/TaskDocument 表增加 tsv/embedding 字段（已在模型中添加，此处确保列存在）"""
    async with engine.begin() as conn:
        if await _table_exists(conn, "documents"):
            await _add_col(conn, "documents", "tsv", "TSVECTOR")
            await _add_col(conn, "documents", "embedding", "vector(1536)")
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_doc_tsv ON documents USING GIN (tsv)"))
            logger.info("  Document vector fields migrated")

        if await _table_exists(conn, "task_documents"):
            # task_documents 模型中已有 tsv/embedding，确保列存在
            await _add_col(conn, "task_documents", "tsv", "TSVECTOR")
            await _add_col(conn, "task_documents", "embedding", "vector(1536)")
            logger.info("  TaskDocument vector fields verified")
