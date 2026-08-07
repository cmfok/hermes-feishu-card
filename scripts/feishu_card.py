#!/usr/bin/env python3
"""飞书交互卡片工具:发送 + 原地更新(message_id 不变)。

用法:
  python feishu_card.py send <chat_id> <card_json_file>   # 发送卡片,打印 message_id 并保存
  python feishu_card.py update <message_id> <card_json_file>  # 用同一条 message_id 原地更新
  python feishu_card.py update <message_id> --status 进行中    # 快捷:更新任务状态卡片

依赖: 无(仅标准库)。app_id/app_secret 从 Hermes .env 读取(FEISHU_APP_ID/FEISHU_APP_SECRET)。
"""
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://open.feishu.cn"
ALLOWED_HOST = "open.feishu.cn"  # 飞书开放平台域名白名单
TOKEN_TTL = 7200  # tenant_access_token 有效期约 2 小时
_TOKEN_CACHE = {"token": None, "expire_at": 0}
_MESSAGE_ID_FILE = Path(__file__).parent / "last_message_id.txt"


def _load_env():
    """从 Hermes .env 读取飞书凭据(兼容 HERMES_HOME 与默认路径)。"""
    candidates = [
        os.environ.get("HERMES_HOME"),
        str(Path.home() / "AppData/Local/hermes"),
        str(Path.home() / ".hermes"),
    ]
    for home in filter(None, candidates):
        env_path = Path(home) / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("FEISHU_APP_ID="):
                    os.environ.setdefault("FEISHU_APP_ID", line.split("=", 1)[1].strip())
                elif line.startswith("FEISHU_APP_SECRET="):
                    os.environ.setdefault("FEISHU_APP_SECRET", line.split("=", 1)[1].strip())
    return os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")


def _validate_url(url: str) -> None:
    """SSRF 防护:仅允许 https 白名单域名,解析后阻断私网/环回/链路本地/保留地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise SystemExit(f"拒绝非白名单 URL: {url}")
    for info in socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SystemExit(f"拒绝非公网地址 {ip} (host={parsed.hostname})")


class _FeishuRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向目标同样过白名单校验,防跳转到白名单外。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_FeishuRedirectHandler)


def _request(method, url, body=None, token=None):
    _validate_url(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        print(f"[HTTP {e.code}] {raw}", file=sys.stderr)
        raise SystemExit(1)


def get_token():
    """获取(并缓存)tenant_access_token。"""
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expire_at"] - 60:
        return _TOKEN_CACHE["token"]
    app_id, app_secret = _load_env()
    if not app_id or not app_secret:
        raise SystemExit("FEISHU_APP_ID / FEISHU_APP_SECRET 未找到")
    resp = _request("POST", f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
                    {"app_id": app_id, "app_secret": app_secret})
    if resp.get("code") != 0:
        raise SystemExit(f"token 获取失败: {resp}")
    _TOKEN_CACHE["token"] = resp["tenant_access_token"]
    _TOKEN_CACHE["expire_at"] = now + TOKEN_TTL
    return resp["tenant_access_token"]


def send_card(chat_id, card):
    """发送交互卡片,返回 message_id。"""
    token = get_token()
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),  # content 必须是 JSON 字符串
    }
    url = f"{BASE}/open-apis/im/v1/messages?receive_id_type=chat_id"
    resp = _request("POST", url, body, token)
    if resp.get("code") != 0:
        raise SystemExit(f"发送失败: {resp}")
    msg_id = resp["data"]["message_id"]
    _MESSAGE_ID_FILE.write_text(msg_id, encoding="utf-8")
    print(f"✅ 已发送卡片 message_id={msg_id}")
    print(f"   已保存到 {_MESSAGE_ID_FILE}")
    return msg_id


def update_card(message_id, card):
    """用同一条 message_id 原地更新卡片。"""
    token = get_token()
    body = {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    url = f"{BASE}/open-apis/im/v1/messages/{message_id}"
    resp = _request("PATCH", url, body, token)
    if resp.get("code") != 0:
        raise SystemExit(f"更新失败: {resp}")
    print(f"✅ 已原地更新卡片 message_id={message_id}")


def markdown_to_card(md: str, title: str = "🤖 AI 助理", template: str = "blue"):
    """任意 Markdown(含表格) → JSON 2.0 卡片。

    JSON 2.0 的 markdown 组件支持标准 markdown 语法(含表格),旧版 1.0 不支持表格。
    """
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": md},
            ]
        },
    }


def task_card(status="待办"):
    """任务状态卡片(JSON 2.0,含表格演示)。"""
    emoji = {"待办": "⏳", "进行中": "🔧", "已完成": "✅"}.get(status, "📌")
    md = (
        f"{emoji} **当前状态:{status}**\n\n"
        "| 字段 | 值 |\n"
        "|:----|:----|\n"
        "| 任务 | 飞书卡片原地更新演示 |\n"
        f"| 状态 | {status} |\n"
        "| 方式 | 同一条 message_id PATCH |\n\n"
        "状态流转:⏳ 待办 → 🔧 进行中 → ✅ 已完成\n\n"
        "> 这张卡片支持原地更新,不会发新消息"
    )
    return markdown_to_card(md, title="📋 任务状态")


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    cmd = args[0]
    if cmd == "send":
        if len(args) < 3:
            raise SystemExit("用法: send <chat_id> <card_json_file>")
        card = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        send_card(args[1], card)
    elif cmd == "update":
        if len(args) < 3:
            raise SystemExit("用法: update <message_id> <card_json_file>")
        msg_id = args[1]
        if args[2] == "--status":
            status = args[3] if len(args) > 3 else "进行中"
            card = task_card(status)
        else:
            card = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        update_card(msg_id, card)
    elif cmd == "task":
        # 快捷演示:发送任务状态卡片(默认待办)
        status = args[1] if len(args) > 1 else "待办"
        chat_id = os.environ.get("FEISHU_CHAT_ID", "")
        if not chat_id:
            raise SystemExit("缺少 chat_id,请用 FEISHU_CHAT_ID 环境变量或 send 命令")
        send_card(chat_id, task_card(status))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
