"""卡片出站适配器 —— 官方 FeishuAdapter 的子类，**只**覆盖出站卡片链路。

继承白拿：入站 WS/重连、消息批处理、媒体下载、webhook、审批与卡片回调、reactions…
覆盖三处：
  1. ``send`` / ``edit_message``  → 走卡片会话（原地 PATCH 更新）
  2. ``create_card`` / ``patch_card`` → 卡片 I/O，**带硬超时**（本模块的核心修复）
  3. ``_dispatch_inbound_event`` → 新一轮消息时给卡片状态机复位

超时为什么关键：官方/旧 fork 的 ``_run_blocking`` 把 lark SDK 丢进线程池后无限等待。
2026-09-13 的事故里，一次挂起的卡片 PATCH 永久占住了锁，连"发回复"这条路也被堵死，
最终表现为"进程活着、日志停住、飞书不回话"。这里给卡片调用加 ``asyncio.wait_for``，
超时即判失败 → 换卡续写 → 再失败就退回官方纯文本发送，绝不允许"等下去"。

注意：超时只包**卡片调用**（create/patch），不动上传/媒体等可能合法耗时较长的调用。
线程池里被放弃的那次调用无法强杀，会在 max_workers 用尽前一直占着线程（有上限，不会拖垮进程）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

from gateway.platforms.base import SendResult

from . import card_render as R
from .card_plugin import load_upstream_module
from .card_session import ChatCardSession

log = logging.getLogger(__name__)

_UPSTREAM = load_upstream_module()
FeishuAdapter = _UPSTREAM.FeishuAdapter

#: 单次卡片 API 调用（create/patch）的硬超时（秒）
DEFAULT_CARD_TIMEOUT = float(os.getenv("HERMES_FEISHU_CARD_TIMEOUT") or 15.0)


# ---------------------------------------------------------------- 卡死自愈（方案 A）
# 现象：**收信后回合不派发** —— 网关认为会话"还在忙"（运行哨兵泄漏），之后每一条消息
# 都被收下、组包、回一张"收到"卡，却永远进不到派发（日志里看不到
# `gateway.run: inbound message: platform=`）。上游**有**清理机制，但门槛是"空闲 30 分钟"
# （`HERMES_AGENT_TIMEOUT` 默认 1800s），报不出活动时最坏要 `max(timeout*10, 7200)` = **5 小时**
# —— 实测卡 15~40 分钟时永远等不到（2026-09-16 一天内 H-FU 两次、FU_2 一次）。
#
# 上游**自带**一条"活着的网关"自愈路径（`gateway/session_lifecycle.py:60`
# `_is_session_ended_in_db`，文档串写明是给 #54878/#99106 设计的）：
#   路由时若发现该会话在 state.db 里 `end_reason` 非空（= 已结束）→ 丢弃陈旧槽位、重建会话。
# 我们**只做一件事**：确认卡死后，把那条会话行的 `end_reason` 标上 → 下一条消息即自愈。
# 不碰上游代码，`hermes update` 也不会冲掉。
#
# 代价（必须知情）：自愈会**新建会话** → 该聊天丢失连续上下文（旧会话历史仍在库里）。
# 但对比"机器人一直不回"，这是可接受的；且仅在该聊天确认卡死时触发。
# 关掉它的开关：环境变量 `HERMES_FEISHU_STUCK_HEAL=0`。
STUCK_HEAL_ENABLED = str(os.getenv("HERMES_FEISHU_STUCK_HEAL", "1")).strip().lower() not in {"0", "off", "false", "no"}
#: 上一条消息之后多久没有任何卡片活动，就判定卡死（> 心跳 180s，避免误判正常长回合）
STUCK_HEAL_AFTER_SEC = float(os.getenv("HERMES_FEISHU_STUCK_AFTER") or 300.0)
_LAST_INBOUND: Dict[str, float] = {}
_LAST_ACTIVITY: Dict[str, float] = {}
_STUCK_HEALED: Dict[str, float] = {}


def _candidate_state_dbs() -> List[str]:
    """本机所有 profile 的 state.db 路径（default + profiles/*）。"""
    import glob
    home = os.getenv("HERMES_HOME") or os.path.join(
        os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "hermes")
    out = [os.path.join(home, "state.db")]
    out += sorted(glob.glob(os.path.join(home, "profiles", "*", "state.db")))
    return [p for p in out if os.path.exists(p)]


def _mark_session_ended(chat_id: str, reason: str) -> bool:
    """把该聊天的会话行标成已结束（**只写 end_reason 一个字段**）。

    上游 `_is_session_ended_in_db` 只认 `end_reason` 非空，所以这是最小写入面。
    找不到 / 已标记 / 任何异常 → 返回 False，绝不抛（不允许影响消息处理）。
    """
    import sqlite3
    key = f"agent:main:feishu:dm:{chat_id}"
    for db in _candidate_state_dbs():
        try:
            con = sqlite3.connect(db, timeout=3.0)
            try:
                row = con.execute(
                    "SELECT id FROM sessions WHERE session_key=? AND end_reason IS NULL "
                    "ORDER BY last_activity_at DESC LIMIT 1", (key,)).fetchone()
                if not row:
                    continue
                con.execute("UPDATE sessions SET end_reason=? WHERE id=?", (reason, row[0]))
                con.commit()
                log.warning("[feishu-card] 卡死自愈：已把会话标记为结束 chat=%s session=%s "
                            "（下一条消息将由上游路由自愈重建）", chat_id, row[0])
                return True
            finally:
                con.close()
        except Exception as exc:
            log.debug("[feishu-card] stuck-heal db %s skipped: %s", db, exc)
    return False


class CardCallTimeout(Exception):
    """飞书卡片 API 调用超过硬超时。"""


def _card_mode_enabled() -> bool:
    """紧急开关：HERMES_FEISHU_CARD_MODE=0/off/false → 完全退回官方行为。"""
    raw = str(os.getenv("HERMES_FEISHU_CARD_MODE", "1")).strip().lower()
    return raw not in {"0", "off", "false", "no"}


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _is_english_reasoning(text: str) -> bool:
    """这段是否"纯英文的内心推理"。

    CM 2026-09-15 的需求：**要**中文的阶段汇报（"① 已落盘…""会话已恢复…"这类，
    他可以据此判断 agent 有没有跑偏），**不要**英文的内心独白
    （`Mystery solved — a sibling cron job…`、`Now writing the run summary…`）。
    两类都走 `interim_assistant_messages` 通道，上游没有区分开关 → 在插件层按语言过滤。

    口径刻意保守（宁可多显示、不可少显示）：**完全没有汉字**且拉丁字母 ≥15 才算英文推理；
    中英混排、含一个汉字的都照常显示（实测样本里唯一"漏过"的是一条夹了中文引号的英文句）。
    """
    t = str(text or "")
    return not _HAN_RE.search(t) and len(_LATIN_RE.findall(t)) >= 15


def _skip_as_interim_reasoning(text: str, metadata: Optional[Dict[str, Any]]) -> bool:
    """中途叙述（上游标记 `metadata["_interim_send"]`）里的英文推理 → 静默不展示。

    只对该标记生效：**最终回复没有这个标记，永远显示**，不受影响。
    （标记来源：`gateway/stream_consumer_fallback.py:330` 给中途叙述加 `_interim_send=True`。）
    """
    if not isinstance(metadata, dict) or not metadata.get("_interim_send"):
        return False
    return _is_english_reasoning(text)


class CardFeishuAdapter(FeishuAdapter):
    """飞书适配器 + 卡片会话层。"""

    CARD_CALL_TIMEOUT = DEFAULT_CARD_TIMEOUT

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._card_sessions: Dict[str, ChatCardSession] = {}
        self._card_tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 工具

    async def _with_deadline(self, coro: Any, *, what: str = "card call", timeout: Optional[float] = None) -> Any:
        limit = float(self.CARD_CALL_TIMEOUT if timeout is None else timeout)
        try:
            return await asyncio.wait_for(coro, timeout=limit)
        except asyncio.TimeoutError as exc:  # noqa: UP041 - 3.11 别名
            raise CardCallTimeout(f"{what} exceeded {limit:.0f}s") from exc

    def track_card_task(self, task: asyncio.Task) -> None:
        """会话把后台任务（密封定时器）登记到这里，断连时统一取消。"""
        self._card_tasks.add(task)
        task.add_done_callback(self._card_tasks.discard)

    def _session(self, chat_id: str) -> ChatCardSession:
        session = self._card_sessions.get(chat_id)
        if session is None:
            session = ChatCardSession(chat_id, transport=self, logger=log)
            self._card_sessions[chat_id] = session
        return session

    # ------------------------------------------------- 卡片 I/O（transport）

    async def create_card(self, chat_id: str, payload: str) -> SendResult:
        """新建一张卡片消息（带超时）。"""
        try:
            response = await self._with_deadline(
                self._feishu_send_with_retry(
                    chat_id=chat_id,
                    msg_type="interactive",
                    payload=payload,
                    reply_to=None,
                    metadata=None,
                ),
                what="card create",
            )
        except CardCallTimeout as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
        result = self._finalize_send_result(response, "send failed")
        return result

    async def patch_card(self, message_id: str, payload: str) -> SendResult:
        """原地更新卡片（PATCH；带超时）。"""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        try:
            from lark_oapi.api.im.v1.model.patch_message_request import PatchMessageRequest
            from lark_oapi.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody

            body = PatchMessageRequestBody.builder().content(payload).build()
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = await self._with_deadline(
                self._run_blocking(self._client.im.v1.message.patch, request),
                what="card patch",
            )
        except CardCallTimeout as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
        result = self._finalize_send_result(response, "patch failed")
        if result.success:
            result.message_id = message_id
        return result

    # -------------------------------------------------------------- 出站覆盖

    @staticmethod
    def _refine_kind(kind: R.MsgKind, formatted: str, session: Any, *, interim: bool = False) -> R.MsgKind:
        """把"看起来像正文、其实是工具"的文本纠正为 TOOL。

        场景：上游连续 terminal 调用会**刻意丢掉重复表头**（`run_turn_runner.py:239`：
        `header = "" if last_was_terminal_block else f"{emoji} {tool_name}\\n"`），
        第 2 条起的工具进度就是**裸 fenced 代码块**，没有任何 emoji 可识别 →
        `classify()` 只能判成 TEXT → 落进正文块 → 而正文里的短代码块**按设计不折叠**
        → 用户看到"只有第一条工具折叠、其余全摊在外面"。

        **判据（2026-09-15 第二次修正）**：用上游的可靠标记 + 本轮是否出现过工具，
        **不再看"最后一块是不是工具"**。
        旧实现要求"会话最后一块仍是未结束的工具面板"（`in_tool_context()`），
        但中途汇报会把最后一块变成正文块 —— 于是汇报之后到来的工具命令又被判成正文、
        **该折叠的不折叠**（CM 反馈的"修来修去越修越坏"）。这条依赖已删除。

        现在的规则：
          · `interim=True`（上游 `_interim_send` 标记 = 给用户看的汇报）→ 永远不是工具；
          · 整条消息就是一个 fenced 块 **且本轮跑过工具、且工具阶段还没被真正的正文结束**
            → 是（连续 terminal 丢表头的）工具命令，该折叠；
          · 否则（本轮没跑过工具，或工具阶段已被真正的回复结束）→ 保持正文可见，
            这覆盖"用户要一段代码，回复就是一整块代码"的场景。
        """
        if kind is not R.MsgKind.TEXT:
            return kind
        if interim:
            return kind
        if (
            R.is_bare_fence(formatted)
            and session is not None
            and session.saw_tools()
            and not session.tool_phase_closed()
        ):
            return R.MsgKind.TOOL
        return kind

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """出站总入口：卡片链路，失败自动退回官方发送（内容不丢）。"""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        # 中途叙述里的英文内心推理 → 静默不展示（最终回复无 `_interim_send` 标记，永远显示）
        if _skip_as_interim_reasoning(content, metadata):
            log.debug("[feishu-card] english interim narration dropped: %s", str(content or "")[:60])
            return SendResult(success=True)
        if not _card_mode_enabled():
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

        formatted = self.format_message(content)
        session = None
        try:
            import time as _t; _LAST_ACTIVITY[chat_id] = _t.monotonic()
            session = self._session(chat_id)
            # 上游的可靠标记：`_interim_send=True` ⇒ 这条是"给用户看的中途汇报"，
            # **一定不是工具进度**。分类必须用它，不能靠猜文本形状（见 classify 的说明）。
            interim = bool(isinstance(metadata, dict) and metadata.get("_interim_send"))
            # 上游对**最终回复**打 notify=True（全仓唯一设置点 base.py:148）→ 这是可靠的"回合结束"信号，
            # 用它把卡片标成「✅ 完成」；中途汇报/工具进度都不带这个标记。
            is_final = bool(isinstance(metadata, dict) and metadata.get("notify"))
            kind = self._refine_kind(R.classify(formatted, interim=interim), formatted, session, interim=interim)
            if interim:
                # 可见性日志（[[教训检讨]] 015：没有可见性的修改就是猜）：
                # 中途阶段汇报是 CM 明确要求要看到的内容（docs/requirements.md R1），
                # 记下它被判成哪一类 —— 若被判成 TOOL 就会进折叠面板而不是正文，需要立刻发现。
                log.info(
                    "[feishu-card] interim narration | kind=%s | %s",
                    kind.value,
                    re.sub(r"\s+", " ", formatted)[:100],
                )
            if kind is R.MsgKind.CLOSING:
                outcome = await session.handle_closing()
            elif kind is R.MsgKind.TOOL:
                outcome = await session.handle_tool(formatted)
            else:
                outcome = None
                # 长回复先按字符切、再按卡片表格数上限切；所有块合并进同一张卡
                chunks = R.split_chunks_by_tables(
                    self.truncate_message(formatted, R.MAX_MESSAGE_LENGTH)
                )
                for index, chunk in enumerate(chunks):
                    outcome = await session.handle_text(chunk, interim=interim)
                    if not outcome.ok:
                        # 只补发**未入卡**的剩余块。
                        # 旧实现回退的是一整段 formatted → 已入卡的块被再发一遍
                        # （2026-09-15 实测：日志 `falling back to plain send` 58 次）。
                        remaining = "\n\n".join(chunks[index:])
                        log.warning(
                            "[feishu-card] card pipeline failed at chunk %d/%d (%s); "
                            "falling back to plain send for the REMAINING %d chunk(s)",
                            index + 1,
                            len(chunks),
                            getattr(outcome, "error", None) or "unknown",
                            len(chunks) - index,
                        )
                        return await super().send(
                            chat_id, remaining, reply_to=reply_to, metadata=metadata
                        )
            if outcome is not None and outcome.ok:
                if is_final:
                    # **真实的回合结束信号**（上游对最终回复打 `notify=True`，
                    # `gateway/platforms/base.py:148 _mark_notify_metadata` 是全仓唯一设置点）
                    # → 此刻才把卡片标成「✅ 完成」。此前用"8 秒静默"猜，实测会提前误报。
                    await session.seal(reason="final")
                return SendResult(success=True, message_id=outcome.message_id)
            log.warning(
                "[feishu-card] card pipeline failed (%s); falling back to plain send",
                getattr(outcome, "error", None) or "unknown",
            )
        except Exception as exc:  # 卡片链路任何异常都不许影响这一轮回复
            log.warning("[feishu-card] card pipeline raised: %s", exc, exc_info=True)
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """编辑消息：卡片走 PATCH（PUT 不接受 interactive，实测报 [230001]）。"""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        # 注意：**不**在这里做 interim 过滤 —— 中途叙述走上游的 `_send_commentary` → `send()`
        # （`stream_consumer_fallback.py:336`），而本方法签名没有 metadata，拿不到 `_interim_send` 标记。
        if not _card_mode_enabled():
            return await super().edit_message(chat_id, message_id, content, finalize=finalize)

        formatted = self.format_message(content)
        session = self._card_sessions.get(chat_id)
        try:
            import time as _t; _LAST_ACTIVITY[chat_id] = _t.monotonic()
            if session is not None and session.message_id == message_id:
                # 语义分流（2026-09-14 修"心跳吞内容"；2026-09-15 修"重复"）：
                # Hermes 的长任务心跳 = 首次 send() + 之后每 N 分钟 edit_message 同一条消息；
                # 而那条消息 id 就是本会话卡片 → 若按"整卡替换"处理，心跳一来就把已累积内容清空。
                # 另外：`tool_progress_grouping=accumulate` 的 profile 会用 edit_message
                # 原地更新**工具进度气泡**——这类文本必须继续进折叠面板，不能被当成状态行摊平。
                #   · 工具进度行（含连续 terminal 丢 header 的裸代码块）→ handle_tool（折叠面板）
                #   · finalize=True 或长/多行文本 → replace_last_text（**替换**语义，见下）
                #   · 其余短状态行（"已持续工作 N 分钟"）→ update_notice（原地更新、收尾消失）
                #
                # 为什么正文走 replace_last_text 而不是 handle_text：
                # 上游调 edit_message 的语义是"**原地替换/对账**"（上游源码注释：
                # "a plain send here would duplicate it"），旧实现无条件 append
                # → 同一段文本在卡片里出现两次（探针 I11 复现）。
                # replace_last_text 会在"新文本以旧文本开头"时就地替换，否则退回追加（保守）。
                text = formatted.strip()
                kind = self._refine_kind(R.classify(formatted), formatted, session)
                # finalize=True 是"这一轮结束了"的收尾编辑 → 一定是正文，优先判正文；
                # 其余情况下的裸 fenced 块 = 上游丢表头的连续 terminal 命令 → 必须折叠。
                # 注意顺序：`"\n" in text` 对裸代码块也成立，若不先判 TOOL，它会走
                # replace_last_text 落进正文块 → **不折叠**（CM 反馈的"该折叠的又没折叠"）。
                if kind is R.MsgKind.TOOL and not finalize:
                    outcome = await session.handle_tool(formatted)
                elif finalize or len(text) > 300 or "\n" in text:
                    outcome = await session.replace_last_text(formatted)
                else:
                    outcome = await session.update_notice(formatted)
                if outcome.ok:
                    if finalize:
                        await session.seal(reason="finalize")
                    return SendResult(success=True, message_id=outcome.message_id)
            # 非本会话当前卡（例如审批卡等）→ 直接按卡片 PATCH
            payload = R.card_json(
                [{"type": "message", "text": formatted}],
                R.STATUS_SEALED if finalize else R.STATUS_TYPING,
            )
            result = await self.patch_card(message_id, payload)
            if result.success:
                return result
        except Exception as exc:
            log.warning("[feishu-card] card edit failed: %s", exc)
        return await super().edit_message(chat_id, message_id, content, finalize=finalize)

    # ------------------------------------------------- 互动卡片（授权/追问）

    # 互问卡（授权/追问）发出时的硬超时。
    # 网关侧对审批卡只等 15s（超时按 ambiguous 继续，审批请求保持有效），等的是**我们**这个
    # 发送协程；旧实现这条协程没有截止时间 → 一旦卡住就"审批永远 pending、日志完全静默"
    # （2026-09-13 那次整进程卡死里最可疑的一环）。这里给它一个上限：超时即返回失败结果，
    # 网关会走 "BLOCKED: Failed to send approval request to user" 让 agent 正常收尾。
    # 注：超时≠卡没发出去（线程里的 HTTP 可能稍后完成），所以仍然按"互动已发生"收口卡片。
    INTERACTION_SEND_TIMEOUT = float(os.getenv("HERMES_FEISHU_APPROVAL_SEND_TIMEOUT") or 30.0)

    # 授权卡 / 追问卡是**插在会话中间的一条独立消息**（官方用 _feishu_send_with_retry 直发，
    # 不进卡片会话）。如果之后的内容继续 PATCH 它**上方**的旧卡，飞书聊天窗不会自动上滚，
    # 用户根本看不到新内容（2026-09-14 CM 反馈）。
    # 约定：发出互动卡 = 这一段的终点 → 旧卡封口（✅ 完成）、下一次输出开**新卡**。
    #
    # 形参用 *args/**kwargs 透传：以后 hermes update 给上游方法加参数也不会把我们顶崩
    # （这两个方法的第一位形参是 chat_id）。
    async def send_exec_approval(self, *args: Any, **kwargs: Any) -> SendResult:
        return await self._interaction_send(super().send_exec_approval, args, kwargs, "exec approval")

    async def send_update_prompt(self, *args: Any, **kwargs: Any) -> SendResult:
        return await self._interaction_send(super().send_update_prompt, args, kwargs, "update prompt")

    async def _interaction_send(
        self, super_call: Any, args: tuple, kwargs: Dict[str, Any], why: str
    ) -> SendResult:
        """发互动卡：带硬超时 + 无条件收口当前卡片会话。"""
        timeout = float(self.INTERACTION_SEND_TIMEOUT)
        try:
            result = await self._with_deadline(
                super_call(*args, **kwargs), what=why, timeout=timeout
            )
        except CardCallTimeout as exc:
            log.warning(
                "[feishu-card] %s 发送超时（%.0fs）→ 返回失败，审批不会静默悬着：%s",
                why,
                timeout,
                exc,
            )
            result = SendResult(success=False, error=f"{why} timed out after {timeout:.0f}s")
        except Exception as exc:  # 互动卡本身的任何异常都不许外抛
            log.warning("[feishu-card] %s 发送异常：%s", why, exc)
            result = SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
        # 成功或超时（可能晚到发出）都收口：互动之后的内容应开新卡
        self._start_new_card_after_interaction(args, kwargs, why)
        if getattr(result, "success", False):
            log.info(
                "[feishu-card] %s 已发出，等待用户点击（approvals.timeout 后按拒绝处理）", why
            )
        return result

    def _start_new_card_after_interaction(
        self, args: tuple, kwargs: Dict[str, Any], why: str
    ) -> None:
        """互动卡发出后收口当前卡片会话（旧卡封口，下一段内容换新卡）。"""
        try:
            chat_id = str(args[0]) if args else str(kwargs.get("chat_id") or "")
            if not chat_id:
                return
            session = self._card_sessions.pop(chat_id, None)
            if session is None or not session.message_id:
                return
            old_card = session.message_id
            session.reset()  # 取消失效定时器 + 尽力给旧卡 PATCH「✅ 完成」
            log.info(
                "[feishu-card] %s 已发出 → 当前卡收口，下一段内容将开新卡 | chat=%s old_card=%s",
                why,
                chat_id,
                old_card,
            )
        except Exception as exc:  # 任何异常都不许影响互动卡本身
            log.debug("[feishu-card] interaction reset skipped: %s", exc)

    # ------------------------------------------------------------ 入站/生命周期

    @staticmethod
    def _should_reset_card_for(event: Any) -> tuple:
        """入站事件是否该让卡片会话复位（= 下一段内容开新卡）？

        **合成事件不复位**：框架自己注入的 ``event.internal=True``（子代理/后台任务完成通知、
        wake、cron 回执等）属于同一次工作流，内容应继续累积在同一张卡上；否则一批子任务
        完成通知会各开一张新卡（2026-09-14 CM 反馈"子代理用的卡片不是同一个"）。
        只有真人消息才算"新一轮"。
        """
        if getattr(event, "internal", False):
            return False, ""
        chat_id = str(getattr(getattr(event, "source", None), "chat_id", "") or "")
        return bool(chat_id), chat_id

    def _heal_if_previous_turn_stuck(self, chat_id: str) -> None:
        """收信时自愈（方案 A）：上一条消息之后**卡片毫无活动** → 判定卡死 → 标记会话结束。

        上游路由发现 `end_reason` 非空就会丢弃陈旧槽位、重建会话（`session_lifecycle.py:60`）。
        判据：距上次收信 > `STUCK_HEAL_AFTER_SEC`，且这段时间内**没有任何卡片活动**
        （send/edit 都会刷新 `_LAST_ACTIVITY`；长任务心跳也是 send/edit → 正常长回合不会误判）。
        """
        if not STUCK_HEAL_ENABLED or not chat_id:
            return
        import time as _t
        now = _t.monotonic()
        prev = _LAST_INBOUND.get(chat_id)
        _LAST_INBOUND[chat_id] = now
        if prev is None:
            return
        silent = now - (_LAST_ACTIVITY.get(chat_id) or prev)
        if silent < STUCK_HEAL_AFTER_SEC:
            return
        if now - (_STUCK_HEALED.get(chat_id) or 0) < STUCK_HEAL_AFTER_SEC:
            return          # 刚治过，别重复
        _STUCK_HEALED[chat_id] = now
        log.warning("[feishu-card] 判定上一条消息卡死（静默 %.0f 秒无任何卡片活动）→ 触发自愈 | chat=%s",
                    silent, chat_id)
        _mark_session_ended(chat_id, "feishu_card_stuck_heal")

    async def _dispatch_inbound_event(self, event: Any) -> None:
        """新一轮（真人）消息：先给卡片会话复位（下一轮发新卡，旧卡收尾）。"""
        try:
            _ev_chat = getattr(getattr(event, "source", None), "chat_id", None) or ""
            if _ev_chat:
                self._heal_if_previous_turn_stuck(_ev_chat)
        except Exception as exc:
            log.debug("[feishu-card] stuck-heal check skipped: %s", exc)
        try:
            should_reset, chat_id = self._should_reset_card_for(event)
            if should_reset:
                session = self._card_sessions.pop(chat_id, None)
                if session is not None:
                    session.reset()
        except Exception as exc:
            log.debug("[feishu-card] session reset skipped: %s", exc)
        await super()._dispatch_inbound_event(event)

    async def disconnect(self) -> None:
        for session in list(self._card_sessions.values()):
            try:
                session.cancel_pending()
            except Exception:
                pass
        self._card_sessions.clear()
        for task in list(self._card_tasks):
            if not task.done():
                task.cancel()
        self._card_tasks.clear()
        await super().disconnect()
