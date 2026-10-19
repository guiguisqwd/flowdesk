import argparse
import secrets
import socket
from pathlib import Path

import uvicorn

from .app import ROOT, create_app


def lan_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description="本地功能模块工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="独立项目数据目录")
    parser.add_argument("--phone", action="store_true",
                        help="允许同一 Wi-Fi 下的手机打开审核台（需要带密钥的链接）")
    args = parser.parse_args()
    if not args.phone:
        uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port, access_log=False)
        return
    key_file = Path(args.data_dir or ROOT / "projects") / ".phone-key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        key_file.chmod(0o600)
    token = key_file.read_text(encoding="utf-8").strip()
    address = lan_address() or "本机局域网地址"
    print(f"手机上审核（同一 Wi-Fi 下打开，链接里有密钥，别发给别人）：http://{address}:{args.port}/?key={token}")
    uvicorn.run(create_app(args.data_dir, phone_token=token), host="0.0.0.0", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
