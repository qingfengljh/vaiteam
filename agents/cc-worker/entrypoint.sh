#!/usr/bin/env bash
# CC Worker 容器入口
# 环境变量:
#   AGENT_ID              (必填) Agent 唯一标识
#   AGENT_ROLE            (可选) 角色: senior|mid|junior|architect|devops|tester, 默认 mid
#   DISPATCHER_BASE       (可选) Dispatcher 地址, 默认 http://dispatcher:8080
#   AGENT_API_TOKEN       (可选) API 鉴权 token
#   SKIP_CLAUDE           (可选) 1=跳过 claude 执行(测试用)
#   CC_DRY_RUN            (可选) 1=只写提示词不执行 claude
#
# Claude Code 配置优先从 Dispatcher /api/agent-providers/active 获取，
# 支持三种协议适配模式：
#   anthropic_direct:   供应商原生支持 Anthropic 协议
#   openai_via_litellm: 供应商仅支持 OpenAI 协议，容器内启动 litellm 代理 + 协议转换
#   codex:              使用 OpenAI Codex（原生 OpenAI 协议）
set -euo pipefail

: "${AGENT_ID:?AGENT_ID environment variable is required}"

ROLE="${AGENT_ROLE:-mid}"
DISPATCHER="${DISPATCHER_BASE:-http://dispatcher:8080}"

echo "[entrypoint] CC Worker starting"
echo "  agent_id: ${AGENT_ID}"
echo "  role: ${ROLE}"
echo "  dispatcher: ${DISPATCHER}"
echo "  skip_claude: ${SKIP_CLAUDE:-0}"
echo "  dry_run: ${CC_DRY_RUN:-0}"

# 确保 workspace 目录存在并可写
mkdir -p /workspace /var/log/vaiteam
chown -R worker:worker /workspace /var/log/vaiteam 2>/dev/null || true
chmod -R u+w /workspace 2>/dev/null || true

# Git 安全策略：允许 worker 用户操作 /workspace 下所有仓库
git config --global --add safe.directory /workspace
git config --global --add safe.directory '*'

# 确保 SSH key 可被 worker 用户读取（挂载的 key 通常是 root:root 且 ro）
# 复制到 worker 家目录并修正权限
if [ -d /tmp/host-ssh ] && [ -f /tmp/host-ssh/id_ed25519 ]; then
  mkdir -p /home/worker/.ssh
  cp -r /tmp/host-ssh/. /home/worker/.ssh/ 2>/dev/null || true
  chown -R worker:worker /home/worker/.ssh 2>/dev/null || true
  chmod 700 /home/worker/.ssh 2>/dev/null || true
  chmod 600 /home/worker/.ssh/id_* 2>/dev/null || true
  echo "[entrypoint] SSH keys copied from /tmp/host-ssh"
fi
# 兼容直接挂载 .ssh 的情况（docker run -v ~/.ssh:/home/worker/.ssh:ro）
if [ ! -f /home/worker/.ssh/id_ed25519 ] && [ -f /etc/ssh/ssh_host_ed25519_key ]; then
  # 没有外部 key，跳过
  :
fi

# ── 从 Dispatcher 获取 Agent Provider 配置 ──
# 优先从 API 获取角色对应的供应商配置（支持多供应商、角色级路由、协议适配）
CC_API_KEY=""
CC_API_BASE=""
CC_CRED_ENV="ANTHROPIC_API_KEY"
PROTOCOL_ADAPTER="anthropic_direct"
LITELLM_CONFIG='{}'
MODEL_MAPPING='{}'
DEFAULT_MODEL=""

CONFIG_URL="${DISPATCHER}/api/agent-providers/active?role=${ROLE}&agent_type=claude_code"
echo "[entrypoint] Fetching agent provider config from: ${CONFIG_URL}"

# 尝试从 Dispatcher 获取配置（最多重试 3 次）
for i in 1 2 3; do
  CONFIG_JSON=$(curl -s -m 10 "${CONFIG_URL}" 2>/dev/null || echo "")
  if [ -n "$CONFIG_JSON" ] && echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'api_key' in d else 1)" 2>/dev/null; then
    CC_API_KEY=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))")
    CC_API_BASE=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_base',''))")
    CC_CRED_ENV=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('credential_env_name','ANTHROPIC_API_KEY'))")
    PROTOCOL_ADAPTER=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('protocol_adapter','anthropic_direct'))")
    LITELLM_CONFIG=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('litellm_config',{}); print(json.dumps(d))")
    MODEL_MAPPING=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('model_mapping',{}); print(json.dumps(d))")
    DEFAULT_MODEL=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('default_model',''))")
    PROVIDER_NAME=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('provider_name',''))")
    echo "[entrypoint] Loaded provider config: provider=${PROVIDER_NAME}, adapter=${PROTOCOL_ADAPTER}, cred_env=${CC_CRED_ENV}"
    break
  fi
  echo "[entrypoint] Config fetch attempt $i failed, retrying..."
  sleep 2
done

# 回退到环境变量（兼容旧配置方式）
if [ -z "$CC_API_KEY" ]; then
  CC_API_KEY="${CC_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
  echo "[entrypoint] Fallback to env CC_ANTHROPIC_API_KEY"
fi
if [ -z "$CC_API_BASE" ]; then
  CC_API_BASE="${CC_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-}}"
fi

# ── 协议适配：根据 protocol_adapter 设置环境变量 ──
CLAUDE_HOME=/home/worker/.claude
mkdir -p "$CLAUDE_HOME"

PROTOXY_PID=""

if [ "$PROTOCOL_ADAPTER" = "codex" ]; then
  # TODO: Codex 模式预留。Codex 是 OpenAI 原生工具（非 Claude Code），
  # 需要独立的执行器和启动逻辑。当前先回退到 anthropic_direct 模式。
  echo "[entrypoint] Protocol adapter: codex (TODO: not fully implemented, falling back to anthropic_direct behavior)"
  CC_CRED_ENV="ANTHROPIC_API_KEY"
  python3 -c "
import json
env = {'CLAUDE_CODE_SIMPLE': '1'}
if '$CC_API_KEY':
    env['$CC_CRED_ENV'] = '$CC_API_KEY'
if '$CC_API_BASE':
    env['ANTHROPIC_BASE_URL'] = '$CC_API_BASE'
cfg = {
    'env': env,
    'skipAutoPermissionPrompt': True,
    'skipDangerousModePermissionPrompt': True,
}
print(json.dumps(cfg, indent=2))
" > "$CLAUDE_HOME/settings.json"

elif [ "$PROTOCOL_ADAPTER" = "openai_via_litellm" ]; then
  # openai_via_litellm 模式：启动 litellm 代理 + 协议转换
  echo "[entrypoint] Protocol adapter: openai_via_litellm"
  echo "[entrypoint] Starting litellm proxy + anthropic-to-openai protocol bridge..."

  # 解析 litellm_config
  PROXY_MODEL=$(echo "$LITELLM_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('proxy_model_name',''))")
  LITELLM_ALIAS=$(echo "$LITELLM_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('litellm_model_alias',''))")

  # 选择实际模型：litellm_alias > default_model > mapping 第一个
  LITELLM_MODEL="${LITELLM_ALIAS:-$DEFAULT_MODEL}"
  if [ -z "$LITELLM_MODEL" ] && [ "$MODEL_MAPPING" != "{}" ]; then
    LITELLM_MODEL=$(echo "$MODEL_MAPPING" | python3 -c "import sys,json; d=json.load(sys.stdin); print(next(iter(d.values()), ''))")
  fi

  if [ -z "$LITELLM_MODEL" ]; then
    echo "[WARN] openai_via_litellm 模式未配置模型，litellm 代理可能无法正常工作"
    LITELLM_MODEL="gpt-4o"
  fi

  echo "[entrypoint] litellm model: $LITELLM_MODEL"

  # 启动 litellm proxy（后台）— 连接供应商 OpenAI 端点
  LITELLM_PORT=8000
  export LITELLM_API_KEY="$CC_API_KEY"
  if [ -n "$CC_API_BASE" ]; then
    export LITELLM_API_BASE="$CC_API_BASE"
  fi

  # 启动 litellm proxy
  litellm --model "$LITELLM_MODEL" --port "$LITELLM_PORT" \
    --api_key "$CC_API_KEY" \
    ${CC_API_BASE:+--api_base "$CC_API_BASE"} \
    > /var/log/vaiteam/litellm.log 2>&1 &
  LITELLM_PID=$!
  echo "[entrypoint] litellm proxy started (pid=$LITELLM_PID, port=$LITELLM_PORT)"

  # 等待 litellm 就绪
  for i in 1 2 3 4 5; do
    if curl -s -m 2 "http://localhost:$LITELLM_PORT/health" > /dev/null 2>&1; then
      echo "[entrypoint] litellm proxy is ready"
      break
    fi
    sleep 1
  done

  # 启动 anthropic-to-openai 协议转换代理（后台）
  # 这个代理将 Claude Code 的 Anthropic 协议请求转换为 OpenAI 协议，转发给 litellm
  PROTOXY_PORT=8001
  python3 /opt/vaiteam-cc-worker/protocol_bridge.py \
    --upstream "http://localhost:$LITELLM_PORT" \
    --port "$PROTOXY_PORT" \
    > /var/log/vaiteam/protocol_bridge.log 2>&1 &
  PROTOXY_PID=$!
  echo "[entrypoint] Protocol bridge started (pid=$PROTOXY_PID, port=$PROTOXY_PORT)"

  # 等待协议桥就绪
  for i in 1 2 3; do
    if curl -s -m 2 "http://localhost:$PROTOXY_PORT/health" > /dev/null 2>&1; then
      echo "[entrypoint] Protocol bridge is ready"
      break
    fi
    sleep 1
  done

  # Claude Code 连接协议桥（Anthropic 协议）
  CC_API_BASE="http://localhost:$PROTOXY_PORT"
  CC_CRED_ENV="ANTHROPIC_API_KEY"
  # Claude Code 的 API key 在协议桥中只用于身份验证占位，实际转发给 litellm 时会替换
  python3 -c "
import json
env = {'CLAUDE_CODE_SIMPLE': '1'}
if '$CC_API_KEY':
    env['ANTHROPIC_API_KEY'] = '$CC_API_KEY'
env['ANTHROPIC_BASE_URL'] = 'http://localhost:$PROTOXY_PORT'
cfg = {
    'env': env,
    'skipAutoPermissionPrompt': True,
    'skipDangerousModePermissionPrompt': True,
}
print(json.dumps(cfg, indent=2))
" > "$CLAUDE_HOME/settings.json"

else
  # anthropic_direct 模式（默认）：直连供应商 Anthropic 端点
  echo "[entrypoint] Protocol adapter: anthropic_direct"
  python3 -c "
import json
env = {'CLAUDE_CODE_SIMPLE': '1'}
if '$CC_API_KEY':
    env['$CC_CRED_ENV'] = '$CC_API_KEY'
if '$CC_API_BASE':
    env['ANTHROPIC_BASE_URL'] = '$CC_API_BASE'
cfg = {
    'env': env,
    'skipAutoPermissionPrompt': True,
    'skipDangerousModePermissionPrompt': True,
}
print(json.dumps(cfg, indent=2))
" > "$CLAUDE_HOME/settings.json"
fi

chown -R worker:worker "$CLAUDE_HOME"

# 检查必需的 API key
if [ -z "$CC_API_KEY" ]; then
  echo "[WARN] 未获取到 Agent Provider 配置，且 CC_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY 均未设置，Claude Code 可能无法运行"
fi

# 日志输出实际使用的配置（便于调试）
echo "[entrypoint] Final config summary:"
echo "  protocol_adapter: $PROTOCOL_ADAPTER"
echo "  credential_env: $CC_CRED_ENV"
if [ -n "$CC_API_BASE" ]; then
  echo "  api_base: $CC_API_BASE"
fi
if [ -n "$DEFAULT_MODEL" ]; then
  echo "  default_model: $DEFAULT_MODEL"
fi

export CLAUDE_CODE_SIMPLE=1

# 注册清理函数：退出时停止后台进程
cleanup() {
  if [ -n "$PROTOXY_PID" ]; then
    echo "[entrypoint] Stopping protocol bridge (pid=$PROTOXY_PID)..."
    kill $PROTOXY_PID 2>/dev/null || true
  fi
  if [ -n "${LITELLM_PID:-}" ]; then
    echo "[entrypoint] Stopping litellm proxy (pid=$LITELLM_PID)..."
    kill $LITELLM_PID 2>/dev/null || true
  fi
}
trap cleanup EXIT

# 切换到 worker 用户运行主程序
exec gosu worker python3 /opt/vaiteam-cc-worker/run_task_pack.py "$@"
