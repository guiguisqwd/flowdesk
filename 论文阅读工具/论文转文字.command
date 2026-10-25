#!/bin/bash
# 双击：选一个或几个 PDF，转成带页码的文字版（同名 .txt，放在 PDF 旁边），开始一条论文阅读时交这个 .txt。
cd "$(dirname "$0")" || exit 1
PY=""
for c in "$HOME/Flowdesk/.venv/bin/python" \
  "$(sed -n 's/^REPO="\(.*\)"$/\1/p' /Applications/Flowdesk.app/Contents/Resources/launcher.zsh 2>/dev/null)/.venv/bin/python" \
  "$(command -v python3 2>/dev/null)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "找不到 Python：请先装好 Flowdesk（它会带上 Python），再双击一次。"; read -r -p "按回车关闭"; exit 1; }
PICKED=$(osascript -e 'set fs to choose file with prompt "选要转成文字的论文 PDF" of type {"com.adobe.pdf"} with multiple selections allowed' \
  -e 'set out to ""' -e 'repeat with f in fs' -e 'set out to out & POSIX path of f & linefeed' -e 'end repeat' -e 'return out' 2>/dev/null)
[ -n "$PICKED" ] || { echo "没选文件。"; exit 0; }
ARGS=()
while IFS= read -r line; do [ -n "$line" ] && ARGS+=("$line"); done <<< "$PICKED"
"$PY" reading_progress.py pdf2txt "${ARGS[@]}"
echo
echo "转出来的 .txt 在 PDF 旁边。检查开头写的「没转出文字的页」：扫描版的页面转不出文字，需要截图交给 Flowdesk。"
read -r -p "按回车关闭"
