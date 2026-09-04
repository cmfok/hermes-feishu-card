# 变更记录 / Changelog

本项目所有重要变更都记录在此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。All notable changes are documented here.

## [v1.1.0] · 2026-09-04

### 新增 / Added
- **完整定制版 adapter**（`custom/feishu-adapter-full-20260826.py`，6251 行）：生产验证的整体替换方案——默认 JSON 2.0 卡片出站、会话内单卡片持续 PATCH 更新、密封定时器（防跨轮误 PATCH）、工具消息块排版、30 次追加容量保护、interactive→post→text 回退链。附带 `custom/README.md`（对比表 + 安装/恢复步骤 + SHA256）。
- 背景：Hermes 0.21.0 升级后官方 adapter 默认 text 出站且 PATCH 交互卡片的 `_build_update_message_body` 会带 `msg_type=interactive`（实测 `[230001]`），补丁脚本在 0.21.0 上有 1 处锚点需手补——完整版规避了全部版本漂移问题。

Full custom adapter added under `custom/` — the production drop-in replacement tested through Hermes 0.21 upgrade, with per-chat single-card PATCH updates, seal timers, tool-block rendering and capacity guard.

### 新增 / Added
- **容量保护**:同一张卡追加 ≥30 次 → 主动开新卡(旧卡保留),防止卡片触达飞书 interactive 请求体 30KB / JSON 2.0 元素 200 上限后 PATCH 被拒、旧内容丢失。指南新增「步骤 10」,补丁脚本补丁 5/6 内置容量保护。Capacity guard: cards start fresh after 30 appends — prevents PATCH rejections at Feishu's 30KB payload / 200-element caps from losing old content. Guide step 10 + built into patch 5/6.

### 变更 / Changed
- 效果对比表按源码实证重写（`accumulate`=编辑同一气泡被覆盖；`cleanup_progress` 删除过程消息；`separate`=每工具一条独立消息）。Comparison table rewritten with source-verified mechanics.

### 安全 / Security
- `feishu_card.py`：SSRF 加固——域名白名单、解析后 IP 边界校验、重定向限制。SSRF hardening — domain whitelist, resolved-IP boundary checks, redirect restrictions.

## [v1.0.0] · 2026-08-08

### 新增 / Added
- 首发：9 步升级指南（`docs/guide.md`）、幂等重打补丁脚本（`scripts/apply_feishu_card_patch.py`，9 个补丁，自动备份）、零依赖卡片工具（`scripts/feishu_card.py`）、MIT 许可证。Initial release: 9-step upgrade guide, idempotent re-patch script (9 patches, auto-backup), zero-dependency card tool, MIT License.
