#!/bin/zsh
# 生成 Flowdesk.app：放进「应用程序」，可选加到 Dock。
# 用法：scripts/macos/install-app.sh [--dock]
#
# 用 AppleScript 小程序（osacompile）包一层，而不是直接用 shell 脚本当程序：
# 仓库常放在「桌面」「文稿」里，这两个文件夹受系统保护。小程序第一次打开时，
# 系统会弹一次「允许 Flowdesk 访问…文件夹」，点允许后它启动的服务就能读仓库；
# 纯 shell 脚本程序不会弹这个询问，只会被静默拒绝。
set -eu
HERE="${0:A:h}"
REPO="${HERE:h:h}"
APPS="/Applications"
[[ -w "$APPS" ]] || { APPS="$HOME/Applications"; mkdir -p "$APPS"; }
APP="$APPS/Flowdesk.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/Flowdesk.applescript" <<'APPLESCRIPT'
on run
	set launcher to POSIX path of (path to resource "launcher.zsh")
	try
		do shell script "/bin/zsh " & quoted form of launcher
	end try
end run
APPLESCRIPT

rm -rf "$APP"
osacompile -o "$APP" "$TMP/Flowdesk.applescript"
RES="$APP/Contents/Resources"
PLIST="$APP/Contents/Info.plist"

# 启动程序，写入本机仓库路径
sed "s|__REPO__|$REPO|" "$HERE/launcher.zsh" > "$RES/launcher.zsh"

# 图标：PNG → icns，替换小程序默认图标
ICONSET="$TMP/Flowdesk.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$HERE/Flowdesk.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z $double $double "$HERE/Flowdesk.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/applet.icns"
rm -f "$RES/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true

set_key() {
  /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST"
}
set_key CFBundleIdentifier local.flowdesk.app
set_key CFBundleName Flowdesk
set_key CFBundleDisplayName Flowdesk
set_key CFBundleIconFile applet

# 改过内容后重新签名（本机签名即可），系统才会正常弹出文件夹访问询问
codesign --force --deep --sign - "$APP" >/dev/null 2>&1
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true

if [[ "${1:-}" == "--dock" ]]; then
  if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Flowdesk.app"; then
    defaults write com.apple.dock persistent-apps -array-add \
      "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${APP// /%20}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
  fi
  killall Dock
fi

echo "已安装：$APP"
echo "第一次点开时，如果系统问是否允许 Flowdesk 访问「桌面」或「文稿」文件夹，点「允许」。"
