---
title: "Hermes 飞书卡片模式升级指南"
date: 2026-08-04
type: 教程
tags: [Hermes, 飞书, 飞书卡片, 升级指南]
status: 定稿
source: "实战验证(2026-08-04)"
---

# Hermes 飞书卡片模式升级指南

> **升级任务书**：你的 Hermes 默认用「文本/富文本」回复飞书消息。本指南教你把它升级为「**交互卡片模式**」——回复变成一张可原地更新的卡片，过程消息全部保留。
>
> 全程实战验证通过（2026-08-04）：发送卡片 ✅、原地更新 ✅、过程消息 10/10 全保留 ✅。

---

## 一、升级后是什么效果

| 场景 | 升级前 | 升级后 |
|:----|:----|:----|
| 回复形式 | 纯文本 / 富文本消息 | **交互卡片**(带卡片样式) |
| 表格 | 富文本可渲染 | 卡片内 markdown 表格可渲染(JSON 2.0) |
| 每轮对话 | 一条消息 | **一张新卡片** |
| 同一轮内多条消息 | 散成多条 / 被覆盖 | **全部追加到同一张卡片**,旧的保留 |
| 过程消息(工具调用) | 闪一下被清理 | **全部保留,一条不丢** |
| 更新卡片 | 不支持 | **同一条 message_id PATCH 原地更新**(不发新消息) |

---

## 二、前置条件

- [ ] Hermes 已安装,飞书 bot 已接入(gateway 连接正常)
- [ ] 飞书应用凭证存在:`~/.hermes/.env` 或 Hermes 配置目录下的 `.env` 里有 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- [ ] 能访问飞书开放平台 API(`open.feishu.cn`)
- [ ] 知道你的飞书会话 chat_id(形如 `oc_` 开头;当前会话的 chat_id 可以从 gateway 日志里看到)

---

## 三、升级步骤(9 步)

> 以下步骤由 Agent 执行。所有文件操作前先备份。

### 步骤 1:备份适配器

```bash
cd ~/AppData/Local/hermes/hermes-agent
cp plugins/platforms/feishu/adapter.py plugins/platforms/feishu/adapter.py.bak-card
```

### 步骤 2:修改回复格式 → JSON 2.0 卡片

找到 `_build_outbound_payload` 方法,把它的返回值从 `post`/`text` 改为 `interactive` 卡片:

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

**⚠️ 为什么必须 JSON 2.0(`"schema": "2.0"`)?**
旧版卡片(JSON 1.0)的 markdown 组件**不支持表格**。只有 2.0 支持标准 markdown(含表格、代码块、分割线)。

### 步骤 3:加回退链(interactive → post → text)

在 `send()` 方法中,发送失败时的回退逻辑改为三级回退,保证极端情况消息必达:

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

(API 响应层失败时同样处理:`interactive` 失败 → 重发 `post`;`post` 失败 → 重发 `text`。)

### 步骤 4:修改 `edit_message` → 卡片更新必须走 PATCH

**这是最容易踩的坑**:Hermes SDK 的 `message.update` 走的是 **PUT** 接口,而 PUT **不接受** `interactive`(实测报 `[230001] invalid msg_type`)。卡片原地更新**必须走 PATCH**:

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

> 注意:`PatchMessageRequestBody` **只有 `content` 字段**(实测传 `msg_type` 会报 `[230001] invalid msg_type` / `[99992402] field validation failed`)。

### 步骤 5:加「会话 → 卡片」映射(同一轮内追加)

在 `__init__` 中初始化(纯内存,不持久化):

```python
# 卡片回复模式:chat_id → 最近卡片 {message_id, content}(纯内存)
# 不持久化:重启后映射为空 → 一律发新卡,绝不去 PATCH 旧卡,
# 避免用不完整的 content 覆盖旧卡导致"信息不见"。
self._chat_card_map: Dict[str, str] = {}
```

在 `send()` 开头加追加逻辑(同一轮内多条消息 → 追加到同一张卡片,不覆盖):

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

在 `send()` 结尾记录新卡:

```python
result = self._finalize_send_result(last_response, "send failed")
if result.success and result.message_id:
    self._chat_card_map[chat_id] = {"message_id": result.message_id, "content": formatted}
return result
```

在收到用户入站消息处(如 `_dispatch_inbound_event`)清掉映射 = 翻页(用户新消息 = 新轮次 → 发新卡):

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

### 步骤 6:配置工具进度分组 = separate(关键!)

**这是"过程消息被覆盖消失"的根因**:Hermes 默认 `tool_progress_grouping = accumulate`,会把多条工具消息**编辑到同一个气泡、后一条覆盖前一条**(实测:10 条消息最后只剩 2、3)。

```bash
hermes config set display.platforms.feishu.tool_progress_grouping "separate"
```

> `separate` = 每个工具一条独立消息,互不覆盖,全部保留。

### 步骤 7:配置不清理过程消息

```bash
hermes config set display.platforms.feishu.cleanup_progress "false"
```

> `cleanup_progress: true` 时,工具过程消息会在最终回复后**被删除**,只剩结果。用户要求看到所有过程 → 必须关掉。

### 步骤 8:重启 gateway

```bash
# 在电脑上(不要在 gateway 进程内):
hermes gateway restart
# 或:在飞书里给 bot 发 /restart
```

> ⚠️ gateway 进程**不能从内部重启自己**(信号传播会杀死自身)。用计划任务/外部 shell 执行,或直接发 `/restart`。

### 步骤 9:验证

发一句话给 bot,让它连续执行几次工具调用(如 `echo "消息 1"` ~ `echo "消息 10"`),确认:

- [ ] 收到的是**卡片**(不是纯文本)
- [ ] 同一轮内 10 条消息**全部保留**,追加在同一张卡上
- [ ] 没有消息"闪一下消失"
- [ ] 下一轮对话是**新卡片**(旧卡保留)

---

## 四、配套脚本

### 脚本 1:`feishu_card.py` —— 手动发送/更新卡片

位于本仓库 `scripts/feishu_card.py`(可复制到任意目录):

```bash
# 发送卡片(返回并保存 message_id 到 last_message_id.txt)
python feishu_card.py send <chat_id> <card.json>
FEISHU_CHAT_ID="oc_xxx" python feishu_card.py task 待办    # 快捷任务状态卡

# 原地更新(同一 message_id,PATCH,飞书端原地刷新不发新消息)
python feishu_card.py update <message_id> <card.json>
python feishu_card.py update <message_id> --status 进行中
```

核心机制(三行 API,记住就够):

1. **获取 token**:`POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`,body:`{"app_id": "...", "app_secret": "..."}` → 返回 `tenant_access_token`(有效期约 2 小时,脚本自动缓存)
2. **发送卡片**:`POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id`,body:`{"receive_id": "<chat_id>", "msg_type": "interactive", "content": "<卡片JSON字符串>"}` → 记下返回的 `message_id`(`om_` 开头)
3. **更新卡片**:`PATCH https://open.feishu.cn/open-apis/im/v1/messages/{message_id}`,body:`{"content": "<新卡片JSON字符串>"}` → 同一 message_id,飞书端**原地刷新**

> ⚠️ `content` 字段是整个卡片 JSON 的**字符串**(要 `json.dumps` 转义)。

### 脚本 2:`apply_feishu_card_patch.py` —— 一键重打补丁

`hermes update` 会覆盖 `adapter.py`,卡片功能会失效。运行:

```bash
python apply_feishu_card_patch.py
```

幂等:已打补丁会跳过;每次应用前自动备份 `adapter.py.bak-feishu-card-<时间戳>`。包含全部补丁(步骤 2~5)。

---

## 五、常见问题(Pitfalls 全记录)

| 症状 | 根因 | 解法 |
|:----|:----|:----|
| 更新卡片报 `[230001] invalid msg_type` | Hermes SDK 的 `message.update` 走 **PUT**,PUT 不接受 `interactive` | 卡片更新改用 **PATCH**(步骤 4) |
| 更新卡片报 `[99992402] field validation failed` | 更新 body 带了 `msg_type` 字段 | PATCH body **只传 `content`** |
| 消息"闪一下不见了" | `tool_progress_grouping = accumulate`,气泡被后一条覆盖 | 改成 `separate`(步骤 6) |
| 过程消息最后全被清掉 | `cleanup_progress = true` | 改成 `false`(步骤 7) |
| 表格显示成 `\|` 管道符 | 用了 JSON 1.0 卡片 | 必须 `"schema": "2.0"`(步骤 2) |
| 重启后旧卡内容被改 | 映射持久化,重启后拿旧 content 覆盖旧卡 | 映射改**纯内存**,重启一律发新卡(步骤 5) |
| gateway 无法从内部重启 | Hermes 安全机制 | 外部 shell 执行或飞书发 `/restart` |
| 收到卡片但消息分块成多张 | 长回复超过 8000 字符被 truncate 分块 | 正常行为;同一轮内分块会追加到同一张卡(步骤 5 保证) |

---

## 六、验证清单(交付前自检)

- [ ] `hermes gateway status` 显示运行中
- [ ] 飞书收到卡片消息(带卡片样式)
- [ ] 卡片内 markdown 表格正常渲染
- [ ] 同一轮 10 条消息全部保留
- [ ] 用户回复后发新卡片(轮次翻页)
- [ ] `hermes update` 后重跑 `apply_feishu_card_patch.py` 能恢复

---

## 七、直接把本文档交给 Agent 执行

如果你不想手动操作,直接把本文档(二~五节)发给你的 Hermes,说:

> **「请按这份文档,把我的飞书回复升级为卡片模式。」**

Agent 会:备份 → 打补丁(步骤 2~5)→ 改配置(步骤 6~7)→ 提示重启 → 验证。全程可回退(备份文件在 `adapter.py.bak-card`)。

---

*本指南由作者在 Hermes 上实测产出(2026-08-04),所有坑都踩过一遍,照做即可。*
