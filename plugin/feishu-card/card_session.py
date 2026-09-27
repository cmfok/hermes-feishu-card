"""卡片会话状态机 —— 每会话一张卡，单写者 + 硬超时 + 失败换卡。

为什么要有这个模块（2026-09-13 卡死事故的结构性修复）：

  旧实现把卡片状态散在 adapter 的三个结构里（``_chat_card_map`` /
  ``_card_seal_timers`` / ``_card_patch_locks``），由 ``send()``、密封定时器、
  入站处理、逐块合并分支**四条路径**各自改写，而且 PATCH 调用**没有任何超时**。
  于是一次挂起的 PATCH 会永久占住锁，连"发回复"这条路也一起堵死 —— 表现为
  「进程活着、日志停住、飞书不回话」。

这里的三条硬约束：

  1. **单写者**：所有状态变更都在 ``self._lock`` 内完成，外部只通过
     ``handle_text`` / ``handle_tool`` / ``handle_closing`` / ``update_notice``
     / ``seal`` 提交"意图"。
  2. **每次 PATCH 带 seq**：状态推进可推理、可对账；密封定时器只在 seq 未变时才生效。
  3. **失败绝不外抛**：PATCH 失败 → 换卡续写（内容不丢）；再失败 → 返回失败结果，
     由 adapter 决定降级（退回官方纯文本发送）。任何情况下都不许"等下去"。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, NamedTuple, Optional, Protocol, Sequence

from . import card_render as R

log = logging.getLogger(__name__)


class CardTransport(Protocol):
    """卡片 I/O 抽象（由 adapter 实现；单测里用假实现替换）。"""

    async def create_card(self, chat_id: str, payload: str) -> Any: ...

    async def patch_card(self, message_id: str, payload: str) -> Any: ...


class CardOutcome(NamedTuple):
    ok: bool
    message_id: Optional[str]
    action: str  # created | appended | rotated | sealed | noop | failed
    error: Optional[str] = None


@dataclass
class CardState:
    """一张卡的完整状态（快照可读，写入只允许 session 内部）。"""

    message_id: Optional[str] = None
    blocks: List[dict] = field(default_factory=list)
    append_count: int = 0
    status: str = R.STATUS_RUNNING
    seq: int = 0
    rotations: int = 0
    failures: int = 0
    write_attempts: int = 0
    last_error: Optional[str] = None
    #: 最后一次写入卡片的正文文本（供 edit 的幂等/替换语义判定，见 replace_last_text）
    last_text: Optional[str] = None
    #: 本会话历史上出现过的正文文本（跨卡去重：同一条文本不再重复写进新卡）
    written_texts: List[str] = field(default_factory=list)
    #: **已成功上卡的块数**（PATCH/CREATE 成功时更新）。
    #: 换卡时 `blocks[committed_count:]` 就是"还没上卡的增量"——只有这部分需要带到新卡，
    #: 已上卡的部分留在旧卡上（旧卡封口不退内容），这样同一段内容永不出现两次。
    committed_count: int = 0
    #: **工具阶段是否已经结束**（被一条"非汇报"的正文划上句号）。
    #: 用于判定上游丢表头后的裸 ``` 代码块到底是"工具命令"还是"用户要的代码回复"：
    #:   · 中途汇报（上游 `_interim_send` 标记）**不算**结束 —— 汇报之后工具还会继续跑；
    #:   · 真正的正文回复（无该标记）才算结束。
    #: 为什么不用"最后一块是不是工具面板"：汇报会把最后一块变成正文块，
    #: 于是汇报之后的工具命令被判成正文、该折叠的不折叠（2026-09-15 CM 反馈）。
    tool_phase_closed: bool = False
    #: 已发出的"空闲提示"次数（长时间无进展时递增；用于算"已 N 分钟无新动作"并封顶 PATCH 次数）
    idle_notices: int = 0

    def snapshot(self) -> "CardState":
        return CardState(
            message_id=self.message_id,
            blocks=list(self.blocks),
            append_count=self.append_count,
            status=self.status,
            seq=self.seq,
            rotations=self.rotations,
            failures=self.failures,
            write_attempts=self.write_attempts,
            last_error=self.last_error,
            last_text=self.last_text,
            written_texts=list(self.written_texts),
            committed_count=self.committed_count,
            tool_phase_closed=self.tool_phase_closed,
            idle_notices=self.idle_notices,
        )

    def rotate_budget(self) -> int:
        """本会话还允许换卡多少次（与写入次数成比例，见 ROTATE_BUDGET_DIVISOR）。"""
        return max(R.CONSECUTIVE_FAILURE_LIMIT, self.write_attempts // R.ROTATE_BUDGET_DIVISOR)


def _ok(result: Any) -> bool:
    return bool(getattr(result, "success", False))


def _err(result: Any, default: str = "card call failed") -> str:
    return str(getattr(result, "error", None) or default)


class ChatCardSession:
    """一个聊天会话对应一张"当前卡"。"""

    def __init__(
        self,
        chat_id: str,
        transport: CardTransport,
        *,
        idle_delay: float = R.CARD_IDLE_DELAY,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.chat_id = chat_id
        self._t = transport
        self._idle_delay = float(idle_delay)
        self._log = logger or log
        self._lock = asyncio.Lock()
        self._state = CardState()
        self._seal_task: Optional[asyncio.Task] = None
        self._seal_seq: Optional[int] = None

    # ------------------------------------------------------------------ 只读

    @property
    def message_id(self) -> Optional[str]:
        return self._state.message_id

    def snapshot(self) -> CardState:
        return self._state.snapshot()

    def _describe(self) -> str:
        st = self._state
        return (
            f"chat={self.chat_id} card={st.message_id} seq={st.seq} "
            f"appends={st.append_count} blocks={len(st.blocks)} status={st.status}"
        )

    # ------------------------------------------------------------ 对外动作

    def reset(self) -> None:
        """入站新消息（新的一轮）→ 放弃当前卡，下一轮发新卡。

        旧卡如果还停在"运行中/回复中"，补一次「✅ 完成」PATCH 收尾
        （fire-and-forget，不阻塞入站路径）。
        """
        self._cancel_idle()
        stale = self._state
        self._state = CardState()
        if stale.message_id and stale.status != R.STATUS_SEALED:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            payload = R.card_json(stale.blocks, R.STATUS_SEALED)
            task = loop.create_task(self._best_effort_patch(stale.message_id, payload))
            self._track_background(task)

    async def handle_text(self, text: str, *, interim: bool = False) -> CardOutcome:
        """正文：工具框收起 → 追加消息方块 → 状态「回复中」→ 重置密封定时器。

        **幂等**：同一条文本已经在本次会话里写过（`written_texts`）→ 直接返回成功、不重复追加。
        为什么：上游会在同一回合里用 `send()` + `edit_message()` 各推一次同样内容
        （对账/刷新路径，上游注释自己写着 "a plain send here would duplicate it"），
        旧实现无条件追加 → 同一段文本在卡片里出现两次（2026-09-15 CM 反馈，探针 I11 复现）。
        """
        clean = (text or "").strip()
        async with self._lock:
            if clean and clean in self._state.written_texts:
                self._log.info("[feishu-card] duplicate text skipped (already on this card) | %s", self._describe())
                return CardOutcome(True, self._state.message_id, "noop")
            self._cancel_idle()
            await self._rotate_if_needed_locked()
            self._drop_notices_locked()  # 正文来了 → 心跳通知已过时
            for block in self._state.blocks:
                if block.get("type") == "tools":
                    block["expanded"] = False
                    block.pop("tool_pending", None)
            self._state.blocks.append({"type": "message", "text": text})
            self._state.append_count += 1
            self._state.status = R.STATUS_TYPING
            if not interim:
                # 只有"非汇报"的正文才算给工具阶段划上句号（见 CardState.tool_phase_closed）
                self._state.tool_phase_closed = True
            if clean:
                self._state.last_text = clean
                self._state.written_texts.append(clean)
            outcome = await self._write_locked("appended")
            if outcome.ok:
                self._schedule_idle()
            return outcome

    def saw_tools(self) -> bool:
        """本轮到目前为止**是否出现过工具面板**。

        用于判定上游丢表头后的裸 ``` 代码块：
          · 本轮已经跑过工具（有 tools 块）→ 这个裸块是连续 terminal 的命令 → 该折叠；
          · 本轮一个工具都没跑过 → 它更可能是"用户要的一段代码"回复 → 保持正文可见。
        为什么不看"最后一块"（旧实现的 `in_tool_context`）：中途汇报会把最后一块变成正文块，
        于是汇报之后到来的工具命令被判成正文、**该折叠的不折叠**（2026-09-15 CM 反馈）。
        "本轮有没有出现过工具"不会被汇报打断，是稳定的判据。

        与 `tool_phase_closed` 配合使用：**本轮出现过工具** 且 **工具阶段尚未被真正的正文结束**
        → 裸块是工具命令。这样两种场景都对：
          · 汇报（interim）→ 工具裸块 → 该折叠（汇报不算结束）；
          · 工具跑完 → 真正的正文回复 → 之后若出现整块代码 → 是"用户要的代码"，保持可见。
        """
        return any(b.get("type") == "tools" for b in self._state.blocks)

    def tool_phase_closed(self) -> bool:
        """工具阶段是否已被一条**真正的正文回复**（非中途汇报）划上句号。"""
        return bool(self._state.tool_phase_closed)

    def in_tool_context(self) -> bool:
        """当前是否仍在"工具流"里（跳过瞬时通知后，最后一块是未结束的工具面板）。

        用于判定上游连续 terminal 调用丢 header 后的**裸 ``` 代码块**：
        它在工具流中就是工具命令（该折叠），在正文里则是普通代码块（保持原样）。
        单看文本形态无法区分（`card_render.is_bare_fence` 只给候选），必须结合上下文。

        **必须跳过 `notice` 块**（2026-09-15 定位到的"只有第一条折叠"根因）：
        长任务心跳「已持续工作 N 分钟」经 `update_notice` 落成最后一个 notice 块；
        若它紧跟在一批工具命令之后，则下一次工具命令到来时 `blocks[-1]` 是 notice →
        旧实现返回 False → 裸代码块被判成正文 → **摊在卡片上不折叠**。
        notice 是瞬时覆盖层（`_drop_notices_locked` 会在真实内容到达时清掉），不代表工具流结束。
        """
        for block in reversed(self._state.blocks):
            kind = block.get("type")
            if kind == "notice":
                continue
            return kind == "tools" and bool(block.get("tool_pending"))
        return False

    async def replace_last_text(self, text: str) -> CardOutcome:
        """把**最后一块正文**替换成 `text`（上游 edit 的"原地替换"语义）。

        仅当最后一块是 message 且 `text` 以它开头（即新文本是旧文本的超集，典型是
        "回复完了再补一段"）时才替换；否则退回 `handle_text` 追加（保守，防丢内容）。
        """
        clean = (text or "").strip()
        async with self._lock:
            blocks = self._state.blocks
            last = blocks[-1] if blocks else None
            if not (last and last.get("type") == "message"):
                return await self.handle_text(text)
            old = str(last.get("text") or "").strip()
            if not clean or not old or not clean.startswith(old):
                return await self.handle_text(text)
            last["text"] = text
            self._state.last_text = clean
            if clean not in self._state.written_texts:
                self._state.written_texts.append(clean)
            self._cancel_idle()
            outcome = await self._write_locked("replaced")
            if outcome.ok:
                self._schedule_idle()
            return outcome

    async def handle_tool(self, text: str) -> CardOutcome:
        """工具进度：与上一条未完成的工具块合并 → 状态「运行中」→ 取消密封定时器。

        块结构照抄 ZCode：`{type:"tools", summaries:[...]}` —— 每次工具调用**追加一条摘要**，
        而不是把原始命令全文拼进正文。展开状态**不在建块时写死**：由渲染层按卡片状态决定
        （ZCode：`expanded ?? (status === "running")` → 运行中展开、完成/收尾后折叠）。
        """
        async with self._lock:
            self._cancel_idle()
            await self._rotate_if_needed_locked()
            self._drop_notices_locked()  # 工具进度来了 → 心跳通知已过时
            blocks = self._state.blocks
            if blocks and blocks[-1].get("type") == "tools" and blocks[-1].get("tool_pending"):
                blocks[-1].setdefault("summaries", []).append(text)
            else:
                blocks.append({"type": "tools", "summaries": [text], "tool_pending": True})
            self._state.append_count += 1
            self._state.status = R.STATUS_RUNNING
            self._state.tool_phase_closed = False   # 又有工具在跑 → 工具阶段重新打开
            return await self._write_locked("appended")

    async def handle_closing(self) -> CardOutcome:
        """收尾消息（记忆保存等）：不改内容、不打断状态行，只重置密封定时器。"""
        async with self._lock:
            if not self._state.message_id:
                return CardOutcome(True, None, "noop")
            self._schedule_idle()
            return CardOutcome(True, self._state.message_id, "noop")

    async def update_notice(self, text: str) -> CardOutcome:
        """状态类通知（长任务心跳等）：原地更新卡片里的状态行，**绝不动已有内容**。

        Hermes 的长任务心跳逻辑是：**首次** ``send()`` 通知，之后每 N 分钟
        ``edit_message(chat_id, 同一条消息 id, "已持续工作 N 分钟…")`` 原地更新——
        而那条消息 id 就是本会话的卡片 id。

        旧实现把 ``edit_message`` 当成"整卡替换"，于是心跳一来就把已累积的方块全部清空，
        之后只追加新内容（2026-09-14 CM 反馈的"内容被吞掉、而且不再恢复"）。
        正确语义：把通知放成一个 ``notice`` 方块——重复通知**原地替换**（不堆叠），
        出正文/工具/收尾时自动丢弃。
        """
        async with self._lock:
            self._cancel_idle()  # 心跳说明还在干活，别密封
            blocks = self._state.blocks
            if blocks and blocks[-1].get("type") == "notice":
                blocks[-1]["text"] = text  # 原地更新，不新增方块
            else:
                self._drop_notices_locked()
                blocks.append({"type": "notice", "text": text})
                self._state.append_count += 1
            self._state.status = R.STATUS_RUNNING
            return await self._write_locked("appended")

    def _drop_notices_locked(self) -> None:
        """丢掉瞬态 notice 方块（有真正内容进来时调用）。"""
        blocks = self._state.blocks
        kept = [b for b in blocks if b.get("type") != "notice"]
        if len(kept) != len(blocks):
            self._state.blocks = kept

    async def seal(self, *, reason: str = "idle") -> CardOutcome:
        """把当前卡标成「✅ 完成」。**只允许由真实回合结束信号调用**：
        上游最终回复的 `metadata["notify"]=True`、`edit_message(finalize=True)`、
        或新一轮入站复位（旧卡收尾）。定时器**不得**调用它。
        """
        async with self._lock:
            self._cancel_idle()
            if not self._state.message_id:
                return CardOutcome(True, None, "noop")
            if self._state.status == R.STATUS_SEALED:
                return CardOutcome(True, self._state.message_id, "noop")
            self._drop_notices_locked()  # 收尾不留心跳行
            self._state.status = R.STATUS_SEALED
            outcome = await self._flush_locked("sealed")
            self._log.info("[feishu-card] sealed (%s) | %s", reason, self._describe())
            return outcome

    def cancel_pending(self) -> None:
        """断开连接前清掉定时器（避免重连后残留任务）。"""
        self._cancel_idle()

    # ------------------------------------------------------------ 内部实现

    async def _rotate_locked(self, reason: str) -> bool:
        """**统一的换卡**：旧卡封口（保持它已上卡的内容）+ 新卡 = 提示行 + 未上卡的增量。

        关键记账是 `committed_count`（已成功上卡的块数）：
          · 旧卡封口时只 PATCH `blocks[:committed_count]` —— 即它**实际已经有**的内容，
            不追加任何新内容（否则新卡再带一份就重复了）；
          · 新卡带 `blocks[committed_count:]` —— 只有"还没上卡的增量"需要搬过去，
            已上卡的留在旧卡上；同一段内容因此**永远只出现一次**。

        这同时覆盖两种场景（2026-09-15 CM 反馈"同一段东西分两个卡片发、内容重复"）：
          · 容量换卡：committed == 全部 → 增量空 → 新卡只有提示行；
          · 失败换卡：增量 = 那次没写成功的内容 → 搬进新卡（不丢），旧卡不留副本（不重）。
        """
        st = self._state
        old_id = st.message_id
        committed = list(st.blocks)[: st.committed_count]
        pending = list(st.blocks)[st.committed_count :]
        st.message_id = None
        if old_id:
            sealed_blocks = committed or [{"type": "message", "text": R.MOVED_NOTICE}]
            await self._best_effort_patch(old_id, R.card_json(sealed_blocks, R.STATUS_SEALED))
        st.blocks = [{"type": "message", "text": R.CONTINUATION_NOTICE}] + pending
        st.append_count = len(st.blocks)
        st.committed_count = 0
        st.last_text = None
        st.rotations += 1
        self._log.info(
            "[feishu-card] rotated card (%s): kept %d block(s) on the old card, "
            "carried %d pending block(s) to the new one | %s",
            reason,
            len(committed),
            len(pending),
            self._describe(),
        )
        return True

    async def _rotate_if_needed_locked(self) -> None:
        """容量超限 → 换新卡（判断基于**追加前**的状态）。"""
        st = self._state
        if not st.message_id or not R.exceeds_limits(st.blocks, st.append_count):
            return
        if st.committed_count == 0:
            # **换卡循环防护**（2026-09-15 01:25 实机抓到的病灶）：
            # 换卡会把"还没上卡的增量"整份搬进新卡。若搬过去的内容本身就超限，
            # 新卡一出生就超限 → 立刻又触发换卡 → 把同一份内容再搬一次 → 无限循环。
            # 实测：45 秒内连换 8 张卡，最后以
            # `[230099] … ErrCode: 11310; ErrMsg: card table number over limit` 建卡失败收场。
            # `committed_count == 0` 正好刻画"自从上次换卡以来还没有任何内容成功写上去"，
            # 此时再换一次**不解决任何问题**（搬的还是同一份）→ 直接不换，
            # 交给出站前的元素折叠（`fold_elements`）兜底。
            self._log.warning(
                "[feishu-card] card over limit but nothing committed since the last rotation "
                "(%s); rotating again would carry the SAME payload → skipping rotation | %s",
                R.limit_reason(st.blocks, st.append_count),
                self._describe(),
            )
            return
        if st.rotations >= min(st.rotate_budget(), R.MAX_ROTATIONS):
            # 换卡已超配额：继续写当前卡（出站前有元素折叠兜底），避免变成换卡机器
            self._log.warning(
                "[feishu-card] card at capacity but rotation budget exhausted (rotations=%d budget=%d); "
                "keeping the current card | %s",
                st.rotations,
                st.rotate_budget(),
                self._describe(),
            )
            return
        self._log.info(
            "[feishu-card] card at capacity (triggered by %s | appends=%d blocks=%d bytes=%d tables=%d); "
            "rotating | %s",
            R.limit_reason(st.blocks, st.append_count),
            st.append_count,
            len(st.blocks),
            R.blocks_bytes(st.blocks),
            R.card_tables(st.blocks),
            self._describe(),
        )
        await self._rotate_locked("capacity")

    async def _write_locked(self, action: str) -> CardOutcome:
        """写当前状态：没有卡就建卡（内容随卡一起发，一次调用），有卡就 PATCH。"""
        st = self._state
        if st.failures >= R.CONSECUTIVE_FAILURE_LIMIT:
            # 连续失败已达上限：**不再建卡**，让上层降级成纯文本（避免刷屏式建卡）
            self._log.warning(
                "[feishu-card] %d consecutive card failures — giving up on cards for this turn "
                "(caller will fall back) | %s",
                st.failures,
                self._describe(),
            )
            return self._failed()
        if not st.message_id:
            if await self._create_locked():
                return CardOutcome(True, self._state.message_id, "created")
            return self._failed()
        return await self._flush_locked(action)

    async def _create_locked(self) -> bool:
        st = self._state
        st.write_attempts += 1
        st.seq += 1
        payload = R.card_json(st.blocks, st.status)
        try:
            result = await self._t.create_card(self.chat_id, payload)
        except Exception as exc:  # transport 不应抛，但绝不因此卡死
            st.failures += 1
            st.last_error = f"{type(exc).__name__}: {exc}"
            self._log.warning("[feishu-card] create_card raised: %s", st.last_error)
            return False
        if _ok(result):
            st.message_id = getattr(result, "message_id", None) or st.message_id
            st.committed_count = len(st.blocks)   # 这些块已成功上卡
            # 注意：**不**在这里重置 st.failures。failures 语义是"连续 PATCH 失败次数"，
            # 只应由一次成功的 PATCH 归零。若在建卡成功时归零，"PATCH 失败 → 换卡 → 建卡成功"
            # 会让计数永远回到 0 → 连续失败上限失效 → 持续失败时无限建卡
            # （2026-09-15 实测：15 次失败注入建了 15 张卡）。
            st.last_error = None
            self._log.info("[feishu-card] card created | %s", self._describe())
            return True
        st.failures += 1
        st.last_error = _err(result, "create failed")
        self._log.warning("[feishu-card] create_card failed: %s", st.last_error)
        return False

    async def _flush_locked(self, action: str) -> CardOutcome:
        """把当前状态 PATCH 到卡片；失败则换卡续写（内容不丢）。"""
        st = self._state
        if not st.message_id:
            return self._failed()
        st.seq += 1
        payload = R.card_json(st.blocks, st.status)
        try:
            result = await self._t.patch_card(st.message_id, payload)
        except Exception as exc:
            result = None
            st.last_error = f"{type(exc).__name__}: {exc}"
        if result is not None and _ok(result):
            st.failures = 0
            st.last_error = None
            st.committed_count = len(st.blocks)   # 这些块已成功上卡
            return CardOutcome(True, st.message_id, action)

        st.write_attempts += 1
        st.failures += 1
        st.last_error = st.last_error or _err(result, "patch failed")
        over_budget = st.rotations >= min(st.rotate_budget(), R.MAX_ROTATIONS)
        if st.failures >= R.CONSECUTIVE_FAILURE_LIMIT or over_budget:
            # 连续失败 / 换卡超出配额 → 停止换卡（避免"每条消息冒一张新卡"的刷屏循环）
            self._log.warning(
                "[feishu-card] patch failed, stopping rotation (failures=%d rotations=%d budget=%d): %s | %s",
                st.failures,
                st.rotations,
                st.rotate_budget(),
                st.last_error,
                self._describe(),
            )
            return self._failed()
        self._log.warning(
            "[feishu-card] patch failed (%s); rotating card to keep the pending content | %s",
            st.last_error,
            self._describe(),
        )
        # 统一换卡：旧卡封口（只保留它**已上卡**的内容，不加副本）+ 新卡带未上卡的增量。
        # 旧实现把完整 blocks 同时灌给旧卡与新卡 → 两张卡内容几乎一样（CM 反馈的症状）。
        await self._rotate_locked("failure")
        if await self._create_locked():
            return CardOutcome(True, st.message_id, "rotated")
        return self._failed()

    async def _best_effort_patch(self, message_id: Optional[str], payload: str) -> None:
        if not message_id:
            return
        try:
            await self._t.patch_card(message_id, payload)
        except Exception as exc:  # 收尾失败不影响主流程
            self._log.debug("[feishu-card] seal patch ignored: %s", exc)

    def _failed(self) -> CardOutcome:
        st = self._state
        return CardOutcome(False, st.message_id, "failed", st.last_error)

    # ------------------------------------------------------------ 密封定时器

    def _schedule_idle(self) -> None:
        self._cancel_idle()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._seal_seq = self._state.seq
        task = loop.create_task(self._idle_after_delay())
        self._seal_task = task
        self._track_background(task)

    def _cancel_idle(self) -> None:
        task = self._seal_task
        self._seal_task = None
        self._seal_seq = None
        if task and not task.done():
            task.cancel()

    async def _idle_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._idle_delay)
        except asyncio.CancelledError:
            return
        # 期间有新活动（seq 变了）→ 不处理，交给下一次调度
        if self._seal_seq is None or self._seal_seq != self._state.seq:
            return
        st = self._state
        if st.status == R.STATUS_SEALED or not st.message_id:
            return
        # **绝不在这里宣称"完成"**（那是假状态：agent 中途思考超过延迟就会误报"已完成"，
        # 而长任务心跳又会让它永远显示"回复中"）。只写一条诚实的空闲提示：
        # 说清"多久没动静了"，让 CM 一眼看出后台其实已经不动了（CM 2026-09-16 需求）。
        st.idle_notices += 1
        minutes = (st.idle_notices * self._idle_delay) / 60.0
        await self.update_notice(R.idle_notice_text(minutes))
        if st.idle_notices < R.CARD_IDLE_NOTICE_MAX:
            self._schedule_idle()   # 继续盯着，让"多久没动"保持真实

    def _track_background(self, task: asyncio.Task) -> None:
        """把后台任务登记到 adapter 的集合里，断开连接时统一取消。"""
        sink = getattr(self._t, "track_card_task", None)
        if callable(sink):
            try:
                sink(task)
            except Exception:
                pass
