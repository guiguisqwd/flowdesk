#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
runtime_node="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
runtime_tools="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback"
if [[ -x "$runtime_node/node" ]]; then
  export PATH="$runtime_node:$runtime_tools:$PATH"
fi
if ! command -v uv >/dev/null; then
  echo '需要 uv 和 Python 3.12。请先安装 uv，或在 Codex 中打开 README 进行配置。'
  exit 1
fi
UV_CACHE_DIR=.uv-cache uv sync --locked --python 3.12
# 界面没构建过，或者界面代码比上次构建新（例如刚更新过），就重新构建。
ui_stale=0
if [[ ! -f ui/dist/index.html ]]; then
  ui_stale=1
elif [[ -n "$(find ui/src ui/index.html ui/package.json ui/pnpm-lock.yaml ui/vite.config.ts -newer ui/dist/index.html -print -quit 2>/dev/null || true)" ]]; then
  ui_stale=1
fi
if (( ui_stale )); then
  if ! command -v pnpm >/dev/null; then
    if [[ ! -f ui/dist/index.html ]]; then
      echo '首次构建需要 Node.js 22+ 和 pnpm。配置好后重新启动。'
      exit 1
    fi
    echo '界面代码有更新，但找不到 pnpm，先用上次构建的界面。'
  else
    (cd ui && pnpm install --frozen-lockfile && node ./node_modules/typescript/bin/tsc -b && node ./node_modules/vite/bin/vite.js build)
  fi
fi
echo '工作台地址：http://127.0.0.1:8765'
echo '保持此窗口运行；按 Ctrl+C 停止。'
exec .venv/bin/python -m workbench --port 8765
