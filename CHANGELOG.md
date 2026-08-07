# Changelog · 变更记录

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), versioning follows [SemVer](https://semver.org/lang/zh-CN/).

本项目的所有重要变更都记录在此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased] · 未发布

### Added · 新增
- **EN** Bilingual docs (EN + 中文): README and upgrade guide are now fully bilingual.
- **中文** 文档中英双文：README 与升级指南已全文中英对照。

### Changed · 变更
- **EN** Comparison table rewritten with source-verified mechanics (`accumulate` = edit one bubble; `cleanup_progress` deletes progress messages; `separate` = one message per tool).
- **中文** 效果对比表按源码实证重写（`accumulate`=编辑同一气泡被覆盖；`cleanup_progress` 删除过程消息；`separate`=每工具一条独立消息）。

### Security · 安全
- **EN** `feishu_card.py`: SSRF hardening — domain whitelist, resolved-IP boundary checks, redirect restrictions.
- **中文** `feishu_card.py`：SSRF 加固——域名白名单、解析后 IP 边界校验、重定向限制。

## [v1.0.0] · 2026-08-08

### Added · 新增
- **EN** Initial release: 9-step upgrade guide (`docs/guide.md`), idempotent re-patch script (`scripts/apply_feishu_card_patch.py`, 9 patches, auto-backup), zero-dependency card tool (`scripts/feishu_card.py`), MIT License.
- **中文** 首发：9 步升级指南（`docs/guide.md`）、幂等重打补丁脚本（`scripts/apply_feishu_card_patch.py`，9 个补丁，自动备份）、零依赖卡片工具（`scripts/feishu_card.py`）、MIT 许可证。
