from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_size=10, max_overflow=20, pool_timeout=10, pool_recycle=1800)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


def _build_add_column_sql(table_name: str, col) -> str:
    """根据 Column 对象生成 ADD COLUMN 的 SQL 片段。

    - 不添加 NOT NULL：避免已有行因 NULL 导致迁移失败
    - 有 server_default 时添加 DEFAULT 子句
    - 有 Python default 时尝试提取为 DEFAULT（仅字符串类型）
    """
    type_str = str(col.type.compile())
    server_default = col.server_default
    python_default = col.default

    parts = [col.name, type_str]

    # 数据库级默认值
    if server_default is not None:
        sd = server_default.arg
        if callable(sd):
            try:
                sd = sd({})
            except Exception:
                pass
        parts.append(f"DEFAULT {sd}")
    elif python_default is not None:
        # Python 级默认值：仅字符串可安全转为 SQL DEFAULT
        try:
            pd = python_default.arg
            if callable(pd) and not isinstance(pd, type):
                pd = pd({})
            if isinstance(pd, str):
                parts.append(f"DEFAULT '{pd}'")
            elif isinstance(pd, (int, float, bool)):
                parts.append(f"DEFAULT {pd}")
            # dict/list 等复杂类型不添加 DEFAULT，由 ORM 处理
        except Exception:
            pass

    for fk in col.foreign_keys:
        fk_sql = f"REFERENCES {fk.column.table.name}({fk.column.name})"
        if fk.ondelete:
            fk_sql += f" ON DELETE {fk.ondelete.upper()}"
        if fk.onupdate:
            fk_sql += f" ON UPDATE {fk.onupdate.upper()}"
        parts.append(fk_sql)

    return " ".join(parts)


async def _add_missing_columns():
    """自动检测并添加缺失的数据库列（用于无 alembic 环境的轻量迁移）。

    每个 ALTER TABLE 在独立连接中执行，单列失败不影响其他列。"""
    from sqlalchemy import inspect

    def _sync_find_missing(sync_conn) -> list:
        inspector = inspect(sync_conn)
        missing = []
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing_cols:
                    col_sql = _build_add_column_sql(table.name, col)
                    missing.append((table.name, col.name, col_sql))
        return missing

    async with engine.connect() as scan_conn:
        missing_cols = await scan_conn.run_sync(_sync_find_missing)

    if not missing_cols:
        print("[init_db] No missing columns detected")
        return

    for table_name, col_name, col_sql in missing_cols:
        try:
            async with engine.connect() as alt_conn:
                sql = f"ALTER TABLE {table_name} ADD COLUMN {col_sql}"
                await alt_conn.execute(text(sql))
                await alt_conn.commit()
                print(f"[init_db] Added column: {table_name}.{col_name}")
        except Exception as e:
            print(f"[init_db] Skip adding {table_name}.{col_name}: {e}")


async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    await _add_missing_columns()
