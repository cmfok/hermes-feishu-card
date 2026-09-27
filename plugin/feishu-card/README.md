# feishu-card（插件版 v2.0）

把 Hermes 的**飞书出站**升级为「可原地更新的交互卡片」，以**插件**方式覆盖内置
`feishu` 适配器 —— 不再修改官方 `adapter.py`，`hermes update` 覆盖官方文件也不影响本插件。

## 装 / 卸（30 秒）

```powershell
# 装（默认 profile）
powershell -ExecutionPolicy Bypass -File plugin\install-plugin.ps1 -Restart

# 装到某个 profile
powershell -ExecutionPolicy Bypass -File plugin\install-plugin.ps1 -Profile feishu2 -Restart
```
双击 `plugin\install-plugin.bat` 也可以（窗口不会自动关，看完自己关）。

**卸载/回退**（任选其一，都是即时的）：
1. 临时关卡片：设环境变量 `HERMES_FEISHU_CARD_MODE=0` → 完全走官方行为；
2. 关插件：从该 profile `config.yaml` 的 `plugins.enabled` 里删掉 `feishu-card`
   （或 `hermes -p <profile> plugins disable feishu-card`）→ 内置适配器自动接管；
3. 彻底移除：删掉 `<hermes home>\profiles\<profile>\plugins\feishu-card\`。

## 设计（三条硬约束）

| 约束 | 做法 | 解决的老问题 |
|---|---|---|
| **单写者** | 每个会话一个 `ChatCardSession`，状态（message_id / blocks / append_count / status / seq）只在该对象内变更，所有更新串行经过一把锁 | 老实现状态散在三个 dict、四条路径各自改写 → 竞态与"内容被顶掉" |
| **绝不死等** | 卡片 create/patch 全部包 `asyncio.wait_for`（默认 15s，`HERMES_FEISHU_CARD_TIMEOUT` 可调）；超时即判失败 | 老实现 PATCH 无超时，一次挂起永久占住锁 → 2026-09-13 整轮卡死、飞书不回话 |
| **失败换卡** | PATCH 失败/超时 → 旧卡封口 + 用同一份 blocks 新建卡（内容不丢）→ 再失败则退回官方纯文本发送 | 老实现失败即丢弃或阻塞 |

其他保留的特性：工具进度折叠块（同一工具连续调用合并）、长代码块默认收起、
收尾消息（💾 记忆保存）不打断状态行、容量保护（30 次追加 / 60 块 / 15KB / 2 表）、
8 秒无新消息 → 状态行「✅ 完成」、多块长回复合并进同一张卡。

**互动卡（授权/追问）后自动换新卡**：审批卡与追问卡是插在会话中间的独立消息，
但它们之后的输出**不会**再写回上方的旧卡——发出互动卡时旧卡收口（✅ 完成），
下一段内容开新卡。原因：飞书聊天窗不会因为旧卡被更新而自动上滚，用户根本看不到。
（2026-09-14 修，覆盖 `send_exec_approval` / `send_update_prompt`，形参用 `*args/**kwargs`
透传以防上游改签名。）

**长任务心跳不会吞内容**：Hermes 每 N 分钟发一条「已持续工作 N 分钟」并用 `edit_message`
原地更新——而那条消息 id 就是本会话的卡片。插件把这类**短状态行**渲染为可原地替换的
`notice` 行（重复心跳只更新同一行，出正文/收尾时自动消失），**已有内容绝不清空**；
带 `finalize=True` 或长/多行文本的编辑则按**正文追加**处理。
（2026-09-14 修——旧实现按"整卡替换"处理，心跳一来就把卡片内容吞掉。）

**工具面板默认折叠**：连续调用工具时，所有工具进度**合并进同一个折叠面板**并保持收起
（标题为 `🛠️ 工具调用 ×N · 首行摘要`，折叠状态下也能看出跑了几个/在跑什么），
面板内容超过 4000 字符只保留最新的部分并标注省略量。
（2026-09-15 修——旧实现建块时写死展开、只在有正文时才收起，所以连续调工具期间一直摊着。
设计与 CM 在 DSH 插件定的规则对齐：工具面板默认折叠、点开才看详情。）

**连续 terminal 的裸代码块也会折叠**：上游会**刻意丢掉连续 terminal 调用重复的表头**
（`run_turn_runner.py:239`），第 2 条起的工具进度是没有任何 emoji 的裸 ``` 块——
单看文本无法与"用户要的纯代码回复"区分，因此靠**上下文**判定：
只有当会话最后一块仍是未结束的工具面板时，裸代码块才升级为工具进度（进折叠面板）；
正文里的代码块保持原样渲染。文案 emoji 黑名单也已修正（📄🖼️💬❓💡 都是真实工具），
并支持"emoji + 变体选择符"（🖼️⚠️🗓️）。（2026-09-15）

**同一段内容只出现一次**（2026-09-15）：换卡时旧卡封口只保留它**已上卡**的内容、新卡只带
「（接上一条卡片）」+ 未上卡的增量（`committed_count` 记账）；`edit_message` 实现为
**幂等/替换**语义（相同 → 跳过、超集 → 替换、其余追加），因为上游的 edit 语义本就是
"原地替换/对账"；`send()` 分块失败也只补发**未入卡**的剩余块。不变量探针新增
I11（卡内不重复）/I12（跨卡不重复）锁住这三条。

**互动卡发送带硬超时**（默认 30s，`HERMES_FEISHU_APPROVAL_SEND_TIMEOUT` 可调）：超时即返回
失败结果（网关会走 "Failed to send approval request" 让 agent 正常收尾），不再出现
"审批请求永远 pending、日志完全静默"。无论成功或超时都按"互动已发生"收口卡片。

> 参考：Hermes 审批链路的既有兜底（v0.21.0 实测）——智能判定 LLM **30s**（`auxiliary.approval.timeout`）、
> 网关等待发卡结果 **15s**（超时按 ambiguous 继续、不重发）、等你点按钮 **300s**
> （`approvals.timeout`，超时按拒绝）。本插件补的是"发送协程自己没有截止时间"那一环。

## 结构

```
feishu-card/
├── plugin.yaml        # 插件清单（name/kind: platform）
├── __init__.py        # 只导出 register（轻量：不导入 lark）
├── card_plugin.py     # 注册层：同名覆盖平台 "feishu"，官方能力全部惰性透传
├── card_adapter.py    # CardFeishuAdapter(官方 FeishuAdapter)：只覆盖出站 + 卡片超时
├── card_session.py    # ChatCardSession：单写者状态机（可脱离飞书单测）
├── card_render.py     # 纯函数：卡片 JSON / 分块 / 容量 / 消息分类
└── tests/
    ├── test_card_session.py   # 42 项单测，不联网
    └── smoke_live.py          # 真机烟测（会向指定会话发卡片）
```

**继承 vs 覆盖**：入站（WS 重连、消息批处理、媒体下载、webhook、审批与卡片回调、
reactions）全部继承官方实现；我们只维护出站卡片这一层（约 500 行）。

## 自检

```powershell
# 单元测试（不联网、不需要 lark）
& "<hermes home>\hermes-agent\venv\Scripts\python.exe" tests\test_card_session.py

# 真机烟测（向飞书发卡片，验证创建/追加/工具块/多块/密封）
$env:HERMES_SMOKE_CHAT="oc_xxx"
& "<hermes home>\hermes-agent\venv\Scripts\python.exe" tests\smoke_live.py
```

装完确认生效：日志（`<profile>\logs\agent.log`）里应出现
`[feishu-card] platform 'feishu' registered with card-mode adapter`；
回复时出现 `[feishu-card] card created | chat=… card=… seq=1 appends=1 blocks=1 …`。

## 注意

* 插件目录名/清单名固定 `feishu-card`，`plugins.enabled` 里写的就是这个名字；
* 覆盖只在**同一个 profile 作用域**内生效 → 每个要用卡片模式的 profile 各装一份；
* 超时只包卡片调用；上传等可能合法耗时较长的调用不受影响；
  被放弃的线程会占住线程池一个 worker（上限 10），不会拖垮进程。
