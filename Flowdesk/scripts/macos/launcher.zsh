#!/bin/zsh
# Flowdesk.app 的启动程序。安装脚本会把 __REPO__ 换成本机仓库路径。
# 点一下：服务已在运行就直接打开网页；没在运行就在后台启动（不弹终端），等它就绪再打开。
REPO="__REPO__"
PORT=8765
URL="http://127.0.0.1:$PORT"
LOG="$HOME/Library/Logs/Flowdesk.log"
# 从 Dock 打开的程序拿不到终端里的环境，这里补上中文编码和 uv / node / pnpm 的常见位置。
export LANG="${LANG:-en_US.UTF-8}" LC_ALL="${LC_ALL:-en_US.UTF-8}"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

running() {
  [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$URL/" 2>/dev/null)" != "000" ]]
}

say_problem() {
  local answer
  answer=$(osascript -e "button returned of (display dialog \"$1\" with title \"Flowdesk\" buttons {\"看看原因\", \"好\"} default button \"好\" with icon caution)" 2>/dev/null)
  if [[ "$answer" == "看看原因" && -f "$LOG" ]]; then
    xattr -w com.apple.TextEncoding 'utf-8;134217984' "$LOG" 2>/dev/null
    open -e "$LOG"
  fi
}

if running; then
  open "$URL"
  exit 0
fi

if [[ ! -f "$REPO/start.command" ]]; then
  say_problem "找不到 Flowdesk 的文件夹：$REPO 。它可能被移动了，请重新运行安装。"
  exit 1
fi

# 能看到文件但读不了，说明系统没允许 Flowdesk 访问它所在的文件夹（桌面、文稿受保护）。
if ! head -c 1 "$REPO/start.command" >/dev/null 2>&1; then
  say_problem "Flowdesk 还没获准打开它所在的文件夹。请到「系统设置 → 隐私与安全性 → 文件和文件夹」，打开 Flowdesk 下面的开关，再点一次 Flowdesk。"
  exit 1
fi

mkdir -p "${LOG:h}"
print "==== $(date '+%Y-%m-%d %H:%M:%S') 启动 Flowdesk ====" >> "$LOG"
nohup /bin/zsh "$REPO/start.command" >> "$LOG" 2>&1 < /dev/null &
pid=$!
disown

# 最多等 3 分钟（第一次要安装依赖、构建界面，会慢一些）。
for i in {1..360}; do
  if running; then
    open "$URL"
    exit 0
  fi
  if ! kill -0 $pid 2>/dev/null; then
    break
  fi
  if (( i == 8 )); then
    osascript -e 'display notification "正在启动，稍等一下，好了会自动打开网页。" with title "Flowdesk"' 2>/dev/null
  fi
  sleep 0.5
done

say_problem "Flowdesk 没能启动。"
exit 1
