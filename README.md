# 🤖 Hermes Feishu Card Mode · Hermes 飞书卡片模式

> **EN** Upgrade [Hermes](https://github.com/tanmengxuan/Hermes)' Feishu (Lark) replies from plain text to **interactive cards that update in place** — tool-progress messages are all kept, replies become live task boards.
>
> **中文** 把 [Hermes](https://github.com/tanmengxuan/Hermes) 的飞书回复从「纯文本」升级为「**可原地更新的交互卡片**」——过程消息一条不丢，回复秒变任务看板。

**EN** Field-tested with every pitfall documented: send cards ✅ in-place update ✅ all 10/10 progress messages kept ✅ (verified 2026-08-04).
**中文** 实战踩坑产出，坑坑有记录：发送卡片 ✅ 原地更新 ✅ 过程消息 10/10 全保留 ✅（2026-08-04 实测验证）。

---

## ✨ What You Get · 升级后是什么效果

**EN** Before vs after — upgrade from default config:

**中文** 升级前（默认配置）vs 升级后对比：

| Scenario · 场景 | Before (default) · 升级前（默认配置） | After · 升级后 |
|:----|:----|:----|
| Reply format · 回复形式 | Plain text / rich text · 纯文本 / 富文本消息 | **Interactive card** · **交互卡片**（带卡片样式） |
| Tables · 表格 | Rich-text rendering · 富文本可渲染 | Markdown tables in card (JSON 2.0) · 卡片内 markdown 表格可渲染（JSON 2.0） |
| Messages per turn · 每轮对话 | One or more messages · 一条或多条消息 | **One new card** (appended within a turn) · **一张新卡片**（同轮内追加） |
| Multiple messages in one turn · 同一轮内多条消息 | `accumulate`: edits one bubble, overwritten; `separate`: scattered messages, repeated notifications · `accumulate`:编辑同一气泡被覆盖;`separate`:散成多条、多次提醒 | **All appended to one card** · **全部追加到同一张卡片** |
| Progress messages (tool calls) · 过程消息（工具调用） | Deleted at the end when `cleanup_progress=true` · `cleanup_progress=true` 时最终被删除 | **All kept, none lost** · **全部保留，一条不丢** |
| Updating a card · 更新卡片 | Not supported · 不支持 | **PATCH the same message_id in place** (no new message) · **同一条 message_id PATCH 原地更新**（不发新消息） |

> **EN** Mechanism notes (verified in Hermes source): `tool_progress_grouping` defaults to `accumulate` = "edit one bubble" (`gateway/run.py`, official comment); when `cleanup_progress=true`, progress messages are deleted one by one after the final reply (`delete_message`, best-effort).
>
> **中文** 机制说明（Hermes 源码实证）：`tool_progress_grouping` 默认 `accumulate` = 编辑同一气泡（`gateway/run.py`，官方注释 "edit one bubble"）；`cleanup_progress=true` 时过程消息在最终回复后逐条 `delete_message`（best-effort）。

## 📦 What's Inside · 仓库内容

```
hermes-feishu-card/
├── docs/guide.md              # Upgrade guide (9 steps + pitfall table + checklist) · 升级指南（9 步教程+避坑表+验证清单）
├── scripts/
│   ├── feishu_card.py         # Send / update cards manually (zero deps, stdlib only) · 手动发送/原地更新卡片工具（零依赖）
│   └── apply_feishu_card_patch.py  # Idempotent re-patch script (restore after `hermes update`) · 一键重打补丁（幂等）
├── SECURITY.md                # How to report vulnerabilities · 漏洞报告
├── CHANGELOG.md               # Change log · 变更记录
└── README.md
```

## 🚀 Quick Start · 快速开始

### EN — Way 1: One-shot patch (recommended)

**中文 — 方式一：一键打补丁（推荐）**

```bash
git clone https://github.com/cmfok/hermes-feishu-card.git
cd hermes-feishu-card

# 1. Backup + apply patches (auto-backup: adapter.py.bak-feishu-card-<timestamp>)
python scripts/apply_feishu_card_patch.py

# 2. Two key settings (root cause of lost progress messages)
hermes config set display.platforms.feishu.tool_progress_grouping "separate"
hermes config set display.platforms.feishu.cleanup_progress "false"

# 3. Restart the gateway (it cannot restart itself from inside)
hermes gateway restart
# Or send /restart to the bot in Feishu
```

### EN — Way 2: Hand this guide to your agent

**中文 — 方式二：把指南交给你的 Agent 执行**

**EN** Open [`docs/guide.md`](docs/guide.md), send sections 2–5 to your Hermes, and say:

**中文** 打开 [`docs/guide.md`](docs/guide.md)，把「二 ~ 五节」发给你的 Hermes，说：

> **EN** "Please follow this document and upgrade my Feishu replies to card mode."
>
> **中文** 「请按这份文档，把我的飞书回复升级为卡片模式。」

**EN** The agent will: backup → apply patches → set config → restart → verify. Fully reversible.

**中文** Agent 会自动：备份 → 打补丁 → 改配置 → 提示重启 → 验证，全程可回退。

### EN — Try the card tool manually

**中文 — 手动验证卡片工具**

```bash
# Send a task-status card (credentials auto-read from Hermes .env)
FEISHU_CHAT_ID="oc_xxx" python scripts/feishu_card.py task 待办

# Update the same card in place
python scripts/feishu_card.py update <message_id> --status 进行中
```

> **EN** Credentials: `FEISHU_APP_ID` / `FEISHU_APP_SECRET` are read from Hermes `.env` (`~/.hermes/.env` or `~/AppData/Local/hermes/.env`), or from environment variables.
>
> **中文** 凭据来源：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 从 Hermes 的 `.env`（`~/.hermes/.env` 或 `~/AppData/Local/hermes/.env`）读取，也支持环境变量。

## ⚠️ Notes · 注意

- **EN** `hermes update` overwrites `adapter.py` — re-run `apply_feishu_card_patch.py` to restore card mode.
  **中文** `hermes update` 会覆盖 `adapter.py`，卡片功能失效 → 重跑 `apply_feishu_card_patch.py` 即可恢复。
- **EN** The patch targets `plugins/platforms/feishu/adapter.py`. If a Hermes update changes the code, the script reports a mismatch for manual review.
  **中文** 补丁针对 Hermes 的 `plugins/platforms/feishu/adapter.py`，Hermes 升级后若补丁匹配失败，脚本会提示人工检查。

## 🐛 Known Issues · 常见问题（全部踩过）

| Symptom · 症状 | Root cause · 根因 | Fix · 解法 |
|:----|:----|:----|
| `[230001] invalid msg_type` on update · 更新卡片报 `[230001] invalid msg_type` | SDK `message.update` uses **PUT**, which rejects `interactive` · SDK 的 `message.update` 走 **PUT**，PUT 不接受 `interactive` | Use **PATCH** for card updates (built into the patch) · 卡片更新改用 **PATCH**（补丁已内置） |
| `[99992402] field validation failed` on update · 更新卡片报 `[99992402] field validation failed` | Update body included `msg_type` · 更新 body 带了 `msg_type` 字段 | PATCH body takes **`content` only** · PATCH body **只传 `content`** |
| Messages "disappear" · 消息「闪一下不见了」 | `tool_progress_grouping = accumulate` overwrites one bubble · `accumulate` 气泡被后一条覆盖 | Set to `separate` · 改成 `separate` |
| Progress messages all deleted · 过程消息最后全被清掉 | `cleanup_progress = true` | Set to `false` · 改成 `false` |
| Tables render as `\|` pipes · 表格显示成 `\|` 管道符 | JSON 1.0 card · 用了 JSON 1.0 卡片 | Use `"schema": "2.0"` · 必须 `"schema": "2.0"` |

**EN** More details: [`docs/guide.md`](docs/guide.md).
**中文** 更多细节见 [`docs/guide.md`](docs/guide.md)。

## 📄 License · 许可证

**EN** [MIT](LICENSE) © 2026 cmfok. Free to use, modify and commercialize with attribution.

**中文** [MIT](LICENSE) © 2026 cmfok。任何人可自由使用/修改/商用，仅需保留版权声明。

## 🙏 Disclaimer · 声明

**EN** This repository is an enhancement patch for Hermes' Feishu adapter, **not affiliated with the Hermes project**. Every change is fully reversible via the `adapter.py.bak-feishu-card-*` backups.

**中文** 本仓库是对 Hermes 飞书适配层的增强补丁，**与 Hermes 官方无关**。所有修改均可在 `adapter.py.bak-feishu-card-*` 备份下完整回退。
