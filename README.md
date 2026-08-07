# 🤖 Hermes Feishu Card Mode

> 把 [Hermes](https://github.com/tanmengxuan/Hermes) 的飞书回复从「纯文本」升级为「**可原地更新的交互卡片**」——过程消息一条不丢，回复秒变任务看板。
>
> Field-tested upgrade for Hermes: interactive cards (JSON 2.0), PATCH in-place updates with the same message_id, and every tool-progress message preserved — all pitfalls documented.

**实测验证（2026-08-04）**：发送卡片 ✅ 原地更新 ✅ 过程消息 10/10 全保留 ✅

---

## ✨ 升级后是什么效果 / What Changed

| 场景 | 升级前（默认配置） | 升级后 |
|:----|:----|:----|
| 回复形式 | 纯文本 / 富文本消息 | **交互卡片**（带卡片样式） |
| 表格 | 富文本可渲染 | 卡片内 markdown 表格可渲染（JSON 2.0） |
| 每轮对话 | 一条或多条消息 | **一张新卡片**（同轮内追加） |
| 同一轮内多条消息 | `accumulate`：编辑同一气泡被覆盖；`separate`：散成多条、多次提醒 | **全部追加到同一张卡片** |
| 过程消息（工具调用） | `cleanup_progress=true` 时最终被删除 | **全部保留，一条不丢** |
| 更新卡片 | 不支持 | **同一条 message_id PATCH 原地更新**（不发新消息） |

> 机制说明（Hermes 源码实证）：`tool_progress_grouping` 默认 `accumulate` = 编辑同一气泡（`gateway/run.py`，官方注释 "edit one bubble"）；`cleanup_progress=true` 时过程消息在最终回复后逐条 `delete_message`（best-effort）。

## 📦 仓库内容 / Repository

```
hermes-feishu-card/
├── docs/guide.md              # 升级指南（9 步教程+避坑表+验证清单）
├── scripts/
│   ├── feishu_card.py         # 手动发送/原地更新卡片工具（零依赖，仅标准库）
│   └── apply_feishu_card_patch.py  # 一键重打补丁（幂等，hermes update 后恢复用）
├── SECURITY.md                # 安全漏洞报告
├── CHANGELOG.md               # 变更记录
└── README.md
```

## 🚀 快速开始 / Quick Start

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

English quick start: clone → `python scripts/apply_feishu_card_patch.py` → set the two configs above → `hermes gateway restart` (or send `/restart` to the bot in Feishu). The full 9-step walkthrough with every pitfall is in [`docs/guide.md`](docs/guide.md) — you can also hand sections 2–5 of that guide to your Hermes agent and it will do everything for you.

手动验证卡片工具 / Try the card tool:

```bash
# 发送任务状态卡片（凭据从 Hermes .env 自动读取）
FEISHU_CHAT_ID="oc_xxx" python scripts/feishu_card.py task 待办

# 原地更新同一张卡片
python scripts/feishu_card.py update <message_id> --status 进行中
```

> 凭据来源：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 从 Hermes 的 `.env`（`~/.hermes/.env` 或 `~/AppData/Local/hermes/.env`）读取，也支持环境变量。Credentials are read from the Hermes `.env` file or environment variables — nothing is hardcoded.

## ⚠️ 注意 / Notes

- `hermes update` 会覆盖 `adapter.py`，卡片功能失效 → 重跑 `apply_feishu_card_patch.py` 即可恢复。The patch script is idempotent and auto-backs up `adapter.py.bak-feishu-card-<timestamp>` before each apply.
- 补丁针对 Hermes 的 `plugins/platforms/feishu/adapter.py`，Hermes 版本升级后若补丁匹配失败，脚本会提示人工检查。If a Hermes update changes the adapter code, the script reports exactly which patch didn't match for manual review.

## 🐛 常见问题 / Known Issues（全部踩过）

| 症状 | 根因 | 解法 |
|:----|:----|:----|
| 更新卡片报 `[230001] invalid msg_type` | SDK 的 `message.update` 走 **PUT**，PUT 不接受 `interactive` | 卡片更新改用 **PATCH**（补丁已内置） |
| 更新卡片报 `[99992402] field validation failed` | 更新 body 带了 `msg_type` 字段 | PATCH body **只传 `content`** |
| 消息「闪一下不见了」 | `tool_progress_grouping = accumulate`，气泡被后一条覆盖 | 改成 `separate` |
| 过程消息最后全被清掉 | `cleanup_progress = true` | 改成 `false` |
| 表格显示成 `\|` 管道符 | 用了 JSON 1.0 卡片 | 必须 `"schema": "2.0"` |

更多细节见 [`docs/guide.md`](docs/guide.md)。For non-Chinese readers, the guide includes English explanations alongside every step.

## 📄 License

[MIT](LICENSE) © 2026 cmfok。任何人可自由使用/修改/商用，仅需保留版权声明。Free to use, modify and commercialize with attribution.

## 🙏 声明 / Disclaimer

本仓库是对 Hermes 飞书适配层的增强补丁，**与 Hermes 官方无关**。所有修改均可在 `adapter.py.bak-feishu-card-*` 备份下完整回退。

This project is an enhancement patch for Hermes' Feishu adapter, not affiliated with the Hermes project. Every change is fully reversible via the `adapter.py.bak-feishu-card-*` backups.
