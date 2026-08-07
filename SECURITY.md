# 安全策略

## 支持的版本

仅维护最新发布版本。安全修复会发布到当前 `main` 分支及对应 tag。

| 版本 | 支持 |
|:----|:----|
| 最新版（v1.0.0+） | ✅ |

## 报告漏洞

发现安全问题**请不要开公开 Issue**，请私下报告：

- GitHub Security Advisory（推荐）：https://github.com/cmfok/hermes-feishu-card/security/advisories/new

我们会在 7 天内回复评估结论。确认后尽快发布修复，并在修复发布后公开披露。

## 范围

本项目零运行时依赖（仅 Python 标准库）。凭证一律从环境变量 / `.env` 读取，脚本不会存储任何密钥。

## CI 中的安全检查

每次推送都会通过 GitHub Actions 运行密钥扫描（API key / token 模式）和 Python 语法检查。
