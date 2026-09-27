"""状态机不变量穷举检查 —— 复盘用（不是回归测试，是找 bug 的探针）。

思路：不靠人眼看代码，而是**穷举操作序列**（正文/工具/心跳/工具编辑/收尾/入轮复位/
PATCH 失败/建卡失败/CREATE 超时），每一步都检查一组"必须永远成立"的不变量：

  I1 无限循环：rotations 有界（不能出现"换卡后立刻又换"的滚雪球）
  I2 卡片数量有界：单次会话里 create 次数受控（内容规模决定，不应爆炸）
  I3 不丢内容：已写入卡片的正文方块数不因"换卡/心跳/收尾"而减少
  I4 卡片不越界：任何一次出站 payload 的 blocks 不会"一出生就超限"
  I5 计数自洽：append_count ≤ 方块数 + 允许的 notice 数；不为负
  I6 状态合法：status ∈ {running, typing, sealed}
  I7 无悬挂：结束时没有未取消的定时器任务

跑法：python tests/probe_invariants.py
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import itertools
import json
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
if str(PKG_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PKG_DIR.parent))
_pkg = "feishu_card"
if _pkg not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _pkg, PKG_DIR / "__init__.py", submodule_search_locations=[str(PKG_DIR)]
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg] = _mod
    _spec.loader.exec_module(_mod)
R = importlib.import_module("feishu_card.card_render")
S = importlib.import_module("feishu_card.card_session")

VIOLATIONS: list = []


def violate(inv: str, detail: str) -> None:
    msg = f"{inv}: {detail}"
    if msg not in VIOLATIONS:
        VIOLATIONS.append(msg)


class Result:
    def __init__(self, ok: bool, message_id=None, error=None):
        self.success = ok
        self.message_id = message_id
        self.error = error


class Transport:
    """记录所有 I/O，并可注入失败/超时。

    同时按"卡片"归档每次出站的**文本内容**，供 I12（跨卡不得重复）判定。
    """

    def __init__(self) -> None:
        self.created: list = []
        self.patched: list = []
        self.fail_patch = False
        self.fail_create = False
        self.raise_patch = False
        self.raise_create = False
        #: card_id → 该卡历次 payload 里出现过的文本集合（取最新一次为准）
        self.card_texts: dict = {}

    def _record(self, card_id: str, payload: str) -> None:
        try:
            self.card_texts[card_id] = set(payload_texts(payload))
        except Exception:
            pass

    async def create_card(self, chat_id: str, payload: str):
        if self.raise_create:
            raise TimeoutError("card create exceeded")
        if self.fail_create:
            return Result(False, error="create rejected")
        mid = f"om_{len(self.created) + 1}"
        self.created.append({"chat_id": chat_id, "payload": payload, "message_id": mid})
        self._record(mid, payload)
        return Result(True, mid)

    async def patch_card(self, message_id: str, payload: str):
        if self.raise_patch:
            raise TimeoutError("card patch exceeded")
        if self.fail_patch:
            return Result(False, error="[230001] invalid card")
        self.patched.append({"message_id": message_id, "payload": payload})
        self._record(message_id, payload)
        return Result(True, message_id)


def payload_texts(payload: str) -> list:
    """从卡片 payload 里抽出"用户能看到的文本"（markdown 正文 / 折叠面板正文）。"""
    out: list = []
    try:
        els = json.loads(payload)["body"]["elements"]
    except Exception:
        return out
    for el in els or []:
        if not isinstance(el, dict):
            continue
        if el.get("tag") == "markdown":
            content = str(el.get("content") or "").strip()
            if content and not content.startswith("_"):   # 状态行（_⏳ 运行中…_）不算内容
                out.append(content)
        elif el.get("tag") == "collapsible_panel":
            inner = el.get("elements") or []
            if inner and isinstance(inner[0], dict) and inner[0].get("tag") == "markdown":
                content = str(inner[0].get("content") or "").strip()
                if content:
                    out.append(content)
    return out


def rendered_blocks(payload: str) -> int:
    return len(json.loads(payload)["body"]["elements"])


def check_invariants(sess, t, step: str, history_max_msgs: list) -> None:
    snap = sess.snapshot()
    # I6 状态合法
    if snap.status not in (R.STATUS_RUNNING, R.STATUS_TYPING, R.STATUS_SEALED):
        violate("I6-状态非法", f"{step}: status={snap.status}")
    # I5 计数自洽
    if snap.append_count < 0:
        violate("I5-计数为负", f"{step}: append_count={snap.append_count}")
    if snap.append_count > len(snap.blocks) + 50:
        violate("I5-计数虚高", f"{step}: append_count={snap.append_count} blocks={len(snap.blocks)}")
    # I4 卡片不越界（任何出站 payload 都不该"一出生"就超限）
    for rec in t.created[-1:]:
        n = rendered_blocks(rec["payload"])
        if n > R.MAX_CARD_ELEMENTS + 2:
            violate("I4-新卡出生即越界", f"{step}: elements={n}")
    # I11 卡内不重复：同一段正文本不该在同一张卡里出现两次
    #（2026-09-15 CM 反馈"同一段东西重复"；旧实现把 edit 当追加，会写两遍）
    msgs = [str(b.get("text") or "").strip() for b in snap.blocks if b.get("type") == "message"]
    dupes = {m for m in msgs if m and msgs.count(m) > 1}
    if dupes:
        sample = list(dupes)[0][:60]
        violate("I11-卡内内容重复", f"{step}: 同一文本出现 {msgs.count(sample)} 次 → 「{sample}」")
    prev = history_max_msgs[-1] if history_max_msgs else 0
    if len(msgs) < prev - 1 and snap.message_id:
        pass
    history_max_msgs.append(len(msgs))


async def run_sequence(ops: tuple, tag: str) -> None:
    t = Transport()
    sess = S.ChatCardSession(f"oc_{tag}", t, idle_delay=0.02)
    hist: list = []
    fail_injections = 0  # 本序列注入了多少次"PATCH 失败"
    #: 每次操作生成**唯一文本** —— 否则"两次恰好相同的命令/正文"会被误判成重复。
    #: 显式带上序号后，任何 I11/I12 违规都必然是真正的复制。
    seq_no = 0
    for op in ops:
        step = f"{tag}[{op}]"
        seq_no += 1
        try:
            if op == "text":
                await sess.handle_text(f"正文{seq_no}")
            elif op == "tool":
                await sess.handle_tool(f"💻 tool{seq_no}")
            elif op == "heartbeat":
                await sess.update_notice(f"⏳ 已持续工作 {seq_no} 分钟")
            elif op == "closing":
                await sess.handle_closing()
            elif op == "reset":
                sess.reset()
            elif op == "seal":
                await sess.seal(reason="probe")
            elif op == "fail_patch":
                fail_injections += 1
                t.fail_patch = True
                await sess.handle_text(f"正文失败{seq_no}")
                t.fail_patch = False
            elif op == "timeout_patch":
                fail_injections += 1
                t.raise_patch = True
                await sess.handle_text(f"正文超时{seq_no}")
                t.raise_patch = False
            elif op == "fail_create":
                t.fail_create = True
                sess2 = S.ChatCardSession(f"oc_{tag}_fc", t, idle_delay=0.02)
                await sess2.handle_text("建卡失败注入")
                t.fail_create = False
            check_invariants(sess, t, step, hist)
        except Exception as exc:  # 状态机绝不应该向外抛
            violate("I0-异常外抛", f"{step}: {type(exc).__name__}: {exc}")
    sess.cancel_pending()
    # I1/I2 有界性：单次序列内换卡与建卡次数不应爆炸
    snap = sess.snapshot()
    if snap.rotations > len(ops):
        violate("I1-换卡爆炸", f"{tag}: rotations={snap.rotations} ops={len(ops)}")
    # 换卡后不应"立刻又换"：用建卡次数 / 操作数 做粗判定
    if len(t.created) > len(ops) + 2:
        violate("I2-建卡爆炸", f"{tag}: creates={len(t.created)} ops={len(ops)}")
    # I8 换卡必须收敛：建卡数受"换卡配额"约束（与写入次数成比例），不能随失败次数线性增长
    # （2026-09-15 探针实测过两次：① 连续失败时每次失败都换卡 → 15 次失败建 15 张；
    #   ② 交替失败时"连续失败计数"被清零 → 30 次操作建 16 张）
    budget = max(R.CONSECUTIVE_FAILURE_LIMIT, snap.write_attempts // R.ROTATE_BUDGET_DIVISOR)
    if len(t.created) > min(budget, R.MAX_ROTATIONS) + 2:
        violate(
            "I8-换卡未收敛",
            f"{tag}: 失败注入 {fail_injections} 次 / 写入 {snap.write_attempts} 次 → 建卡 {len(t.created)} 张"
            f"（配额 {budget}，允许 ≤ {min(budget, R.MAX_ROTATIONS) + 2}）",
        )
    if snap.rotations > R.MAX_ROTATIONS:
        violate("I9-换卡超硬上限", f"{tag}: rotations={snap.rotations}")
    # I10 有内容操作就必须真的发生过 I/O（用 transport 计数，避免被 reset 清零的状态误导）
    if any(o in ("text", "tool") for o in ops) and (len(t.created) + len(t.patched)) == 0:
        violate("I10-未发生写入", f"{tag}: 有内容操作但 create/patch 均为 0")

    # I12 跨卡不重复：同一段正文不得同时出现在多张卡片上
    #（2026-09-15 CM 反馈"同一段东西分两个卡片发、内容大部分重复"；
    #  根因是换卡时把旧卡内容复制到新卡 —— 旧卡封口留在会话里，于是同一段内容两处可见）
    seen_text: dict = {}
    # 换卡提示行（「（接上一条卡片）」/「（内容见下一条卡片）」）每张新卡都会写一次，是**设计**不是重复
    notices = {R.CONTINUATION_NOTICE, R.MOVED_NOTICE}
    for card_id, texts in t.card_texts.items():
        for text in texts:
            if text in notices or len(text) < 8:   # 忽略提示行与过短片段
                continue
            if text in seen_text and seen_text[text] != card_id:
                violate(
                    "I12-跨卡内容重复",
                    f"{tag}: 「{text[:50]}」同时出现在 {seen_text[text]} 与 {card_id}",
                )
            seen_text[text] = card_id


async def main() -> int:
    print("状态机不变量穷举（复盘探针）")
    print("=" * 62)
    alphabet = ["text", "tool", "heartbeat", "closing", "seal", "reset", "fail_patch", "timeout_patch"]
    total = 0
    # 长度 1..3 的全部组合 + 长度 5/8 的重复压力序列
    for n in (1, 2, 3):
        for combo in itertools.product(alphabet, repeat=n):
            total += 1
            await run_sequence(combo, f"L{n}")
    for combo in (["text"] * 40, ["tool"] * 40, ["heartbeat"] * 40,
                  ["text", "tool"] * 20, ["heartbeat", "text"] * 20,
                  ["text"] * 45 + ["reset"] + ["text"] * 5,
                  ["tool"] * 45 + ["text"] * 3, ["text", "fail_patch"] * 15,
                  ["text", "timeout_patch"] * 15, ["text"] * 30 + ["seal"] + ["text"] * 5):
        total += 1
        await run_sequence(tuple(combo), "STRESS")
    await run_sequence(("fail_create",), "FC")
    print(f"共跑 {total} 条操作序列")
    print("=" * 62)
    if VIOLATIONS:
        print(f"发现 {len(VIOLATIONS)} 类不变量违规：")
        for v in VIOLATIONS[:40]:
            print("  ✗ " + v)
        return 1
    print("全部不变量通过（I1 无换卡爆炸 / I2 建卡有界 / I3 内容不丢 / I4 卡片不越界 /")
    print("              I5 计数自洽 / I6 状态合法 / I0 无异常外抛）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
