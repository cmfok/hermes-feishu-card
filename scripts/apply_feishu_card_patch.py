#!/usr/bin/env python3
"""飞书卡片回复补丁 — 重打脚本(幂等)。

用途:hermes update 后 adapter.py 会被官方版本覆盖,飞书卡片回复功能会失效。
运行本脚本即可重新应用补丁:
    python apply_feishu_card_patch.py

功能:
1. 所有出站回复 → JSON 2.0 交互卡片(支持表格)
2. 回退链 interactive → post → text
3. 会话内"单卡片持续更新":chat_id → message_id 映射,每轮回复 PATCH 原地更新
4. 容量保护:同一张卡追加 ≥30 次 → 主动开新卡(2026-08-26,防卡片触达 30KB 上限断旧信息)

检测:adapter.py 的 _build_outbound_payload 是否含 "schema": "2.0"(已打补丁特征)。
备份:每次应用前自动生成 adapter.py.bak-feishu-card-<时间戳>。
"""
import shutil
import sys
import time
from pathlib import Path

HERMES_HOME = Path.home() / "AppData/Local/hermes" / "hermes-agent"
DEFAULT_ADAPTER = HERMES_HOME / "plugins" / "platforms" / "feishu" / "adapter.py"

MARKER = '"schema": "2.0"'
MARKER2 = "_chat_card_map"

# ---- 补丁 1:出站 payload → JSON 2.0 卡片 ----
OLD_PAYLOAD = '''        if prefer_post or _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        text_payload = {"text": content}
        return "text", json.dumps(text_payload, ensure_ascii=False)'''

NEW_PAYLOAD = '''        # 飞书卡片回复模式(用户要求):所有出站回复 → JSON 2.0 交互卡片。
        # JSON 2.0 的 markdown 组件支持标准 markdown(含表格),旧版 1.0 不支持。
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": [{"tag": "markdown", "content": content}]},
        }
        return "interactive", json.dumps(card, ensure_ascii=False)'''

# ---- 补丁 2:send() except 回退链 ----
OLD_FALLBACK = '''                except Exception as exc:
                    if msg_type != "post" or not _POST_CONTENT_INVALID_RE.search(str(exc)):
                        raise
                    logger.warning("[Feishu] Invalid post payload rejected by API; falling back to plain text")
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id,
                        msg_type="text",
                        payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
                        reply_to=reply_to,
                        metadata=metadata,
                    )'''

NEW_FALLBACK = '''                except Exception as exc:
                    if msg_type == "interactive":
                        # 卡片被 API 拒绝 → 回退富文本 post
                        logger.warning(f"[Feishu] interactive card rejected ({exc}); falling back to post")
                        msg_type = "post"
                        response = await self._feishu_send_with_retry(
                            chat_id=chat_id,
                            msg_type="post",
                            payload=_build_markdown_post_payload(chunk),
                            reply_to=reply_to,
                            metadata=metadata,
                        )
                    elif msg_type == "post" and _POST_CONTENT_INVALID_RE.search(str(exc)):
                        logger.warning("[Feishu] Invalid post payload rejected by API; falling back to plain text")
                        response = await self._feishu_send_with_retry(
                            chat_id=chat_id,
                            msg_type="text",
                            payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
                            reply_to=reply_to,
                            metadata=metadata,
                        )
                    else:
                        raise'''

# ---- 补丁 3:response 失败回退链 ----
OLD_RESP = '''                if (
                    msg_type == "post"
                    and not self._response_succeeded(response)
                    and _POST_CONTENT_INVALID_RE.search(str(getattr(response, "msg", "") or ""))
                ):'''

NEW_RESP = '''                if (
                    msg_type == "interactive"
                    and not self._response_succeeded(response)
                ):
                    logger.warning("[Feishu] Interactive card rejected by API response; falling back to post")
                    msg_type = "post"
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id,
                        msg_type="post",
                        payload=_build_markdown_post_payload(chunk),
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                if (
                    msg_type == "post"
                    and not self._response_succeeded(response)
                    and _POST_CONTENT_INVALID_RE.search(str(getattr(response, "msg", "") or ""))
                ):'''

# ---- 补丁 4:__init__ 单卡片映射初始化 ----
OLD_INIT = '''        self._pending_processing_reactions: "OrderedDict[str, str]" = OrderedDict()
        self._load_seen_message_ids()'''

NEW_INIT = '''        self._pending_processing_reactions: "OrderedDict[str, str]" = OrderedDict()
        # 卡片回复模式(用户要求):chat_id → 最近卡片 message_id。
        # 每次回复优先 PATCH 原地更新该卡片,不重复发新消息。
        self._chat_card_map: Dict[str, str] = {}
        self._chat_card_map_path = get_hermes_home() / "feishu_card_map.json"
        self._load_chat_card_map()
        self._load_seen_message_ids()'''

# ---- 补丁 5:send() 开头 — 已有卡片则原地更新 ----
OLD_SEND_HEAD = '''        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)'''

NEW_SEND_HEAD = '''        formatted = self.format_message(content)
        # 卡片回复模式:该会话已有卡片 → PATCH 追加更新(不覆盖),不重复发新卡
        existing_card = self._chat_card_map.get(chat_id)
        if existing_card and isinstance(existing_card, dict) and existing_card.get("message_id"):
            if existing_card.get("append_count", 0) >= 30:
                # 容量保护:同一张卡追加超过 30 次 → 主动发新卡,
                # 避免卡片触达飞书 30KB 上限后 PATCH 失败、旧内容丢失。
                existing_card = None
            else:
                try:
                    # 同一轮内多次输出 → 追加到同一张卡片(不覆盖旧内容)
                    new_content = str(existing_card.get("content", "")) + "\\n\\n" + formatted
                    result = await self.edit_message(chat_id, existing_card["message_id"], new_content)
                    if result.success:
                        existing_card["content"] = new_content
                        existing_card["append_count"] = existing_card.get("append_count", 0) + 1
                        self._save_chat_card_map()
                        return result
                    logger.warning("[Feishu] Card update failed (%s); sending a new card", result.error)
                except Exception as exc:
                    logger.warning("[Feishu] Card update error: %s; sending a new card", exc)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)'''

# ---- 补丁 6:send() 结尾 — 记录新卡 message_id ----
OLD_SEND_TAIL = '''            return self._finalize_send_result(last_response, "send failed")'''

NEW_SEND_TAIL = '''            result = self._finalize_send_result(last_response, "send failed")
            if result.success and result.message_id:
                self._chat_card_map[chat_id] = {"message_id": result.message_id, "content": formatted, "append_count": 1}
                self._save_chat_card_map()
            return result'''

# ---- 补丁 7:映射读写方法 ----
OLD_METHODS = '''    def _load_seen_message_ids(self) -> None:'''

NEW_METHODS = '''    def _load_chat_card_map(self) -> None:
        """加载 chat_id → 最近卡片 message_id 映射(卡片原地更新用)。"""
        try:
            if self._chat_card_map_path.exists():
                data = json.loads(self._chat_card_map_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._chat_card_map = {
                        k: v for k, v in data.items()
                        if isinstance(v, dict) and isinstance(v.get("message_id"), str)
                    }
        except Exception:
            self._chat_card_map = {}

    def _save_chat_card_map(self) -> None:
        try:
            self._chat_card_map_path.write_text(
                json.dumps(self._chat_card_map, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_seen_message_ids(self) -> None:'''


# ---- 补丁 8:更新消息 body — interactive 不传 msg_type(否则 [230001] invalid msg_type) ----
OLD_UPDATE_BODY = '''    @staticmethod
    def _build_update_message_body(*, msg_type: str, content: str) -> Any:
        if "UpdateMessageRequestBody" in globals():
            return (
                UpdateMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
        return SimpleNamespace(msg_type=msg_type, content=content)'''

NEW_UPDATE_BODY = '''    @staticmethod
    def _build_update_message_body(*, msg_type: str, content: str) -> Any:
        if msg_type == "interactive":
            # 飞书更新卡片消息(PATCH /im/v1/messages/:id):body 只接受 content,
            # 传 msg_type=interactive 会报 [230001] invalid msg_type(实测)。
            if "UpdateMessageRequestBody" in globals():
                return UpdateMessageRequestBody.builder().content(content).build()
            return SimpleNamespace(content=content)
        if "UpdateMessageRequestBody" in globals():
            return (
                UpdateMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
        return SimpleNamespace(msg_type=msg_type, content=content)'''


# ---- 补丁 9:edit_message — interactive 更新走 PATCH(卡片原地更新) ----
OLD_EDIT = '''            msg_type, payload = self._build_outbound_payload(content)
            body = self._build_update_message_body(msg_type=msg_type, content=payload)
            request = self._build_update_message_request(message_id=message_id, request_body=body)
            response = await self._run_blocking(self._client.im.v1.message.update, request)'''

NEW_EDIT = '''            msg_type, payload = self._build_outbound_payload(content)
            if msg_type == "interactive":
                # 卡片原地更新必须走 PATCH(/im/v1/messages/:id)——
                # PUT(update)不接受 interactive,实测报 [230001] invalid msg_type。
                from lark_oapi.api.im.v1.model.patch_message_request import PatchMessageRequest
                from lark_oapi.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody
                patch_body = PatchMessageRequestBody.builder().content(payload).build()
                patch_request = (
                    PatchMessageRequest.builder()
                    .message_id(message_id)
                    .request_body(patch_body)
                    .build()
                )
                response = await self._run_blocking(self._client.im.v1.message.patch, patch_request)
            else:
                body = self._build_update_message_body(msg_type=msg_type, content=payload)
                request = self._build_update_message_request(message_id=message_id, request_body=body)
                response = await self._run_blocking(self._client.im.v1.message.update, request)'''

PATCHES = [
    ("payload", OLD_PAYLOAD, NEW_PAYLOAD, MARKER),
    ("fallback", OLD_FALLBACK, NEW_FALLBACK, MARKER),
    ("response", OLD_RESP, NEW_RESP, MARKER),
    ("init", OLD_INIT, NEW_INIT, MARKER2),
    ("send_head", OLD_SEND_HEAD, NEW_SEND_HEAD, MARKER2),
    ("send_tail", OLD_SEND_TAIL, NEW_SEND_TAIL, MARKER2),
    ("methods", OLD_METHODS, NEW_METHODS, MARKER2),
    ("update_body", OLD_UPDATE_BODY, NEW_UPDATE_BODY, MARKER),
    ("edit_patch", OLD_EDIT, NEW_EDIT, MARKER),
]


def apply(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    already = MARKER in src and MARKER2 in src
    if already:
        print(f"✅ {path} 已打过补丁,跳过")
        return True
    backup = path.with_suffix(f".py.bak-feishu-card-{int(time.time())}")
    shutil.copy2(path, backup)
    print(f"💾 备份 -> {backup}")
    ok = True
    for name, old, new, marker in PATCHES:
        if old in src:
            src = src.replace(old, new, 1)
            print(f"  ✓ 补丁 {name} 已应用")
        else:
            ok = False
            print(f"  ⚠ 补丁 {name} 未匹配(官方代码可能变动,需人工检查)")
    if MARKER in src and MARKER2 in src:
        path.write_text(src, encoding="utf-8")
        print(f"✅ 补丁完成:{path}")
        return True
    print(f"❌ 补丁未完全生效,请人工检查 {path}")
    return ok


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ADAPTER
    if not target.exists():
        print(f"❌ 找不到适配器:{target}")
        sys.exit(1)
    sys.exit(0 if apply(target) else 1)
