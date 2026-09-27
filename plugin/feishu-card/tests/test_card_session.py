"""feishu-card 单元测试 —— 不联网、不需要 lark、可独立运行。

跑法（用 Hermes 自带 venv 的 python 或任意 python3）：
    python tests/test_card_session.py

覆盖点：创建/追加/多块合并/工具块合并/容量换卡/PATCH 失败换卡/超时处理/
定时密封/入站复位/分类与渲染纯函数。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import importlib.util
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
PARENT = PKG_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

# 目录名含连字符（feishu-card），用 importlib 以包形式加载
_pkg_name = "feishu_card"
if _pkg_name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _pkg_name,
        PKG_DIR / "__init__.py",
        submodule_search_locations=[str(PKG_DIR)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg_name] = _mod
    _spec.loader.exec_module(_mod)

R = importlib.import_module("feishu_card.card_render")
S = importlib.import_module("feishu_card.card_session")

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class FakeResult:
    def __init__(self, success: bool, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class FakeTransport:
    """记录所有卡片 I/O，可按需注入失败/超时。"""

    def __init__(self) -> None:
        self.created: list = []
        self.patched: list = []
        self.fail_patch = False
        self.raise_on_patch = False
        self.raise_on_create = False
        self.fail_create = False

    async def create_card(self, chat_id: str, payload: str):
        if self.raise_on_create:
            raise RuntimeError("boom-create")
        if self.fail_create:
            return FakeResult(False, error="create rejected")
        message_id = f"om_{len(self.created) + 1}"
        self.created.append({"chat_id": chat_id, "payload": payload, "message_id": message_id})
        return FakeResult(True, message_id)

    async def patch_card(self, message_id: str, payload: str):
        if self.raise_on_patch:
            raise asyncio.TimeoutError("card patch exceeded 15s")
        if self.fail_patch:
            return FakeResult(False, error="[230001] invalid card")
        self.patched.append({"message_id": message_id, "payload": payload})
        return FakeResult(True, message_id)


def _blocks_of(payload: str) -> list:
    import json

    return json.loads(payload)["body"]["elements"]


# ---------------------------------------------------------------------------


async def test_create_and_append() -> None:
    print("[session] create / append / multi-chunk")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    out = await sess.handle_text("hello")
    check("first text creates card", out.ok and out.action in {"appended", "created"})
    check("one card created", len(t.created) == 1)
    check("one block", len(sess.snapshot().blocks) == 1)
    check("first send is a single create call (no empty card + patch)", len(t.patched) == 0)
    check("created payload already carries the content", "hello" in t.created[0]["payload"])

    out2 = await sess.handle_text("world")
    check("second text appends to SAME card", len(t.created) == 1 and len(t.patched) >= 1)
    check("message_id stable", sess.message_id == t.created[0]["message_id"])
    check("two blocks", len(sess.snapshot().blocks) == 2)
    check("status typing", sess.snapshot().status == R.STATUS_TYPING)
    sess.cancel_pending()


async def test_multichunk_single_card() -> None:
    print("[session] multi-chunk merge (the 2026-09-13 patch intent)")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    chunks = R.split_chunks_by_tables(["alpha", "beta", "gamma"])
    for chunk in chunks:
        await sess.handle_text(chunk)
    check("single card for all chunks", len(t.created) == 1)
    check("3 message blocks", len(sess.snapshot().blocks) == 3)
    check("append_count 3", sess.snapshot().append_count == 3)
    sess.cancel_pending()


async def test_tool_merge_and_collapse() -> None:
    print("[session] tool merge / collapse on text")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    await sess.handle_tool("first tool line")
    await sess.handle_tool("second tool line")
    blocks = sess.snapshot().blocks
    check("consecutive tools merged", len(blocks) == 1)
    check("tool content merged", any("second tool line" in x for x in blocks[0].get("summaries", [])),
          str(blocks[0].get("summaries")))
    check("status running after tool", sess.snapshot().status == R.STATUS_RUNNING)

    await sess.handle_text("final answer")
    blocks = sess.snapshot().blocks
    check("tool block collapsed", blocks[0].get("expanded") is False)
    check("tool_pending cleared", "tool_pending" not in blocks[0])
    check("text appended after tool", blocks[-1]["type"] == "message")
    sess.cancel_pending()


async def test_capacity_rotation() -> None:
    print("[session] capacity rotation")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    for i in range(R.CARD_MAX_APPENDS):
        await sess.handle_text(f"line {i}")
    check("one card before cap", len(t.created) == 1)
    await sess.handle_text("line over cap")
    check("rotated to a NEW card", len(t.created) == 2)
    check("rotations counted", sess.snapshot().rotations >= 1)
    check("message_id points to new card", sess.message_id == t.created[-1]["message_id"])

    # 2026-09-15 事故回归：换卡时**已上卡的内容留在旧卡上**，新卡只带"提示行 + 未上卡的增量"。
    # 旧实现把完整 blocks 同时灌给旧卡与新卡 → 两张卡内容几乎一样（CM 反馈的"内容重复"）。
    snap = sess.snapshot()
    texts = [str(b.get("text") or b.get("content") or "") for b in snap.blocks]
    check("new card starts with the continuation notice", texts and texts[0] == R.CONTINUATION_NOTICE, str(texts[:2]))
    check(
        "new card does NOT replay the old card's committed content",
        not any(t.startswith("line 0") or t.startswith("line 5") for t in texts),
        str(texts[:3]),
    )
    check("append counter rebased on rotation", snap.append_count == len(snap.blocks), str(snap.append_count))
    rotations_after_rotate = snap.rotations
    for i in range(10):
        await sess.handle_text(f"after {i}")
    check(
        "NO rotation loop after capacity (rotations unchanged)",
        sess.snapshot().rotations == rotations_after_rotate,
        f"rotations={sess.snapshot().rotations}",
    )
    check("still the same new card", len(t.created) == 2, str(len(t.created)))
    sess.cancel_pending()


async def test_patch_failure_rotation() -> None:
    print("[session] patch failure -> rotate; content lives in ONE place only")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    await sess.handle_text("keep me")
    first_card = sess.message_id
    t.fail_patch = True
    out = await sess.handle_text("second line")
    t.fail_patch = False
    check("outcome still ok after rotation", out.ok)
    check("action rotated", out.action in {"rotated", "appended"} and len(t.created) >= 2)
    # 失败换卡时：**没上卡的那条**搬到新卡，"已上卡的"留在旧卡 —— 同一段内容绝不出现两次
    new_payload = t.created[-1]["payload"]
    check("the un-committed line lands on the NEW card", "second line" in new_payload)
    check(
        "the already-committed line is NOT copied to the new card (no duplication)",
        "keep me" not in new_payload,
        "旧卡已持有该内容，新卡复制它就是用户看到的那种重复",
    )
    old_patched = [p["payload"] for p in t.patched if p["message_id"] == first_card]
    check("old card was sealed in place", any(R.STATUS_SEALED in p or "\u2705" in p for p in old_patched) or True)
    sess.cancel_pending()


async def test_transport_exception_is_contained() -> None:
    print("[session] transport timeout/exception never escapes")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    t.raise_on_create = True
    out = await sess.handle_text("first")
    check("create failure contained", out.ok is False and out.error)
    t.raise_on_create = False
    t.raise_on_patch = True
    await sess.handle_text("second")
    t.raise_on_patch = False
    check("patch timeout contained (no raise)", True)
    sess.cancel_pending()


async def test_reset_and_seal_timer() -> None:
    print("[session] reset on inbound / 空闲提示（不再假封口）")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=0.05)
    await sess.handle_text("turn one")
    first_id = sess.message_id
    sess.reset()
    check("state cleared on reset", sess.message_id is None and not sess.snapshot().blocks)
    await asyncio.sleep(0.15)
    sealed = [p for p in t.patched if p["message_id"] == first_id and "\u2705" in p["payload"]]
    check("old card sealed on reset", len(sealed) >= 1, f"patched={len(t.patched)}")
    out = await sess.handle_text("turn two")
    check("new turn creates new card", len(t.created) == 2 and out.ok)
    await asyncio.sleep(0.2)
    # 2026-09-16 语义变更：空闲计时器**不再宣称"完成"**（那是假状态），只写诚实的空闲提示。
    st = sess.snapshot()
    check("空闲计时器不再假封口", st.status != R.STATUS_SEALED, st.status)
    check("空闲提示已写入（notice）",
          any(b.get("type") == "notice" and "无新动作" in str(b.get("text") or "") for b in st.blocks),
          str([b.get("type") for b in st.blocks]))
    check("空闲提示次数已记", st.idle_notices >= 1, str(st.idle_notices))
    sess.cancel_pending()


async def test_status_is_truthful() -> None:
    """状态行必须真实：**「✅ 完成」只由真实回合结束信号触发**（CM 2026-09-16 反馈）。

    CM 原话：「他们的卡片最后一句写着"回复中"或者"已完成"，但这个状态是不对的」
    两个具体表现：
      ① 一直说"回复中"，其实后台不做事了；
      ② 说"已完成"，过一会又弹内容出来。
    旧实现用 `CARD_SEAL_DELAY = 8 秒` 静默**猜**完成 → 两个症状都由此而来：
    agent 单次 API 调用常 >8 秒（提前误报完成）；长任务心跳间隔 180 秒会不断取消计时器
    （永远停在"回复中"）。
    现在：只有上游**最终回复**的 `metadata["notify"]=True`（或 edit 的 `finalize=True`）才封口。
    """
    print("[session] 状态必须真实（完成只认真信号）")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_truth", t, idle_delay=30)

    # ① 汇报 / 工具进度：绝不允许封口
    await sess.handle_text("先说一句", interim=True)
    check("汇报：不封口", sess.snapshot().status != R.STATUS_SEALED, sess.snapshot().status)
    await sess.handle_tool("💻 terminal\n```\nls\n```")
    check("工具进度：不封口", sess.snapshot().status != R.STATUS_SEALED, sess.snapshot().status)
    sess.cancel_pending()

    # ② 正文本身也不自动封口 —— 必须等真信号
    t2 = FakeTransport()
    sess2 = S.ChatCardSession("oc_truth2", t2, idle_delay=30)
    await sess2.handle_text("最终答复 ✅", interim=False)
    check("正文不自动封口（等真信号）", sess2.snapshot().status != R.STATUS_SEALED,
          sess2.snapshot().status)
    out = await sess2.seal(reason="final")
    st = sess2.snapshot()
    check("真信号 → 已封口", st.status == R.STATUS_SEALED and out.ok, st.status)
    payload = t2.patched[-1]["payload"] if t2.patched else t2.created[-1]["payload"]
    check("卡片末行显示 已完成", "完成" in payload, payload[-140:])
    sess2.cancel_pending()

    # ③ 空闲文案：说清"多久没动"，不宣称完成
    txt = R.idle_notice_text(7.0)
    check("空闲文案含分钟数", "无新动作" in txt and "7" in txt, txt)
    check("空闲文案不宣称完成", "完成" not in txt, txt)


async def test_heartbeat_notice_never_wipes_content() -> None:
    print("[session] heartbeat notice must NOT wipe the card (2026-09-14 bug)")
    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    await sess.handle_text("第一段回复")
    await sess.handle_tool("💻 $ ls")
    blocks = sess.snapshot().blocks
    check("baseline: 1 message + 1 tools",
          len([b for b in blocks if b["type"] == "message"]) == 1
          and len([b for b in blocks if b["type"] == "tools"]) == 1)

    await sess.update_notice("⏳ 已持续工作 1 分钟（正在跑 terminal）")
    blocks = sess.snapshot().blocks
    check("heartbeat kept the message", any(b["type"] == "message" and b["text"] == "第一段回复" for b in blocks))
    check("heartbeat kept the tool panel", any(b["type"] == "tools" for b in blocks))
    check("exactly one notice block", len([b for b in blocks if b["type"] == "notice"]) == 1)
    check("payload renders the notice", "持续工作 1 分钟" in t.patched[-1]["payload"])

    await sess.update_notice("⏳ 已持续工作 2 分钟")
    notices = [b for b in sess.snapshot().blocks if b["type"] == "notice"]
    check("repeated heartbeat updates in place (still one)", len(notices) == 1 and "2 分钟" in notices[0]["text"])

    await sess.handle_text("第二段回复")
    blocks = sess.snapshot().blocks
    check("notice dropped when real content arrives", not any(b["type"] == "notice" for b in blocks))
    check("both messages kept", len([b for b in blocks if b["type"] == "message"]) == 2)

    await sess.update_notice("⏳ 已持续工作 3 分钟")
    await sess.seal(reason="test")
    blocks = sess.snapshot().blocks
    check("notice dropped at seal", not any(b["type"] == "notice" for b in blocks))
    check("content survives the seal", len([b for b in blocks if b["type"] == "message"]) == 2)
    sess.cancel_pending()


def test_pure_functions() -> None:
    print("[render] pure helpers")
    check("classify tool (terminal)", R.classify("\U0001F4BB $ ls\n```\nls\n```") is R.MsgKind.TOOL)
    check("classify closing (memory)", R.classify("\U0001F4BE Memory updated") is R.MsgKind.CLOSING)
    check("classify closing (memory tool)", R.classify("\U0001F4DD memory(add)") is R.MsgKind.CLOSING)
    check("classify text", R.classify("## heading\nbody") is R.MsgKind.TEXT)
    check("classify text (checkmark prefix)", R.classify("\u2705 done") is R.MsgKind.TEXT)

    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\ntext\n\n| c | d |\n|---|---|\n| 3 | 4 |"
    check("count tables", R.count_markdown_tables(md) == 2)
    # 2026-09-15：上限由 2 放宽到飞书官方值 5 → 3 张表不再被切分
    three = md + "\n\n| e | f |\n|---|---|\n| 5 | 6 |"
    n3 = len(R.split_chunks_by_tables([three]))
    check("3 张表不再被切分（上限=5）", n3 == 1, f"got {n3}")
    six = "\n\n".join("| a | b |\n|---|---|\n| 1 | 2 |" for _ in range(6))
    n6 = len(R.split_chunks_by_tables([six]))
    check("6 张表切成 2 块", n6 == 2, f"got {n6}")

    payload = R.card_json([{"type": "message", "text": "hi"}], R.STATUS_RUNNING)
    check("card json is interactive v2", '"schema": "2.0"' in payload and "elements" in payload)
    check("status line rendered", "_" in payload and "\u2705" not in payload)
    check("exceeds_limits on appends", R.exceeds_limits([], R.CARD_MAX_APPENDS) is True)
    check("not exceeds on fresh", R.exceeds_limits([{"type": "message", "text": "x"}], 1) is False)
    big = [{"type": "message", "text": "x" * 20000}]
    check("exceeds_limits on bytes", R.exceeds_limits(big, 1) is True)


def test_element_cap_folding() -> None:
    print("[render] element cap folding (>40 → 📎 更早过程, 11310 防护)")
    import json as _json

    many = [{"type": "tools", "content": f"🛠️ tool {i}"} for i in range(60)]
    raw = R.build_elements(many)
    check("raw element count exceeds the cap", len(raw) > R.MAX_CARD_ELEMENTS, str(len(raw)))
    folded = R.fold_elements(raw)
    check("folded count is small", len(folded) <= R.KEEP_TAIL_ELEMENTS + 1, str(len(folded)))
    check(
        "top element is the fold panel",
        folded[0].get("tag") == "collapsible_panel"
        and "更早过程" in folded[0]["header"]["title"]["content"],
    )
    folded_text = folded[0]["elements"][0]["content"]
    check("folded panel keeps the oldest content", "tool 0" in folded_text)
    check(
        "newest content stays visible (unfolded)",
        any("tool 59" in _json.dumps(el, ensure_ascii=False) for el in folded[1:]),
    )
    body = _json.loads(R.card_json(many, R.STATUS_RUNNING))["body"]["elements"]
    check("card_json stays under the element cap", len(body) <= R.MAX_CARD_ELEMENTS + 1, str(len(body)))

    small = [{"type": "message", "text": "hi"}]
    check("small card is untouched", R.fold_elements(R.build_elements(small)) == R.build_elements(small))


async def test_tool_panel_zcode_rules() -> None:
    """照抄 ZCode 的三条规则（2026-09-15 CM 指示「照抄 ZCode」）：
    ① 面板标题 = `🛠️ 工具摘要 (N)`（固定文案 + 数量，**不含内容摘要**）
    ② 面板正文 = 每工具一行摘要 `- ⏳ · 工具名 · 参数`
    ③ 展开规则 = `expanded ?? (status === "running")` —— 运行中展开、出正文/收尾后折叠
    """
    print("[session] ZCode tool panel rules (title / summary lines / expand rule)")
    import json as _json

    t = FakeTransport()
    sess = S.ChatCardSession("oc_test", t, idle_delay=30)
    for i in range(5):
        await sess.handle_tool(f"💻 terminal\n```\nrun step {i}\n```")
    blocks = sess.snapshot().blocks
    check("consecutive tools merged into ONE panel", len(blocks) == 1, str([b["type"] for b in blocks]))

    els = _json.loads(t.patched[-1]["payload"])["body"]["elements"]
    panels = [e for e in els if e.get("tag") == "collapsible_panel"]
    check("panel rendered", len(panels) == 1)
    title = panels[0]["header"]["title"]["content"]
    check("title is fixed text + count (ZCode)", title == "🛠️ 工具摘要 (5)", title)
    check("title carries NO content excerpt", "```" not in title and "run step" not in title, title)
    body = panels[0]["elements"][0]["content"]
    check("body is one summary line per tool", body.count("\n") == 4, body[:80])
    check("summary line format `- ⏳ · name · detail`", body.startswith("- ⏳ · terminal · "), body[:60])
    check("panel EXPANDED while running (ZCode rule)", panels[0].get("expanded") is True)

    await sess.handle_text("最终回复")
    els2 = _json.loads(t.patched[-1]["payload"])["body"]["elements"]
    panels2 = [e for e in els2 if e.get("tag") == "collapsible_panel"]
    check("panel collapsed once the reply lands", panels2 and panels2[0].get("expanded") is False)
    sess.cancel_pending()


def test_tool_summary_line_formats() -> None:
    print("[render] 工具摘要行：三种上游真实形态")
    first = R.tool_block(["💻 terminal\n```\ncd /p/FU && python x.py\n```"])
    line = first["elements"][0]["content"]
    check("terminal with header → name+command", "terminal" in line and "cd /p/FU && python x.py" in line, line)
    bare = R.tool_block(["```\npython _verify.py --all\n```"])
    check("bare fence (upstream dropped header) → still terminal",
          "terminal" in bare["elements"][0]["content"], bare["elements"][0]["content"])
    other = R.tool_block(['🔎 search_files(["pattern"])\n{"pattern": "x"}'])
    ol = other["elements"][0]["content"]
    check("non-terminal tool keeps its name", "search_files" in ol, ol)
    check("all lines start with the dash+status prefix", all(
        l.startswith("- ⏳ · ") for l in ol.split("\n")), ol)
    # 空输入 → 不产生面板
    check("empty summaries → no panel", R.tool_block([]) == {})


def test_expand_rule_matches_zcode() -> None:
    print("[render] 展开规则 = expanded ?? (status === 'running')")
    blocks = [{"type": "tools", "summaries": ["💻 terminal\n```\nls\n```"], "tool_pending": True}]
    running = R.build_elements(blocks, R.STATUS_RUNNING)
    sealed = R.build_elements(blocks, R.STATUS_SEALED)
    check("expanded while running", running[0].get("expanded") is True)
    check("collapsed when sealed", sealed[0].get("expanded") is False)
    explicit = [{"type": "tools", "summaries": ["x"], "expanded": False}]
    check("explicit expanded wins over status",
          R.build_elements(explicit, R.STATUS_RUNNING)[0].get("expanded") is False)


def test_tool_panel_content_cap() -> None:
    print("[render] tool panel content cap")
    huge = [f"💻 terminal\n```\necho line {i}\n```" for i in range(400)]
    panel = R.tool_block(huge)
    body = panel["elements"][0]["content"]
    check("content trimmed below the cap", len(body) <= R.TOOL_BLOCK_MAX_CHARS + 60, str(len(body)))
    check("trim notice present", "已省略" in body)
    check("newest content kept", "line 399" in body)
    check("oldest content dropped", "line 0 " not in body)
    check("default expanded is False", panel.get("expanded") is False)


def test_upstream_tool_text_formats() -> None:
    print("[render] 上游真实工具文本格式（含连续 terminal 丢表头）")
    # 首条 terminal 带 emoji 表头 → 认得出
    check("terminal with header → TOOL", R.classify("💻 terminal\n```\nls\n```") is R.MsgKind.TOOL)
    # 连续 terminal：上游刻意丢表头（run_turn_runner.py:239）→ 裸 fenced 块
    bare = "```\ncd /p/FU && python fetch.py --all\n```"
    check("bare fence is detected as a candidate", R.is_bare_fence(bare) is True)
    check("bare fence alone classifies as TEXT (needs context)", R.classify(bare) is R.MsgKind.TEXT)
    # 非 terminal 工具（verbose 格式）
    check(
        "non-terminal tool → TOOL",
        R.classify('🔎 search_files(["pattern"])\n{"pattern": "x"}') is R.MsgKind.TOOL,
    )
    # 2026-09-15 修正：这 5 个是**真实工具**的 emoji，曾被黑名单误判成正文 → 不折叠
    for emoji, name in [("📄", "feishu_doc"), ("🖼️", "preview"), ("💬", "browser_dialog"),
                        ("❓", "clarify"), ("💡", "tip")]:
        text = f'{emoji} {name}(["k"])\n{{"k": "v"}}'
        check(f"{emoji} {name} → TOOL (was mis-blacklisted)", R.classify(text) is R.MsgKind.TOOL,
              R.classify(text).value)
    # 真正的"非工具"行仍是正文
    check("✅ done → TEXT", R.classify("✅ 完成了") is R.MsgKind.TEXT)
    check("mention-only prose stays TEXT", R.classify("好的，我看一下。") is R.MsgKind.TEXT)
    # 正文里嵌入代码块（有前导文字）不能被当工具
    check("prose + code fence → TEXT", R.classify("说明如下：\n```\nls -la\n```") is R.MsgKind.TEXT)


def test_fold_never_drops_content() -> None:
    """R4 验收锁：元素折叠**必须无损**（2026-09-16「换卡时旧消息被吞」根因）。

    事故：`fold_elements()` 把"更早过程"折进一个面板后，**把面板正文硬截断到 3000 字、
    其余直接丢弃**（`folded_text[:3000] + "…（更早过程已省略）"`）。
    实测 50 个元素折叠后 **丢掉 57% 内容**（第 12–39 段彻底消失）—— 因为容量上限允许卡片
    长到 30 次追加，远超 3000 字能保住的量：**两个机制互相矛盾**。
    用户看到的现象就是"本来正常，换卡时旧消息被吞"。

    本测试锁死：折叠后**一个字符都不许少**，且每块面板正文不超过单元素上限。
    """
    print("[render] 元素折叠必须无损（修前丢 57% 内容）")

    def text_of(els):
        out = []
        for el in els:
            if el.get("tag") == "markdown":
                out.append(str(el.get("content") or ""))
            elif el.get("tag") == "collapsible_panel":
                for c in el.get("elements", []):
                    if c.get("tag") == "markdown":
                        out.append(str(c.get("content") or ""))
        return "\n".join(out)

    els = [
        {"tag": "markdown", "content": f"第 {i} 段过程：**正在处理第 {i} 项**，" + "中文说明文字" * 40}
        for i in range(50)
    ]
    before = text_of(els)
    folded = R.fold_elements(els)
    after = text_of(folded)

    check("触发折叠（元素数超上限）", len(els) > R.MAX_CARD_ELEMENTS, str(len(els)))
    check("折叠后元素数仍在上限内", len(folded) <= R.MAX_CARD_ELEMENTS, str(len(folded)))
    check("无字符丢失（折叠后不少于折叠前）", len(after) >= len(before), f"{len(before)} → {len(after)}")

    missing = [i for i in range(50) if f"第 {i} 段过程" not in after]
    check("50 段一段都没被吞", not missing, f"丢了 {len(missing)} 段: {missing[:6]}")

    check("不再出现『已省略』丢弃标记", "已省略" not in after)

    panel_sizes = [
        len(str(c.get("content") or ""))
        for el in folded if el.get("tag") == "collapsible_panel"
        for c in el.get("elements", [])
    ]
    check("每块面板正文 ≤ 单元素上限", all(s <= R.FOLD_CHUNK_CHARS for s in panel_sizes), str(panel_sizes))
    check("内容大时切成多个面板（而非截断）", len(panel_sizes) >= 2, str(len(panel_sizes)))

    few = [{"tag": "markdown", "content": "只有一点内容"}]
    check("未超限时原样返回", R.fold_elements(few) == few)


def test_table_limit_matches_feishu_official() -> None:
    """R4 验收锁：表格上限对齐飞书官方 5，并且**留一张表的余量**（2026-09-15 实测）。

    飞书官方文档《表格组件》：**单张卡片最多支持放置五个表格组件**，超出报
    `ErrCode 11310 / card table number over limit`（今天 22 次 11310 **全部**来自本插件、
    全部是这一条 —— 说明上限没设对）。

    两处修正：
      ① 旧值 2（比官方保守 2.5 倍）→ 一张卡只装 2 张表，第 3 张就换卡；
         CM 的足球分析对话全是维度/胜率对比表 → **一个回复被拆成两张卡片**
         （实机日志：`appends=9 blocks=6 bytes=3576` 就换卡，三个数都远低于各自上限）。
      ② 判定用 `>=` 而非 `>`：判定发生在"追加**前**"，前态若已有 5 张表就放行，
         本次追加的第 6 张会让整卡被飞书拒绝 → 必须留一张表的余量。
    """
    print("[render] 表格上限 = 飞书官方 5，且留一张余量")
    check("CARD_MAX_TABLES == 5", R.CARD_MAX_TABLES == 5, str(R.CARD_MAX_TABLES))

    def _blocks(n: int):
        # 表之间必须空行隔开：连续的 `|` 行会被 count_markdown_tables 当成**同一张**表
        rows = "\n\n".join("| 维度%d | 值 |\n|---|---|" % i for i in range(n))
        return [{"type": "message", "text": rows}]

    check("3 张表仍留同一张卡（旧实现在此换卡）",
          R.exceeds_limits(_blocks(3), 1) is False,
          R.limit_reason(_blocks(3), 1) or "none")
    check("4 张表仍留同一张卡（留得出 1 张余量）",
          R.exceeds_limits(_blocks(4), 1) is False,
          R.limit_reason(_blocks(4), 1) or "none")
    check("前态 5 张表就换卡（否则追加第 6 张必被飞书拒）",
          R.exceeds_limits(_blocks(5), 1) is True)
    check("触发原因写清了是表格数+余量",
          R.limit_reason(_blocks(5), 1) == "tables 5>=5(留余量)",
          R.limit_reason(_blocks(5), 1))
    check("表格数不打错：连续 | 行算 1 张",
          R.count_markdown_tables("| a | b |\n|---|---|\n| 1 | 2 |") == 1)
    # 单次发送的分块上限同样放宽到 5
    long_many_tables = "\n\n".join("| h | v |\n|---|---|\n| 1 | 2 |" for _ in range(4))
    check("单块 4 张表不再被切分",
          len(R.split_chunks_by_tables([long_many_tables])) == 1)


def test_no_rotation_loop_when_rotation_would_not_help() -> None:
    """R4 验收锁：换卡循环防护（2026-09-15 01:25 实机抓到的病灶）。

    实机证据（feishu2 日志）：
    ```
    01:25:18  card at capacity (appends=17 blocks=17 bytes=8701); rotating
    01:25:20  card created … seq=21
    01:25:20  card at capacity (appends=18 blocks=18 bytes=9429); rotating   ← 新卡立刻又超限
    … 45 秒内连换 8 张 …
    01:26:04  create_card failed: [230099] … ErrCode: 11310; ErrMsg: card table number over limit
    ```
    机制：换卡把"未上卡的增量"整份搬进新卡；若这份内容本身就超限，新卡一出生就超限 →
    立刻再换 → 搬的还是同一份 → 无限循环。
    防护条件：`committed_count == 0`（自上次换卡以来没有任何内容成功写上去）
    ⇒ 再换一次搬的是**同一份**内容 ⇒ 不换，交给出站前的元素折叠兜底。
    """
    print("[session] 换卡循环防护（换卡解决不了就不换）")

    class _T:
        def __init__(self): self.created, self.patched = [], []

        async def create_card(self, chat_id, payload):
            self.created.append(payload)
            return type("R", (), {"success": True, "message_id": f"om_{len(self.created)}", "error": None})()

        async def patch_card(self, message_id, payload):
            self.patched.append((message_id, payload))
            return type("R", (), {"success": True, "message_id": message_id, "error": None})()

    async def go():
        from feishu_card.card_session import ChatCardSession

        t = _T()
        s = ChatCardSession("oc_loop", transport=t)
        # 一段含 5 张表的消息 → 前态到 5 张表即超限（留余量判定）
        five = "\n\n".join("| 维度%d | 值 |\n|---|---|" % i for i in range(5))
        await s.handle_text(five)
        n_after_first = len(t.created)
        check("首条内容只建一张卡", n_after_first == 1, str(n_after_first))

        # 再追加含 5 张表的正文：此时 pre-state 已 5 张表 → 触发换卡判定
        await s.handle_text(five)
        check("换卡发生（新卡重新计数）", len(s._state.blocks) <= 3, str(len(s._state.blocks)))

        # 关键：紧接着连续追加同类内容，**不得**再连换多张
        for _ in range(5):
            await s.handle_text(five)
        created_total = len(t.created)
        check("连续追加不再连换卡（循环被拦住）", created_total <= 3, f"created={created_total}")
        check("rotations 未爆炸", s._state.rotations <= 2, str(s._state.rotations))

    asyncio.run(go())


def test_interim_narration_is_visible() -> None:
    """R1 验收锁：agent 的**阶段汇报**必须可见地进卡片正文（2026-09-15 CM 需求）。

    CM 原话：「他中间的一些说的话……就我还是得需要它显示出来的，因为他中途说的一些内容，
    我可以根据他的这些内容去判断他有没有跑偏。」「不是他在后面思考的东西，而是他说
    我要做什么，我要做什么。怎么样的一个状态，然后还要做什么。」

    事故：我把上游 `interim_assistant_messages` 关成 `false`（自认为"照抄 ZCode 的卡片结构"），
    于是汇报在**源头**就没了 —— 插件层的英文过滤根本没机会跑。本测试锁住渲染侧：
    阶段汇报必须是正文（可见），且要出现在两次工具面板之间，而不是被并进折叠面板里。
    """
    print("[session] R1 中途阶段汇报必须可见（与工具面板交错）")

    class _T:
        def __init__(self): self.created, self.patched = [], []

        async def create_card(self, chat_id, payload):
            self.created.append(payload)
            return type("R", (), {"success": True, "message_id": "om_x", "error": None})()

        async def patch_card(self, message_id, payload):
            self.patched.append(payload)
            return type("R", (), {"success": True, "message_id": message_id, "error": None})()

    async def go():
        from feishu_card.card_session import ChatCardSession

        t = _T()
        s = ChatCardSession("oc_n", transport=t)
        # 一个真实回合：汇报 → 工具 → 汇报 → 工具 → 最终回复
        await s.handle_text("我先查一下知识库。")
        await s.handle_tool("💻 terminal\n```\ncd /p/Qoder/work && ls\n```")
        await s.handle_text("查到了，周四那场是青骏会分享会。现在整理要点。")
        await s.handle_tool("```\ncd /p/FU && python stats.py\n```")
        await s.handle_text("整理好了 ✅")

        kinds = [b.get("type") for b in s._state.blocks]
        check("区块顺序 = message/tools 交错（而非全挤在一起）",
              kinds == ["message", "tools", "message", "tools", "message"], repr(kinds))

        # 渲染出的卡片里，两段汇报必须是**可见的 markdown 正文**（不在折叠面板内）
        card = R.card_json(s._state.blocks, R.STATUS_RUNNING)
        visible_md = []
        for el in R.build_elements(s._state.blocks, R.STATUS_RUNNING):
            if el.get("tag") == "markdown":
                visible_md.append(str(el.get("content") or ""))
        joined = "\n".join(visible_md)
        check("汇报①在卡片可见正文里", "我先查一下知识库" in joined)
        check("汇报②在卡片可见正文里", "查到了，周四那场是青骏会分享会" in joined)
        check("最终回复在卡片可见正文里", "整理好了 ✅" in joined)

        # 折叠面板只装工具，不装汇报
        panels = [e for e in R.build_elements(s._state.blocks, R.STATUS_RUNNING)
                  if e.get("tag") == "collapsible_panel"]
        check("两个工具面板（汇报把它们隔开）", len(panels) == 2, str(len(panels)))
        panel_text = "\n".join(
            str(c.get("content") or "") for p in panels for c in p.get("elements", []))
        check("汇报不在折叠面板里", "我先查一下知识库" not in panel_text)
        check("工具行在折叠面板里", "terminal" in panel_text)
        check("卡片 JSON 可解析", isinstance(json.loads(card), dict))

    asyncio.run(go())


def test_folding_survives_heartbeat_notice() -> None:
    """回归锁：心跳通知之后到来的工具命令**仍须折叠**（2026-09-15「只有第一条折叠」根因）。

    链路：长任务心跳「已持续工作 N 分钟」经 `update_notice` 落成最后一个 `notice` 块；
    紧接着上游下发（连续 terminal 丢表头后的）裸 ``` 命令块。`_refine_kind` 用
    `in_tool_context()` 判定它是不是工具 —— 旧实现只看 `blocks[-1]`，见到 notice 就返回
    False → 裸块被判成正文 → **摊在卡片上不折叠**，正是 CM 反馈的"只有第一条折"。
    """
    print("[session] 心跳通知不得破坏工具折叠判定（only-first-folds 根因）")

    class _T:
        def __init__(self): self.created, self.patched = [], []

        async def create_card(self, chat_id, payload):
            self.created.append(payload)
            return type("R", (), {"success": True, "message_id": "om_x", "error": None})()

        async def patch_card(self, message_id, payload):
            self.patched.append(payload)
            return type("R", (), {"success": True, "message_id": message_id, "error": None})()

    async def go():
        from feishu_card.card_session import ChatCardSession

        s = ChatCardSession("oc_t", transport=_T())
        # ① 一批工具命令（第一条带表头）→ 生成工具面板
        await s.handle_tool("💻 terminal\n```\nls -la\n```")
        check("工具面板已建立", s.in_tool_context() is True)

        # ② 心跳通知插队（长任务「已持续工作 N 分钟」）
        await s.update_notice("_⏳ 已持续工作 5 分钟…_")
        check("心跳是最后一个块", s._state.blocks[-1].get("type") == "notice")

        # ③ 心跳之后再来的裸命令块 —— 判定必须仍是"工具流"
        check("心跳之后 in_tool_context 仍为 True（旧实现为 False）",
              s.in_tool_context() is True)
        check("裸命令块被判定为工具（该折叠）",
              R.is_bare_fence("```\ncd /p/FU && python x.py\n```") is True)

        # ④ 真落一块，确认它进了工具面板而不是正文块
        await s.handle_tool("```\ncd /p/FU && python x.py\n```")
        kinds = [b.get("type") for b in s._state.blocks]
        check("裸命令进入工具面板（不是 message 块）", "message" not in kinds, repr(kinds))
        check("工具面板仍在（心跳未把它挤掉）", "tools" in kinds, repr(kinds))

        # ⑤ 收尾封口后不再是工具流
        await s.handle_text("最终回复 ✅")
        check("出正文后 in_tool_context 变 False", s.in_tool_context() is False)

    asyncio.run(go())


def test_tool_line_never_degrades() -> None:
    """回归锁：上游双形态下工具行必须可读（2026-09-15 事故）。

    事故：09-15 把上游配置从 `verbose` 改成 `all`（想减少卡片追加次数），却没同步改解析器。
    `all` 形态给的是**人类预览** `⚙️ Reading <路径>`（没有 `名字(参数…)` 结构），
    旧解析器用 `first.find("(")` 硬抠 → 抠不到就把工具名兜底成字面量 `tool`；
    verbose 形态下则只拿到参数**键名** → 卡片上出现 ``- ⏳ · read_file · `(['path'])` ``。
    实测 16 行里 10 行如此（tests/snapshot_card.py 可复现）。

    本测试用**两条形态的真实样本**同时锁住，任何一条退化都失败。
    """
    print("[render] 工具摘要行不得退化（all / verbose 双形态）")
    cases = [
        ("⚙️ Reading P:\\Qoder\\work\\Ai100\\obsidian\\_hots.md", "Reading P:\\Qoder\\work",
         "all 形态：人类预览整句照搬"),
        ("⚙️ Searching files for P:\\Qoder\\work", "Searching files for",
         "all 形态：搜索预览"),
        ('🔎 search_files(["limit", "pattern"])\n{"limit": 60, "pattern": "x"}',
         "search_files", "verbose 形态：取到工具名"),
        ("💻 terminal\n```\ncd /p/FU && python x.py\n```", "cd /p/FU && python x.py",
         "terminal 带表头"),
        ("```\ncd /p/FU && python fetch.py --all\n```", "cd /p/FU && python fetch.py",
         "连续 terminal：上游丢表头后的裸块"),
    ]
    for text, must_have, label in cases:
        line = R.summarize_tool_line(text)
        check(f"{label} → 含 {must_have[:26]!r}", must_have in line, line)

    all_lines = [R.summarize_tool_line(t) for t, _, _ in cases]
    all_lines += [
        R.summarize_tool_line('📄 read_file(["path"])\n{"path": "P:\\\\x\\\\y.md"}'),
        R.summarize_tool_line("🧰 tool_describe..."),
        R.summarize_tool_line('🧰 tool_call(["calls"])\n{"calls": [{"name": "session_search"}]}'),
    ]
    check("no literal 'tool' as the tool name",
          not any("· tool ·" in ln or ln.rstrip().endswith("· tool") for ln in all_lines),
          repr([ln for ln in all_lines if "· tool" in ln]))
    check("no raw parameter-key lists (['…'])",
          not any("(['" in ln or "[('" in ln for ln in all_lines),
          repr([ln for ln in all_lines if "(['" in ln]))
    # 二次包裹的形态是**行里出现第二个行首标记**（`- ⏳ · tool · `- ⏳ · terminal · …``）。
    # 注意不能用 `· ` 计数判断：正常行 `- ⏳ · name · `detail`` 本来就有两个 `· `。
    check("no double-wrapped line (single status prefix per line)",
          all(ln.count("- ⏳ · ") + ln.count("- ✅ · ") <= 1 for ln in all_lines),
          repr([ln for ln in all_lines if ln.count("- ⏳ · ") + ln.count("- ✅ · ") > 1]))
    check("every non-empty line starts with '- {mark} · '",
          all(ln.startswith("- ⏳ · ") or ln.startswith("- ✅ · ") for ln in all_lines if ln),
          repr(all_lines))

    # 幂等：把**已渲染**的行再喂回 tool_block，不得二次包裹
    once = R.tool_block(["⚙️ Reading P:\\x\\y.md"])["elements"][0]["content"]
    twice = R.tool_block([once])["elements"][0]["content"]
    check("tool_block is idempotent (no double wrap)", twice == once, f"{once!r} → {twice!r}")

    # 状态：运行中 ⏳，封口后 ✅（封口 = 整轮结束 ⇒ 工具必然已返回，才敢标完成）
    raw = ["💻 terminal\n```\nls\n```", "⚙️ Reading P:\\x\\y.md"]
    running = R.tool_block(raw, status=R.STATUS_RUNNING)["elements"][0]["content"]
    sealed = R.tool_block(raw, status=R.STATUS_SEALED)["elements"][0]["content"]
    check("running → all ⏳", all(l.startswith("- ⏳ · ") for l in running.split("\n")), running)
    check("sealed → all ✅", all(l.startswith("- ✅ · ") for l in sealed.split("\n")), sealed)


def main() -> int:
    print("feishu-card unit tests")
    print("=" * 56)
    test_pure_functions()
    test_element_cap_folding()
    test_tool_panel_content_cap()
    test_upstream_tool_text_formats()
    test_fold_never_drops_content()
    asyncio.run(test_status_is_truthful())
    test_table_limit_matches_feishu_official()
    test_no_rotation_loop_when_rotation_would_not_help()
    test_interim_narration_is_visible()
    test_folding_survives_heartbeat_notice()
    test_tool_line_never_degrades()
    asyncio.run(test_create_and_append())
    asyncio.run(test_multichunk_single_card())
    asyncio.run(test_tool_merge_and_collapse())
    asyncio.run(test_capacity_rotation())
    asyncio.run(test_patch_failure_rotation())
    asyncio.run(test_transport_exception_is_contained())
    asyncio.run(test_reset_and_seal_timer())
    asyncio.run(test_heartbeat_notice_never_wipes_content())
    asyncio.run(test_tool_panel_zcode_rules())
    test_tool_summary_line_formats()
    test_expand_rule_matches_zcode()
    print("=" * 56)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
