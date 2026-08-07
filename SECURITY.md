# 安全策略 / Security Policy

## 支持的版本 / Supported Versions

仅维护最新发布版本。安全修复会发布到当前 `main` 分支及对应 tag。Only the latest release is supported; fixes ship on `main` and tagged releases.

| 版本 / Version | 支持 / Supported |
|:----|:----|
| 最新版（v1.0.0+） | ✅ |

## 报告漏洞 / Reporting a Vulnerability

发现安全问题**请不要开公开 Issue**，请私下报告。Please **do not open a public issue** — report privately:

- GitHub Security Advisory（推荐 / preferred）：https://github.com/cmfok/hermes-feishu-card/security/advisories/new

我们会在 7 天内回复评估结论，确认后尽快发布修复。We aim to respond within 7 days and ship fixes as soon as possible once confirmed.

## 范围 / Scope

本项目零运行时依赖（仅 Python 标准库）。凭证一律从环境变量 / `.env` 读取，脚本不会存储任何密钥。This project has zero runtime dependencies (Python stdlib only); credentials are read from environment variables / `.env` and never stored.

## CI 中的安全检查 / Security Checks in CI

每次推送都会通过 GitHub Actions 运行密钥扫描（API key / token 模式）和 Python 语法检查。Every push runs a secret scan and a Python syntax check via GitHub Actions.
