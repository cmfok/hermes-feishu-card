#!/usr/bin/env python3
"""卡片内容快照 — 把「真实工具调用 → 上游进度文本 → 卡片实际显示」固定下来。

为什么必须有它
--------------
卡片上显示什么，取决于上游把工具调用**格式化成什么文本**。而那段文本有两种截然不同的
形状（gateway/run_turn_runner.py:226-275），且随 tool_progress 模式切换：

    verbose : f"{emoji} {tool_name}({list(args.keys())})\n{args_str}"   # 括号里是"参数键名"
    其它     : f"{emoji} {verb}{connector}{preview}"                     # 人类预览，无括号
    terminal : fenced 块；连续调用时**上游有意丢掉重复表头**（:239）→ 裸 ```

只要文本形状变了而解析器没跟着变，它不会报错，只会静默产出无意义的行 ——
2026-09-15 实际发生过（verbose→all 之后，16 行里 10 行工具名退化成字面量 `tool`）。
没有这个快照，每次改动都只能"改完看效果"，也就是靠猜。

用法
----
    python tests/snapshot_card.py            # 打印 + 写入 tests/out/snapshot-*.txt
    python tests/snapshot_card.py --check    # 只跑断言（回归门禁）

夹具来源
--------
TURN_FIXTURE 取自真实一轮：session 20260915_172221_ace90b18「选择周四分享会项目」，
2026-09-15 17:22:33 收到「周四的分享会，你觉得我们应该分享些什么项目？」，
跑 316.5s / 9 次 API 调用 / 16 次工具调用（中间无叙述，仅末尾 996 字结论）。
即用户当时屏幕上那张卡的来源。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import card_render as R  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"

# ---------------------------------------------------------------------------
# 夹具：真实一轮的 16 次工具调用（顺序即卡片上的顺序）
# ---------------------------------------------------------------------------
TURN_FIXTURE: List[Tuple[str, Dict[str, Any]]] = [
    ("terminal", {"command": 'cd /p/Qoder/work && ls -la && date "+%Y-%m-%d %H:%M %A"'}),
    ("search_files", {"limit": 60, "output_mode": "files_only",
                      "path": "P:\\Qoder\\work", "pattern": "分享会|分享课|分享主题"}),
    ("terminal", {"command": 'cd /p/Qoder/work/分享会 && ls -la --time-style=long-iso '
                             '&& echo "=== AIAGENT分享 ===" && ls -la --time-style=long-iso "ppt大纲/AIAGENT分享"'}),
    ("search_files", {"limit": 80, "output_mode": "content", "path": "P:\\Qoder\\work",
                      "pattern": "周四|星期四|9月18|09-18|09/18", "file_glob": "*.md"}),
    ("read_file", {"path": "P:\\Qoder\\work\\memories\\work-0cd8e837f8085c1a\\memory\\qingjunhui-course-plan.md"}),
    ("terminal", {"command": "cd /p/Qoder/work/memories/work-0cd8e837f8085c1a/memory "
                             "&& ls -la --time-style=long-iso | sort -k6 | tail -40"}),
    ("terminal", {"command": 'cd /p/Qoder/work/Ai100 && find . -type f -newermt "2026-09-01" '
                             '! -path "*/.git/*" ! -path "*/node_modules/*" -printf "%T@ %TY-%Tm-%Td %p\\n" | sort -rn | head -30'}),
    ("search_files", {"limit": 60, "output_mode": "files_only", "path": "P:\\Qoder\\work",
                      "pattern": "分享会|沙龙|线下分享|分享嘉宾|分享主题"}),
    ("tool_describe", {"names": ["session_search"]}),
    ("terminal", {"command": "cd /p/Qoder/work/Ai100 && ls -la --time-style=long-iso obsidian/ "
                             '&& echo "=== 我的想法 ===" && ls -la --time-style=long-iso obsidian/我的想法/'}),
    ("tool_call", {"calls": [{"arguments": {"limit": 5, "query": "分享会 周四 分享什么",
                                            "sort": "newest"}, "name": "session_search"}]}),
    ("terminal", {"command": "cd /p/Qoder/work/Ai100/obsidian && ls -la --time-style=long-iso 项目进度/ "
                             '&& echo "=== grep 分享 ===" && grep -rl "分享" --include="*.md" . | head -20'}),
    ("read_file", {"path": "P:\\Qoder\\work\\Ai100\\obsidian\\社交\\青骏会\\AI兴趣小组-小型分享会-运行SOP.md"}),
    ("read_file", {"path": "P:\\Qoder\\work\\Ai100\\obsidian\\社交\\青骏会\\AI分享系列策划-多智能体实战营-2026-09-14.md"}),
    ("read_file", {"path": "P:\\Qoder\\work\\Ai100\\obsidian\\_hots.md"}),
    ("read_file", {"path": "P:\\Qoder\\work\\Ai100\\obsidian\\项目进度\\待办清单.md"}),
]

# 上游 tool_preview_length 默认值（gateway/display_config.py:38）
PREVIEW_CAP = 40

# 非 terminal 工具的 emoji 兜底表（优先用上游 agent.display，缺失时用这张表）
_FALLBACK_EMOJI = {
    "terminal": "💻", "read_file": "📄", "write_file": "✏️", "patch": "🩹",
    "search_files": "🔎", "grep": "🔎", "web_search": "🌐", "web_fetch": "🌐",
    "tool_describe": "🧰", "tool_call": "🧰", "session_search": "🗂️",
}


def _upstream_display():
    """尽力加载上游 agent.display；失败则返回 None（用兜底表）。"""
    try:
        hermes_agent = os.environ.get("HERMES_AGENT_DIR") or str(
            Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
        )
        if hermes_agent not in sys.path:
            sys.path.insert(0, hermes_agent)
        from agent import display  # type: ignore
        return display
    except Exception:
        return None


_DISPLAY = _upstream_display()


def emoji_for(tool: str) -> str:
    if _DISPLAY is not None:
        try:
            return _DISPLAY.get_tool_emoji(tool, default="⚙️")
        except Exception:
            pass
    return _FALLBACK_EMOJI.get(tool, "⚙️")


def preview_for(tool: str, args: Dict[str, Any]) -> str:
    """非 terminal 工具的"人类预览"。

    上游的 preview 在上游管线更早处算出（本函数不可得）。这里取第一个有意义的字符串参数
    作为**近似** —— 足以复现「解析器面对这段文本产出什么」。这是本快照唯一的近似处。
    """
    for key in ("path", "file_path", "query", "pattern", "url", "command"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in args.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def upstream_text(tool: str, args: Dict[str, Any], *, mode: str, last_was_terminal: bool) -> str:
    """复刻 gateway/run_turn_runner.py:_progress_build_message 的相关分支。"""
    emoji = emoji_for(tool)

    if tool == "terminal" and isinstance(args.get("command"), str) and args["command"].strip():
        cmd_full = args["command"].rstrip()
        header = "" if last_was_terminal else f"{emoji} {tool}\n"
        lines = cmd_full.splitlines()
        cmd_short = lines[0] if lines else cmd_full
        if len(cmd_short) > PREVIEW_CAP:
            cmd_short = cmd_short[: PREVIEW_CAP - 3] + "..."
        elif len(lines) > 1:
            cmd_short += " ..."
        body = cmd_full if mode == "verbose" else cmd_short
        return f"{header}```\n{body}\n```"

    if mode == "verbose":
        if args:
            return f"{emoji} {tool}({list(args.keys())})\n{json.dumps(args, ensure_ascii=False, default=str)}"
        return f"{emoji} {tool}..."

    preview = preview_for(tool, args)
    verb, connector, drops = None, " ", False
    if _DISPLAY is not None:
        try:
            verb = _DISPLAY.get_tool_verb(tool)
            connector = _DISPLAY.tool_verb_connector(tool)
            drops = _DISPLAY.verb_drops_preview(tool)
        except Exception:
            verb = None
    if not preview:
        return f"{emoji} {tool}..."
    if not verb:
        return f'{emoji} {tool}: "{preview}"'
    return f"{emoji} {verb}" if drops else f"{emoji} {verb}{connector}{preview}"


def refine_kind(kind: R.MsgKind, text: str, in_tool_context: bool) -> R.MsgKind:
    """复刻 card_adapter._refine_kind：裸 ``` 块在"工具流中"时才算工具。

    单独看文本无法区分"连续 terminal 丢表头的命令"与"用户要的纯代码回复"，
    所以必须结合上下文 —— 这里用 `in_tool_context`（上一条仍是未结束的工具面板）模拟。
    """
    if kind is not R.MsgKind.TEXT:
        return kind
    if R.is_bare_fence(text) and in_tool_context:
        return R.MsgKind.TOOL
    return kind


def simulate(mode: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按卡会话的真实规则走一遍，返回 (逐条轨迹, 卡片 elements)。

    轨迹每条：{tool, text, kind, refined, folded}
    """
    trace: List[Dict[str, Any]] = []
    block_texts: List[str] = []          # 面板内的原始文本（与卡会话的 summaries 一致）
    in_tool_context = False
    last_terminal = False

    for tool, args in TURN_FIXTURE:
        text = upstream_text(tool, args, mode=mode, last_was_terminal=last_terminal)
        last_terminal = tool == "terminal" and bool(str(args.get("command") or "").strip())
        kind = R.classify(text)
        refined = refine_kind(kind, text, in_tool_context)
        folded = refined is R.MsgKind.TOOL
        if folded:
            block_texts.append(text)
            in_tool_context = True
        trace.append({"tool": tool, "text": text, "kind": kind, "refined": refined, "folded": folded})

    elements = R.build_elements([{"type": "tools", "summaries": list(block_texts)}], R.STATUS_RUNNING)
    return trace, elements


def panel_lines(elements: Sequence[Dict[str, Any]]) -> List[str]:
    """把卡片里工具面板的正文行抽出来（纯比对用）。"""
    lines: List[str] = []
    for el in elements:
        if el.get("tag") == "collapsible_panel":
            for child in el.get("elements", []):
                if child.get("tag") == "markdown":
                    lines.extend(str(child.get("content", "")).splitlines())
    return [ln for ln in lines if ln.strip()]


def junk_lines(lines: Sequence[str]) -> List[str]:
    """无效行：工具名退化成字面量 `tool`、只剩参数键名、或二次包裹。"""
    bad: List[str] = []
    for ln in lines:
        if "· tool ·" in ln or "· tool`" in ln or ln.rstrip().endswith("· tool"):
            bad.append(ln)
        elif "([" in ln or "[('" in ln:
            bad.append(ln)
        elif ln.count("- ") > 1 and "· " in ln:  # 形如 `- ⏳ · tool · `- ⏳ · ...``
            bad.append(ln)
    return bad


def main() -> int:
    check = "--check" in sys.argv
    print("=" * 78)
    print("夹具：session 20260915_172221_ace90b18 —— 16 次工具调用（你屏幕上那张卡的来源）")
    print("=" * 78)

    bad_total: List[str] = []
    for mode in ("all", "verbose"):
        trace, elements = simulate(mode)
        lines = panel_lines(elements)
        bad = junk_lines(lines)

        print(f"\n--- 上游文本形状 = {mode} ---")
        for row in trace:
            head = row["text"].splitlines()[0] if row["text"].splitlines() else ""
            print(f"  [{row['tool']:14s}] classify={row['kind'].name:5s} "
                  f"→ {row['refined'].name:5s} 折叠={'✓' if row['folded'] else '✗'}  "
                  f"上游首行: {head[:58]!r}")

        title = ""
        for el in elements:
            if el.get("tag") == "collapsible_panel":
                title = el["header"]["title"]["content"]
        print(f"\n  卡片实际渲染（{mode}）— 面板标题: {title}")
        for ln in lines:
            print(f"    {ln}")
        if bad:
            print(f"\n  ⚠️ 无效行 {len(bad)}/{len(lines)}")
        bad_total.extend(bad)

    print("\n" + "=" * 78)
    if bad_total:
        print(f"❌ 共 {len(bad_total)} 行无效（工具名退化 / 只剩参数键名 / 二次包裹）")
    else:
        print("✅ 无无效行：两条上游形态下工具摘要都能读懂")
    print("=" * 78)

    if check:
        return 1 if bad_total else 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for mode in ("all", "verbose"):
        _, elements = simulate(mode)
        (OUT_DIR / f"snapshot-{mode}.txt").write_text(
            "\n".join(panel_lines(elements)), encoding="utf-8"
        )
    print(f"\n已写入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
