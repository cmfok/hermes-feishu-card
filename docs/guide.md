---
title: "Hermes Feishu Card Mode Upgrade Guide · Hermes 飞书卡片模式升级指南"
date: 2026-08-04
type: tutorial
tags: [Hermes, Feishu, Lark, Interactive Card, Upgrade Guide]
status: 定稿
source: "Field-tested 2026-08-04 · 实战验证(2026-08-04)"
---

# Hermes Feishu Card Mode Upgrade Guide · Hermes 飞书卡片模式升级指南

> **EN** Upgrade task book: By default Hermes replies to Feishu messages with plain text / rich text. This guide upgrades it to **interactive card mode** — replies become one card that updates in place, and every progress message is kept.
>
> **中文** 升级任务书：Hermes 默认用「文本/富文本」回复飞书消息。本指南把它升级为「**交互卡片模式**」——回复变成一张可原地更新的卡片，过程消息全部保留。
>
> **EN** Fully field-tested (2026-08-04): send card ✅ in-place update ✅ all 10/10 progress messages kept ✅.
> **中文** 全程实战验证通过（2026-08-04）：发送卡片 ✅、原地更新 ✅、过程消息 10/10 全保留 ✅。

---

## 1 · What It Looks Like After · 升级后是什么效果

| Scenario · 场景 | Before (default) · 升级前（默认配置） | After · 升级后 |
|:----|:----|:----|
| Reply format · 回复形式 | Plain text / rich text · 纯文本 / 富文本消息 | **Interactive card** · **交互卡片**(带卡片样式) |
| Tables · 表格 | Rich-text rendering · 富文本可渲染 | Markdown tables in card (JSON 2.0) · 卡片内 markdown 表格可渲染(JSON 2.0) |
| Messages per turn · 每轮对话 | One or more messages · 一条或多条消息 | **One new card** (appended within a turn) · **一张新卡片**(同轮内追加) |
| Multiple messages in one turn · 同一轮内多条消息 | `accumulate`: edits one bubble, overwritten; `separate`: scattered messages, repeated notifications · `accumulate`:编辑同一气泡被覆盖;`separate`:散成多条、多次提醒 | **All appended to one card** · **全部追加到同一张卡片** |
| Progress messages (tool calls) · 过程消息(工具调用) | Deleted at the end when `cleanup_progress=true` · `cleanup_progress=true` 时最终被删除 | **All kept, none lost** · **全部保留,一条不丢** |
| Updating a card · 更新卡片 | Not supported · 不支持 | **PATCH the same message_id in place** (no new message) · **同一条 message_id PATCH 原地更新**(不发新消息) |

## 2 · Prerequisites · 前置条件

- [ ] Hermes installed, Feishu bot connected (gateway running) · Hermes 已安装,飞书 bot 已接入(gateway 连接正常)
- [ ] Feishu app credentials exist: `FEISHU_APP_ID` / `FEISHU_APP_SECRET` in `~/.hermes/.env` or the Hermes config dir `.env` · 飞书应用凭证存在:`~/.hermes/.env` 或 Hermes 配置目录下的 `.env` 里有 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- [ ] Can reach the Feishu Open Platform API (`open.feishu.cn`) · 能访问飞书开放平台 API(`open.feishu.cn`)
- [ ] Know your Feishu chat_id (starts with `oc_`; see the gateway log for the current chat) · 知道你的飞书会话 chat_id(形如 `oc_` 开头;当前会话的 chat_id 可以从 gateway 日志里看到)

---

## 3 · The 9 Steps · 升级步骤(9 步)

> **EN** Execute with an agent or manually. **Back up before any file change.**
> **中文** 以下步骤由 Agent 执行。所有文件操作前先备份。

### Step 1 · Backup the adapter · 步骤 1:备份适配器

```bash
cd ~/AppData/Local/hermes/hermes-agent
cp plugins/platforms/feishu/adapter.py plugins/platforms/feishu/adapter.py.bak-card
```

### Step 2 · Reply format → JSON 2.0 card · 步骤 2:修改回复格式 → JSON 2.0 卡片

**EN** Find `_build_outbound_payload` and change its return from `post`/`text` to an `interactive` card:

**中文** 找到 `_build_outbound_payload` 方法,把它的返回值从 `post`/`text` 改为 `interactive` 卡片:

```python
def _build_outbound_payload(
    self, content: str, *, prefer_post: bool = False,
) -> tuple[str, str]:
    # 飞书卡片回复模式:所有出站回复 → JSON 2.0 交互卡片。
    # JSON 2.0 的 markdown 组件支持标准 markdown(含表格),旧版 1.0 不支持表格。
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    return "interactive", json.dumps(card, ensure_ascii=False)
```

> **EN ⚠️ Why JSON 2.0 (`"schema": "2.0"`)?** The old card (JSON 1.0) markdown component **does not support tables**. Only 2.0 supports standard markdown (tables, code blocks, dividers).
> **中文 ⚠️ 为什么必须 JSON 2.0(`"schema": "2.0"`)?** 旧版卡片(JSON 1.0)的 markdown 组件**不支持表格**。只有 2.0 支持标准 markdown(含表格、代码块、分割线)。

### Step 3 · Fallback chain (interactive → post → text) · 步骤 3:加回退链

**EN** In `send()`, change the failure fallback to a three-level chain so messages always get through:

**中文** 在 `send()` 方法中,发送失败时的回退逻辑改为三级回退,保证极端情况消息必达:

```python
except Exception as exc:
    if msg_type == "interactive":
        # 卡片被 API 拒绝 → 回退富文本 post
        logger.warning(f"[Feishu] interactive card rejected ({exc}); falling back to post")
        msg_type = "post"
        response = await self._feishu_send_with_retry(
            chat_id=chat_id, msg_type="post",
            payload=_build_markdown_post_payload(chunk),
            reply_to=reply_to, metadata=metadata,
        )
    elif msg_type == "post" and _POST_CONTENT_INVALID_RE.search(str(exc)):
        logger.warning("[Feishu] Invalid post payload rejected by API; falling back to plain text")
        response = await self._feishu_send_with_retry(
            chat_id=chat_id, msg_type="text",
            payload=json.dumps({"text": _strip_markdown_to_plain_text(chunk)}, ensure_ascii=False),
            reply_to=reply_to, metadata=metadata,
        )
    else:
        raise
```

**EN** (Handle the API-response layer the same way: `interactive` fails → resend as `post`; `post` fails → resend as `text`.)
**中文** (API 响应层失败时同样处理:`interactive` 失败 → 重发 `post`;`post` 失败 → 重发 `text`。)

### Step 4 · `edit_message` → card updates must use PATCH · 步骤 4:修改 `edit_message` → 卡片更新必须走 PATCH

> **EN ⚠️ Easiest trap to fall into**: Hermes SDK's `message.update` uses **PUT**, and PUT **rejects** `interactive` (field-tested: `[230001] invalid msg_type`). In-place card updates **must use PATCH**:
> **中文 ⚠️ 这是最容易踩的坑**:Hermes SDK 的 `message.update` 走的是 **PUT** 接口,而 PUT **不接受** `interactive`(实测报 `[230001] invalid msg_type`)。卡片原地更新**必须走 PATCH**:

```python
msg_type, payload = self._build_outbound_payload(content)
if msg_type == "interactive":
    # 卡片原地更新必须走 PATCH(/im/v1/messages/:id)
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
    response = await self._run_blocking(self._client.im.v1.message.update, request)
```

> **EN** Note: `PatchMessageRequestBody` has **only `content`** (passing `msg_type` fails: `[230001] invalid msg_type` / `[99992402] field validation failed`).
> **中文** 注意:`PatchMessageRequestBody` **只有 `content` 字段**(实测传 `msg_type` 会报 `[230001] invalid msg_type` / `[99992402] field validation failed`)。

### Step 5 · "Chat → card" map (append within a turn) · 步骤 5:加「会话 → 卡片」映射(同一轮内追加)

**EN** Initialize in `__init__` (in-memory only, not persisted):

**中文** 在 `__init__` 中初始化(纯内存,不持久化):

```python
# 卡片回复模式:chat_id → 最近卡片 {message_id, content}(纯内存)
# 不持久化:重启后映射为空 → 一律发新卡,绝不去 PATCH 旧卡,
# 避免用不完整的 content 覆盖旧卡导致"信息不见"。
self._chat_card_map: Dict[str, str] = {}
```

**EN** Add append logic at the start of `send()` (multiple messages in one turn → append to the same card, never overwrite):

**中文** 在 `send()` 开头加追加逻辑(同一轮内多条消息 → 追加到同一张卡片,不覆盖):

```python
formatted = self.format_message(content)
# 卡片回复模式:该会话已有卡片 → PATCH 追加更新(不覆盖),不重复发新卡
existing_card = self._chat_card_map.get(chat_id)
if existing_card and isinstance(existing_card, dict) and existing_card.get("message_id"):
    try:
        # 同一轮内多次输出 → 追加到同一张卡片(不覆盖旧内容)
        new_content = str(existing_card.get("content", "")) + "\n\n" + formatted
        result = await self.edit_message(chat_id, existing_card["message_id"], new_content)
        if result.success:
            existing_card["content"] = new_content
            return result
        logger.warning("[Feishu] Card update failed (%s); sending a new card", result.error)
    except Exception as exc:
        logger.warning("[Feishu] Card update error: %s; sending a new card", exc)
chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
```

**EN** Record the new card at the end of `send()`:

**中文** 在 `send()` 结尾记录新卡:

```python
result = self._finalize_send_result(last_response, "send failed")
if result.success and result.message_id:
    self._chat_card_map[chat_id] = {"message_id": result.message_id, "content": formatted}
return result
```

**EN** Clear the map on inbound user messages (e.g. in `_dispatch_inbound_event`) = page turn (a new user message = a new turn → new card):

**中文** 在收到用户入站消息处(如 `_dispatch_inbound_event`)清掉映射 = 翻页(用户新消息 = 新轮次 → 发新卡):

```python
async def _dispatch_inbound_event(self, event: MessageEvent) -> None:
    # 卡片回复模式:用户新消息 = 新轮次 → 下一轮回复发新卡片
    try:
        if event.source and event.source.chat_id:
            self._chat_card_map.pop(str(event.source.chat_id), None)
    except Exception:
        pass
    ...
```

### Step 6 · Tool progress grouping = separate (key!) · 步骤 6:配置工具进度分组 = separate(关键!)

> **EN This is the root cause of "progress messages disappearing"**: Hermes defaults `tool_progress_grouping = accumulate`, which **edits multiple tool messages into one bubble — each overwrites the previous** (field-tested: 10 messages ended with only 2–3 left).
> **中文 这是"过程消息被覆盖消失"的根因**:Hermes 默认 `tool_progress_grouping = accumulate`,会把多条工具消息**编辑到同一个气泡、后一条覆盖前一条**(实测:10 条消息最后只剩 2、3)。

```bash
hermes config set display.platforms.feishu.tool_progress_grouping "separate"
```

> **EN** `separate` = one independent message per tool — nothing overwrites anything, everything is kept.
> **中文** `separate` = 每个工具一条独立消息,互不覆盖,全部保留。

### Step 7 · Don't clean up progress messages · 步骤 7:配置不清理过程消息

```bash
hermes config set display.platforms.feishu.cleanup_progress "false"
```

> **EN** When `cleanup_progress: true`, tool progress messages are **deleted after the final reply** — only the result remains. If you want to see all the process, you must turn it off.
> **中文** `cleanup_progress: true` 时,工具过程消息会在最终回复后**被删除**,只剩结果。用户要求看到所有过程 → 必须关掉。

### Step 8 · Restart the gateway · 步骤 8:重启 gateway

```bash
# Run on the computer (not inside the gateway process):
hermes gateway restart
# Or: send /restart to the bot in Feishu
```

> **EN ⚠️ The gateway cannot restart itself from inside** (signal propagation kills itself). Use a scheduled task / external shell, or send `/restart`.
> **中文 ⚠️ gateway 进程**不能从内部重启自己**(信号传播会杀死自身)。用计划任务/外部 shell 执行,或直接发 `/restart`。

### Step 9 · Verify · 步骤 9:验证

**EN** Send a message that triggers several tool calls (e.g. `echo "message 1"` … `echo "message 10"`), and confirm:

**中文** 发一句话给 bot,让它连续执行几次工具调用(如 `echo "消息 1"` ~ `echo "消息 10"`),确认:

- [ ] You receive a **card** (not plain text) · 收到的是**卡片**(不是纯文本)
- [ ] All 10 messages in one turn are **kept and appended to one card** · 同一轮内 10 条消息**全部保留**,追加在同一张卡上
- [ ] No message "flashes and disappears" · 没有消息"闪一下消失"
- [ ] The next turn is a **new card** (old card kept) · 下一轮对话是**新卡片**(旧卡保留)

---

## 4 · Companion Scripts · 配套脚本

### Script 1 · `feishu_card.py` — send / update cards manually · 脚本 1:手动发送/更新卡片

**EN** Located at `scripts/feishu_card.py` in this repo (copyable anywhere):

**中文** 位于本仓库 `scripts/feishu_card.py`(可复制到任意目录):

```bash
# Send a card (returns message_id, saved to last_message_id.txt)
python feishu_card.py send <chat_id> <card.json>
FEISHU_CHAT_ID="oc_xxx" python feishu_card.py task 待办    # quick task-status card

# Update in place (same message_id, PATCH — Feishu refreshes in place, no new message)
python feishu_card.py update <message_id> <card.json>
python feishu_card.py update <message_id> --status 进行中
```

**EN** Core mechanism (three API calls, that's all you need to remember):

**中文** 核心机制(三行 API,记住就够):

1. **Get token**: `POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`, body: `{"app_id": "...", "app_secret": "..."}` → returns `tenant_access_token` (~2h validity, auto-cached) · **获取 token**:同上,返回 `tenant_access_token`(有效期约 2 小时,脚本自动缓存)
2. **Send card**: `POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id`, body: `{"receive_id": "<chat_id>", "msg_type": "interactive", "content": "<card JSON string>"}` → note the returned `message_id` (starts with `om_`) · **发送卡片**:同上,记下返回的 `message_id`(`om_` 开头)
3. **Update card**: `PATCH https://open.feishu.cn/open-apis/im/v1/messages/{message_id}`, body: `{"content": "<new card JSON string>"}` → same message_id, Feishu **refreshes in place** · **更新卡片**:同上 → 同一 message_id,飞书端**原地刷新**

> **EN ⚠️** `content` must be the whole card JSON as a **string** (`json.dumps`-escaped).
> **中文 ⚠️** `content` 字段是整个卡片 JSON 的**字符串**(要 `json.dumps` 转义)。

### Script 2 · `apply_feishu_card_patch.py` — one-shot re-patch · 脚本 2:一键重打补丁

**EN** `hermes update` overwrites `adapter.py` and card mode stops working. Run:

**中文** `hermes update` 会覆盖 `adapter.py`,卡片功能会失效。运行:

```bash
python apply_feishu_card_patch.py
```

**EN** Idempotent: skips if already patched; auto-backs up `adapter.py.bak-feishu-card-<timestamp>` before each apply. Contains all patches (steps 2–5).

**中文** 幂等:已打补丁会跳过;每次应用前自动备份 `adapter.py.bak-feishu-card-<时间戳>`。包含全部补丁(步骤 2~5)。

---

## 5 · Pitfalls · 常见问题(全记录)

| Symptom · 症状 | Root cause · 根因 | Fix · 解法 |
|:----|:----|:----|
| `[230001] invalid msg_type` on update · 更新卡片报 `[230001] invalid msg_type` | SDK `message.update` uses **PUT**; PUT rejects `interactive` · Hermes SDK 的 `message.update` 走 **PUT**,PUT 不接受 `interactive` | Use **PATCH** for card updates (step 4) · 卡片更新改用 **PATCH**(步骤 4) |
| `[99992402] field validation failed` on update · 更新卡片报 `[99992402] field validation failed` | Update body included `msg_type` · 更新 body 带了 `msg_type` 字段 | PATCH body takes **`content` only** · PATCH body **只传 `content`** |
| Messages "flash and disappear" · 消息"闪一下不见了" | `tool_progress_grouping = accumulate` — bubbles overwrite each other · `tool_progress_grouping = accumulate`,气泡被后一条覆盖 | Set to `separate` (step 6) · 改成 `separate`(步骤 6) |
| Progress messages all deleted · 过程消息最后全被清掉 | `cleanup_progress = true` | Set to `false` (step 7) · 改成 `false`(步骤 7) |
| Tables render as `\|` pipes · 表格显示成 `\|` 管道符 | JSON 1.0 card · 用了 JSON 1.0 卡片 | Must use `"schema": "2.0"` (step 2) · 必须 `"schema": "2.0"`(步骤 2) |
| Old card content changed after restart · 重启后旧卡内容被改 | Persisted map — restart used stale content to overwrite the old card · 映射持久化,重启后拿旧 content 覆盖旧卡 | Keep map **in-memory only**; always send a new card after restart (step 5) · 映射改**纯内存**,重启一律发新卡(步骤 5) |
| Gateway can't restart from inside · gateway 无法从内部重启 | Hermes safety mechanism · Hermes 安全机制 | External shell, or `/restart` in Feishu · 外部 shell 执行或飞书发 `/restart` |
| Card received but split into multiple cards · 收到卡片但消息分块成多张 | Long reply > 8000 chars gets truncated into chunks · 长回复超过 8000 字符被 truncate 分块 | Normal; chunks in one turn append to the same card (step 5) · 正常行为;同一轮内分块会追加到同一张卡(步骤 5 保证) |

---

## 6 · Verification Checklist · 验证清单(交付前自检)

- [ ] `hermes gateway status` shows running · `hermes gateway status` 显示运行中
- [ ] Card message received in Feishu (with card styling) · 飞书收到卡片消息(带卡片样式)
- [ ] Markdown table renders inside the card · 卡片内 markdown 表格正常渲染
- [ ] All 10 messages in one turn are kept · 同一轮 10 条消息全部保留
- [ ] User reply starts a new card (page turn) · 用户回复后发新卡片(轮次翻页)
- [ ] After `hermes update`, re-running `apply_feishu_card_patch.py` restores card mode · `hermes update` 后重跑 `apply_feishu_card_patch.py` 能恢复

---

## 7 · Hand This Document to Your Agent · 直接把本文档交给 Agent 执行

**EN** If you'd rather not do it manually, send sections 2–5 of this document to your Hermes and say:

**中文** 如果你不想手动操作,直接把本文档(二~五节)发给你的 Hermes,说:

> **EN** "Please follow this document and upgrade my Feishu replies to card mode."
> **中文** 「请按这份文档,把我的飞书回复升级为卡片模式。」

**EN** The agent will: backup → apply patches (steps 2–5) → set config (steps 6–7) → prompt restart → verify. Fully reversible (backup at `adapter.py.bak-card`).

**中文** Agent 会:备份 → 打补丁(步骤 2~5)→ 改配置(步骤 6~7)→ 提示重启 → 验证。全程可回退(备份文件在 `adapter.py.bak-card`)。

---

*EN Field-tested on Hermes by the author (2026-08-04); every pitfall here was hit for real. · 中文 本指南由作者在 Hermes 上实测产出(2026-08-04),所有坑都踩过一遍,照做即可。*
