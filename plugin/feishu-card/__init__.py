"""feishu-card：把 Hermes 的飞书出站升级成"可原地更新的交互卡片"。

以同名平台（``feishu``）注册，覆盖内置适配器；出站走卡片会话状态机
（单写者 + 硬超时 + 失败换卡），入站与协议细节全部继承官方实现。

模块入口保持轻量：不在这里 import lark / 官方 adapter（冷启约 8 秒），
重导入推迟到 adapter 真正被创建时（见 card_plugin._build_card_adapter）。
"""

from .card_plugin import register

__all__ = ["register"]
