# Custom：完整版飞书适配器 / Full Feishu Adapter

> 本目录存放**完整定制版** `plugins/platforms/feishu/adapter.py`（6251 行，2026-08-26 容量保护版）。
> This directory holds the **full custom adapter** — the production-tested replacement for Hermes' Feishu platform adapter.

## 与补丁脚本的关系 / Relationship to the patch script

| | `scripts/apply_feishu_card_patch.py` | `custom/feishu-adapter-full-20260826.py` |
|:--|:--|:--|
| 形式 | 对官方 adapter 打 9 个锚点补丁（轻量） | 整体替换 adapter.py（完整定制版） |
| 默认回复 | 交互卡片（JSON 2.0） | 交互卡片（JSON 2.0） |
| 会话内单卡片持续更新 | ✅ `_chat_card_map` | ✅ `_chat_card_map` + PATCH 锁 |
| 密封定时器（防跨轮误 PATCH 旧卡） | ❌ | ✅ |
| 工具消息块渲染（代码框缩短等） | ❌ | ✅ |
| 容量保护（≥30 次追加开新卡） | ✅ | ✅（更完整） |
| 回退链 interactive→post→text | ✅ | ✅ |
| 官方版本兼容 | 需锚点匹配（0.21 后有 1 处需手补） | 基于 0.19–0.21 fork 实测兼容 |
| 升级后恢复 | 重跑脚本 | 整体替换 + 重启 gateway |

> 结论：追求完整体验（长对话卡片不被撑爆、工具过程排版好）用**完整版**；只想快速恢复基础卡片用**补丁脚本**。

## 安装 / Install（完整版）

```bash
# 1. 备份现役文件
cp ~/AppData/Local/hermes/hermes-agent/plugins/platforms/feishu/adapter.py{,.bak}

# 2. 整体替换为完整版
cp custom/feishu-adapter-full-20260826.py \
   ~/AppData/Local/hermes/hermes-agent/plugins/platforms/feishu/adapter.py

# 3. 语法检查 + 重启 gateway（当前 profile）
python -m py_compile <adapter.py 路径>
hermes gateway restart
# 多 profile 时：python -m hermes_cli.main --profile <名> gateway restart
```

## 校验 / Checksum

```
SHA256(feishu-adapter-full-20260826.py) = eae045fd468f3c017b53...
```

> `hermes update` 会覆盖 adapter.py —— 更新后重跑上面第 2–3 步即可恢复。
> Hermes 更新会覆盖此文件；恢复方法见上。与官方 Hermes 项目无关，MIT 许可。
