本项目所有重要变更都记录在此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。All notable changes are documented here.

## [v2.1.8] · 2026-09-16

### 修复 / Fixed
**卡片状态行不真实**（CM：「最后一句写着"回复中"或者"已完成"，但这个状态是不对的」）。两个具体表现：
1. 一直说「回复中」，其实后台已经不动了；
2. 说「已完成」，过一会儿又冒内容出来。

**根因：用计时器猜，而不是用真信号。** 旧实现 `CARD_SEAL_DELAY = 8.0` —— "8 秒没新内容就当成完成"：
- agent 单次 API 调用常 >8 秒（实测 #16 那次 latency=60.4s）→ **提前误报"已完成"** → 症状 2；
- 长任务心跳间隔 **180 秒**（`HERMES_AGENT_NOTIFY_INTERVAL` 默认 180），每次心跳都
  `_cancel_idle()` → 计时器永远没机会触发 → **永远停在"回复中"** → 症状 1。

**真信号终于找到**：上游对**最终回复**打的 `metadata["notify"] = True`
（`gateway/platforms/base.py:148 _mark_notify_metadata`，注释原文
*"Final content gets notify=True; typing metadata stays unmarked"*，全仓唯一设置点）。

**修法**
- `card_adapter.send()`：识别 `notify=True` → 写完内容后 `session.seal(reason="final")`。
  这是「✅ 完成」**唯一**的触发路径（另一条是 `edit_message(finalize=True)`）。
- `card_session`：空闲计时器**不再宣称完成** —— 到点只写一条诚实提示
  `_⏳ 运行中…（已 N 分钟无新动作）_`（复用 notice 机制：原地更新、出正文自动消失），
  并续盯（最多 `CARD_IDLE_NOTICE_MAX = 5` 次，防无限 PATCH）。
- `CARD_SEAL_DELAY=8.0` → `CARD_IDLE_DELAY=200.0`（> 心跳 180s，避免与心跳互相打架）；
  构造参数 `seal_delay` → `idle_delay`（名字必须与行为一致）。
- `CardState` 新增 `idle_notices`（算"已 N 分钟"并封顶）。

**效果**：回合真正结束时才显示「✅ 完成」；长时间没动静时明确写出"多久没动"，
不再用假状态糊弄（配合门狗 v9.3 的会话卡死告警）。

### 测试 / Tests
- 新增 `test_status_is_truthful`（7 项）：汇报/工具进度/正文本身都**不**自动封口；
  真信号（`seal(reason="final")`）才封口且末行显示「完成」；空闲文案含分钟数且**不宣称完成**。
- 更新 `test_reset_and_seal_timer` 为新语义（空闲计时器只写 notice、不再假封口）。
- 新增**端到端**断言（`smoke_live.offline_feature_interaction` ⑤）：走真实 `adapter.send`，
  `notify=True` 才封口、末行显示「_✅ 完成_」。
- **过程中抓到两个"假保险丝"并修正**：
  ① 新测试写成 `async def` 却在 `main()` 里直接调用（没 `asyncio.run`）→ **一次都没跑过**、也不报错；
  ② 端到端断言写在了 `finally` 恢复真实传输**之后** → 打到真飞书 API 失败。
  两者都印证 [[开发标准]] §10.2「禁假保险丝」：断言必须证明它真的会执行。
- 四道门全绿；4 profile 分发重启 + 落地副本逐字节复验（含确认旧常量只剩注释）。

## [v2.1.7] · 2026-09-16

### 修复 / Fixed
**换卡时旧消息被吞**（CM：「本来正常，在换卡时出问题了，旧消息被吞」）。

**根因：两个机制互相矛盾 —— 元素折叠会静默丢弃内容。**
`fold_elements()` 把"更早过程"折进一个面板后，把面板正文**硬截断到 3000 字、其余直接丢弃**：
```python
if len(folded_text) > 3000:
    folded_text = folded_text[:3000].rstrip() + "\n\n…（更早过程已省略）"
```
而容量上限允许卡片长到 **30 次追加 / 60 块 / 15000 字节**，远超 3000 字能保住的量。
于是卡片一长（正好也是换卡前后），**中间那一大段历史被删掉**，只剩一句"更早过程已省略"。

**复现证据**（按 §10 先复现再改）：50 个元素折叠 → 折叠前 13229 字 → 折叠后 5662 字，
**丢 57%、28/50 段彻底消失**（第 12–39 段）。

**修法（无损折叠）**：不再截断，改为按 `FOLD_CHUNK_CHARS = 3000` **切成多个面板**：
新增 `_chunk_text()`（按段落边界切、单段超长硬切，**不丢字符**）与 `_fold_panel()`；
面板标题多块时显示 `📎 更早过程 (i/N)`。
- 修复后同一用例：50 元素 / 13229 字 → **14 元素 / 13265 字，0 丢失**，每块 ≤ 3000 字，元素数 ≤ 40。
- 元素预算自洽：面板数 ≈ ⌈折叠字数/3000⌉，整卡受 `CARD_MAX_BYTES`(15000) 约束 ⇒ 最多约 5 块 + 尾部 10 ≈ 15 ≪ 40。

### 测试 / Tests
- 新增 `test_fold_never_drops_content`（8 项）：触发折叠 / 折叠后元素数在上限内 /
  **折叠后字符数不少于折叠前** / **50 段一段都没被吞** / 不再出现「已省略」丢弃标记 /
  每块面板 ≤ 单元素上限 / 内容大时切成多个面板 / 未超限时原样返回。
- 四道门全绿；已分发 4 profile + 重启（00:29–00:30）；**线上副本行为实测**：
  50 元素 / 40389 字 → 24 元素 / 40415 字，**丢失 0/50**、无丢弃标记、14 块面板每块 ≤ 3000 字。

### 说明 / Notes
- 同类"静默丢弃"还有一处**保留**：`tool_block()` 的 `TOOL_BLOCK_MAX_CHARS=4000`
  会裁掉更早的工具行并标注「更早记录已省略」。它是有界裁剪（受换卡上限约束、通常够用），
  且工具行不是 CM 的需求重点 —— 按 §8 最小化本次不改，已在 `docs/maintenance.md` 记为已知行为。

## [v2.1.6] · 2026-09-16

### 真机验收通过（feishu2，2026-09-16 00:15–00:16 多轮对话）

CM 要求「你去看一下现在正常没有」。按 `docs/requirements.md` 逐条核对，**修前 vs 修后**：

| 指标 | 修前（09-15 全天） | 修后（09-16 00:00 起） | 判定 |
|---|---|---|---|
| 中途汇报被判成 `kind=tool`（会进折叠面板 = 用户看不到） | 1 | **0** | R1 ✅ |
| 中途汇报 `kind=text`（可见正文） | — | **24 条，全部** | R1 ✅ |
| 飞书 11310 报错 | 22 | **0** | R3/R4 ✅ |
| 换卡次数 | 24 | **1** | R4 ✅ |
| 换卡触发原因 | 表格数（旧上限 2） | `appends 30>=30`（设计内正常上限） | R4 ✅ |
| 卡片链路失败降级纯文本 | 11 | **0** | ✅ |

样例（可见正文里的阶段汇报，原文）：
```
00:15:41  interim narration | kind=text | 后台跑着。同时开始**攒真实盘口样本** —— 先看预测记录里已有什么：
00:16:16  interim narration | kind=text | 0 匹配 —— 总表用的是英文队名。先看名字格式再修：
00:16:55  interim narration | kind=text | 46 个中文名（丹超/挪超/英冠等无别名表）。**建映射 → 用「日期+联赛」实际对局自校验**：
```
换卡日志（新格式，能直接看出是哪条阈值触发）：
```
card at capacity (triggered by appends 30>=30 | appends=30 blocks=26 bytes=9202 tables=0); rotating
rotated card (capacity): kept 26 block(s) on the old card, carried 0 pending block(s) to the new one
```
（`carried 0` ⇒ 新卡没有再复制旧卡内容 ⇒ 无重复；`tables=0` ⇒ 表格已不是诱因。）

**结论：R1 / R2 / R3 / R4 全部真机通过。**

### 新增 / Added
- **`docs/maintenance.md` —— 维护与技术手册**（CM 要求：「把这个飞书卡片踩过的坑全部整理，
  写一个开发技术文档……不要下次我要修的时候，又重复造轮子或者重复踩坑」）。共 8 节：
  1. 系统全景（数据流图 + 文件职责 + **部署模型：4 份物理副本**）
  2. 关键机制判据表（15 条，改行为前先在这里找对应机制，别新发明）+ 常量依据表
  3. **上游耦合点清单 U1–U8**（每条：上游行为 / 代码位置 / 我们依赖它做什么 / 它变了会怎样 / 怎么发现）
  4. **踩坑全表 P1–P14**（症状 / 根因 / 修法 / 防复发断言）+ 两类方法论坑
  5. 维护流程与标准（改前三条硬性动作 / 六条禁令 / **四道门** / 4 步部署 / 回滚）
  6. **排障手册**（13 个日志关键字 → 含义 → 处置；含"Git Bash grep 中文会乱码，要用 Python 以 UTF-8 读"）
  7. 边界与红线
  8. 一句话总结（所有历史事故都源于"猜"）
- README 目录树加入 `docs/maintenance.md` 与 `docs/requirements.md` 入口（标 ⭐ 必读）。

### 说明 / Notes
- 手册里把「**单功能各自测通 ≠ 一起用没问题**」写成了硬规定：同一条数据流上多功能的改动，
  功能交互测试是**交付门槛**（2026-09-15 一天内因这条返工 3 次）。
- 把「**不许用自己的判断覆盖 CM 明确要求**」也列进红线（P10：我曾把中途汇报关掉）。

## [v2.1.5] · 2026-09-15

### 修复 / Fixed
**启用中途汇报（R1）把工具折叠（R2）撞坏了** —— CM：「该折叠的工具调用又没有折叠了」
「还出现了一个新问题：发给我的东西直接丢掉了，丢失了，不显示了」「修来修去，越修越坏」。

**两个症状同一个根因：用文本形状猜分类。**

| 症状 | 触发文本 | 旧判据 | 结果 |
|---|---|---|---|
| 内容"丢了" | `🚨 **查出严重问题，我先认：** …` | "首字符是不是工具 emoji"（黑名单） | 🚨 不在黑名单 → 判成 TOOL → **扔进折叠面板** → 用户看不到 |
| 该折叠的没折叠 | 汇报之后到来的裸 ``` 命令块 | "会话最后一块是不是未结束的工具面板" | 汇报把最后一块变成正文块 → 判成 TEXT → **摊在正文里** |

实机证据（feishu2 日志）：`interim narration | kind=tool | 🚨 **查出严重问题，我先认：**…`
—— 上游标记为"中途汇报"的消息，被我的形状启发式判成了工具。

### 变更 / Changed
**改用上游的可靠标记，不再猜文本形状**（`gateway/run.py:457 _interim_metadata` 明确标注）：
- `card_render.classify(content, *, interim=False)`：`interim=True`（上游 `metadata["_interim_send"]`）
  → **一律正文**，绝不可能是工具进度。带标记的消息是"给用户看的汇报"，与形状无关。
- `card_adapter._refine_kind(...)`：**删除**对 `session.in_tool_context()`（"最后一块"）的依赖 ——
  那正是被汇报打断的脆弱点。改为 `saw_tools() and not tool_phase_closed()`：
  - `saw_tools()`：本轮是否出现过工具面板（**不会**被汇报打断）；
  - `tool_phase_closed()`：工具阶段是否已被一条**非汇报**的正文划上句号
    （`handle_text(..., interim=True)` 不算结束）。
  这样两种情况都对：汇报后的工具裸块 → 折叠；工具跑完、出了正文回复后出现的整块代码
  → 是"用户要的代码"，保持可见（既有的那条断言因此仍然通过）。
- `card_adapter.edit_message()`：`finalize=True` 时优先判正文（收尾编辑一定是正文），
  否则裸块先判 TOOL —— 否则 `"\n" in text` 会让裸块走 `replace_last_text`、又落回正文不折叠。
- `card_adapter.send()`：把 `interim` 透传给 `handle_text`。

### 测试 / Tests（这次补上真正缺的那一类）
- **新增功能交互测试** `smoke_live.py::offline_feature_interaction`（20 项断言）：
  在**同一轮**里混入 带标记的汇报 / 带表头的工具 / 裸块工具 / `⚙️ Reading` 工具 /
  edit 路径的裸块 / 收尾正文，逐条断言：
  ① 汇报必须出现在**可见正文**里且不在折叠面板里；② 工具必须出现在**折叠面板**里且不在正文里；
  ③ 每条输入的关键片段都能在卡片里找到（**内容不许丢**）；④ 正文与面板不重叠。
  **这一类测试此前完全缺失** —— 这就是"各自单测都绿、一起用就撞坏"的原因。
- 四道门全绿（单测 / 快照门禁 / 594 条不变量探针 / 烟测），落地副本 4 profile 逐一复验
  （含确认 `interim` 透传、`tool_phase_closed`、循环防护、`CARD_MAX_TABLES = 5` 都已上线）。

### 规范 / Process
- **`Ai100/obsidian/系统/开发标准.md` 新增 §10 改动纪律**（CM 要求："去做一套开发标准出来，
  每一次改代码之前强制性阅读它"）。按 CM 2026-08-10 的"唯一开发标准文件"裁定，**不新建文件**，
  而是补进既有标准文件；AGENTS.md A18 已同步指向 §10。要点：
  10.1 动手前三件事（通读受影响代码 / 建基线证据 / 写影响面清单）；
  10.2 六条禁令（禁编造兜底值 / 禁靠猜分类 / 禁覆盖 CM 明确要求 / 禁无回归改共享阈值 /
       禁新增容器循环 / 禁假保险丝）；
  10.3 **功能交互必须组合验证**（本次最大教训）；
  10.4 改动前检查单（10 条）；10.5 违规处理。

## [v2.1.4] · 2026-09-15

### 修复 / Fixed
**换卡循环**（CM：「FU2 这个 BOT，它一直在重复发东西」的实机病灶，本次抓到确证）。

实机证据（feishu2 日志，2026-09-15 01:25）：
```
01:25:18  card at capacity (appends=17 blocks=17 bytes=8701); rotating
01:25:20  card created … seq=21
01:25:20  card at capacity (appends=18 blocks=18 bytes=9429); rotating   ← 新卡 1 秒后又超限
…  45 秒内连换 8 张  …
01:26:04  create_card failed: [230099] … ErrCode: 11310; ErrMsg: card table number over limit
```
**机制**：`_rotate_locked()` 把"还没上卡的增量"整份搬进新卡；若这份内容本身就超限，
新卡一出生就超限 → 立刻再触发换卡 → 搬的还是**同一份**内容 → 无限循环，
直到飞书拒绝建卡（11310）或配额耗尽。

**修法**：`_rotate_if_needed_locked()` 增加单点防护 —— `committed_count == 0`
（自上次换卡以来**没有任何内容成功写上去**）时不再换卡：此时换卡搬的是同一份内容，
**换了也解决不了**，不如交给出站前的 `fold_elements()` 元素折叠兜底。日志新增一句说明。

### 修复 / Fixed（同日第二处：表格上限的判定方向错了）
v2.1.3 把 `CARD_MAX_TABLES` 由 2 改为 5 时，判定仍是 `>` —— **这是错的**：
判定发生在"追加**前**"，前态若已有 5 张表就放行，本次追加的第 6 张会让**整卡被飞书拒绝**。
改为 `>=`（留一张表的余量），实际发出的一卡最多 5 张表 = 官方上限，合规。

统计口径（feishu2 日志全量）：11310 有两种含义，此前一直混为一谈 ——
- `card table number over limit` 81 次（**今天 22 次全部来自本插件、全部是这一条**）
- `element exceeds the limit` 80 次（另有 `fold_elements()` 防护；今天 0 次）

### 测试 / Tests
- 新增 `test_no_rotation_loop_when_rotation_would_not_help`（4 项）：连追加同类超限内容时
  **不得连换卡**（created ≤ 3、rotations ≤ 2）。
- `test_table_limit_matches_feishu_official` 更新为新语义：4 张表不换、**前态 5 张表就换**
  （留余量）、触发原因为 `tables 5>=5(留余量)`。
- 四道门全绿；已分发 4 profile + 重启 + 落地副本逐字节复验（含确认 loopguard 与常量）。

## [v2.1.3] · 2026-09-15

### 修复 / Fixed
**表格上限偏保守 2.5 倍 → 一个回复被拆成两张卡片**（R4 残留）。

实机日志（feishu2，2026-09-15 20:23:01）：
```
card at capacity (appends=9 blocks=6 bytes=3576); rotating
```
三个数字都远低于各自上限（30 / 60 / 15000）—— 真正触发的是**表格数**，但旧日志没写出来，
只看日志根本判不出原因（[[教训检讨]] 015：没有可见性的排查就是猜）。

**根因**：`CARD_MAX_TABLES = 2`，而**飞书官方文档《表格组件》明确：单张卡片最多支持放置五个表格组件**，
超出才报 `ErrCode 11310 / card table number over limit`；上游 Hermes 自身用的也是 5。
CM 的足球分析类对话全是维度/胜率对比表 → 第 3 张表就换卡 → "一个回复两张卡片"。

**修法（两处，都很小）**
- `CARD_MAX_TABLES: 2 → 5`（对齐官方上限与上游实现）。判定条件是 `> CARD_MAX_TABLES`，
  所以恰好 5 张表仍留在同一张卡。`split_chunks_by_tables()` 的单块上限同步放宽（同一常量驱动）。
- 换卡日志补上**触发原因**：新增 `R.limit_reason()`，日志改为
  `card at capacity (triggered by tables 6>5 | appends=… blocks=… bytes=… tables=…)`。

### 测试 / Tests
- 新增 `test_table_limit_matches_feishu_official`（7 项）：常量 == 5、3 张表不换卡、
  5 张表（恰好上限）不换卡、6 张表才换卡、触发原因文案、表数计数不错（连续 `|` 行算 1 张）、
  单块 4 张表不再被切分。
- 更新既有 `split by tables` 断言以匹配新上限（3 张表不再切分 / 6 张表切 2 块）。
- 四道门全绿：单测 / 快照门禁 / 594 条不变量探针 / 出站烟测，全部 exit 0。
- 已分发 4 个 profile + 重启 + 落地副本逐字节复验（含确认副本内 `CARD_MAX_TABLES = 5`）。

### 实机验收（feishu2，2026-09-15 19:59–20:39 多轮对话）
- **R1 中途阶段汇报 ✅ 生效**：新增的可见性日志共记录 17 条汇报，**全部 `kind=text`**
  （即进卡片正文、不是被折进工具面板），例：
  `interim narration | kind=text | 数据对上了。现在用脚本机械地跑那场。`
  `interim narration | kind=text | 别名表有「中文名 → 规范名」列。我把它接起来：`
- **R2 折叠 ✅**：同轮对话未见"裸代码块摊开"。
- **R4 ⚠️→✅**：该轮出现 1 次容量换卡（表格数触发），即本版修复项。

## [v2.1.2] · 2026-09-15

### 修复 / Fixed
**中途阶段汇报完全不显示**（CM：「我说的是他中间的一些说的话，我也要看到……你现在完全搞没了」）。

根因：**是我在源头把它关掉了**。4 个 profile 都被我设成 `interim_assistant_messages: false`，
注释还写着"照抄 ZCode：它的卡片里没有「中途叙述」这一项" —— 我拿自己的判断覆盖了 CM 的明确要求。
后果：`gateway/run_turn_runner.py:909` 的 `want_interim_messages` 为假 → 连 stream consumer 都不建 →
汇报**根本到不了插件**；我为此专门写的英文推理过滤层（`_skip_as_interim_reasoning`）**从未运行过**
（没有东西可过滤）。CM 看到的自然是"完全没有"。

修法（最小化，只回退配置）：
- 4 个 profile 的 `interim_assistant_messages: false` → **`true`**（上游默认值就是 `True`）。
  备份 `config.yaml.bak-20260915-195406`。插件层英文过滤保持不变（保留中文汇报、丢掉纯英文内心独白）。
- 已验证解析结果：4/4 profile 的 `resolve_display_setting(cfg, "feishu", "interim_assistant_messages") == True`。

### 新增 / Added
- **`docs/requirements.md`** —— CM 的**需求与验收清单**（CM 明确要求："搞一个专门的项目文件，
  去记录我的要求，然后改完之后就看看能不能达到我的要求"）。逐条**照抄 CM 原话**（R1–R7），
  每条附可执行的验收方式与命令；改完必须回来逐条打勾、拿证据说话，不许"应该可以了"。

### 测试 / Tests
- 新增 `test_interim_narration_is_visible`（8 项）—— R1 验收锁：一个真实回合（汇报 → 工具 → 汇报 →
  工具 → 最终回复）跑完后，两段汇报必须出现在**卡片可见正文**里、且**不在折叠面板内**，
  区块顺序必须是 `message/tools` **交错**（而不是全挤在一起）。
- 四道门全绿：单测 exit 0 ｜ 快照门禁 exit 0 ｜ 594 条不变量探针 exit 0 ｜ 出站烟测 exit 0。

## [v2.1.1] · 2026-09-15

### 修复 / Fixed
**①「只有第一条工具折叠、其余全摊开」**（CM 反馈的另一半）。

根因：长任务心跳「已持续工作 N 分钟」经 `update_notice` 落成最后一个 `notice` 块；而
`ChatCardSession.in_tool_context()` 只看 `blocks[-1]` —— 见到 notice 就返回 False，
于是心跳之后到来的（连续 terminal 丢表头产生的）**裸 ``` 命令块被判成正文** → 不折叠。
修法：`in_tool_context()` **跳过 notice**（瞬时覆盖层不代表工具流结束），4 行单点改动。

**② 工具摘要行不可读**（CM：「我都不知道你搞的是什么东西」——16 行里 10 行如此）。

根因：**在"上游已经为人类排版好的文本"上做二次解析**（属于信息源选错，不是解析器不够强）。
`gateway/run_turn_runner.py:239,262-275` 会因为 `tool_progress` 模式产出两种完全不同的形状：

| 模式 | 上游产出 | 旧解析器（按 verbose 写的） |
|---|---|---|
| `verbose` | `🔎 search_files(['limit','pattern'])` + 次行 JSON | 只抓到括号里的**参数键名** → ``- ⏳ · read_file · `(['path'])` `` |
| 其它（当时改了 `all`） | `⚙️ Reading P:\…\MOC.md`（人类预览，**无括号**） | `first.find("(")` 抠不到 → 工具名兜底成字面量 `tool` → ``- ⏳ · tool · `⚙️ Reading …` `` |

雪上加霜：09-15 把上游配置从 `verbose` 改成 `all` 时**没有同步改解析器**，解析器静默走兜底分支
—— 不报错，只是产出垃圾。**这是"改来改去改不好"的真正原因：缺"卡片实际显示什么"的可见性。**

### 变更 / Changed
- `card_render.parse_tool_line()` 取代旧的 `summarize_tool_line()` 内联解析：**能确定工具名就取，
  取不到就整句照搬上游原文**（上游那句本来就是给人读的），**绝不编造兜底名**。
  - verbose 形态：取 JSON 里第一个**值**（不是键名）；优先级 `command > query > pattern > names > … > path`
    —— `search_files` 的关键词比它扫的目录有用。
  - 其它形态：`⚙️ Reading P:\…\MOC.md` → 整句保留，不硬拆成 `Reading · files for …`。
  - JSON 解析失败时用正则捞第一个 `"键": "值"`，**不把整段 JSON 当详情**。
- `card_render.tool_block()` 改为**幂等**：入参可以是上游原文，也可以是已渲染的行；已渲染的行只替换
  状态符号，不再二次解析（旧实现无条件再跑一遍摘要 → 存渲染好的行会变成
  `- ⏳ · tool · \`- ⏳ · terminal · …\``）。
- 状态符号只表达**确实知道的事**：运行中 `⏳`；卡片封口 → `✅`（整轮结束 ⇒ 工具必然已返回）。
  上游进度文本不含完成/失败信号，因此**不猜** `✅/❌`（猜错比不显示更坏）。
- `tool_progress: all` → **`verbose`**（回退到原始值）：verbose 才带真实工具名与参数值，
  正是 ZCode 的效果；旧解析器只会取到键名，所以当初改成了 `all`，现根因已消除。
  `tool_progress_grouping: accumulate` 保持不变（它才是"不每条新发消息"的开关）。
- 4 个 profile（default / feishu2 / basketball / football2）配置已回退（备份 `config.yaml.bak-20260915-192939`）。

### 测试 / Tests
- 新增 `tests/snapshot_card.py`：**卡片内容快照**。用真实一轮（session `20260915_172221_ace90b18`，
  16 次工具调用）复刻 `上游文本 → classify → refine_kind → 卡片元素`，两种模式都打印卡片实际行数，
  `--check` 作为回归门禁（当前 exit 0）。**它的价值在这次立刻体现：一眼看到 10/16 行退化。**
- 新增 12 项断言（`test_tool_line_never_degrades`）：all/verbose 双形态、无字面量 `tool`、
  无参数键名、无二次包裹、幂等、⏳→✅ 分档。
- 全量：单测 exit 0（含新增 12 项）、快照门禁 exit 0、594 条状态机不变量探针全通过。
- 部署后 4 份落地副本逐一复验（直接 import 已部署文件跑三形态样本）。

## [v2.1.0] · 2026-09-15

### 变更 / Changed
**照抄 ZCode**（CM 指示：「zcode 是怎么样发过来的，你就给我改成什么样，不懂就去看 zcode 源码照抄」）。
做法：从 `P:\Program Files\ZCode\resources\app.asar` 提取 `out/host/index.js`（2.3MB），
直接读它的飞书卡片实现并逐条对齐 —— **不再靠猜**。

| 项 | ZCode 源码实现 | 本插件（改前 → 改后） |
|---|---|---|
| 面板标题 | `🛠️ ${streamingToolSummaries} (${n})` → **`🛠️ 工具摘要 (3)`**（固定文案 + 数量） | `🛠️ 工具调用 ×3 · <内容首行>`（连续命令丢表头时首行是 ```` ``` ```` → **标题乱码**）→ **改为固定文案 + 数量** |
| 面板正文 | 每工具**一行摘要**：`- {状态} · {工具名} · {详情}` | 原始命令**全文**（多行代码块，又长又乱）→ **改为逐条摘要行** |
| 展开规则 | `expanded ?? (status === "running")` | 一律折叠 → **改为运行中展开、出正文/收尾后折叠** |
| 中途叙述 | **卡片结构里没有这一项**（只有 回复文本 + 工具面板 + 状态行） | 之前混着模型的内心独白 → **关掉**（`interim_assistant_messages: false`） |

### 修复 / Fixed
- **工具面板标题乱码**：连续 terminal 调用时上游**有意丢弃表头**（`run_turn_runner.py:239`），
  旧实现拿"内容首行"当标题 → 标题变成 `🛠️ 工具调用 ×3 · ``` `。照抄 ZCode 的固定文案后彻底解决。
- **面板正文不再是原始命令全文**：新增 `summarize_tool_line()`，把上游三种真实文本形态
  （带表头的 terminal / 裸 fenced 块 / 非 terminal 工具）都转成 `- ⏳ · 工具名 · 参数` 摘要行。

### 说明 / Notes
- 判断"agent 有没有跑偏"改由**工具摘要行**承担（能看到在读哪个文件、跑哪条命令），
  与 ZCode 提供的能力一致；模型的内心独白（`reasoning == content`，实测 3009 字符）不再进卡片。

### 测试 / Tests
- 新增三组断言（共 16 项）：面板标题为固定文案+数量且**不含内容摘要** / 正文每行一条摘要 /
  **运行中展开、封口折叠、显式 expanded 优先** / 三种上游文本形态的摘要行 / 空输入不产面板。
- 单测 + 不变量探针（594 序列）+ 离线烟测**全绿**；4 个 profile 重分发 + 重启。

## [v2.0.10] · 2026-09-15

### 修复 / Fixed
- **恢复"中途说的话"，只过滤英文内心推理**（CM：*"以前他中途说的一些内容会显示出来，现在好像不显示了…
  我还是需要它显示出来，因为我可以根据这些内容判断他有没有跑偏"*）。
  前一天我把 `interim_assistant_messages` 整个关掉了 —— 那是**过度修正**：
  CM 嫌吵的是**英文内心独白**（`Mystery solved — a sibling cron job…`、`Now writing the run summary…`），
  但他需要的是**中文阶段汇报**（`**① 已落盘**：… ✅ 跑完了`、`【会话已恢复 ✅】网关重启完成…`）
  —— 两类走的是**同一个通道**，一关就都没了。
  **修法**：配置恢复 `interim_assistant_messages: true`（4 个 profile），过滤改在插件层做，
  且**只针对中途叙述**：
  * 上游给中途叙述带了标记 `metadata["_interim_send"] = True`
    （`gateway/stream_consumer_fallback.py:330`）→ 只对该标记生效；
  * **最终回复没有这个标记，永远显示**（哪怕是纯英文），不受任何过滤影响；
  * 判定规则（`_is_english_reasoning`）：**完全没有汉字**且拉丁字母 ≥15 → 判为英文推理，静默跳过。
    口径刻意保守：中英混排、含一个汉字的都照常显示。
  * 用 CM 的真实历史样本验证：40 条中间内容 → **25 条显示 / 15 条过滤**，分类准确
    （唯一"漏过"的是一条夹了中文引号的英文句，属保守侧的偏差）。

### 修复 / Fixed（同日早前）
- 见 v2.0.9（两张卡/内容重复、工具代码框只有第一条折叠）。

## [v2.0.9] · 2026-09-15

**结构性复盘第二批**（CM：完整读代码 → 盘根因 → 计划 → 确认后改）。针对两个反馈：
①「同一段东西分两个卡片发、内容大部分重复」②「工具代码框只有第一条折叠、其余全展示」。
**方法**：探针先写"能抓住重复"的不变量（I11 卡内不重复 / I12 跨卡不重复），在**旧代码上跑出失败**
（`I12:「💻 tool 14」同时出现在 om_1 与 om_2`），再修到全绿。

### 修复 / Fixed
- **问题②根因（单点）**：上游 `run_turn_runner.py:239` 对连续 terminal 调用**刻意丢掉重复表头**
  （`header = "" if last_was_terminal_block else …`）→ 第 2 条起的工具进度是**裸 fenced 代码块**，
  而 `classify()` 靠 emoji 判断 → 误判成正文 → 落进 `message` 块 → 短代码块按设计不折叠。
  修法：`is_bare_fence()` + **上下文判定**（`ChatCardSession.in_tool_context()`）——
  裸代码块仅在"最后一块仍是未结束的工具面板"时升级为 TOOL，正文里的代码块不受影响。
- **`_NON_TOOL_PREFIX` 误伤真实工具 emoji**：📄(feishu_doc) 🖼️(preview) 💬(browser_dialog)
  ❓(clarify) 💡(tip) 曾被黑名单判为正文 → 不折叠。已移出。
- **带变体选择符的 emoji 漏判**：`🖼️`/`⚠️`/`🗓️` 是"emoji + U+FE0F"两个码点，
  `_TOOL_MSG_RE` 要求 emoji 后紧跟空白 → 整类漏判。已允许可选的 VS16。
- **问题①（重复）—— 三处根因，全部修掉**：
  1. **换卡时复制内容**：旧 `_carry_over_locked` 把尾部 3 块搬到新卡，而旧卡（已封口）还在会话里
     → 同一段内容两处可见。现在统一换卡 `_rotate_locked()`：
     **旧卡封口只保留它已上卡的内容**（`blocks[:committed_count]`）+ **新卡只带"提示行 + 未上卡的增量"**
     （`committed_count` = 已成功上卡的块数，新增记账）。内容永远只出现一次。
  2. **`edit_message` 语义错配**：上游语义是"原地替换/对账"（其源码注释就写着
     "a plain send here would duplicate it"），旧实现无条件**追加** → 同一段文本在卡内出现两次。
     现在 `replace_last_text()`：新文本 == 最后一块 → 跳过（幂等）；以最后一块开头 → **替换**；否则追加（保守）。
  3. **分块失败回退整段**：旧实现任一 chunk 失败就 `super().send(整段)` → 已入卡的块再发一遍。
     现在只补发**未入卡**的剩余块。

### 变更 / Changed
- **配置（4 个 profile）**：`display.platforms.feishu.tool_progress_grouping: separate → accumulate`
  （工具进度不再每个一条消息）、`tool_progress: verbose → all`（不再把完整参数 JSON 写进卡片，
  并启用重复命令去重）。这是"频繁触顶换卡"的主要推手，改后出站次数与卡片体积大幅下降。
- 修正 09-15 早前的一次**无效改动**：`display.streaming` 是 **CLI-only**（`display_config.py:106`），
  网关根本不读它 —— 那行已恢复原值；网关是否流式由 `streaming.enabled` 决定（本来就是 false）。

### 测试 / Tests
- 不变量探针新增 **I11（卡内同一文本不得出现两次）/ I12（同一文本不得跨卡出现）**，
  修复前在旧代码上跑出真实违规（TDD 红→绿）；594 条序列 × 12 项不变量全绿。
- 单测新增：上游 3 种真实文本格式（含裸 fence）、VS16 emoji、换卡不复制已上卡内容、edit 幂等/替换。
- 离线烟测新增：工具上下文升级、edit 幂等、超集替换。

## [v2.0.8] · 2026-09-15

### 修复 / Fixed
- **连续调用工具时面板折叠不起来**（CM 反馈）：工具面板建块时写死 `expanded=True`，且只在
  **有正文输出时**才收起 → 连续调工具、中间没有正文的整段期间，面板一直摊开着。
  这与 CM 在 DSH 插件里定的设计（"工具面板默认折叠，需要时点开"）不一致。
  改为**建块即默认折叠**（`expanded=False`），并把标题做成有信息量的摘要：
  `🛠️ 工具调用 ×N · <首行摘要>` —— 折叠状态下也能看出跑了几个、在跑什么。
- **单个工具面板内容无上限**：连续调用会全部合并进同一个面板，内容无限增长 → 整卡越来越长、
  PATCH 越来越重。新增 `TOOL_BLOCK_MAX_CHARS = 4000`：超出只保留**最新**部分，
  前面用一行「…（更早 N 字符已省略）」标注（正在发生的事更重要）。

### 测试 / Tests
- 新增 6 项断言（连续工具合并成**一个**面板 / 面板 `expanded=False` / 标题含调用次数与 🛠️ 前缀 /
  出正文后仍保持折叠）+ 面板内容上限 5 项断言。
- 全量单测 + 不变量探针（594 序列）+ 离线烟测 **全绿**；4 个 profile 重分发 + 重启。

## [v2.0.7] · 2026-09-15

本次是**结构性复盘**（CM：「不要总是出现问题，然后打补丁」）——
新增**不变量穷举探针** `tests/probe_invariants.py`（594 条操作序列 × 10 项不变量），
**靠它找出并修掉 2 个真实缺陷**，而不是靠人眼。完整报告见 `docs/review-20260915.md`。

### 修复 / Fixed
- **持续拒卡时无限建卡**（严重）：`_flush_locked` 失败路径是"封旧卡 → 建新卡"，
  飞书若持续拒卡就变成"每条消息冒一张新卡"——用户看到的就是不停重复发东西。
  探针实测：15 次失败注入 → 建 15 张卡。
  **第一版修复无效**（计数器在建卡成功时被清零，永远是 0/1，达不到上限；探针第二次跑仍建 15 张）。
  最终两层修复：① `failures` 语义收紧为**连续 PATCH 失败**，只在成功 PATCH 时归零；
  ② 新增**换卡配额** `rotations ≤ max(3, 写入次数 // 5)`——因为"成功/失败交替"时连续计数会被清零，
  只有与写入次数成比例的预算能兜住频率。验证：连续失败 15 次→建 3 张（原 15）；交替失败 30 次→建 4 张（原 16）。
- **容量换卡缺总量兜底**：容量换卡同样纳入配额约束，超配额时继续写当前卡（出站前有元素折叠兜底）。

### 新增 / Added
- `tests/probe_invariants.py`：状态机不变量穷举探针（I0 无异常外抛 / I1 无换卡爆炸 / I2 建卡有界 /
  I3 内容不丢 / I4 卡片不越界 / I5 计数自洽 / I6 状态合法 / I8 换卡收敛 / I9 硬上限 / I10 有内容必有 I/O）。
  **建议纳为发布门禁**：改动状态机后必跑。
- `docs/review-20260915.md`：完整复盘报告（含"为什么全部卡住"的真因、上游 0.21.0→0.21.3 契约核对、
  剩余结构性风险与建议）。

### 说明 / Notes
- 上游 0.21.3 把 `send_exec_approval` 从飞书适配器**上移到基类**（飞书改覆写 `_send_exec_approval_prompt`），
  且**未打 deprecated 标记**；本插件用 `*args/**kwargs` 透传，仍正常工作。核对详情见复盘报告 §4。

## [v2.0.6] · 2026-09-15

### 修复 / Fixed
- **无限换卡循环（表现为"机器人一直在重复发东西"）**：换卡（容量超限）时把**整份已累积内容**
  原样带到新卡上、且追加计数不归零 → **新卡一出生就又是超限状态** → 下一次追加立刻再触发换卡
  → 每 5-10 秒换一张新卡。football2 实测：40 秒内 6 次换卡，一个小时桶内建了 42 张卡。
  修复：新增 `_carry_over_locked()` —— 换卡时新卡最多带 **3 个尾部方块且 ≤4000 字节** 的上下文，
  并把 `append_count` 归零；**内容不丢**（旧卡此刻已封口 ✅ 留在会话上方，且旧卡内容完整）。
  测试：容量换卡用例新增 4 项回归断言（新卡只带有限尾部 / 计数归零 / **换卡后连续 10 次追加
  不再触发换卡** / 始终停在同一张新卡）。
  背景：这条 latent bug 是 v2.0.4「子代理共用一张卡」之后才暴露的——卡不再频繁重置，
  长任务就真的会涨到容量上限，从而走进这条从未被走通的换卡路径。

### 修复 / Fixed（同日早前）
- **`[230099] ErrCode 11310 element exceeds the limit`** 见 v2.0.5。
- **长任务心跳吞内容** 见 v2.0.3；**子代理各开一张卡 / accumulate 不折叠** 见 v2.0.4。

## [v2.0.5] · 2026-09-15

### 修复 / Fixed
- **`[230099] ErrCode 11310 element exceeds the limit`（元素超限被拒卡 → 降级成纯文本）**：
  重构时漏掉了元素上限保护，长任务卡片（工具面板 + 代码块 + 正文方块不断累积）元素数超过飞书
  上限后**整卡被拒**，插件只能降级发纯文本（用户仍收到回复，但没有卡片样式）。
  实测 feishu2 累计撞了 **159 次**，其中 09-14 19 点一小时 80 次。
  修复：`card_render.build_elements()` + `fold_elements()` —— 出站前统一做元素折叠：
  元素数 > **40** 时，把**更早的过程**折进顶部一个「📎 更早过程 (N)」面板（正文取原文、
  面板取内部 markdown，>3000 字截断标注），**保留最新 10 个元素可见**
  （设计：实时进度与结论留在底部）。阈值/尾部保留数与 DSH 飞书插件的实测实现一致。
  测试：单测新增 6 项（原始超限 / 折叠后数量 / 顶部折叠面板 / 旧内容保留 / 最新内容仍可见 /
  小卡不受影响 / card_json 不超上限），全量单测 + 烟测全绿；四个 profile 重分发 + 重启
  （00:17-00:18），重启后 11310 = 0。

## [v2.0.4] · 2026-09-15

### 修复 / Fixed
- **子代理（异步委派）完成通知各开一张卡**：这类通知是框架注入的**合成事件**
  （`MessageEvent.internal=True`，正文形如 `[ASYNC DELEGATION BATCH COMPLETE …]`）。
  旧实现把"每条入站消息"都当成新一轮 → 卡片会话复位 → 一批子任务完成通知在几分钟内开出多张卡
  （实测 23:57 一分钟内建了 6 张）。修复：新增 `_should_reset_card_for()`，**只对真人消息复位**；
  合成事件继续累积写同一张卡。
- **`tool_progress_grouping=accumulate` 的 profile 里工具/代码不折叠**：accumulate 模式下工具进度
  是经 `edit_message` 原地更新气泡送来的（而非 `send()`），旧实现把它当状态行摊平。
  修复：`edit_message` 先按 `classify()` 判定——工具进度行 → `handle_tool`（折叠面板）；
  `finalize=True` 或长/多行文本 → 正文追加；其余短状态行 → notice。

### 变更 / Changed
- **basketball profile 的 `display` 段整段缺失**（原本走框架默认 `tool_progress=new` +
  `tool_progress_grouping=accumulate`）→ 补齐为与其它三个 profile 一致
  （`tool_progress: all` + `platforms.feishu: {tool_progress_grouping: separate,
  cleanup_progress: false, tool_progress: verbose}` + `streaming/长期通知` 显式打开）；
  备份 `config.yaml.bak-20260915-display`。四个 profile 的 display 配置现已对齐。
- 测试：离线断言新增 7 项（合成事件不换卡 / 真人消息换卡 / 工具编辑进折叠面板且保留旧内容 /
  心跳仍是 notice 且工具面板存活），全量单测 + 烟测全绿。

## [v2.0.3] · 2026-09-14

### 修复 / Fixed
- **长任务心跳"吞掉"卡片内容**：Hermes 的长任务通知逻辑是「首次 `send()` + 之后每 N 分钟
  `edit_message(同一条消息 id, "已持续工作 N 分钟…")` 原地更新」，而那条消息 id 就是本会话的卡片。
  旧实现把 `edit_message` 当成**整卡替换**（`replace_text` 直接 `blocks = [新文本]`），于是心跳一来
  就把已累积的正文/工具面板全部清空，之后只追加新内容——CM 反馈的"内容被吞掉、还不会恢复"。
  修复：
  * 新增 `notice` 方块 + `ChatCardSession.update_notice()`：短状态行**原地更新**（重复心跳不堆叠、
    不新增方块），有正文/工具进度进来或收尾时**自动丢弃**；
  * `edit_message` 语义分流：`finalize=True`（最终答案对账）或长/多行文本 → 走 `handle_text`
    当**正文追加**（永不丢内容）；其余短状态行 → `update_notice`；
  * `seal()` / `handle_text()` / `handle_tool()` 都会清掉 notice，卡片收尾保持干净。
  测试：单测新增 7 项（心跳保留正文与工具面板 / 重复心跳原地更新 / 出正文即丢弃 / 收尾丢弃 /
  内容存活），全量单测 + 离线烟测全绿；四个 profile 已重新分发并重启（23:52-23:53）。

## [v2.0.2] · 2026-09-14

### 新增 / Added
- **互动卡发送硬超时**（默认 30s，`HERMES_FEISHU_APPROVAL_SEND_TIMEOUT` 可调）：`send_exec_approval` /
  `send_update_prompt` 的超时即返回失败 `SendResult`（网关据此走 "Failed to send approval request to user"
  让 agent 正常收尾），不再留下"审批请求永远 pending、日志完全静默"的状态；
  无论成功或超时都按"互动已发生"收口卡片（下一段内容开新卡）。

### 说明 / Notes
审查上游审批链路后发现（v0.21.0）：智能判定 LLM 已有 30s 显式超时（`auxiliary.approval.timeout`）、
网关等待发卡结果已有 15s guard（超时按 ambiguous 继续、不重发）、人工等待 300s（`approvals.timeout`）。
本版补的是**我们自己的发送协程没有截止时间**这一环。测试：`tests/smoke_live.py --no-send` 新增 6 项断言。

## [v2.0.1] · 2026-09-14

### 修复 / Fixed
- **授权卡/追问卡之后的输出不再写回旧卡**：`send_exec_approval` / `send_update_prompt` 发出的
  是插在会话中间的独立消息，之前其后的内容会继续 PATCH **上方**的旧卡——飞书聊天窗不会自动上滚，
  用户看不到新内容。现在发出互动卡即给该会话收口（旧卡 PATCH「✅ 完成」），下一段内容开新卡。
  实现用 `*args/**kwargs` 透传上游方法，上游改签名也不会顶崩；异常一律不影响互动卡本身。
  测试：`tests/smoke_live.py --no-send` 新增离线断言（旧卡密封 + 续写换新卡，monkeypatch 卡片 I/O 不联网）。

## [v2.0.0] · 2026-09-14

### 新增 / Added
- **插件版发行（`plugin/`）**：`feishu-card` 插件以同名平台注册覆盖内置 feishu 适配器，
  **不再修改官方 `adapter.py`**（Hermes `platform_registry` 后写胜出，官方文档明示支持）。
  代码量从 6000+ 行整文件 fork 降到约 500 行出站层，入站/协议全部继承官方实现。
- `install-plugin.ps1` / `install-plugin.bat` / `enable-plugin.py`：一键装到指定 profile
  （复制 → 幂等启用（带备份 + 校验）→ 可选重启 → 日志自检），安装器本身幂等。
- 单写者卡片状态机（`card_session.py`）：每会话一个 `ChatCardSession`，状态只在其内部变更，
  每次写入带 `seq`；容量换卡、密封定时器、失败换卡收敛到一处。
- **卡片调用硬超时**（`card_adapter.py`，默认 15s，`HERMES_FEISHU_CARD_TIMEOUT` 可调）：
  超时即判失败 → 旧卡封口 + 同一份 blocks 新建卡（内容不丢）→ 再失败退回官方纯文本发送。
- 纯函数层（`card_render.py`）：卡片 JSON / 表格分块 / 容量判定 / 消息分类集中一处；
  `tests/test_card_session.py` 42 项单测（不联网）+ `tests/smoke_live.py` 真机烟测。

### 背景 / Background
2026-09-13 23:04 一次会话内热改官方 `adapter.py`（给多块合并加锁）后，进程 40 秒内静默卡死：
PATCH 无超时 + 锁被挂起调用占住 → 连"发回复"也堵死，表现为「进程活着、日志停住、飞书不回话」。
结构性分析与方案见 `docs/refactor-plan-20260914.md`。

### 变更 / Changed
- `scripts/apply_feishu_card_patch.py`（9 补丁脚本）与 `custom/` 整体替换版 **标记为 legacy**：
  仍可用于老版本 Hermes，新装建议插件版（`plugin/`）。

## [v1.1.0] · 2026-09-04

### 新增 / Added
- **完整定制版 adapter**（`custom/feishu-adapter-full-20260826.py`，6251 行）：生产验证的整体替换方案——默认 JSON 2.0 卡片出站、会话内单卡片持续 PATCH 更新、密封定时器（防跨轮误 PATCH）、工具消息块排版、30 次追加容量保护、interactive→post→text 回退链。附带 `custom/README.md`（对比表 + 安装/恢复步骤 + SHA256）。
- 背景：Hermes 0.21.0 升级后官方 adapter 默认 text 出站且 PATCH 交互卡片的 `_build_update_message_body` 会带 `msg_type=interactive`（实测 `[230001]`），补丁脚本在 0.21.0 上有 1 处锚点需手补——完整版规避了全部版本漂移问题。

Full custom adapter added under `custom/` — the production drop-in replacement tested through Hermes 0.21 upgrade, with per-chat single-card PATCH updates, seal timers, tool-block rendering and capacity guard.

### 新增 / Added
- **容量保护**:同一张卡追加 ≥30 次 → 主动开新卡(旧卡保留),防止卡片触达飞书 interactive 请求体 30KB / JSON 2.0 元素 200 上限后 PATCH 被拒、旧内容丢失。指南新增「步骤 10」,补丁脚本补丁 5/6 内置容量保护。Capacity guard: cards start fresh after 30 appends — prevents PATCH rejections at Feishu's 30KB payload / 200-element caps from losing old content. Guide step 10 + built into patch 5/6.

### 变更 / Changed
- 效果对比表按源码实证重写（`accumulate`=编辑同一气泡被覆盖；`cleanup_progress` 删除过程消息；`separate`=每工具一条独立消息）。Comparison table rewritten with source-verified mechanics.

### 安全 / Security
- `feishu_card.py`：SSRF 加固——域名白名单、解析后 IP 边界校验、重定向限制。SSRF hardening — domain whitelist, resolved-IP boundary checks, redirect restrictions.

## [v1.0.0] · 2026-08-08

### 新增 / Added
- 首发：9 步升级指南（`docs/guide.md`）、幂等重打补丁脚本（`scripts/apply_feishu_card_patch.py`，9 个补丁，自动备份）、零依赖卡片工具（`scripts/feishu_card.py`）、MIT 许可证。Initial release: 9-step upgrade guide, idempotent re-patch script (9 patches, auto-backup), zero-dependency card tool, MIT License.
