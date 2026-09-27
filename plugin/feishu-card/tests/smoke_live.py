"""feishu-card 真机烟测 —— 用真实飞书 API 验证卡片链路（会向指定会话发消息）。

用法（用 Hermes venv 的 python）：
    python tests/smoke_live.py                     # 默认读 feishu2 profile 的 .env
    python tests/smoke_live.py --chat oc_xxx
    python tests/smoke_live.py --no-send           # 只做无网络的连接/超时自检

验证内容：
  1) 创建卡片（第 1 条正文）
  2) 第 2 条正文追加进**同一张卡**（PATCH，而不是新消息）
  3) 工具进度消息 → 折叠块
  4) 超长多块正文 → 合并进同一张卡（容量够时）；容量超限则换卡
  5) 定时密封 → 状态行变「✅ 完成」
  6) ``_with_deadline`` 超时包装真的会抛 CardCallTimeout（用 sleep 假任务验证）

凭据只从 .env / 环境变量读，绝不打印。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import os
import sys
import time
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
PARENT = PKG_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

_pkg = "feishu_card"
if _pkg not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _pkg, PKG_DIR / "__init__.py", submodule_search_locations=[str(PKG_DIR)]
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg] = _mod
    _spec.loader.exec_module(_mod)

R = importlib.import_module("feishu_card.card_render")

DEFAULT_ENV = Path.home() / "AppData" / "Local" / "hermes" / "profiles" / "feishu2" / ".env"
DEFAULT_CHAT = "oc_ac7f5fa7803da6690bde504fcbf844d7"

FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (" " + detail if detail else ""))
    if not cond:
        FAILED.append(name)


def load_env(path: Path) -> None:
    """把 .env 读进进程环境（**文件优先于已存在的环境变量**）。

    否则进程里残留的 FEISHU_APP_ID（例如用户级环境变量属于另一个 app）
    会盖掉 profile 的凭据，实测表现为 [230002] Bot/User can NOT be out of the chat。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def build_adapter(upstream, card_adapter):
    from gateway.config import PlatformConfig

    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("FEISHU_APP_ID / FEISHU_APP_SECRET not found in env file")

    pconfig = PlatformConfig()
    extra = dict(getattr(pconfig, "extra", {}) or {})
    extra.update({"app_id": app_id, "app_secret": app_secret})
    pconfig.extra = extra

    adapter = card_adapter.CardFeishuAdapter(pconfig)
    # 只建 API 客户端，不碰 WS（避免与在跑的网关抢连接）
    domain_name = getattr(adapter, "_domain_name", "feishu")
    domain = getattr(upstream, "LARK_DOMAIN" if domain_name == "lark" else "FEISHU_DOMAIN")
    adapter._client = adapter._build_lark_client(domain)
    return adapter


async def offline_interaction_reset(adapter, upstream) -> None:
    """离线验证：授权/追问卡发出后，卡片会话收口 → 下一段内容开新卡。

    全程 monkeypatch 掉卡片 I/O（不联网、不打扰飞书）。
    """
    print("[offline] interaction (approval) resets the card session")
    SendResult = importlib.import_module("gateway.platforms.base").SendResult
    recorded = {"created": [], "patched": []}
    # 保存原始实现，测完必须还原（否则后面的真机测试会打到假卡片上）
    orig_create = adapter.create_card
    orig_patch = adapter.patch_card

    async def fake_create(chat_id, payload):
        recorded["created"].append(chat_id)
        return SendResult(success=True, message_id=f"om_fake_{len(recorded['created'])}")

    async def fake_patch(message_id, payload):
        recorded["patched"].append((message_id, payload))
        return SendResult(success=True, message_id=message_id)

    adapter.create_card = fake_create
    adapter.patch_card = fake_patch

    chat = "oc_offline_test"
    session = adapter._session(chat)
    await session.handle_text("第一段")
    first_id = session.message_id
    check("session has a card before approval", bool(first_id), str(first_id))

    adapter._start_new_card_after_interaction((chat,), {}, "offline-test")
    check("session dropped after approval card", adapter._card_sessions.get(chat) is None)
    await asyncio.sleep(0.1)  # 封口是 fire-and-forget 后台任务，等它跑完再断言
    sealed = [p for p in recorded["patched"] if p[0] == first_id and "\u2705" in p[1]]
    check("old card sealed (✅) on interaction", len(sealed) >= 1, f"patched={len(recorded['patched'])}")

    new_session = adapter._session(chat)
    await new_session.handle_text("授权后的新内容")
    check("continuation starts a NEW card", bool(new_session.message_id) and new_session.message_id != first_id,
          f"{new_session.message_id} vs {first_id}")
    check("two cards created in total", len(recorded["created"]) == 2, str(recorded["created"]))
    new_session.cancel_pending()
    adapter._card_sessions.pop(chat, None)

    # --- 互动卡发送超时兜底（2026-09-14 加固）---
    # 注意：这里仍用上面装的假卡片 I/O，真实现留到函数末尾还原
    print("[offline] interaction send timeout guard")
    base = upstream.FeishuAdapter
    orig_approval = base.send_exec_approval

    async def slow_approval(*a, **k):
        await asyncio.sleep(5)
        return SendResult(success=True, message_id="om_slow")

    chat2 = "oc_offline_timeout"
    sess2 = adapter._session(chat2)
    await sess2.handle_text("授权前的进度")
    check("card exists before approval send", bool(sess2.message_id))

    base.send_exec_approval = slow_approval
    saved_timeout = adapter.INTERACTION_SEND_TIMEOUT
    adapter.INTERACTION_SEND_TIMEOUT = 0.2
    started = time.monotonic()
    res = await adapter.send_exec_approval(chat2, "rm -rf /tmp/guard-test", "sess-key")
    elapsed = time.monotonic() - started
    check("approval send returns FAILURE on timeout",
          res.success is False and "timed out" in (res.error or ""), str(res.error))
    check("deadline actually enforced (<1s)", elapsed < 1.0, f"{elapsed:.2f}s")
    check("session closed even on timeout", adapter._card_sessions.get(chat2) is None)
    adapter.INTERACTION_SEND_TIMEOUT = saved_timeout

    async def fast_approval(*a, **k):
        return SendResult(success=True, message_id="om_fast")

    base.send_exec_approval = fast_approval
    sess3 = adapter._session("oc_offline_ok")
    await sess3.handle_text("授权前的进度")
    ok_res = await adapter.send_exec_approval("oc_offline_ok", "ls", "sess-key-2")
    check("approval send success path ok", ok_res.success is True, str(ok_res.error))
    check("session closed on success too", adapter._card_sessions.get("oc_offline_ok") is None)
    base.send_exec_approval = orig_approval
    # 还原真实卡片 I/O（之后的真机测试用）
    adapter.create_card = orig_create
    adapter.patch_card = orig_patch


async def offline_routing_checks(adapter, upstream) -> None:
    """离线验证：① 合成事件（internal）不换卡 ② 工具进度经 edit_message 也进折叠面板。"""
    print("[offline] routing: internal events keep the card; tool edits fold")
    SendResult = importlib.import_module("gateway.platforms.base").SendResult
    recorded = {"created": [], "patched": []}
    orig_create, orig_patch = adapter.create_card, adapter.patch_card

    async def fake_create(chat_id, payload):
        recorded["created"].append(chat_id)
        return SendResult(success=True, message_id=f"om_r{len(recorded['created'])}")

    async def fake_patch(message_id, payload):
        recorded["patched"].append((message_id, payload))
        return SendResult(success=True, message_id=message_id)

    adapter.create_card, adapter.patch_card = fake_create, fake_patch

    class _Src:
        chat_id = "oc_route"

    class _Ev:
        def __init__(self, internal):
            self.internal = internal
            self.source = _Src()

    should, chat = adapter._should_reset_card_for(_Ev(True))
    check("internal (delegation result) event does NOT reset the card", should is False, str((should, chat)))
    should2, chat2 = adapter._should_reset_card_for(_Ev(False))
    check("real user message DOES reset (new card per turn)", should2 is True and chat2 == "oc_route", str((should2, chat2)))

    # 工具进度走 edit_message（tool_progress_grouping=accumulate 的 profile）→ 仍要折叠
    sess = adapter._session("oc_route")
    await sess.handle_text("先给个结论")
    res = await adapter.edit_message("oc_route", sess.message_id, "💻 $ ls -la")
    blocks = sess.snapshot().blocks
    check("tool edit accepted", bool(res.success))
    check("tool edit folded into a tools block", any(b["type"] == "tools" for b in blocks), str([b["type"] for b in blocks]))
    check("earlier content kept after tool edit",
          any(b["type"] == "message" and b["text"] == "先给个结论" for b in blocks))

    await adapter.edit_message("oc_route", sess.message_id, "⏳ 已持续工作 3 分钟")
    blocks2 = sess.snapshot().blocks
    check("heartbeat edit is still a notice", any(b["type"] == "notice" for b in blocks2), str([b["type"] for b in blocks2]))
    check("tools panel survives the heartbeat", any(b["type"] == "tools" for b in blocks2))

    # --- 连续 terminal 丢表头的裸代码块：靠上下文升级为 TOOL ---
    print("[offline] bare fenced block (upstream drops the repeated header)")
    bare = "```\ncd /p/FU && python fetch.py --all\n```"
    sess3 = adapter._session("oc_bare")
    await sess3.handle_tool("💻 terminal\n```\nls\n```")           # 进入工具上下文
    upgraded = adapter._refine_kind(R.classify(bare), bare, sess3)
    check("bare fence INSIDE tool context → TOOL (folds)", upgraded is R.MsgKind.TOOL, upgraded.value)
    await sess3.handle_text("一段正文，工具阶段结束")               # 退出工具上下文
    kept = adapter._refine_kind(R.classify(bare), bare, sess3)
    check("bare fence OUTSIDE tool context → TEXT (stays visible)", kept is R.MsgKind.TEXT, kept.value)
    sess3.cancel_pending()
    adapter._card_sessions.pop("oc_bare", None)

    # --- edit 幂等：同一段文本不该被追加两次 ---
    print("[offline] edit idempotency (same text must not be appended twice)")
    sess4 = adapter._session("oc_idem")
    await sess4.handle_text("结论：一切正常")
    before = len([b for b in sess4.snapshot().blocks if b["type"] == "message"])
    await adapter.edit_message("oc_idem", sess4.message_id, "结论：一切正常", finalize=True)
    after = len([b for b in sess4.snapshot().blocks if b["type"] == "message"])
    check("identical edit does not duplicate the text", after == before, f"{before} → {after}")
    # 超集文本 → 替换最后一块（不新增）
    await adapter.edit_message("oc_idem", sess4.message_id, "结论：一切正常\n补充：已核对三项")
    msgs = [b["text"] for b in sess4.snapshot().blocks if b["type"] == "message"]
    check("superset edit REPLACES the last block", len(msgs) == before and "补充" in msgs[-1], str(msgs))
    sess4.cancel_pending()
    adapter._card_sessions.pop("oc_idem", None)

    # --- 中途叙述的过滤口径：保留中文阶段汇报，丢弃英文内心推理 ---
    print("[offline] interim narration filter (keep Chinese, drop English reasoning)")
    CA = importlib.import_module("feishu_card.card_adapter")
    INT = {"_interim_send": True}
    keep = [
        "**① 已落盘**：`football-kb/03-预测记录/x.md` ✅ 跑完了，结果比我预想的清楚。",
        "【会话已恢复 ✅】网关重启完成，之前的进度都在，未丢数据。",
        "你好 👋 现在是 **2026-09-15（周二）15:02**。",
        "Now a gap check across all 32 leagues vs ESPN（禁用时发现旧）",   # 中英混排 → 保守保留
    ]
    drop = [
        "Mystery solved — a sibling cron job (0 10 schedule) ran the same pipeline concurrently.",
        "Now writing the run summary and index log row.",
        "Something else wrote to the table concurrently (16711 lines now vs 16631 before).",
        "Operation interrupted.",
    ]
    for text in keep:
        check(f"kept: {text[:34]}…", not CA._skip_as_interim_reasoning(text, INT))
    for text in drop:
        check(f"dropped: {text[:34]}…", CA._skip_as_interim_reasoning(text, INT))
    # 最终回复没有 _interim_send 标记 → 即便是纯英文也必须显示
    check(
        "final answer is NEVER filtered (no marker)",
        not CA._skip_as_interim_reasoning("This is the final English answer.", None)
        and not CA._skip_as_interim_reasoning("This is the final English answer.", {}),
    )

    sess.cancel_pending()
    adapter._card_sessions.pop("oc_route", None)
    adapter.create_card, adapter.patch_card = orig_create, orig_patch


async def offline_feature_interaction(adapter, upstream) -> None:
    """**功能交互测试**：把"中途汇报 × 工具进度 × 折叠"混在**同一轮**里跑。

    为什么必须单独有这一类测试（2026-09-15 两次真机回归的教训）：
    R1（显示中途汇报）与 R2（工具折叠）**各自单测都是绿的，一起用却互相撞坏** ——
    改完 R1 只跑了各自的测试，实机立刻出问题：
      · 汇报 `🚨 **查出严重问题…**` 首字符不在工具 emoji 黑名单里 → 被判成 TOOL
        → 扔进折叠面板 → 用户"发给我的东西直接丢掉了，不显示了"；
      · 汇报之后到来的（连续 terminal 丢表头的）裸代码块 → "上一块不是工具"
        → 被判成正文 → "该折叠的工具调用又没有折叠了"。
    两个症状同源：**用文本形状猜分类**。修法是改用上游的可靠标记 `_interim_send`。
    本测试就是防再犯：一轮里同时有带标记的汇报、带表头的工具、裸块工具、收尾正文，
    逐条断言"汇报必须可见、工具必须折叠、内容不许丢"。
    """
    print("[offline] 功能交互：中途汇报 × 工具折叠（R1×R2 不得互相破坏）")
    SendResult = importlib.import_module("gateway.platforms.base").SendResult
    orig_create, orig_patch = adapter.create_card, adapter.patch_card
    created = []

    async def fake_create(chat_id, payload):
        created.append(payload)
        return SendResult(success=True, message_id=f"om_ix_{len(created)}")

    async def fake_patch(message_id, payload):
        created.append(payload)
        return SendResult(success=True, message_id=message_id)

    adapter.create_card = fake_create
    adapter.patch_card = fake_patch
    chat = "oc_ix_test"
    adapter._card_sessions.pop(chat, None)

    INT = {"_interim_send": True}   # 上游给"中途汇报"打的标记（gateway/run.py:457）
    NARRATION_1 = "我先查一下知识库，看有没有现成的。"
    NARRATION_2 = "🚨 **查出严重问题，我先认：** 我说「已落盘」的三个文件，根本不存在。"
    NARRATION_3 = "查到了，现在整理要点。"
    TOOL_HDR = "💻 terminal\n```\ncd /p/Qoder/work && ls -la\n```"
    TOOL_BARE = "```\ncd /p/FU && python stats.py --all\n```"
    TOOL_READ = "⚙️ Reading P:\\Qoder\\work\\Ai100\\obsidian\\_hots.md"
    FINAL = "整理好了 ✅"

    try:
        # 真实顺序走一遍（全部经 adapter.send / edit_message，分类逻辑才真的被执行）
        await adapter.send(chat, NARRATION_1, metadata=INT)
        await adapter.send(chat, TOOL_HDR)
        await adapter.send(chat, TOOL_BARE)                    # ← R2 回归点（汇报之后的裸块）
        await adapter.send(chat, NARRATION_2, metadata=INT)     # ← R1 回归点（🚨 开头）
        await adapter.send(chat, TOOL_READ)
        mid = adapter._card_sessions[chat].message_id
        await adapter.edit_message(chat, mid, TOOL_BARE)        # ← edit 路径的裸块
        await adapter.send(chat, NARRATION_3, metadata=INT)
        await adapter.send(chat, FINAL)

        # ⑤ 状态必须真实：只有上游的**最终回复**（notify=True）才让卡片显示「✅ 完成」。
        #    CM 2026-09-16 反馈「一直说回复中其实不做事 / 说已完成又冒内容」——
        #    旧实现靠 8 秒静默猜完成（agent 单次 API 调用常 >8s → 提前误报；
        #    长任务心跳 180s 又把它钉在"回复中"）。
        #    注意：本段必须在 `finally` 恢复真实传输**之前**跑，否则会打到真飞书 API。
        print("[offline] 状态真实：notify=True 才封口")
        sess_now = adapter._card_sessions.get(chat)
        check("尚未封口（正文到了但没收到真信号）",
              bool(sess_now and sess_now.snapshot().status != R.STATUS_SEALED),
              sess_now.snapshot().status if sess_now else "-")
        await adapter.send(chat, "任务完成，结论如上 ✅", metadata={"notify": True})
        check("收到最终回复(notify=True) → 卡片封口",
              bool(sess_now and sess_now.snapshot().status == R.STATUS_SEALED),
              sess_now.snapshot().status if sess_now else "-")
        last_payload = created[-1] if created else ""
        check("末行显示 已完成", "完成" in last_payload, last_payload[-120:])

    finally:
        adapter.create_card, adapter.patch_card = orig_create, orig_patch

    sess = adapter._card_sessions.get(chat)
    check("会话已建立", sess is not None)
    if sess is None:
        return
    elements = R.build_elements(sess.snapshot().blocks, R.STATUS_RUNNING)
    visible = "\n".join(str(e.get("content") or "") for e in elements if e.get("tag") == "markdown")
    panel = "\n".join(
        str(c.get("content") or "")
        for e in elements if e.get("tag") == "collapsible_panel"
        for c in e.get("elements", [])
    )

    # ① 汇报必须可见（在正文里、且不在折叠面板里）
    for label, text in [("汇报①", NARRATION_1), ("汇报②(🚨开头)", NARRATION_2), ("汇报③", NARRATION_3)]:
        key = text[:12]
        check(f"{label} 在可见正文里", key in visible, key)
        check(f"{label} 不在折叠面板里", key not in panel, key)
    check("最终回复可见", FINAL in visible)

    # ② 工具必须折叠（在面板里、且不在正文里）
    for label, text in [("带表头工具", "cd /p/Qoder/work && ls -la"),
                        ("裸块工具", "cd /p/FU && python stats.py --all"),
                        ("Reading 工具", "Reading P:")]:
        check(f"{label} 在折叠面板里", text in panel, text)
        check(f"{label} 不在正文里", text not in visible, text)

    # ③ 内容不丢：每条输入的关键片段都能在卡片里找到
    for label, key in [("汇报①", "我先查一下知识库"), ("汇报②", "根本不存在"),
                       ("汇报③", "现在整理要点"), ("工具", "python stats.py"),
                       ("回复", "整理好了")]:
        check(f"内容未丢：{label}", (key in visible) or (key in panel), key)

    # ④ 正文与面板不许重叠
    overlap = [s for s in (NARRATION_1[:12], NARRATION_2[:12], FINAL) if s in panel]
    check("无重叠（正文内容不得同时出现在面板里）", not overlap, str(overlap))

    adapter._card_sessions.pop(chat, None)


async def offline_stuck_heal(adapter) -> None:
    """方案 A 回归锁：卡死时**只**把会话行的 end_reason 标上（上游据此路由自愈）。

    CM 2026-09-16 拍板方案 A。上游 `_is_session_ended_in_db` 只认 `end_reason` 非空，
    所以写入面必须只有这一列；且**只在该聊天确认卡死时**触发，正常长回合不许误伤。
    """
    print("[offline] 卡死自愈（方案 A）：只标 end_reason，且只对卡死触发")
    import importlib, sqlite3, tempfile, time as _t
    CA = adapter.__class__.__module__
    mod = importlib.import_module(CA)

    # ① 临时 DB：sessions 表 + 一条未结束的会话行
    tmp = Path(tempfile.mkdtemp()) / "state.db"
    con = sqlite3.connect(str(tmp))
    con.execute("CREATE TABLE sessions (id TEXT, session_key TEXT, end_reason TEXT, last_activity_at REAL)")
    con.execute("INSERT INTO sessions VALUES ('s1','agent:main:feishu:dm:oc_heal',NULL,1000)")
    con.execute("INSERT INTO sessions VALUES ('s2','agent:main:feishu:dm:oc_other',NULL,1000)")
    con.commit(); con.close()

    orig = mod._candidate_state_dbs
    mod._candidate_state_dbs = lambda: [str(tmp)]
    try:
        ok = mod._mark_session_ended("oc_heal", "test_reason")
        check("标记成功", ok is True)
        con = sqlite3.connect(str(tmp))
        got = con.execute("SELECT end_reason FROM sessions WHERE id='s1'").fetchone()[0]
        other = con.execute("SELECT end_reason FROM sessions WHERE id='s2'").fetchone()[0]
        untouched = con.execute("SELECT session_key FROM sessions WHERE id='s1'").fetchone()[0]
        con.close()
        check("end_reason 已写入", got == "test_reason", str(got))
        check("**只写 end_reason**（session_key 未被动）",
              untouched == "agent:main:feishu:dm:oc_heal", str(untouched))
        check("别的会话行没被牵连", other is None, str(other))
        check("已结束的行不重复标", mod._mark_session_ended("oc_heal", "again") is False)
        check("不存在的会话返回 False", mod._mark_session_ended("oc_nobody", "x") is False)
    finally:
        mod._candidate_state_dbs = orig

    # ② 触发判据：静默超过阈值 → 触发；刚有活动 → 不触发
    calls = []
    orig_mark = mod._mark_session_ended
    mod._mark_session_ended = lambda chat, reason: (calls.append((chat, reason)), True)[1]
    try:
        chat = "oc_judge"
        mod._LAST_INBOUND.pop(chat, None); mod._LAST_ACTIVITY.pop(chat, None)
        mod._STUCK_HEALED.pop(chat, None)
        adapter._heal_if_previous_turn_stuck(chat)          # 第一次：只记时间
        check("首次收信不触发", not calls, str(calls))
        mod._LAST_INBOUND[chat] = _t.monotonic() - (mod.STUCK_HEAL_AFTER_SEC + 10)
        mod._LAST_ACTIVITY.pop(chat, None)                   # 期间毫无活动
        adapter._heal_if_previous_turn_stuck(chat)
        check("静默无活动 → 触发自愈", len(calls) == 1, str(calls))
        # 变体：期间有活动（如心跳/正常长回合）→ 不触发
        calls.clear(); mod._STUCK_HEALED.pop(chat, None)
        mod._LAST_INBOUND[chat] = _t.monotonic() - (mod.STUCK_HEAL_AFTER_SEC + 10)
        mod._LAST_ACTIVITY[chat] = _t.monotonic() - 5        # 刚有活动
        adapter._heal_if_previous_turn_stuck(chat)
        check("有活动（正常长回合）→ 不触发", not calls, str(calls))
    finally:
        mod._mark_session_ended = orig_mark


async def run_smoke(chat_id: str, *, send: bool) -> None:
    upstream = importlib.import_module("plugins.platforms.feishu.adapter")
    card_adapter = importlib.import_module("feishu_card.card_adapter")

    loader = getattr(upstream, "_load_lark_oapi", None)
    if loader is not None:
        ok = await asyncio.to_thread(loader)
        if not ok:
            raise SystemExit("lark SDK is not available")
    adapter = build_adapter(upstream, card_adapter)

    print("[timeout] deadline wrapper")
    saved = adapter.CARD_CALL_TIMEOUT
    adapter.CARD_CALL_TIMEOUT = 0.05
    try:
        await adapter._with_deadline(asyncio.sleep(0.4), what="self-test")
        check("timeout raises", False, "no exception")
    except card_adapter.CardCallTimeout:
        check("timeout raises CardCallTimeout", True)
    except Exception as exc:  # noqa: BLE001
        check("timeout raises CardCallTimeout", False, f"got {type(exc).__name__}")
    finally:
        adapter.CARD_CALL_TIMEOUT = saved

    try:
        await adapter._with_deadline(asyncio.sleep(0.01), what="self-test")
        check("fast call passes deadline", True)
    except Exception as exc:  # noqa: BLE001
        check("fast call passes deadline", False, str(exc))

    await offline_interaction_reset(adapter, upstream)
    await offline_routing_checks(adapter, upstream)
    await offline_feature_interaction(adapter, upstream)
    await offline_stuck_heal(adapter)

    if not send:
        return

    print(f"[live] chat={chat_id} (cards will be posted)")
    results = []
    first = await adapter.send(chat_id, "🧪 feishu-card 自检 1/5：创建卡片（本条是第一块）")
    results.append(("create", first))
    check("create ok", bool(first.success), str(first.error or ""))
    card_id = first.message_id

    second = await adapter.send(chat_id, "自检 2/5：这条应当**追加进同一张卡**（PATCH），而不是新消息。")
    results.append(("append", second))
    check("append ok", bool(second.success), str(second.error or ""))
    check("append kept SAME card", second.message_id == card_id, f"{second.message_id} != {card_id}")

    tool = await adapter.send(chat_id, "💻 $ echo feishu-card-smoke\n```\necho feishu-card-smoke\n```")
    results.append(("tool", tool))
    check("tool block ok", bool(tool.success), str(tool.error or ""))

    long_paras = "\n\n".join(
        [f"自检 4/5：多块长正文第 {i} 段。" + "x" * 300 for i in range(1, 9)]
        + ["| a | b |\n|---|---|\n| 1 | 2 |", "| c | d |\n|---|---|\n| 3 | 4 |", "| e | f |\n|---|---|\n| 5 | 6 |"]
    )
    big = await adapter.send(chat_id, long_paras)
    results.append(("long", big))
    check("long multi-chunk ok", bool(big.success), str(big.error or ""))

    session = adapter._card_sessions.get(chat_id)
    check("session exists", session is not None)
    outcome = await session.seal(reason="smoke") if session else None
    check("seal ok", bool(outcome and outcome.ok), str(getattr(outcome, "error", "")))
    check("final status sealed", bool(session and session.snapshot().status == R.STATUS_SEALED))

    snap = session.snapshot() if session else None
    print("[live] summary:")
    print(f"        card_id       = {snap.message_id if snap else '-'}")
    print(f"        seq           = {snap.seq if snap else '-'}")
    print(f"        append_count  = {snap.append_count if snap else '-'}")
    print(f"        blocks        = {len(snap.blocks) if snap else '-'}")
    print(f"        rotations     = {snap.rotations if snap else '-'}")
    print(f"        failures      = {snap.failures if snap else '-'}")
    check("no rotations needed (capacity ok)", bool(snap and snap.rotations == 0))
    check("no failures", bool(snap and snap.failures == 0))
    print(f"        messages      = {[ (n, getattr(r,'message_id',None), getattr(r,'success',None)) for n, r in results ]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--chat", default=os.environ.get("HERMES_SMOKE_CHAT") or DEFAULT_CHAT)
    parser.add_argument("--no-send", action="store_true", help="skip real messages")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    print("feishu-card live smoke test")
    print("=" * 56)
    asyncio.run(run_smoke(args.chat, send=not args.no_send))
    print("=" * 56)
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
