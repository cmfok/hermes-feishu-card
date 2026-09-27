# 维护与技术手册（feishu-card）

> **这份文档的目的（CM 原话）**：「不要下次我要修的时候，又重复造轮子或者重复踩坑。
> 就这东西是要经常维护的事情，该怎么做？标准是怎么样的？有一些容易踩的坑，应该全部写好。
> 不然的话，你下次又要踩一遍坑，要浪费时间，浪费 token。」
>
> **读它的时机**：① 要改这个插件之前（必读）；② 真机出问题时（直接跳 §6 排障手册）；
> ③ 上游 Hermes 升级之后（跳到 §3 逐条核对耦合点）。
>
> **配套**：`requirements.md`（CM 的需求与验收清单，改完必须逐条打勾）｜
> `../CHANGELOG.md`（每次改了什么、为什么）｜ [[开发标准]] §10 改动纪律 ｜ [[教训检讨]] 015/016

---

## 1. 系统全景

### 1.1 这个插件在干什么

把 Hermes 的飞书出站消息，从"一条条新消息"变成**一张持续原地更新的交互卡片**。
它**不是改 Hermes 源码**，而是以同名平台覆盖内置 feishu 适配器（`platform_registry` 后写胜出）。

```
用户在飞书发消息
      ↓
上游 Hermes 网关（gateway/）
  · 跑 agent → 产出：工具进度文本 / 中途汇报 / 最终回复
  · 每种都调用 adapter.send() 或 adapter.edit_message()
      ↓
【本插件】CardFeishuAdapter（card_adapter.py）  ← 唯一入口，判类型
      ↓
ChatCardSession（card_session.py）              ← 单写者状态机，管一张卡的一生
      ↓
card_render.py（纯函数）                        ← blocks → 卡片 JSON / 容量判定 / 分块
      ↓
飞书 API：POST /im/v1/messages（建卡） / PATCH /im/v1/messages/{id}（原地更新）
```

### 1.2 文件职责（改之前先认门）

| 文件 | 职责 | 改它的风险 |
|---|---|---|
| `card_render.py` | **纯函数**：分类、摘要行、元素构造、容量判定、分块。不碰 I/O、不导入 lark | 低（可单测），但它是所有判断的源头，改判据影响面最大 |
| `card_session.py` | **单写者状态机**：一张卡的 blocks/状态/计数/换卡/密封 | 中高：`committed_count`、`tool_phase_closed` 这类记账字段改错会丢内容或重复 |
| `card_adapter.py` | 覆盖 `send`/`edit_message`/`create_card`/`patch_card`/入站复位；硬超时 | 中：这里决定"一条消息走哪条路"，判错就丢内容 |
| `card_plugin.py` | 注册层（同名覆盖 `feishu` 平台），刻意零重导入 | 低 |
| `plugin.yaml` | 插件清单（**不是** plugin.toml） | 低 |

### 1.3 部署模型（容易踩的坑之一）

插件在内置目录是**物理副本**，不是软链。共 4 份：

```
%LOCALAPPDATA%\hermes\plugins\feishu-card                        (default profile)
%LOCALAPPDATA%\hermes\profiles\feishu2\plugins\feishu-card
%LOCALAPPDATA%\hermes\profiles\basketball\plugins\feishu-card
%LOCALAPPDATA%\hermes\profiles\football2\plugins\feishu-card
```

**改了源 → 必须用安装器分发 + 重启**，否则线上跑的还是旧代码：

```powershell
cd <项目>\plugin
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Restart                    # default
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Profile feishu2 -Restart    # 其余 3 个同理
```

部署后**必须逐字节复验落地副本**（§5.4）—— 曾经"部署成功"但其中一个 profile 是旧文件。

---

## 2. 关键机制（判据表）

**改任何行为前，先在这里找到对应机制，别新发明一套。**

| 机制 | 判据 / 规则 | 代码位置 |
|---|---|---|
| **消息分类** | 先判 CLOSING（💾 记忆通知）；**`interim=True`（上游 `_interim_send`）→ 一律正文**；再看形状（terminal emoji / 工具表头 / 裸 fenced 块） | `card_render.classify()` |
| **中途汇报 vs 工具进度** | 看上游标记 `metadata["_interim_send"]`，**不看文本形状** | `card_adapter.send()` |
| **裸 ``` 块是不是工具** | `interim=False` **且** `session.saw_tools()` **且** `not session.tool_phase_closed()` | `card_adapter._refine_kind()` |
| **工具阶段何时结束** | 一条**非汇报**的正文写入即结束；工具再来会重新打开 | `card_session.handle_text(interim=)` / `handle_tool()` |
| **工具摘要行** | `- {⏳\|✅} · {工具名或上游原句} · {详情}`；取不到就照搬原文，**绝不编造** | `card_render.parse_tool_line()` / `render_tool_line()` |
| **状态符号** | 运行中 `⏳`；**卡片封口**才 `✅`（整轮结束⇒工具必然已返回）。上游不提供完成/失败信号，**不猜 ❌** | `card_render._mark()` |
| **「✅ 完成」何时出现** | **只由真信号触发**：上游最终回复的 `metadata["notify"]=True`（唯一设置点 `gateway/platforms/base.py:148`）或 `edit_message(finalize=True)`。**绝不用计时器猜**（见 P16） | `card_adapter.send()` → `session.seal(reason="final")` |
| **长时间无进展** | 空闲 > `CARD_IDLE_DELAY`(200s，>心跳 180s) → 写诚实提示「运行中…（已 N 分钟无新动作）」，**不宣称完成**；最多 `CARD_IDLE_NOTICE_MAX`(5) 次 | `card_session._idle_after_delay()` |
| **折叠展开规则** | `expanded ?? (status == "running")` —— 运行中展开、封口后折叠（照抄 ZCode） | `card_render.block_elements()` |
| **元素超限兜底** | 元素 > 40 → 更早内容折进「📎 更早过程」，保留最新 10 个可见。**必须无损**：超过 `FOLD_CHUNK_CHARS` 就**多切一个面板**，绝不截断丢弃（曾丢 57% 内容，见 P15） | `card_render.fold_elements()` / `_chunk_text()` |
| **容量换卡** | 追加前判定：appends≥30 / blocks≥60 / bytes≥15000 / **tables≥5** 任一命中 | `card_render.limit_reason()` |
| **换卡不丢不重** | 旧卡只 PATCH `blocks[:committed_count]`；新卡 = 一句「（接上一条卡片）」+ 未上卡增量 | `card_session._rotate_locked()` |
| **换卡循环防护** | `committed_count == 0`（换完卡还没写成功过）→ **不换**，换了搬的是同一份 | `card_session._rotate_if_needed_locked()` |
| **重复文本去重** | 同一条文本已在本会话写过 → 跳过（`written_texts`） | `card_session.handle_text()` |
| **心跳不吞内容** | 心跳 ≠ 整卡替换，落成 `notice` 块（原地更新、出正文/收尾自动消失） | `card_session.update_notice()` |
| **英文内心推理过滤** | 仅对带 `_interim_send` 的：**无汉字且拉丁字母≥15** → 丢弃（保守：宁可多显示） | `card_adapter._skip_as_interim_reasoning()` |
| **卡片调用硬超时** | 建卡/PATCH 15s（`HERMES_FEISHU_CARD_TIMEOUT`）；审批卡 30s | `card_adapter._with_deadline()` |
| **紧急总开关** | `HERMES_FEISHU_CARD_MODE=0` → 完全退回官方纯文本行为 | `card_adapter._card_mode_enabled()` |

### 2.1 常量（改前必读：为什么是这个数）

| 常量 | 值 | 依据 |
|---|---|---|
| `MAX_MESSAGE_LENGTH` | 8000 | 飞书单消息上限 |
| `CARD_MAX_APPENDS` | 30 | 自定：防止触达请求体/元素上限后被拒 |
| `CARD_MAX_BLOCKS` | 60 | 自定 |
| `CARD_MAX_BYTES` | 15000 | 飞书 interactive 请求体 ~30KB，留一半 |
| `CARD_MAX_TABLES` | **5** | **飞书官方硬上限 5**（超出报 11310）。**判定用 `>=` 留一张余量**（判定发生在追加前） |
| `MAX_CARD_ELEMENTS` | 40 | 飞书元素上限约 50，留余量 |
| `KEEP_TAIL_ELEMENTS` | 10 | 折叠时保留可见的最新元素数 |
| `FOLD_CHUNK_CHARS` | 3000 | 每个折叠面板的正文上限（单元素正文保守值）。**超过就多切一块面板，不许截断** |
| `TOOL_BLOCK_MAX_CHARS` | 4000 | 工具面板正文上限。**已知有界裁剪**：裁掉更早工具行并标注「更早记录已省略」（工具行非需求重点，受换卡上限约束通常够用） |
| `CONSECUTIVE_FAILURE_LIMIT` | 3 | 连续 PATCH 失败 3 次 → 放弃卡片、降级纯文本 |
| `ROTATE_BUDGET_DIVISOR` | 5 | 换卡配额 = `write_attempts // 5`（防换卡机器） |
| `CARD_SEAL_DELAY` | 8.0s | 无新内容 8 秒 → 状态改「✅ 完成」 |

---

## 3. 上游耦合点（**最容易踩坑的地方**）

**我们依赖上游这些具体行为。上游一升级，逐条核对本表。**

| # | 上游行为 | 位置 | 我们依赖它做什么 | 它变了会怎样 / 怎么发现 |
|---|---|---|---|---|
| U1 | 连续 terminal 调用**丢掉重复表头**（产生裸 ``` 块） | `gateway/run_turn_runner.py:239` | 裸块 = 工具命令（`_refine_kind`） | 上游改了 → 工具可能不再折叠或正文被误折叠。用 `snapshot_card.py` 复查 |
| U2 | 工具进度文本**两种形状**：verbose = `名字(['键名'])+JSON`；其它 = 人类预览（无括号） | `gateway/run_turn_runner.py:258-271` | `parse_tool_line()` 分形态解析 | **改 `tool_progress` 配置必须同步回归解析器**（[[教训检讨]] 015 的根因） |
| U3 | 中途汇报带 `metadata["_interim_send"]=True` | `gateway/run.py:463`、`gateway/stream_consumer_fallback.py:330` | **分类的唯一可靠依据**（R1/R2 都靠它） | 上游去掉这个标记 → 汇报又会被当工具/正文判错。用交互测试发现 |
| U4 | 中途汇报开关默认 `True`，由 `display.interim_assistant_messages` 控制 | `gateway/display_config.py:23`、`run_turn_runner.py:909` | R1（CM 要看到汇报） | 被设 false → **汇报在源头就没了**（曾经就是我干的）。查配置即可 |
| U5 | 同名平台注册"后写胜出"，插件可覆盖内置适配器 | `gateway/platform_registry.py` | 整个插件的存在基础 | 上游改成"先写胜出" → 插件失效。启动日志应有 `platform 'feishu' registered with card-mode adapter` |
| U6 | 插件钩子 API：`ctx.register_hook(name, cb)`（`pre_tool_call`/`post_tool_call` 等） | `hermes_cli/plugins.py:904` | **暂未使用**。若 `parse_tool_line` 再不稳，这是拿结构化数据（`tool_name/args/status`）的正路 | `model_tools.py:677-687` 有 payload 形状 |
| U7 | `display.streaming` 是 **CLI-only**，网关不读它 | `gateway/display_config.py:106` | 别在这上面浪费时间 | 改它无效（曾经白改一次） |
| U8 | 卡片 2.0 消息**无法通过 API 读回**（GET 只返回"请升级客户端"占位符） | 飞书侧 | 所以只能用**本地快照**复现卡片内容 | 别指望拉线上卡片来核对 |

---

## 4. 踩坑全表（**核心章节**）

按"症状 → 根因 → 修法 → 防复发"记。**改动前先扫一遍，看有没有踩到同一类。**

| # | 症状（用户看到什么） | 根因 | 修法 | 防复发断言 |
|---|---|---|---|---|
| P1 | 进程活着、日志停住、飞书完全不回话 | 卡片 PATCH 挂起永久占锁（lark 调用无超时） | 卡片调用加 `asyncio.wait_for` 硬超时 → 失败换卡 → 再失败降级纯文本 | `_with_deadline` 超时断言（烟测） |
| P2 | 长任务心跳一弹，「已有的内容被吞掉」，且不再恢复 | 心跳走 `edit_message`，被当成"整卡替换" | 心跳落成 `notice` 块（原地更新、收尾消失） | `test_heartbeat_notice_never_wipes_content`（10 项） |
| P3 | 同一段内容分 2+ 张卡、内容大部分重复 | ①换卡把尾部内容**复制**到新卡 ②`edit_message` 被当"追加" ③分块失败后整段重发 | `committed_count` 记账（同一内容只留一份）+ 新卡只放提示行 + `replace_last_text` + 只补发**未入卡**的剩余块 | 探针 I11/I12（卡内不重复/跨卡不重复）、`test_patch_failure_rotation` |
| P4 | 只有第一条工具折叠，其余代码框全摊开 | 上游丢表头的裸块被判成正文；判定依赖"最后一块是不是工具面板"，**被汇报打断** | `saw_tools()` + `tool_phase_closed()` 取代"最后一块" | 交互测试断言"工具必须折叠"、快照 16/16 折叠✓ |
| P5 | 工具行读不懂：工具名变成字面量 `tool`、或只剩参数键名 `(['path'])` | 在**上游已排版好的文本**上做二次解析（信息源选错）；且改了 `tool_progress` 配置没同步改解析器 | `parse_tool_line`：能确定就取、取不到**照搬原文**，绝不编造；配置回归 | `snapshot_card.py --check`（门禁）、`test_tool_line_never_degrades`（12 项） |
| P6 | 卡片里出现后台思考过程 | `reasoning` 混进 `content` | 插件层过滤"英文内心推理"（仅对 `_interim_send`） | 烟测过滤层 9 项 |
| P7 | 卡片被飞书拒（11310 element exceeds the limit）→ 降级纯文本 | 元素数超上限 | `fold_elements()`：>40 折进「📎 更早过程」 | `test_element_cap_folding`（7 项） |
| P8 | 卡片被飞书拒（11310 card table number over limit）；一个回复被拆两张卡 | 表格上限设 **2**（官方是 5）；且判定用 `>` **没留余量**（判定在追加前） | 改 `CARD_MAX_TABLES=5` + 判定改 `>=` | `test_table_limit_matches_feishu_official`（7 项） |
| P9 | 「一直在重复发东西」：45 秒连换 8 张卡，最后 `11310` 建卡失败 | 换卡把**超限内容整份**搬给新卡 → 新卡一出生又超限 → 死循环 | `committed_count == 0` 时不换卡（换了也解决不了） | `test_no_rotation_loop_when_rotation_would_not_help`（4 项） |
| P10 | 中途说的话**完全不显示** | 我按自己判断把 `interim_assistant_messages` 关成 `false` → 源头就没了 | 恢复 `true`（上游默认值）；**不许用自己的判断覆盖 CM 明确要求** | §requirements R1 + 启动日志里 `interim narration` 行 |
| P11 | 发给他的内容"丢掉了"（汇报不见了） | 用**emoji 黑名单**猜分类：`🚨` 不在名单 → 判成 TOOL → 扔进折叠面板 | 改用上游可靠标记 `_interim_send` | 交互测试断言"汇报必须在可见正文、不在面板" |
| P12 | 改了源、真机没变化 | 插件是 4 份**物理副本**，没分发；或网关没重启 | 安装器分发 + 重启 + **落地副本逐字节复验** | 部署后 diff 4 份副本 + 关键常量/关键字串核对 |
| P13 | 网关重启后仍跑旧代码（"mixed sys.modules"） | `hermes update` 拉代码但没重启运行中的网关 | 重启并核对启动日志的插件注册行 | 部署脚本输出 `platform 'feishu' registered` |
| P16 | 卡片末行状态不对：①一直「回复中」其实不动了 ②「已完成」后又冒内容 | 用 `CARD_SEAL_DELAY=8s` 静默**猜**完成：agent 单次 API 调用常 >8s（提前误报）；长任务心跳 180s 每次都取消计时器（永远停在「回复中」）。**同源：不用真信号、用计时器猜** | 改用上游 `notify=True` 真信号封口；计时器降级为诚实的空闲提示 | `test_status_is_truthful`(7) + `smoke_live` ⑤ 端到端 |
| P15 | **换卡时旧消息被吞**（卡片越长越明显） | 元素折叠把面板正文**硬截断到 3000 字**、其余丢弃（`folded_text[:3000] + "…（更早过程已省略）"`）；容量上限却允许卡片长到 30 次追加 —— **两个机制互相矛盾**。实测折叠 50 元素丢 57%、28/50 段消失 | 改为按 `FOLD_CHUNK_CHARS` 切成**多个面板**，无损 | `test_fold_never_drops_content`（8 项） |
| P14 | 页面文件/内存打满导致整机卡死（与卡片同源项目的另一条线） | 6 路并行子代理 grep 吃 44GB；pagefile 仅 2048MB | 并发 `max_concurrent_children: 6→3`；pagefile `8192/65536`（重启生效） | [[教训检讨]] 014 |

### 4.1 两类"方法论"坑（比技术坑更贵）

1. **在别人排好版的文本上做二次解析**（P5）—— 信息源选错，再多正则也补不出文本里没有的字段。
   正解顺序：① 有没有**结构化事件/标记**可用？→ ② 有就用它 → ③ 只能解析文本时，**先固定快照样本**，
   取不到就留空/照搬，**绝不编造兜底值**。
2. **单功能各自测通 ≠ 一起用没问题**（P4+P11 同时爆发）—— 2026-09-15 一天内因为这条返工 3 次。
   凡同一条数据流上有多个功能，**必须写功能交互测试**（范例：`smoke_live.py::offline_feature_interaction`）。

---

## 5. 维护流程与标准

### 5.1 改之前（**[开发标准] §10 改动纪律**，缺一条就是违规）

1. **通读受影响的全部代码**（不只改的那几行）：画清调用关系、状态流向。
2. **建立基线证据**：`python tests/snapshot_card.py` 先跑一遍存下来（改前行为可复现）。
3. **写影响面清单**：每个要改的共享函数/常量/状态字段，逐条写"还有谁在用、会不会变样"。
4. 判定"要不要动上游配置"：**动配置 = 必须同步回归解析器**（P5 的根因）。

### 5.2 改的时候（六条禁令）

禁编造兜底值 ｜ 禁靠猜分类外部输入 ｜ 禁用自己判断覆盖 CM 明确要求 ｜
禁无回归改共享阈值（对齐官方真值 + 留余量 + 注释写出处）｜ 禁新增"满了就新建"的循环 ｜ 禁假保险丝。

### 5.3 改之后 —— 四道门（**一条都不许跳**）

```bash
cd plugin/feishu-card
python tests/test_card_session.py     # ① 单测（状态机 + 纯函数 + 各类回归锁）
python tests/snapshot_card.py --check # ② 卡片内容快照门禁（改前/改后逐行可比）
python tests/probe_invariants.py      # ③ 594 条状态机操作序列不变量（不丢/不重/不爆炸）
python tests/smoke_live.py --no-send  # ④ 出站路由 + 中途汇报 + **功能交互**（离线）
```

- **跑四条，不是只跑新加的那几条。**
- 新增断言必须验证过"**去掉修复它就会失败**"（否则是假保险丝）。
- 涉及"同一条数据流上多个功能"的改动 → **交互测试是交付门槛**。

### 5.4 部署（4 步，缺一步就可能线上还是旧代码）

```powershell
# ① 分发 + 重启（4 个 profile 都要）
cd <项目>\plugin
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Restart
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Profile feishu2 -Restart
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Profile basketball -Restart
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Profile football2 -Restart
```
② **逐字节复验落地副本**（`diff` 4 份 vs 源，并确认关键字串在线上副本里，如新常量/新函数名）。
③ 确认**新进程**里注册成功：`agent.log` 里应有 `platform 'feishu' registered with card-mode adapter`。
④ 真机跑一条会"边干边说"的活，按 `requirements.md` 逐条核对。

### 5.5 回滚

- 单个行为回退：`HERMES_FEISHU_CARD_MODE=0` → 完全退回官方纯文本（不建卡）。
- 配置回退：本目录每次改配置都留 `config.yaml.bak-<时间戳>`。
- 代码回退：项目是 git 仓库，`git revert` 后**重跑四道门 + 重新分发**（副本不会被 git 带回）。

---

## 6. 排障手册（日志关键字 → 含义 → 处置）

日志在 `%LOCALAPPDATA%\hermes\{logs|profiles\<p>\logs}\gateway.log`（插件日志在 `agent.log`）。
**注意：用 Python 以 UTF-8 读**，Git Bash 里 grep 中文会显示成乱码。

| 日志关键字 | 含义 | 处置 |
|---|---|---|
| `card created \| … seq=1 appends=1 blocks=1` | 新建一张卡（新一轮或换卡） | 正常。**一个回合多张**才要查 |
| `card at capacity (triggered by …)` | 换卡，并**说明了是哪条阈值触发** | `tables …` 出现过说明表格还有问题；`appends 30>=30` 是设计内正常 |
| `rotated card (capacity): kept N block(s) …, carried M pending block(s)` | 换卡完成，`kept` 留在旧卡、`carried` 搬到新卡 | `carried` 大且反复出现 → 查 P9 循环防护 |
| `… skipping rotation`（`rotating again would carry the SAME payload`） | 循环防护生效：不再换 | 正常（说明换卡解决不了）。若频繁 → 内容单块就超限，需查该块大小 |
| `patch failed` / `create_card failed: … 11310` | 飞书拒绝卡片 | 看 `ErrMsg`：`card table number over limit` → 表格；`element exceeds the limit` → 元素 |
| `falling back to plain send for the REMAINING n chunk(s)` | 卡片链路失败，剩余块降级纯文本 | 单次可接受；频繁 → 查容量/超时 |
| `rotation budget exhausted` | 换卡配额用尽，继续写当前卡 | 正常兜底；频繁 → 容量阈值可能过紧 |
| `interim narration \| kind=tool` | **汇报被判成工具 → 会进折叠面板 = 用户看不到** | 严重：检查 `_interim_send` 标记是否还在（U3） |
| `interim narration \| kind=text` | 汇报正常进正文 | 正常（这是 R1 的通过标志） |
| `english interim narration dropped` | 英文内心推理被过滤 | 正常 |
| `duplicate text skipped` | 同一文本重复写入被拦 | 正常（防重复） |
| 卡片里出现「更早过程已省略」/「更早记录已省略」 | 折叠或工具面板**裁剪了内容** | 「更早过程」不该再出现（P15 已修）；「更早记录」是工具面板的有界裁剪，属已知行为 |
| `platform 'feishu' registered with card-mode adapter` | 插件覆盖成功 | **没有这行 = 插件没生效** |

---

## 6.5 兄弟项目：DSH 的飞书卡片（**改一边要查另一边**）

DSH（DeepSeek Harness）有一个**功能同源**的飞书卡片插件：
`Ai100/projects/dsh-feishucard/`（项目档案 `系统/项目/DSH接入飞书/README.md`）。

**两边是同一条数据流上的同类实现，坑会同时存在。** 2026-09-16 交叉审计的结论：
- DSH **也有**"折叠静默丢内容"（同款 `slice(0, 3000)`）→ 已同步修；
- DSH **完全没有**表格数量防护 → 已加"第 6 张起改代码块"；
- DSH **没有**"靠文本形状猜分类"这个坑 —— 它从**结构化事件**渲染（架构比本插件好），
  这也是本插件 C6（`post_tool_call` 钩子）将来可以借鉴的方向。

**规则**：在本插件修的"机制级"缺陷（折叠 / 容量 / 去重 / 限流），修完必须去 DSH 那份**对照一遍**；
反之亦然。DSH 重启必须走**读 key 的启动器**（裸 `node` 启动 = 空白回复）。

---

### 6.6 症状：飞书发消息，bot 完全不回（2026-09-16 实测排障）

**先看日志里有没有这一行**：`gateway.run: inbound message: platform=feishu …`
（这才是"回合被派发"的标志）

| 日志序列 | 含义 | 处置 |
|---|---|---|
| `Received raw message` → `Inbound dm message received` → `Flushing text batch` → **`gateway.run: inbound message`** → `card created` | 正常：回合已派发 | 若仍无回复，看 agent.log 有无 API 错误 |
| `Received raw message` → … → `Flushing text batch` → `card created` → **之后什么都没有** | **回合没被派发**：上一条消息所在的回合还没结束，新消息被 busy 确认 + 插入/排队进那个回合 | 该会话的回合卡住了 → **重启该 profile 的网关**（清掉内存里的运行状态） |

- **同族线索**：`gateway.run: Queued follow-up … final stream delivery not confirmed` —— 上一次回复的最终投递没被确认，网关就把后续消息排成 follow-up。
  这个告警在 09-14 起就出现过（**早于本插件的折叠/表格改动**），属上游运行机制的老问题。
- **怎么判断跟本插件有关**：查该 profile 有没有卡片写入失败 ——
  `11310` / `patch failed` / `create_card failed` / `card pipeline failed` / `falling back`。
  2026-09-16 那次实测：**这些全是 0 条**，所以与本插件无关（是上游会话/回合状态卡住）。
- **处置命令**（重启该 profile）：
  `powershell -ExecutionPolicy Bypass -File plugin\install-plugin.ps1 -Profile football2 -Restart`
  重启后务必确认 `[Feishu] Connected in websocket mode` + `✓ feishu connected` + 插件注册行。
- **高发场景**：超长回合 + 大量并行子代理（那次是 8 次委派、跨 40 分钟），且卡片触顶
  `card at capacity but rotation budget exhausted`。修卡片不会治这个病 —— 它属上游回合收尾。

## 7. 边界与红线

**不许碰**：
- 上游 `hermes-agent/` 里任何文件（保持其 git 干净）——我们用**同名覆盖**而不是改源码。
- `display.streaming`（U7，无效设置）。
- 用户会话里已有的历史卡片（只影响新产生的）。

**不许做**：
- 用自己的判断覆盖 CM 明确说过的要求（P10 就是这么来的）。
- 在没有基线快照的情况下改判据/阈值（§10.1）。
- 把 `interim_assistant_messages` 关掉 —— CM 明确要求看到中途汇报。

**必须做**：
- 改完回来把 `requirements.md` 逐条打勾，**拿证据**（日志/快照/测试输出），不许说"应该好了"。
- 每个新踩的坑 → 追加到 §4 表格，并在 `CHANGELOG.md` 记一次。

---

## 8. 一句话总结（给未来的自己）

> **这个插件的所有历史事故，都源于"猜"：猜文本形状、猜上游意图、猜阈值、猜部署是否生效。**
> 可靠的做法只有三条：**用协议方给的标记**、**把行为固化成可复现证据再改**、**改完跑全部门禁并核对线上副本**。
