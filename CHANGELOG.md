# 变更记录 / Changelog

本项目所有重要变更都记录在此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。All notable changes are documented here.

## [未发布 / Unreleased]

### 变更 / Changed
- 效果对比表按源码实证重写（`accumulate`=编辑同一气泡被覆盖；`cleanup_progress` 删除过程消息；`separate`=每工具一条独立消息）。Comparison table rewritten with source-verified mechanics.

### 安全 / Security
- `feishu_card.py`：SSRF 加固——域名白名单、解析后 IP 边界校验、重定向限制。SSRF hardening — domain whitelist, resolved-IP boundary checks, redirect restrictions.

## [v1.0.0] · 2026-08-08

### 新增 / Added
- 首发：9 步升级指南（`docs/guide.md`）、幂等重打补丁脚本（`scripts/apply_feishu_card_patch.py`，9 个补丁，自动备份）、零依赖卡片工具（`scripts/feishu_card.py`）、MIT 许可证。Initial release: 9-step upgrade guide, idempotent re-patch script (9 patches, auto-backup), zero-dependency card tool, MIT License.
