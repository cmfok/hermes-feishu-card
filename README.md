# 🤖 Hermes 飞书卡片模式

> 把 [Hermes](https://github.com/tanmengxuan/Hermes) 的飞书回复从「纯文本」升级为「**可原地更新的交互卡片**」——过程消息一条不丢，回复秒变任务看板。

**实战踩坑产出**：发送卡片 ✅ 原地更新 ✅ 过程消息 10/10 全保留 ✅（2026-08-04 实测验证）

---

## ✨ 升级后是什么效果

| 场景 | 升级前 | 升级后 |
|:----|:----|:----|
| 回复形式 | 纯文本 / 富文本消息 | **交互卡片**（带卡片样式） |
| 表格 | 富文本可渲染 | 卡片内 markdown 表格可渲染（JSON 2.0） |
| 每轮对话 | 一条消息 | **一张新卡片** |
| 同一轮内多条消息 | 散成多条 / 被覆盖 | **全部追加到同一张卡片** |
| 过程消息（工具调用） | 闪一下被清理 | **全部保留，一条不丢** |
| 更新卡片 | 不支持 | **同一条 message_id PATCH 原地更新**（不发新消息） |

## 📦 仓库内容

```
hermes-feishu-card/
├── docs/guide.md              # 升级指南（9 步教程 + 避坑表 + 验证清单）
├── scripts/
│   ├── feishu_card.py         # 手动发送 / 原地更新卡片工具（零依赖，仅标准库）
│   └── apply_feishu_card_patch.py  # 一键重打补丁（幂等，hermes update 后恢复用）
└── README.md
```

## 🚀 快速开始

### 方式一：一键打补丁（推荐）

```bash
git clone https://github.com/cmfok/hermes-feishu-card.git
cd hermes-feishu-card

# 1. 备份 + 打补丁（自动备份 adapter.py.bak-feishu-card-<时间戳>）
python scripts/apply_feishu_card_patch.py

# 2. 两个关键配置（过程消息不丢失的根因）
hermes config set display.platforms.feishu.tool_progress_grouping "separate"
hermes config set display.platforms.feishu.cleanup_progress "false"

# 3. 重启 gateway（不能在 gateway 进程内重启自己）
hermes gateway restart
# 或在飞书里给 bot 发 /restart
```

### 方式二：把指南交给你的 Agent 执行

打开 [`docs/guide.md`](docs/guide.md)，把「二 ~ 五节」发给你的 Hermes，说：

> **「请按这份文档，把我的飞书回复升级为卡片模式。」**

Agent 会自动：备份 → 打补丁 → 改配置 → 提示重启 → 验证，全程可回退。

### 手动验证卡片工具

```bash
# 发送任务状态卡片（凭据从 Hermes .env 自动读取）
FEISHU_CHAT_ID="oc_xxx" python scripts/feishu_card.py task 待办

# 原地更新同一张卡片
python scripts/feishu_card.py update <message_id> --status 进行中
```

> 凭据来源：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 从 Hermes 的 `.env`（`~/.hermes/.env` 或 `~/AppData/Local/hermes/.env`）读取，也支持环境变量。

## ⚠️ 注意

- `hermes update` 会覆盖 `adapter.py`，卡片功能失效 → 重跑 `apply_feishu_card_patch.py` 即可恢复
- 补丁针对 Hermes 的 `plugins/platforms/feishu/adapter.py`，Hermes 版本升级后若补丁匹配失败，脚本会提示人工检查

## 🐛 常见问题（全部踩过）

| 症状 | 根因 | 解法 |
|:----|:----|:----|
| 更新卡片报 `[230001] invalid msg_type` | SDK 的 `message.update` 走 **PUT**，PUT 不接受 `interactive` | 卡片更新改用 **PATCH**（补丁已内置） |
| 更新卡片报 `[99992402] field validation failed` | 更新 body 带了 `msg_type` 字段 | PATCH body **只传 `content`** |
| 消息「闪一下不见了」 | `tool_progress_grouping = accumulate`，气泡被覆盖 | 改成 `separate` |
| 过程消息最后全被清掉 | `cleanup_progress = true` | 改成 `false` |
| 表格显示成 `\|` 管道符 | 用了 JSON 1.0 卡片 | 必须 `"schema": "2.0"` |

更多细节见 [`docs/guide.md`](docs/guide.md)。

## 📄 License

[MIT](LICENSE) © 2026 cmfok

## 🙏 说明

本仓库是对 Hermes 飞书适配层的增强补丁，与 Hermes 官方无关。所有修改均可在 `adapter.py.bak-feishu-card-*` 备份下完整回退。
