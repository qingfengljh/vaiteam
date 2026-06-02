#!/usr/bin/env bash
# 包装一次「拉 task-pack →（可选）claude -p → webhook 收口」；与 docs/PROTOTYPE_CC_RUN_PIPELINE.md 一致。
# 必填：VAI_DISPATCHER_BASE、VAI_PROJECT_ID、VAI_RUN_ID、VAI_RUN_SECRET
# 拉包二选一：VAI_PACK_USE_SECRET=1 时用 X-Prototype-Run-Secret（远程 Worker）；否则需 VAI_JWT（浏览器 token）
set -euo pipefail

: "${VAI_DISPATCHER_BASE:?}"
: "${VAI_PROJECT_ID:?}"
: "${VAI_RUN_ID:?}"
: "${VAI_RUN_SECRET:?}"

BASE="${VAI_DISPATCHER_BASE%/}"
WH_URL="${BASE}/api/webhook/prototype-run"

# ── Claude Code 认证（从环境变量注入）──
CC_API_KEY="${CC_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
CC_API_BASE="${CC_ANTHROPIC_BASE_URL:-${ANTHROPIC_BASE_URL:-}}"
if [ -n "$CC_API_KEY" ]; then
  CLAUDE_HOME="${HOME}/.claude"
  mkdir -p "$CLAUDE_HOME"
  python3 -c "
import json
env = {'CLAUDE_CODE_SIMPLE': '1'}
env['ANTHROPIC_API_KEY'] = '$CC_API_KEY'
if '$CC_API_BASE':
    env['ANTHROPIC_BASE_URL'] = '$CC_API_BASE'
cfg = {'env': env, 'skipAutoPermissionPrompt': True, 'skipDangerousModePermissionPrompt': True}
print(json.dumps(cfg, indent=2))
" > "$CLAUDE_HOME/settings.json"
  # 也复制到 worker 用户（如果存在）
  if [ -d /home/worker ]; then
    mkdir -p /home/worker/.claude
    cp "$CLAUDE_HOME/settings.json" /home/worker/.claude/settings.json
    chown -R worker:worker /home/worker/.claude 2>/dev/null || true
  fi
  echo '[wrapper] Claude Code settings written'
  export CLAUDE_CODE_SIMPLE=1
fi

WORKDIR="${VAI_WORKDIR:-$(mktemp -d /tmp/proto-run-XXXXXX)}"
mkdir -p "$WORKDIR"

cleanup() {
  local code=$?
  if [[ "${VAI_KEEP_WORKDIR:-0}" != "1" ]] && [[ -n "${WORKDIR:-}" ]] && [[ "$WORKDIR" == /tmp/proto-run-* ]]; then
    rm -rf "$WORKDIR" 2>/dev/null || true
  fi
  exit "$code"
}
trap cleanup EXIT

echo "[wrapper] fetching task-pack -> $WORKDIR/pack.json"
if [[ "${VAI_PACK_USE_SECRET:-0}" == "1" ]]; then
  PACK_URL="${BASE}/api/prototype-workshop/worker/runs/${VAI_RUN_ID}/task-pack"
  curl -fsS -H "X-Prototype-Run-Secret: ${VAI_RUN_SECRET}" -H "Accept: application/json" \
    "$PACK_URL" -o "$WORKDIR/pack.json"
else
  : "${VAI_JWT:?}"
  PACK_URL="${BASE}/api/prototype-workshop/projects/${VAI_PROJECT_ID}/task-pack"
  curl -fsS -H "Authorization: Bearer ${VAI_JWT}" -H "Accept: application/json" \
    "$PACK_URL" -o "$WORKDIR/pack.json"
fi

SUMMARY="fetched task-pack"
ERR=""
EXIT=0
ART="$WORKDIR/out"
if command -v claude >/dev/null 2>&1 && [[ "${VAI_SKIP_CLAUDE:-0}" != "1" ]]; then
  echo "[wrapper] running claude (set VAI_SKIP_CLAUDE=1 to skip)"
  PROMPT="${VAI_CLAUDE_PROMPT:-Read pack.json — this is the COMPLETE product specification for a customer-facing corporate website.

You MUST produce a PRODUCTION-QUALITY, multi-page responsive website. This is for a real paying client — every detail matters.

=== PHASE 1: PLAN (output a plan first) ===
Before writing code, read pack.json and output a brief plan listing:
- All pages you will create
- The file structure (at least: index.html, css/style.css, js/app.js)
- Key components per page

=== PHASE 2: IMPLEMENT ===
Create ALL files. Quality bar:
- MOBILE-FIRST: Hamburger menu that OPENS/CLOSES. Tested on <768px width
- EVERY PAGE HAS REAL CONTENT: Pull text, data, case studies from pack.json
- MODERN DESIGN: Clean typography, spacing, color palette, transitions
- ALL NAV LINKS WORK: Each link shows its corresponding section/page
- FORM INTERACTIONS: Contact form validates, shows success/error states
- MULTI-LANGUAGE if specified in pack.json
- ACCESSIBLE: alt texts, semantic HTML, keyboard-friendly

=== PHASE 3: VERIFY ===
After writing all files:
1. Read index.html and verify ALL nav links have corresponding content
2. Check CSS has responsive breakpoints (at minimum: mobile + desktop)
3. Check JS has hamburger toggle and page routing
4. If anything is missing or broken, FIX IT before finishing}"
  if ! claude --bare -p "$PROMPT" --allowedTools "Read,Bash,Edit,Write" ; then
    EXIT=$?
    ERR="claude exited $EXIT"
  else
    SUMMARY="claude finished (workspace $WORKDIR)"
  fi
else
  echo "[wrapper] claude not found or skipped (connectivity / pack-only test)"
  SUMMARY="${SUMMARY}; no claude run"
fi

STATUS=succeeded
if [[ "$EXIT" != "0" ]]; then
  STATUS=failed
fi

export STATUS EXIT SUMMARY ERR ART
BODY=$(python3 -c "import json,os;print(json.dumps({
  'run_id':os.environ['VAI_RUN_ID'],
  'status':os.environ['STATUS'],
  'exit_code':int(os.environ['EXIT']),
  'summary':os.environ['SUMMARY'],
  'error':os.environ['ERR'],
  'artifact_ref':os.environ['ART'],
}))")

echo "[wrapper] POST $WH_URL status=$STATUS"
curl -fsS -X POST "$WH_URL" \
  -H "Content-Type: application/json" \
  -H "X-Prototype-Run-Secret: ${VAI_RUN_SECRET}" \
  -d "$BODY"
echo
echo "[wrapper] done"
