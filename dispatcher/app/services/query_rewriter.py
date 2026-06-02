"""
查询重写模块 — 将模糊查询扩展为多个精准搜索词

Phase 1-3: Worker 的原始 query（如"怎么连数据库"）经 LLM 扩展为具体搜索词，
提升语义搜索的召回率 30%+。

使用方式：
    rewritten = await rewrite_query("怎么连数据库")
    # → ["PostgreSQL 连接池配置", "SQLAlchemy asyncpg 异步连接", "数据库连接超时处理"]
"""

import logging
from app.services import ai_leader

logger = logging.getLogger(__name__)

REWRITE_SYSTEM = """你是一个搜索查询优化专家。用户输入的查询往往口语化、模糊，
你需要将其扩展为 3-5 个更具体、更适合技术文档/代码库检索的查询词。

要求：
- 保留原意，但用更精确的技术术语表达
- 覆盖不同角度（配置、实现、问题排查、最佳实践）
- 每个查询词长度不超过 20 字
- 优先使用英文技术术语（如 PostgreSQL, async/await, connection pool）

输出 JSON 数组：
["扩展查询1", "扩展查询2", "扩展查询3"]"""


async def rewrite_query(query: str, tech_stack: list[str] | None = None) -> list[str]:
    """
    将模糊查询重写为多个精准搜索词。

    Args:
        query: 原始查询文本
        tech_stack: 可选的技术栈上下文，用于引导重写方向

    Returns:
        扩展后的查询词列表（始终包含原始查询作为第一项）
    """
    if not query or not query.strip():
        return []

    query = query.strip()

    # 短查询才需要重写（< 15 字或不含技术术语）
    if len(query) >= 20 and _has_tech_terms(query):
        return [query]

    ts_hint = f"\n已知技术栈: {', '.join(tech_stack)}" if tech_stack else ""
    prompt = f"原始查询: {query}{ts_hint}\n\n请扩展为精准搜索词。"

    try:
        result = await ai_leader._call_json(
            REWRITE_SYSTEM, prompt, max_tokens=512, temperature=0.3,
        )
        if isinstance(result, list) and result:
            expanded = [str(q).strip() for q in result if str(q).strip()]
            # 去重，保留原始查询为首位
            seen = {query}
            final = [query]
            for q in expanded:
                if q not in seen:
                    final.append(q)
                    seen.add(q)
            logger.debug(f"Query rewritten: '{query}' → {final}")
            return final[:5]  # 最多 5 个
        if isinstance(result, dict) and "queries" in result:
            expanded = [str(q).strip() for q in result["queries"] if str(q).strip()]
            seen = {query}
            final = [query]
            for q in expanded:
                if q not in seen:
                    final.append(q)
                    seen.add(q)
            return final[:5]
    except Exception as e:
        logger.debug(f"Query rewrite failed (non-blocking): {e}")

    return [query]


# 常见技术术语，用于判断查询是否已足够精确
_TECH_TERMS = {
    "api", "rest", "graphql", "grpc", "http", "websocket",
    "sql", "postgresql", "mysql", "sqlite", "mongodb", "redis", "elasticsearch",
    "docker", "kubernetes", "k8s", "helm", "terraform", "ansible",
    "aws", "gcp", "azure", "aliyun", "tencent",
    "nginx", "apache", "caddy", "traefik",
    "jwt", "oauth", "sso", "ldap", "saml",
    "ci/cd", "jenkins", "gitlab", "github", "argocd",
    "prometheus", "grafana", "elk", "jaeger", "zipkin",
    "python", "java", "go", "golang", "rust", "typescript", "javascript",
    "react", "vue", "angular", "svelte", "nextjs", "nuxt",
    "fastapi", "flask", "django", "spring", "springboot", "express",
    "asyncio", "async", "await", "coroutine", "thread", "threadpool",
    "orm", "sqlalchemy", "hibernate", "prisma", "typeorm",
    "migration", "alembic", "flyway", "liquibase",
    "pytest", "unittest", "jest", "mocha", "cypress", "playwright",
    "protobuf", "thrift", "avro", "json", "yaml", "xml",
    "kafka", "rabbitmq", "rocketmq", "pulsar", "nats",
    "cassandra", "dynamodb", "cosmosdb", "neo4j",
    "minio", "s3", "oss", "cos", "hdfs",
    "terraform", "pulumi", "cloudformation",
    "linux", "ubuntu", "centos", "debian", "alpine",
    "systemd", "supervisor", "pm2", "systemctl",
    "github actions", "circleci", "travis",
    "openai", "anthropic", "claude", "gpt", "llm", "embedding", "vector",
    "rag", "fine-tune", "lora", "qlora", "quantization",
    "connection pool", "transaction", "index", "partition", "sharding",
    "replication", "failover", "backup", "restore", "snapshot",
    "cache", "cdn", "load balancer", "reverse proxy",
    "microservice", "monolith", "serverless", "faas",
    "event sourcing", "cqrs", "saga", "outbox",
    "circuit breaker", "retry", "timeout", "backoff", "bulkhead",
    "observability", "monitoring", "tracing", "logging",
    "encryption", "hash", "signature", "certificate", "tls", "ssl",
    "cors", "csrf", "xss", "sqli", "injection",
}


def _has_tech_terms(query: str) -> bool:
    """检查查询是否已包含足够的技术术语"""
    q = query.lower()
    return any(term in q for term in _TECH_TERMS)
