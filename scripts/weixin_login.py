"""Interactive QR login for the xu-agent Weixin iLink bridge."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from gateway.platforms.weixin import ACCOUNT_FILE, qr_login


async def main() -> int:
    try:
        credentials = await qr_login()
    except Exception as exc:
        print(f"微信登录失败：{exc}", file=sys.stderr)
        return 1
    if not credentials:
        print("微信登录超时。", file=sys.stderr)
        return 1
    print(f"\n微信连接成功：{credentials['account_id']}")
    print(f"凭据已保存到：{ACCOUNT_FILE}")
    print("设置 WEIXIN_ENABLED=true 并重启后端即可启用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
