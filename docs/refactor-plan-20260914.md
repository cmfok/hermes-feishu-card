# 飞书卡片链路 系统性改造方案

> 起因：2026-09-13 23:04 会话在运行中给 `adapter.py` 打了"多块回复合并成一张卡"的补丁，40 秒后整个 gateway 静默卡死（进程活着、0 个外网连接、无任何日志），直接后果是"飞书不回话"。事后判定：**不是上游 0.21.0 的 bug，是我们自维护 fork + 热改运行中代码 + 卡片链路缺少超时/单一状态机**。本文给出一次性结构性方案，替代"东补一个西补一个"。
>
> 代码基线：`hermes-agent/plugins/platforms/feishu/adapter.py`（6349 行，09-13 23:04 版）；上游 `upstream/main` 同名文件 6088 行、**不含**卡片流式能力。

---

## 0. 结论先行

| 判断 | 内容 |
|---|---|
| 现在的病根 | 卡片逻辑长在 6349 行的 fork 里；卡片状态散在 3 个 dict、由 4 条代码路径各自改写；PATCH 调用**没有任何超时**；同一把锁既管"防并发跳变"、又是"发回复"的必经路径 → 任一次 PATCH 挂起 = 整轮永久卡死 |
| 系统性解法（三件） | ① 部署形态：**官方 adapter + 我们插件覆盖**（官方明示支持，`hermes update` 不再冲掉我们的代码）；② 卡片逻辑收敛为 **CardSession 单写者状态机**；③ 传输层统一 **deadline/超时 + 降级**，让"发不出去"变成"换张卡继续发"，而不是"卡死" |
| 迁移 | 分 4 阶段，**阶段 0 只有约 20 行改动**，今天就能上，先拆掉"锁被永久占用"这颗雷 |

---

## 1. 现状架构（读码结果，含行号）

| 环节 | 位置 | 说明 |
|---|---|---|
| 出站总入口 | `send()` adapter.py:1940 | 所有回复/过程消息都走这里 |
| 卡片状态 map | 定义 1546；读 1953；写 2120 / 2132；入站清空 3552；定时器读 4948 | `_chat_card_map[chat_id] = {message_id, content, blocks, append_count}` |
| 密封定时器 | 4908 / 4917 / 4944 / 4951 | 8 秒无新消息 → PATCH「✅ 完成」 |
| PATCH 锁 | 4932（+2001、2040、4955 三处使用） | per-chat `asyncio.Lock` |
| 卡片构造 | `_card_with_blocks` 5051、`_build_outbound_payload` 5091 | 所有出站 → JSON 2.0 `interactive` 卡 |
| 分块 | `MAX_MESSAGE_LENGTH=8000`（1470）、`_split_chunks_by_tables` 4831 | 按字符 + 表格数（≤2 表/卡）切 |
| 容量保护 | `_card_exceeds_limits` 4866（appends≥30 / blocks≥60 / bytes≥15000 / tables>2） | 超限 → 开新卡 |
| 真正发卡 | `_patch_card_raw` 5072 → `_run_blocking` 1745 → lark SDK | **无线程超时、无 HTTP 超时** |
| 消息分类 | `_looks_like_tool_message` 4963、`_is_closing_message` 4890 | emoji/正则启发式 |
| 三条"换卡"路径 | 容量超限 1964；PATCH 失败降级 2007；多块合并 2037（23:04 新增） | 彼此不知情 |

---

## 2. 七个结构性问题（每条都有代码证据）

1. **状态分散、无单一写者。** 卡片状态在 `_chat_card_map` + `_card_seal_timers` + `_card_patch_locks` 三个结构里，改写者有 4 条：`send()`、密封定时器、入站消息处理、以及新增的逐块合并分支。没有任何一处是"唯一权威"，靠约定和 `message_id` 比较勉强兜。
2. **PATCH 无超时。** `_patch_card_raw`(5072) → `_run_blocking`(1745) → `run_in_executor`，线程池里的 lark SDK 调用既不设 socket 超时也不设任务超时。**一次挂起就是永久挂起**。
3. **一把锁承担两个职责。** `_get_patch_lock` 既用于"防并发 PATCH 导致 400 内容跳变"（2001、4955），又被 23:04 的补丁引入**回复发送路径**（2040）。于是"防跳变"的锁一旦被挂起的 PATCH 占住，连回复都发不出去。
4. **就地改 dict + 无版本号。** 1995-2005 直接 `existing_card["blocks"] = blocks`，PATCH 成功与否之外没有序号；降级路径"PATCH 失败→发新卡"（2007）与并发的其它更新交错时，就会重现"前面的内容被顶掉"——23:04 那个补丁修的就是这个症状，但用的是"在 send 里再加一把锁"的方式，等于把问题 2、3 放大。
5. **三条换卡路径互不通信。** 容量超限换新卡（1964）、PATCH 失败换新卡（2007）、多块合并（2037）都可能改变"当前卡是谁"，而密封定时器只靠 `message_id` 相等来判断旧卡（4949），入站清 map（3552）又是第四种"换卡"。缺一个显式的卡生命周期。
6. **消息分类靠猜。** `_looks_like_tool_message`(4963) 用 emoji 前缀 + 正则判断"这是工具进度还是正文"，`_is_closing_message`(4890) 用 💾 前缀判断"收尾消息"。文案一改（换 emoji、改措辞、新工具）就误判 → 这是补丁源源不断的根本原因之一。
7. **无观测。** 卡片状态机没有任何指标/结构化日志：看不到"当前卡 id、append_count、seq、上次 PATCH 耗时与结果、锁等待时长"。09-13 那次卡死只能靠人肉翻日志+看 netstat 才定位。

---

## 3. 目标架构

### 3.1 CardSession：每会话一个有状态对象 + 单写者协程
```
ChatCardSession
├─ message_id / blocks / append_count / bytes_est / seq   ← 全部状态只在这里
├─ status: typing | running | sealed
├─ asyncio.Queue  ← 所有更新请求入队
└─ worker()       ← 唯一写者：串行出队 → 构造 payload → PATCH → 更新 seq
```
- **只有 worker 碰卡片状态**，`send()`/定时器/入站都只是"提交意图"（enqueue），不再直接改 dict → 消灭问题 1、4。
- 每次 PATCH 带 `seq`；成功才推进 `seq`，失败按策略重试或换卡 → 状态与远端一致可推理。
- 换卡 = 显式状态迁移（`rotating`）：旧卡封口（seal）、新卡建好、`message_id` 原子切换；所有引用当前卡的地方只读同一来源 → 消灭问题 5。

### 3.2 传输层：统一 deadline + 快速失败
- 所有 lark 调用（send/patch/delete/上传）统一经一层 `feishu_call(op, timeout=…)`：`asyncio.wait_for` 包住 `run_in_executor`，例如 PATCH 15s、发送 20s、上传 120s。
- 超时即"这次更新失败"，进入降级：**新卡续写**（内容不丢）+ warning 日志 + 计数指标。绝不允许"等下去" → 消灭问题 2、3。
- 每个 turn 给一个总预算（如 60s 内必须至少完成一次出站），保证回复一定能落地。

### 3.3 消息类型显式化，启发式降级为兜底
- 与框架侧对齐 metadata（`kind: tool_progress | assistant_text | closing`）；拿不到 metadata 才走现有正则，并把启发式收敛成一个纯函数 + 单元测试（杜绝"改文案就炸"）→ 消灭问题 6。

### 3.4 部署形态：插件覆盖，不再改官方文件（关键）
Hermes 的插件体系原生支持：
```python
# gateway/platform_registry.py:504 register()
"""If an entry with the same name exists, it is replaced
   (last writer wins -- this lets plugins override built-in adapters if desired)."""
```
- 内置平台就是插件：`plugins/platforms/feishu/__init__.py` 调 `ctx.register_platform(name="feishu", …)`。
- 我们的插件放 `%LOCALAPPDATA%\hermes\plugins\hermes-feishu-card\`（也可 `hermes_home/plugins`、项目 `.hermes/plugins`），`__init__.py` 里：
  - `from plugins.platforms.feishu.adapter import FeishuAdapter` 继承上游；
  - 只覆盖**出站**相关方法（send / edit_message / 卡片构造 / CardSession）；
  - `ctx.register_platform(name="feishu", …)` 注册自己；
  - manifest 用 `requires_plugins` 声明依赖内置 feishu 插件，靠拓扑排序**保证加载在其后**（`hermes_cli/plugins.py:944-981`）。
- 收益：`hermes update` 覆盖官方文件不再影响我们；我们的代码量从"6349 行整文件 fork"降到"**几百行出站逻辑 + 继承**"；入站（WS 重连、消息批处理、媒体、webhook、卡片审批回调、反应等）全部白拿上游维护。→ 消灭问题 7 的根因（维护形态）。

### 3.5 观测与自愈
- 结构化日志：`chat / card_id / seq / append_count / patch_ms / result / lock_wait_ms`；异常时打一条汇总。
- 指标：PATCH 成功率、超时次数、换卡次数、密封延迟。
- 与 watchdog 配合：**仅在"进程存在 ∧ 无飞书 WS 连接 ∧ 持续 N 分钟"** 时告警→重启 profile 网关（正常跑长任务的网关有连接，不会误杀）；再加一条"turn 中间 N 分钟无任何卡片更新"的 warning（09-13 若有这条，早就发现了）。

---

## 4. 迁移路线（每阶段可独立上线、可回退）

| 阶段 | 内容 | 规模 | 验收 | 回退 |
|:-:|---|---|---|---|
| **0 止血** | `_run_blocking` 包一层 `asyncio.wait_for`（PATCH/发送 15-20s），超时走既有降级路径 | ~20 行 | 手动制造一个"假挂起"（断网或 mock sleep）→ 回复仍能落地 | 还原备份文件 + `gateway restart` |
| **1 收敛** | 抽出 `CardSession`（同文件内重构，行为保持），`send`/定时器/入站改为 enqueue；加单测：多块合并 / 容量换卡 / 密封 / 跨轮清卡 / PATCH 失败降级 | 中（1 个模块 + 测试） | 单测全绿 + 飞书实测 3 轮（短回复、长多块、带表格） | 保留旧 `send` 分支，配置开关切换 |
| **2 插件化** | 插件包骨架 + 注册覆盖 + 继承上游；官方 `adapter.py` 复原为上游版本；灰度开关（配置项切回内置） | 中 | `hermes plugins list` 显示覆盖生效；重启后卡片行为一致 | 关闭插件开关即回内置 |
| **3 收尾** | 仓库升级为插件包形态（README/CHANGELOG/docs 重写）；`apply_feishu_card_patch.py` 补丁脚本退役；接入 watchdog 自愈 | 小 | 新机器/更新后一键安装；`hermes update` 后卡片功能不受影响 | — |

**唯一未决技术点（阶段 2 的验证实验，约 30 分钟）**：插件加载顺序是否如预期（内置 feishu 先、我们的覆盖插件后）。验证方法：装上插件后看日志是否出现 `Platform 'feishu' re-registered (was …, now plugin)`，并 `hermes plugins list` 确认最终生效者；若顺序不对，用 manifest 的 `requires_plugins` 显式声明依赖。

---

## 5. 方案对照（为什么选 A）

| 方案 | 卡片体验 | 维护成本 | 升级风险 | 结论 |
|---|---|---|---|---|
| **A. 插件化 + CardSession 重构** | 保留（原地更新、过程消息不丢、容量保护） | 几百行自有代码 | 低（不碰官方文件） | **推荐** |
| B. 回到官方纯文本 adapter | 丢卡片、过程消息体验差 | 0 | 无 | 备选（若卡片收益不值得维护） |
| C. 维持现状（fork + 打补丁） | 保留 | 高（18 个 .bak 版本、每次 update 重打补丁、上游已差 12437 行） | 高（今晚就是代价） | 不建议 |

---

## 6. 附：本次事故与本文的对应关系

| 事故现象 | 对应结构性问题 | 方案落点 |
|---|---|---|
| 补丁后 40 秒整体卡死 | 问题 2（无超时）+ 问题 3（锁进回复路径） | 3.2 传输层 deadline；阶段 0 |
| 之前反复出现"内容被顶掉" | 问题 1、4（无单写者/无版本号） | 3.1 CardSession + seq |
| 需要不断打补丁（18 个 .bak） | 问题 6（启发式）+ 问题 7（无观测）+ 部署形态 | 3.3 显式 metadata；3.4 插件化 |
| `hermes update` 必冲掉适配器 | 部署形态 | 3.4 插件覆盖 |

*文档版本：2026-09-14 v1 · 基线 commit 63279301bcb*

---

## 7. 执行记录（2026-09-14 01:00-01:20 全部落地）

| 阶段 | 状态 | 证据 |
|:-:|:-:|---|
| 0 止血（超时） | ✅ | 不再是"包一层 wait_for"，而是随插件一起落地：`card_adapter._with_deadline` 包住 create/patch（默认 15s，`HERMES_FEISHU_CARD_TIMEOUT` 可调）；单测 `timeout raises CardCallTimeout` |
| 1 收敛（CardSession） | ✅ | `card_session.py` 单写者状态机（seq/容量换卡/密封/失败换卡/复位）+ 42 项单测全绿 |
| 2 插件化 | ✅ | `plugin/feishu-card/`：同名注册覆盖（`[feishu-card] platform 'feishu' registered`）；官方 `adapter.py` 已 `git checkout` 复原（md5 `74414211…`，6088 行）；三个 profile（default / basketball / feishu2）均已装载并在 `plugins.enabled` 启用；网关逐个重启验证 `plugins.platforms.feishu.adapter: [Feishu] Connected` + `[feishu-card] card created` |
| 3 收尾 | ✅ | `install-plugin.ps1/.bat` + `enable-plugin.py`（幂等，已实测）；README/CHANGELOG 升级为 v2.0；补丁脚本与 custom 版标记 legacy；官方 `adapter.py` 不再被持有改动（`git status` 干净） |

### 实测证据（真机）
* 烟测：4 次发送（创建 / 追加 / 工具块 / 超长多块）**落在同一张卡**
  `om_x100b6559370f70a4b1b7e46387b796a`，`appends=5 blocks=5 rotations=0 failures=0`，密封成功；
* 生产：网关重启通知与 delegation 批次回复走的就是插件链路
  （`01:08:48 card created | … seq=1 appends=1 blocks=1`）；
* 回归修复：初版建卡时未带内容（先建空卡再 PATCH，多一次调用 + 空卡闪现）→ 已改为"建卡即带内容"，
  并补了对应断言（`first send is a single create call`）。

### 回退路径（三条，随时可用）
1. 环境变量 `HERMES_FEISHU_CARD_MODE=0` → 插件整体让路，回落官方纯文本行为；
2. 从该 profile `config.yaml` 的 `plugins.enabled` 删掉 `feishu-card`（或 `hermes -p <p> plugins disable feishu-card`）→ 内置适配器接管；
3. 需要旧 fork 时：`custom/feishu-adapter-fork-20260914-0130.py` 覆盖回 `plugins/platforms/feishu/adapter.py` + 重启网关。

### 未做（留给后续）
* 上游 PR（方案丙）：把卡片出站整理成官方可接受的形态，合进去后本插件即可退役；
* 把"插件模式"写进 `docs/guide.md`（现指南仍是补丁脚本流程）。

