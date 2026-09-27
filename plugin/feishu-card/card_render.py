"""纯函数层：卡片 JSON 构造 / 分块 / 容量判定 / 消息分类。

本模块不做任何 I/O，也不导入 lark SDK —— 便于单元测试，也让插件在
"只做插件发现"的进程里保持零重导入。

集中在这里的"形状知识"：
  * 出站卡片 = 飞书 JSON 2.0 interactive 结构（body.elements）
  * 请求体 ~30KB、JSON 2.0 元素 ~200、单卡表格数上限 2 → 容量保护阈值
  * 消息分类（工具进度 / 收尾 / 正文）此前散落在 adapter 的三个正则里，
    现在收敛成 classify() 一个入口，便于测试与改文案。
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------
# 常量（来自飞书 interactive API 的实测上限与历史踩坑）
# ----------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 8000

#: 同一张卡追加 N 次后主动开新卡（防止触达飞书请求体/元素上限后 PATCH 被拒）
CARD_MAX_APPENDS = 30
#: 单卡块数上限
CARD_MAX_BLOCKS = 60
#: 整卡 JSON 字节数上限（飞书 interactive 请求体 ~30KB，留一半余量）
CARD_MAX_BYTES = 15000
#: 单卡 markdown 表格数上限（= 飞书官方硬上限）。
#: 飞书官方文档《表格组件》：**单张卡片最多支持放置五个表格组件**，超出报 `ErrCode 11310 /
#: card table number over limit`（HTTP 400, code 230099）。
#: https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/table.md
#: 2026-09-15 修正两处（原值 2，比官方保守 2.5 倍 → CM 的足球分析对话全是维度/胜率对比表，
#: 一个回复被拆成两张卡）：
#:   ① 2 → 5（对齐官方与上游 Hermes 自身取值）；
#:   ② 判定由 `>` 改为 `>=` —— **必须留一张表的余量**：判定发生在"追加**前**"，
#:      若前态已有 5 张表就放行，本次追加的第 6 张会让整卡被飞书拒绝
#:      （2026-09-15 实机：今天 22 次 11310 全部来自本插件、全部是 `card table number over limit`）。
#:      留余量后，实际发出的一卡最多 5 张表 = 官方上限，合规。
CARD_MAX_TABLES = 5

#: 单卡元素上限：飞书硬上限约 50 个元素，留余量到 40（与 DSH 插件实测值一致）
MAX_CARD_ELEMENTS = 40
#: 折叠时保留可见的最新元素数（设计：实时进度/结论留在底部可见，已看过的历史折到顶部）
KEEP_TAIL_ELEMENTS = 10
#: 每个「更早过程」折叠面板的**正文上限**（单元素正文的保守上限，沿用既有生产值）。
#: 内容超过它就**多切一个面板**，绝不截断丢弃 —— 见 fold_elements() 的说明。
FOLD_CHUNK_CHARS = 3000

#: 换卡（容量超限）时新卡只放这一行提示 —— **不复制**旧卡正文/工具块。
#: 为什么：复制会让同一段内容同时出现在两张卡片上（旧卡已封口、仍留在会话里），
#: 用户看到的就是"同一段东西分两个卡片发、内容大部分重复"（2026-09-15 CM 反馈，
#: 探针 I12-跨卡内容重复 复现）。
CONTINUATION_NOTICE = "（接上一条卡片）"

#: 失败换卡（PATCH 失败后为保住内容而换卡）时，**旧卡**上留的指针。
#: 为什么不能把内容灌回旧卡：那次 PATCH 万一成功，旧卡与新卡就同时持有同一份内容
#: → 用户看到两张内容几乎一样的卡片（2026-09-15 CM 反馈的症状，探针 I11/I12 都复现）。
#: 内容只留一份：放在**新卡**上（旧卡给指针），这样既不丢也不重。
MOVED_NOTICE = "（内容见下一条卡片）"

#: 连续写入失败达到该次数 → **停止换卡**，直接返回失败让上层降级成纯文本。
#: 为什么需要：飞书若持续拒卡（限流/卡片被删/权限），"失败就换新卡"会变成
#: "每条消息都冒一张新卡"的刷屏循环（2026-09-15 探针实测：连续失败注入下连建 15 张）。
CONSECUTIVE_FAILURE_LIMIT = 3
#: 换卡配额：单会话换卡次数不得超过 `max(CONSECUTIVE_FAILURE_LIMIT, 写入次数 // 本值)`。
#: 为什么需要：交替失败（成功/失败各半）时"连续失败计数"会被成功那次清零，
#: 于是又变成"每次失败都换卡"（探针实测 30 次操作建 16 张卡）。用**与写入次数成比例的预算**
#: 兜住频率，正常对话（几乎不失败）完全不受影响。
ROTATE_BUDGET_DIVISOR = 5
#: 单会话换卡次数硬上限（兜底：防容量判定失效导致的无限换卡）。
MAX_ROTATIONS = 50

#: **「✅ 完成」只能由真实的回合结束信号触发**，绝不靠计时器猜。
#: 真信号有两个：
#:   · 上游给**最终回复**打的 `metadata["notify"]=True`
#:     （`gateway/platforms/base.py:148 _mark_notify_metadata`，全仓唯一设置点；
#:      注释原文 *"Final content gets notify=True; typing metadata stays unmarked"*）；
#:   · `edit_message(..., finalize=True)`（上游的收尾编辑）。
#: 这里原来是 `CARD_SEAL_DELAY = 8.0` —— "8 秒没新内容就当成完成"。实测两个错（CM 2026-09-16 反馈）：
#:   ① agent 正常思考（单次 API 调用常 >8 秒）→ 卡片**提前显示"已完成"**，过一会又冒内容；
#:   ② 长任务心跳间隔是 **180 秒**，每次心跳都取消这个计时器 → 卡片**永远停在"回复中"**，
#:      哪怕后台其实已经不动了（心跳把它钉在"进行中"）。
#: 现在这个定时器**不再宣称完成**，只在长时间毫无进展时写一条诚实的空闲提示。
CARD_IDLE_DELAY = 200.0     # 无任何进展多久后提示（> 心跳 180s，避免与心跳互相打架）
CARD_IDLE_NOTICE_MAX = 5    # 最多重复提示次数（防对着卡死的卡无限 PATCH）


def idle_notice_text(minutes: float) -> str:
    """长时间无进展时的**诚实**提示：不宣称完成，只说清"多久没动静了"。"""
    return f"_⏳ 运行中…（已 {max(1, int(minutes))} 分钟无新动作）_"

#: 长代码块折叠阈值
CODE_MAX_LINES = 8
CODE_MAX_CHARS = 600

STATUS_RUNNING = "running"
STATUS_TYPING = "typing"
STATUS_SEALED = "sealed"

_STATUS_TEXT = {
    STATUS_RUNNING: "_⏳ 运行中…_",
    STATUS_TYPING: "_✍️ 回复中…_",
    STATUS_SEALED: "_✅ 完成_",
}

# ----------------------------------------------------------------------------
# 正则
# ----------------------------------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

_TOOL_MSG_RE = re.compile(
    # 允许 emoji 后跟一个**变体选择符**（U+FE0F）：🖼️ / ⚠️ / 🗓️ 这类是"emoji + VS16"两个码点，
    # 旧正则要求 emoji 后直接是空白 → 带 VS16 的工具 emoji 会被整条漏判成正文（2026-09-15 实测）。
    r"^(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uD83C-\uD83E])\uFE0F?\s+"
    r"(.{1,80}?)(?:\n|$|:|\.\.\.)"
)
_TERMINAL_MSG_RE = re.compile(r"^\U0001F4BB.*?```", re.DOTALL)
#: 整条消息就是一个 fenced 代码块（无任何前导文字）。
#: 这是上游的**连续 terminal 调用**格式 —— `run_turn_runner.py:239` 会刻意丢掉重复表头
#: （`header = "" if last_was_terminal_block else f"{emoji} {tool_name}\n"`），
#: 于是第 2 条起的工具进度就是裸 ``` 块，没有任何 emoji 可识别。
#: 单看文本无法区分"工具命令"与"用户要的纯代码回复"，因此它只作为**候选**，
#: 由 `ChatCardSession.in_tool_context()`（上一条是否仍在工具流中）来最终定性。
_BARE_FENCE_RE = re.compile(r"^\s*```[^\n]*\n[\s\S]*?```\s*$")
#: 非工具 emoji 前缀（✅/⚠️/❌ 等 → 不算工具消息）
#: 注意（2026-09-15 修）：曾把 📄🖼️💬❓💡 也列进来，但实测它们都是**真实工具**的 emoji
#: （📄 web_tools / 🖼️ preview_tool / 💬 browser_dialog_tool / ❓ clarify_tool / 💡 tip_tool）
#: —— 误判会让这些工具的进度落进正文块、不折叠。
_NON_TOOL_PREFIX = re.compile(
    r"^(?:\u2705|\u26A0\uFE0F|\u274C|\u2757|\u2139\uFE0F|"
    r"\U0001F44D|\U0001F44C|\U0001F389|\U0001F4CC|"
    r"\U0001F64F|\U0001F60A|\U0001F91D|\U0001F514|\U0001F4E2|"
    r"\U0001F4E3|\U0001F4CE|\U0001F4C2|\U0001F4C1)\s*"
)


def is_bare_fence(text: str) -> bool:
    """整条消息是否就是一个 fenced 代码块（连续 terminal 调用丢 header 后的形态）。"""
    return bool(_BARE_FENCE_RE.match((text or "").strip()))
#: 记忆通知（💾 开头）与 memory 工具调用 = 收尾消息，不进正文方块
_MEMORY_NOTIFY_PREFIX = "\U0001F4BE"
_MEMORY_TOOL_RE = re.compile(r"\bmemory\s*\(")


class MsgKind(Enum):
    """出站消息的种类（决定它进卡片的哪个方块）。"""

    TOOL = "tool"        # 工具进度 → 折叠面板，可与上一条工具消息合并
    CLOSING = "closing"  # 收尾（记忆保存等）→ 不改内容，只重置密封定时器
    TEXT = "text"        # 正文 → 消息方块


def classify(content: str, *, interim: bool = False) -> MsgKind:
    """把一段出站文本分类。集中在唯一入口，避免"改文案就误判"。

    **`interim=True` 时一律判正文**（不看文本形状）。为什么必须有这条：
    上游用 `metadata["_interim_send"]`（`gateway/run.py:457 _interim_metadata`）
    **明确标记"中途汇报"**；带这个标记的消息一定是给用户看的阶段汇报，
    **一定是正文**，绝不可能是工具进度。此前本函数只看文本形状（"首字符是不是工具 emoji"），
    遇到黑名单外的 emoji 就误判 —— 实测 `🚨 **查出严重问题，我先认：**…` 被判成 TOOL
    → 扔进折叠面板 → 用户"发给我的东西直接丢掉了，不显示了"（CM 2026-09-15 反馈）。
    **不猜，用上游给的标记**。
    """
    text = (content or "").strip()
    if not text:
        return MsgKind.CLOSING
    if text.startswith(_MEMORY_NOTIFY_PREFIX):
        return MsgKind.CLOSING
    first_line = text.split("\n")[0]
    if _MEMORY_TOOL_RE.search(first_line):
        return MsgKind.CLOSING
    if interim:
        return MsgKind.TEXT
    if _TERMINAL_MSG_RE.match(text):
        return MsgKind.TOOL
    if _NON_TOOL_PREFIX.match(text):
        return MsgKind.TEXT
    if _TOOL_MSG_RE.match(text):
        return MsgKind.TOOL
    return MsgKind.TEXT


def count_markdown_tables(text: str) -> int:
    """统计 markdown 表格块数（连续 |...| 行 = 一张表）。"""
    count = 0
    prev = False
    for line in str(text or "").split("\n"):
        row = bool(_TABLE_ROW_RE.match(line))
        if row and not prev:
            count += 1
        prev = row
    return count


def split_chunks_by_tables(chunks: Sequence[str], max_tables: Optional[int] = None) -> List[str]:
    """按 markdown 表格数再切分，保证每块的表格数 <= 上限。

    飞书卡片对**单卡表格数量**有硬上限，超出时整张卡被 API 拒绝 →
    旧逻辑只按字符数分块，多表长回复必然回退成富文本。
    """
    limit = CARD_MAX_TABLES if max_tables is None else max_tables
    out: List[str] = []
    for chunk in chunks or []:
        if count_markdown_tables(chunk) <= limit:
            out.append(chunk)
            continue
        buf: List[str] = []
        tables = 0
        prev = False
        for line in str(chunk).split("\n"):
            row = bool(_TABLE_ROW_RE.match(line))
            if row and not prev and tables >= limit:
                seg = "\n".join(buf).strip("\n")
                if seg.strip():
                    out.append(seg)
                buf = []
                tables = 0
                prev = False
            if row and not prev:
                tables += 1
            buf.append(line)
            prev = row
        seg = "\n".join(buf).strip("\n")
        if seg.strip():
            out.append(seg)
    return [c for c in out if c and c.strip()] or list(chunks or [])


def message_elements(text: str) -> List[Dict[str, Any]]:
    """正文文本 → 卡片元素列表：长代码块拆成默认收起的折叠框。

    流式片段修补：Hermes 流式输出会把代码块切成多段，开/闭围栏不在同一段，
    正则匹配不到完整代码块 → 未闭合围栏被飞书渲染错乱。段内 ``` 为奇数时补全。
    """
    if text.count("```") % 2 == 1:
        if text.strip().endswith("```") and not text.strip().startswith("```"):
            text = "```\n" + text
        else:
            text = text + "\n```"
    elements: List[Dict[str, Any]] = []
    pos = 0
    for match in _CODE_FENCE_RE.finditer(text):
        before = text[pos:match.start()]
        if before.strip():
            elements.append({"tag": "markdown", "content": before})
        code = match.group(2)
        lang = match.group(1) or "code"
        lines = code.count("\n") + 1
        if lines > CODE_MAX_LINES or len(code) > CODE_MAX_CHARS:
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "background_color": "grey-50",
                    "border": {"color": "grey", "corner_radius": "8px"},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"{lang} · {lines} 行 · 点击展开",
                        }
                    },
                    "elements": [{"tag": "markdown", "content": f"```{lang}\n{code}\n```"}],
                }
            )
        else:
            elements.append({"tag": "markdown", "content": match.group(0)})
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        elements.append({"tag": "markdown", "content": tail})
    return elements


#: 单个工具面板的内容上限：超出时只保留最新的这一段，旧内容折进"更早"提示行。
#: 为什么：连续调用工具会全部合并进同一个面板，内容无限增长 → 整卡越来越长、PATCH 越来越重。
TOOL_BLOCK_MAX_CHARS = 4000


# ---- ZCode 对齐（2026-09-15：CM 指示「照抄 ZCode」）--------------------------------
# 以下文案/格式全部照抄 ZCode `out/host/index.js` 的实现（逆向 + 源码实证）：
#   · 面板标题      `🛠️ 工具摘要 (N)`                 —— 固定文案 + 数量，**不拼接内容**
#   · 工具行        `- {状态} · {工具名} · {详情}`      —— 一行一条摘要
#   · 状态文案      completed✅已完成 / failed失败 / inProgress⏳运行中 / pending等待中
#   · 展开规则      `expanded ?? (status === "running")` —— 运行中展开、封口后折叠
#   · 卡片结构      message 块 + tools 面板 + 状态行（**没有"中途叙述"这一项**）
_TOOL_PANEL_TITLE = "工具摘要"
_TOOL_STATUS_IN_PROGRESS = "⏳"
_TOOL_STATUS_DONE = "✅"
_TOOL_STATUS_FAILED = "❌"

#: 幕式标记行：`💻 terminal` / `🔎 search_files([...])` —— emoji + 工具名
_TOOL_HEADER_RE = re.compile(
    r"^(?:\S{1,2})\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|$)", re.UNICODE
)

#: 行首 emoji（含变体选择符 U+FE0F）：`⚙️ Reading …` → `Reading …`
_LEAD_EMOJI_RE = re.compile(
    r"^(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]\uFE0F?|\uFE0F)+\s*",
    re.UNICODE,
)

#: 详情最长字符数（超出截断，保证一行能读完）
_DETAIL_MAX_CHARS = 90


def _clip(text: str) -> str:
    """压平空白并截断到一行可读长度。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[: _DETAIL_MAX_CHARS - 3] + "..." if len(flat) > _DETAIL_MAX_CHARS else flat


def _first_value(args: Dict[str, Any]) -> str:
    """取参数里的第一个有意义的**值**（verbose 形态下 JSON 的第二行）。

    旧实现取的是 `list(args.keys())` —— 那是**键名**，卡片上于是出现
    `- ⏳ · read_file · \`(['path'])\``（2026-09-15 快照复现）。
    取不到就返回空**而不是**退回键名：宁可不显示详情，也不能显示得让人误会。

    优先级按"人最想看到哪个参数"排：`search_files` 的关键词比它扫的目录有用，
    所以 `pattern`/`query` 排在 `path` 前面。
    """
    for key in ("command", "query", "pattern", "names", "url", "path", "file_path", "calls"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                inner = _first_value(first)
                if inner:
                    return inner
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_tool_line(text: str) -> Tuple[str, str]:
    """上游的一条工具进度文本 → (工具标识, 详情)。**只取，不编**。

    上游（`gateway/run_turn_runner.py:226-275`）实际会产出三种形态，取决于 tool_progress：

      ① 纯 fenced 块（terminal）：
         `💻 terminal\\n```\\ncd /p/FU && python x.py\\n``` `
      ② 裸 fenced 块：连续 terminal 时上游**有意丢掉重复表头**（:239
         `header = "" if last_was_terminal_block else f"{emoji} {tool_name}\\n"`）
      ③ 非 terminal：
         · verbose → `🔎 search_files(['limit', 'pattern'])\\n{"limit": 60, ...}`
         · 其它     → `⚙️ Reading P:\\Qoder\\work\\...\\MOC.md`（人类预览，**无括号**）

    为什么不再"二次解析"：形态 ③ 的后半段（也是 CM 当前配置 `tool_progress: all` 用的形态）
    **没有** `名字(参数…)` 结构。旧实现用 `first.find("(")` 硬抠，抠不到括号就退化成字面量
    `tool`，verbose 形态下则只拿到参数**键名** `(['path'])` —— 卡片上于是出现
    `- ⏳ · tool · \`⚙️ Reading P:\\...\`` 这种看不出用什么工具的行
    （2026-09-15 用 tests/snapshot_card.py 复现：16 行里 10 行如此）。

    现在：能确定工具名就取，取不到就**照搬上游原文**（它本身就是给人看的），绝不编造兜底名。
    """
    t = str(text or "").strip()
    if not t:
        return "", ""
    first = t.split("\n", 1)[0].strip()

    # ① ② fenced 块（terminal 命令）
    fence = re.search(r"```[^\n]*\n(.*?)(?:\n```|```\s*$)", t, re.DOTALL)
    if fence:
        body_lines = [ln.strip() for ln in fence.group(1).split("\n") if ln.strip()]
        detail = body_lines[0] if body_lines else ""
        header = _TOOL_HEADER_RE.match(first) if first and not first.startswith("```") else None
        # 裸块 = 上游丢表头的连续 terminal（只有 terminal 会走这条路径）
        name = header.group(1) if header else "terminal"
        return name, _clip(detail)

    header = _TOOL_HEADER_RE.match(first) if first else None
    if header:
        name = header.group(1)
        # verbose 形态：`名字(['键名'])` + 下一行 JSON —— 取 JSON 里第一个**值**。
        # 取不到就**留空**，绝不退回括号里的键名（那正是卡片显示 `(['path'])` 的来源）。
        detail = ""
        second = t.split("\n", 2)[1].strip() if t.count("\n") >= 1 else ""
        if second:
            try:
                parsed = json.loads(second)
                if isinstance(parsed, dict):
                    detail = _first_value(parsed)
            except Exception:
                # JSON 解析失败（转义/截断都可能）→ 正则捞第一个 `"键": "值"`。
                # 绝不把整段 JSON 当详情：那等于把参数原文塞进卡片，又长又难读。
                found = re.search(r'"[A-Za-z_][A-Za-z0-9_]*"\s*:\s*"([^"]*)"', second)
                detail = found.group(1) if found else ""
        return name, _clip(detail)

    # ③ 人类预览（如 `⚙️ Reading P:\...`）：上游本来就是为了给人读而拼的整句，
    # 直接整句照搬 —— 硬拆"首词当工具名"会得到 `Searching · files for P:\x` 这种别扭结果。
    return "", _clip(_LEAD_EMOJI_RE.sub("", first).strip())


def _mark(status: str) -> str:
    """状态符号：只有卡片封口才敢标 ✅（整轮结束 ⇒ 工具必然已返回）。"""
    return _TOOL_STATUS_DONE if status == STATUS_SEALED else _TOOL_STATUS_IN_PROGRESS


def render_tool_line(name: str, detail: str, status: str = STATUS_RUNNING) -> str:
    """渲染成 ZCode 风格一行：`- {状态} · {工具} · {详情}`。

    状态只表达**我们确实知道的事**：卡片还在跑 → ⏳（已发出、未见完成）；
    卡片封口 → ✅（整轮结束，工具必然已返回）。上游的进度文本里不含完成/失败信号，
    所以这里不猜 ✅/❌（猜错比不显示更坏）。

    取不到工具名时（上游 `all` 模式给的是人类整句，如 `Reading P:\\…\\MOC.md`），
    整句作为正文 → `- ⏳ · Reading P:\\…\\MOC.md`，不硬拆、不编名字。
    """
    mark = _mark(status)
    name = str(name or "").strip()
    detail = str(detail or "").strip()
    if not name:
        return f"- {mark} · {detail}" if detail else ""
    return f"- {mark} · {name} · `{detail}`" if detail else f"- {mark} · {name}"


#: 已经渲染好的行（`- ⏳ · terminal · \`cmd\``）—— 只换状态符号，绝不再解析一次
_RENDERED_LINE_RE = re.compile(r"^-\s*\S+\s*·\s*(.+)$")


def summarize_tool_line(text: str) -> str:
    """兼容入口：上游文本 → 一行摘要（内部走 parse + render）。"""
    name, detail = parse_tool_line(text)
    if not name and not detail:
        return ""
    return render_tool_line(name, detail)


def tool_block(
    summaries: Sequence[str], expanded: bool = False, status: str = STATUS_RUNNING
) -> Dict[str, Any]:
    """工具面板：**照抄 ZCode** —— 标题固定 `🛠️ 工具摘要 (N)`，正文是每工具一行摘要。

    与旧实现的区别（CM 2026-09-15：「照抄 ZCode」）：
      · 旧标题拼了"内容首行" → 连续命令丢表头时首行是 ```` ``` ````，标题变成乱码；
      · 旧正文是原始命令全文（多行代码块）→ 面板又长又乱。
    现在正文每行一条 `- ⏳ · 工具名 · 参数摘要`，折叠时也一眼看清跑了什么。

    **幂等**：入参可以是上游原始文本，也可以是已渲染好的行。已渲染的行只替换状态符号，
    不会二次解析 —— 旧实现无条件再跑一遍 `summarize_tool_line`，于是存渲染好的行会变成
    `- ⏳ · tool · \`- ⏳ · terminal · ...\``。状态由 `status` 决定 ✅/⏳，在**渲染时**才算。
    """
    lines: List[str] = []
    for entry in summaries or []:
        raw = str(entry or "").strip()
        if not raw:
            continue
        rendered = _RENDERED_LINE_RE.match(raw)
        if rendered:
            lines.append(f"- {_mark(status)} · {rendered.group(1).strip()}")
            continue
        name, detail = parse_tool_line(raw)
        if name or detail:
            lines.append(render_tool_line(name, detail, status))
    if not lines:
        return {}
    body = "\n".join(lines)
    if len(body) > TOOL_BLOCK_MAX_CHARS:
        body = "…（更早记录已省略）\n" + body[-TOOL_BLOCK_MAX_CHARS:]
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "background_color": "grey-50",
        "border": {"color": "grey", "corner_radius": "8px"},
        "padding": "8px 8px 8px 8px",
        "header": {
            "title": {"tag": "plain_text", "content": f"🛠️ {_TOOL_PANEL_TITLE} ({len(lines)})"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": [{"tag": "markdown", "content": body}],
    }


def block_elements(block: Dict[str, Any], status: str = STATUS_RUNNING) -> List[Dict[str, Any]]:
    """一个内部 block → 卡片元素列表。

    工具面板的展开规则**照抄 ZCode**：`expanded ?? (status === "running")`
      —— 块没有显式指定时，**卡片还在运行时展开**（能看到当前在跑什么），
         一旦出正文/收尾（状态变化或块被显式折叠）就收起。
    """
    kind = block.get("type")
    if kind == "tools":
        summaries = block.get("summaries")
        if not summaries:
            summaries = [str(block.get("content") or "")]
        expanded = block.get("expanded")
        if expanded is None:
            expanded = status == STATUS_RUNNING
        panel = tool_block(summaries, expanded=bool(expanded), status=status)
        return [panel] if panel else []
    if kind == "message":
        return message_elements(str(block.get("text") or ""))
    if kind == "notice":
        # 状态类通知（长任务心跳「已持续工作 N 分钟」等）：只占一行，
        # 重复通知原地替换、出正文/收尾时自动消失（见卡会话的 update_notice）。
        text = str(block.get("text") or "").strip()
        return [{"tag": "markdown", "content": text}] if text else []
    return []


def build_elements(blocks: Sequence[Dict[str, Any]], status: str = STATUS_RUNNING) -> List[Dict[str, Any]]:
    """blocks → 卡片元素列表（未做元素折叠）。`status` 决定工具面板是否展开。"""
    elements: List[Dict[str, Any]] = []
    for block in blocks or []:
        elements.extend(block_elements(block, status))
    return elements


def _chunk_text(text: str, size: int) -> List[str]:
    """把长文本按段落边界切成 ≤ ``size`` 的块；单段本身超长则硬切。**不丢任何字符。**

    为什么要它：折叠面板的正文必须每块都 ≤ ``FOLD_CHUNK_CHARS``（单元素正文的保守上限），
    但整体内容不许丢 —— 所以是"多切几块"，不是"截断丢弃"。
    """
    out: List[str] = []
    buf = ""
    for para in str(text or "").split("\n\n"):
        piece = f"{buf}\n\n{para}" if buf else para
        if len(piece) <= size:
            buf = piece
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        buf = para
    if buf:
        out.append(buf)
    return [c for c in out if c.strip()]


def _fold_panel(title: str, text: str) -> Dict[str, Any]:
    """一个「更早过程」折叠面板。"""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey-50",
        "border": {"color": "grey", "corner_radius": "8px"},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": text}],
    }


def fold_elements(elements: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """元素超限时把"更早过程"折进**若干**面板（飞书 11310 element exceeds the limit 防护）。

    保留最新 ``KEEP_TAIL_ELEMENTS`` 个元素可见（实时进度与结论在底部），其余折进
    「📎 更早过程 (N)」面板。参考实现：DSH 飞书插件（2026-08-15 实测，CM 设计）。

    **必须无损（2026-09-16 修正）**：旧实现在这里把折叠正文**硬截断到 3000 字、其余直接丢弃**
    （`folded_text[:3000] + "…（更早过程已省略）"`）。实测 50 个元素折叠后**丢掉 57% 内容**
    （第 12–39 段彻底消失）—— 因为容量上限允许卡片长到 30 次追加，远超 3000 字能保住的量，
    **两个机制互相矛盾**。用户看到的现象就是 CM 反馈的"换卡时旧消息被吞"。
    现在按 ``FOLD_CHUNK_CHARS`` 切成多个面板：每块仍 ≤ 单元素保守上限，但**一个字符都不丢**。

    元素预算：面板数 ≈ ⌈折叠字数 / FOLD_CHUNK_CHARS⌉。整卡受 ``CARD_MAX_BYTES``(15000) 约束，
    面板数最多约 5，加尾部 10 个 ≈ 15 ≪ ``MAX_CARD_ELEMENTS``(40)，安全。
    """
    items = [el for el in (elements or []) if isinstance(el, dict)]
    if len(items) <= MAX_CARD_ELEMENTS:
        return items
    head = items[: len(items) - KEEP_TAIL_ELEMENTS]
    tail = items[len(items) - KEEP_TAIL_ELEMENTS :]
    lines: List[str] = []
    for el in head:
        if el.get("tag") == "markdown":
            content = str(el.get("content") or "").strip()
        else:  # 折叠面板：取内部第一段 markdown
            inner = el.get("elements") or []
            first = inner[0] if inner and isinstance(inner[0], dict) else {}
            content = str(first.get("content") or "").strip() if first.get("tag") == "markdown" else ""
        if content:
            lines.append(content)
    if not lines:
        return list(tail)
    chunks = _chunk_text("\n\n".join(lines), FOLD_CHUNK_CHARS)
    count = len(chunks)
    panels = [
        _fold_panel(
            f"📎 更早过程 ({len(head)})" if count == 1 else f"📎 更早过程 ({idx + 1}/{count})",
            chunk,
        )
        for idx, chunk in enumerate(chunks)
    ]
    return panels + list(tail)


def card_json(blocks: Sequence[Dict[str, Any]], status: str = STATUS_RUNNING) -> str:
    """内部 blocks + 状态 → 飞书 JSON 2.0 交互卡片 payload。

    出站前统一做元素折叠：元素数超过 ``MAX_CARD_ELEMENTS`` 时把更早的过程折进一个面板，
    避免整卡被飞书以 11310 拒绝（旧行为：拒绝 → 降级成纯文本，用户看不到卡片）。
    """
    elements = fold_elements(build_elements(blocks, status))
    elements.append({"tag": "markdown", "content": _STATUS_TEXT.get(status, _STATUS_TEXT[STATUS_RUNNING])})
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)


def blocks_bytes(blocks: Sequence[Dict[str, Any]]) -> int:
    """估算整卡字节数（用最终 payload 长度，最贴近飞书侧的计量）。"""
    try:
        return len(card_json(blocks, STATUS_RUNNING).encode("utf-8"))
    except Exception:
        return 0


def _block_text(block: Dict[str, Any]) -> str:
    """一个内部 block 的纯文本（工具块现在存 summaries 列表）。"""
    summaries = block.get("summaries")
    if summaries:
        return "\n".join(str(s) for s in summaries)
    return str(block.get("text") or block.get("content") or "")


def card_tables(blocks: Sequence[Dict[str, Any]]) -> int:
    total = 0
    for block in blocks or []:
        total += count_markdown_tables(_block_text(block))
    return total


def exceeds_limits(blocks: Sequence[Dict[str, Any]], append_count: int) -> bool:
    """容量保护：追加次数 / 块数 / 字节数 / 表格数任一超限 → 应开新卡。"""
    return bool(limit_reason(blocks, append_count))


def limit_reason(blocks: Sequence[Dict[str, Any]], append_count: int) -> str:
    """哪条容量阈值触发了换卡（""=都没触发）。

    为什么返回原因：2026-09-15 只看到日志写 `appends=9 blocks=6 bytes=3576` 就换卡，
    而这三个数都远低于各自上限 —— 真正触发的是**表格数**，日志却没写出来，
    只能靠猜（[[教训检讨]] 015：没有可见性的排查就是猜）。
    """
    if append_count >= CARD_MAX_APPENDS:
        return f"appends {append_count}>={CARD_MAX_APPENDS}"
    if len(blocks or []) >= CARD_MAX_BLOCKS:
        return f"blocks {len(blocks or [])}>={CARD_MAX_BLOCKS}"
    used_bytes = blocks_bytes(blocks)
    if used_bytes >= CARD_MAX_BYTES:
        return f"bytes {used_bytes}>={CARD_MAX_BYTES}"
    tables = card_tables(blocks)
    if tables >= CARD_MAX_TABLES:
        # 用 `>=` 而非 `>`：判定发生在"追加前"，必须给本次追加留一张表的余量，
        # 否则前态 5 张放行 → 追加第 6 张 → 整卡被飞书以 11310 拒绝。
        return f"tables {tables}>={CARD_MAX_TABLES}(留余量)"
    return ""
