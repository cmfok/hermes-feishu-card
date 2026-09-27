"""插件注册层 —— 保持零重导入。

插件发现的**每个进程**都会执行 ``register()``，而 lark SDK 冷启约 8 秒，
所以本模块刻意不 import lark、也不 import 官方 adapter：

  * ``check_fn`` 用被动探针（``is_available`` / ``find_spec``），不安装任何东西；
  * 官方 adapter（重模块）只在 ``adapter_factory`` 真的被调用时才导入；
  * 其余注册参数（setup / yaml 桥 / 跨进程发送…）都做成"惰性透传"，
    调用时才去拿官方实现，保证行为与官方完全一致。

覆盖机制：``ctx.register_platform(name="feishu", ...)`` 与内置 feishu 平台**同名**，
Hermes 的 platform_registry 是"后写胜出"，且会丢弃内置的 deferred loader
（``gateway/platform_registry.py`` register() 注释：*this lets plugins override
built-in adapters if desired*）。注册失败/插件未启用时，内置适配器照常工作。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_UPSTREAM_MODULE_NAME = "plugins.platforms.feishu.adapter"


def load_upstream_module() -> Any:
    """返回内置 feishu 平台插件模块（``plugins.platforms.feishu.adapter``）。

    优先 ``sys.modules`` / 正常 import；失败时用已加载的 ``gateway`` 包定位
    hermes 仓库根，把它加进 ``sys.path`` 后重试（应对 cwd 不在仓库根的场景）。
    """
    module = sys.modules.get(_UPSTREAM_MODULE_NAME)
    if module is not None:
        return module
    try:
        return importlib.import_module(_UPSTREAM_MODULE_NAME)
    except Exception as exc:
        log.debug("[feishu-card] direct import of upstream failed: %s", exc)
    import gateway  # 必然已加载：插件由 gateway/hermes 进程加载

    root = Path(gateway.__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


# --------------------------------------------------------------- 惰性透传

def _deps_present() -> bool:
    """被动探针：lark-oapi 现在装没装（绝不安任何东西）。"""
    try:
        from tools.lazy_deps import is_available

        return bool(is_available("platform.feishu"))
    except Exception:
        return importlib.util.find_spec("lark_oapi") is not None


def _ensure_deps() -> bool:
    """主动安装器：仅在被动探针为 False 时由 create_adapter() 调用。"""
    try:
        return bool(load_upstream_module().check_feishu_requirements())
    except Exception as exc:
        log.warning("[feishu-card] dependency install failed: %s", exc)
        return False


def _is_connected(config: Any) -> bool:
    """与官方语义一致：配了 app_id 即视为已连接。"""
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("app_id"))


def _setup_fn() -> None:
    load_upstream_module().interactive_setup()


def _apply_yaml_config_fn(yaml_cfg: dict, feishu_cfg: dict) -> Any:
    return load_upstream_module()._apply_yaml_config(yaml_cfg, feishu_cfg)


async def _standalone_send(*args: Any, **kwargs: Any) -> Any:
    """跨进程发送（cron 的 deliver=feishu / hermes send）→ 官方实现。"""
    return await load_upstream_module()._standalone_send(*args, **kwargs)


def _build_card_adapter(config: Any) -> Any:
    """工厂：这里才导入重模块（lark + 官方 adapter）。"""
    from .card_adapter import CardFeishuAdapter

    return CardFeishuAdapter(config)


# ----------------------------------------------------------- 版本指纹 / 抢名告警
# 2026-09-18 事故：备份目录 `feishu-card.bak-*` 留在插件扫描目录里、plugin.yaml 仍是
# `name: feishu-card`，而加载器 scan_directory() 不过滤备份目录、同名冲突"后扫描者胜"
# （plugins.py: winners = {key: m for m in manifests}），字母序下 `.bak-` 排在后面 →
# **旧备份代码覆盖新版**，导致 v2.1.5–2.1.8 的修复从未真正运行。两条防线：

def _build_stamp() -> str:
    """本插件运行文件的内容指纹 → 日志里一眼看出线上跑的是哪一版、哪个目录。"""
    import hashlib

    base = Path(__file__).resolve().parent
    digest = hashlib.md5()
    names = sorted(p.name for p in base.glob("*.py"))
    if (base / "plugin.yaml").exists():
        names.append("plugin.yaml")
    for name in names:
        try:
            digest.update(name.encode("utf-8"))
            digest.update((base / name).read_bytes())
        except OSError:
            return "stamp-failed"
    return digest.hexdigest()[:8]


def _shadowed_siblings() -> list:
    """同级目录里是否存在**同名** plugin.yaml（备份忘改名就会抢走加载权）。"""
    try:
        base = Path(__file__).resolve().parent
        mine = None
        try:
            for line in (base / "plugin.yaml").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("name:"):
                    mine = line.split(":", 1)[1].strip()
                    break
        except OSError:
            return []
        if not mine:
            return []
        found = []
        for sib in base.parent.iterdir():
            if sib.name == base.name or not sib.is_dir():
                continue
            manifest = sib / "plugin.yaml"
            if not manifest.exists():
                continue
            try:
                for line in manifest.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("name:") and line.split(":", 1)[1].strip() == mine:
                        found.append(sib.name)
                        break
            except OSError:
                continue
        return found
    except OSError:
        return []


def register(ctx: Any) -> None:
    """插件入口：以同名平台覆盖内置 feishu 适配器。"""
    ctx.register_platform(
        name="feishu",
        label="Feishu / Lark",
        adapter_factory=_build_card_adapter,
        check_fn=_deps_present,
        ensure_deps_fn=_ensure_deps,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        install_hint="Run `hermes setup` to install Feishu support.",
        setup_fn=_setup_fn,
        apply_yaml_config_fn=_apply_yaml_config_fn,
        allowed_users_env="FEISHU_ALLOWED_USERS",
        allow_all_env="FEISHU_ALLOW_ALL_USERS",
        cron_deliver_env_var="FEISHU_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=8000,
        emoji="🪽",
        allow_update_command=True,
    )
    log.info("[feishu-card] platform 'feishu' registered with card-mode adapter | stamp=%s dir=%s",
             _build_stamp(), Path(__file__).resolve().parent)
    shadows = _shadowed_siblings()
    if shadows:
        log.warning(
            "[feishu-card] 同级目录存在**同名** plugin.yaml，会按『后扫描者胜』抢走加载权，"
            "请把备份目录移出插件扫描目录: %s", ", ".join(shadows))
