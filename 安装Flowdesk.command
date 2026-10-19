#!/bin/bash
# Flowdesk 试用版安装程序（Mac）。双击运行，或在「终端」里输入：bash 后面拖入本文件，回车。
# 做的事：把 Flowdesk 放到 ~/Flowdesk，装好它要用的 Python 环境，检查 Codex 登录，
# 在「应用程序」和 Dock 里放一个 Flowdesk 图标，然后打开它。
set -u
SRC="$(cd "$(dirname "$0")" && pwd)/Flowdesk"
DEST="$HOME/Flowdesk"
APPDIR="/Applications"; [ -w "$APPDIR" ] || APPDIR="$HOME/Applications"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export LANG="${LANG:-en_US.UTF-8}" LC_ALL="${LC_ALL:-en_US.UTF-8}"

step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail() {
  printf '\n\033[31m没装好：%s\033[0m\n' "$1"
  printf '可以把这个窗口的文字截图，连同「反馈表.txt」一起发回来。\n'
  osascript -e "display dialog \"没装好：$1\" with title \"Flowdesk\" buttons {\"好\"} with icon caution" >/dev/null 2>&1
  exit 1
}

clear 2>/dev/null
echo "Flowdesk 试用版安装"
echo "第一次安装要下载一些东西，大约 3–10 分钟，请保持联网，别关这个窗口。"

[ "$(uname)" = "Darwin" ] || fail "这个试用版只支持 Mac。"
[ -f "$SRC/start.command" ] || fail "找不到 Flowdesk 文件夹。请先把下载的 ZIP 解压，再双击解压出来的「安装Flowdesk.command」。"

# 已经有一个别处的 Flowdesk 图标（比如开发者自己的），不随便替换
if [ -f "$APPDIR/Flowdesk.app/Contents/Resources/launcher.zsh" ]; then
  old="$(sed -n 's/^REPO="\(.*\)"$/\1/p' "$APPDIR/Flowdesk.app/Contents/Resources/launcher.zsh")"
  if [ -n "$old" ] && [ "$old" != "$DEST" ]; then
    answer=$(osascript -e "button returned of (display dialog \"这台 Mac 上已经有一个 Flowdesk（在 $old）。试用版装好后，Dock 上的 Flowdesk 图标会改为打开试用版。要继续吗？\" with title \"Flowdesk\" buttons {\"取消\", \"继续\"} default button \"取消\")" 2>/dev/null)
    [ "$answer" = "继续" ] || { echo "已取消，什么都没改。"; exit 0; }
  fi
fi

step "1/5 复制 Flowdesk 到 $DEST"
mkdir -p "$DEST/projects" || fail "不能在你的个人文件夹里建 Flowdesk 文件夹。"
# 重装或升级时保留你已经做的项目
rsync -a --delete --exclude '/projects/' --exclude '/.venv/' --exclude '/.uv-cache/' "$SRC/" "$DEST/" \
  || fail "复制文件失败。"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
chmod +x "$DEST/start.command" "$DEST/scripts/macos/"*.sh "$DEST/scripts/macos/"*.zsh

step "2/5 准备运行环境（uv 和 Python）"
if ! command -v uv >/dev/null 2>&1; then
  echo "安装 uv（Python 的安装工具，来自 astral.sh）…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || fail "下载 uv 失败，请检查网络后再试一次。"
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv 装完了但找不到它。"
fi
(cd "$DEST" && UV_CACHE_DIR=.uv-cache uv sync --locked --python 3.12) \
  || fail "安装 Python 环境失败，请检查网络后再试一次。"

step "3/5 检查 Codex（Flowdesk 用它来调用 AI）"
CODEX=""
for c in "$(command -v codex 2>/dev/null)" \
  "/Applications/Codex.app/Contents/Resources/codex" \
  "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CODEX="$c"; break; fi
done
if [ -z "$CODEX" ]; then
  echo "没找到 Codex。会打开 Codex 的下载页：装好 Codex 桌面版、用 ChatGPT 账号登录后，再双击一次本安装程序。"
  open "https://openai.com/codex/"
  fail "需要先安装 Codex 桌面版并登录 ChatGPT 账号。"
fi
echo "找到 Codex：$CODEX"
logged_in() { "$CODEX" --no-daemon login status >/dev/null 2>&1 || "$CODEX" login status >/dev/null 2>&1; }
if logged_in; then
  echo "Codex 已登录。"
else
  echo "Codex 还没登录。马上会打开浏览器，用你的 ChatGPT 账号登录，登录成功后回到这个窗口。"
  "$CODEX" login || fail "Codex 登录没有完成。"
  logged_in || fail "Codex 登录没有完成。"
fi

step "4/5 在「应用程序」和 Dock 里放 Flowdesk 图标"
/bin/zsh "$DEST/scripts/macos/install-app.sh" --dock || fail "生成 Flowdesk 图标失败。"

step "5/5 打开 Flowdesk"
open "$APPDIR/Flowdesk.app"
cat <<'DONE'

装好了。浏览器会打开 http://127.0.0.1:8765（第一次启动要十几秒）。
以后点 Dock 上的 Flowdesk 图标就能打开。
怎么试、怎么反馈，看「先读我.txt」。这个窗口可以关了。
DONE
