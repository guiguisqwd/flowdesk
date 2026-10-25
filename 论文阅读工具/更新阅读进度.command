#!/bin/bash
# 双击：把 Flowdesk 里已通过的论文阅读进度汇总到「文稿/论文阅读进度」，然后打开它。
cd "$(dirname "$0")" || exit 1
PY=""
for c in "$HOME/Flowdesk/.venv/bin/python" \
  "$(sed -n 's/^REPO="\(.*\)"$/\1/p' /Applications/Flowdesk.app/Contents/Resources/launcher.zsh 2>/dev/null)/.venv/bin/python" \
  "$(command -v python3 2>/dev/null)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "找不到 Python：请先装好 Flowdesk（它会带上 Python），再双击一次。"; read -r -p "按回车关闭"; exit 1; }
"$PY" reading_progress.py update && open "$HOME/Documents/论文阅读进度/阅读进度.md"
