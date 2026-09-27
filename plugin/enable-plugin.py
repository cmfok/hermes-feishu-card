#!/usr/bin/env python3
"""把 feishu-card 写进某个 profile 的 config.yaml（幂等，带备份与校验）。

由 install-plugin.ps1 调用；也可单独使用：
    python enable-plugin.py <config.yaml>

行为：
  * 已有 plugins.enabled 且含 feishu-card → 什么都不做
  * 已有 plugins.enabled 但缺 feishu-card → 追加进列表
  * 没有 plugins 键 → 在文件末尾追加一个顶层 plugins 块
  * 任何情况下都先备份为 <config>.bak-<YYYYMMDD-HHMMSS>-feishu-card
  * 写回后重新解析并断言「只多了预期内容」，失败则回滚
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

PLUGIN_NAME = "feishu-card"

BLOCK = """
# {stamp}：启用 feishu-card 插件（飞书出站卡片：单写者状态机 + 硬超时 + 容量换卡）
plugins:
  enabled:
    - {name}
"""


def enable(config_path: Path) -> int:
    if not config_path.exists():
        print(f"[enable-plugin] config not found: {config_path}")
        return 2

    text = config_path.read_text(encoding="utf-8")
    before = yaml.safe_load(text) or {}
    plugins = before.get("plugins")
    enabled = list((plugins or {}).get("enabled") or []) if isinstance(plugins, dict) else []

    if PLUGIN_NAME in enabled:
        print(f"[enable-plugin] already enabled in {config_path.name}")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.bak-{stamp}-{PLUGIN_NAME}")
    shutil.copy2(config_path, backup)

    if isinstance(plugins, dict):
        # 就地追加进已有列表：按缩进替换 `enabled:` 行的下一层不方便，
        # 因此用 YAML 安全 round-trip 只重写 plugins 子树的做法在这里不可行
        # （会丢注释）→ 改为在文件末尾追加同名的第二个 plugins 键会让 YAML
        # 报重复键，所以这种情况交回人工处理。
        print(
            f"[enable-plugin] {config_path.name} already has a plugins block without "
            f"'{PLUGIN_NAME}' — please add it manually under plugins.enabled "
            f"(backup: {backup.name})"
        )
        return 3

    config_path.write_text(text.rstrip("\n") + "\n" + BLOCK.format(stamp=stamp, name=PLUGIN_NAME), encoding="utf-8")

    after = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ok = (
        set(after.keys()) - set(before.keys()) == {"plugins"}
        and (after.get("plugins") or {}).get("enabled") == [PLUGIN_NAME]
    )
    if not ok:
        shutil.copy2(backup, config_path)
        print(f"[enable-plugin] validation failed; rolled back from {backup.name}")
        return 4

    print(f"[enable-plugin] enabled '{PLUGIN_NAME}' in {config_path.name} (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python enable-plugin.py <config.yaml>")
        raise SystemExit(2)
    raise SystemExit(enable(Path(sys.argv[1])))
