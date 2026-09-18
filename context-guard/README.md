# context-guard

**跨 harness 的会话上下文守卫** —— 在 agent 即将触发 auto-compact 时提醒你收尾，并在新会话自动接上进展。

支持 **Claude Code / CodeBuddy Code / WorkBuddy / Codex CLI**。

---

## 它解决什么问题

所有主流编码 agent 在上下文变长时都会自动压缩（auto-compact）：把早期对话换成摘要。这件事**有损且不可逆**，叠加多次后会出现三种典型症状：

- 记错前面已经定下的结论
- 重复提出已经被否决的方案
- 遗忘早期设定的约束

这不是模型变笨了，是原始记录已经被摘要覆盖了好几层。

`context-guard` 挂在会话生命周期上，读 transcript 里的**真实 token 用量**，在越线时向 agent 注入一条提醒（由它转达你），并在新会话启动时自动加载上一会话的交接文件。

### 为什么必须是 hook，不能是 skill

skill 是**被动加载的指令文本**：没有运行时、不挂在任何生命周期事件上。你不调用它，它就永远不动。它没法"及时"任何东西。

能做这件事的只有 hook —— 内核在每个生命周期事件上主动调用它。

---

## 支持矩阵

| | Claude Code | CodeBuddy Code | WorkBuddy | Codex CLI |
|---|---|---|---|---|
| 配置位置 | `~/.claude/settings.json` | `~/.codebuddy/settings.json` | `~/.workbuddy/settings.json` | `~/.codex/hooks.json` |
| 配置格式 | JSON | JSON | JSON | JSON（也可 TOML 内联） |
| `SessionStart`（交接注入） | ✅ | ✅ | ✅ | ✅ |
| `UserPromptSubmit` / `PostToolUse` | ✅ | ✅ | ✅ | ✅ |
| `PreCompact`（精确计数） | ✅ | ✅ | ✅ | ✅ |
| `PostCompact` | — | — | — | ✅ |
| 上下文占用来源 | `message.usage.input_tokens` | 同左 | 同左 | `payload.info.last_token_usage` |
| **占用算法** | `input_tokens` | `input_tokens` | `input_tokens` | `input + cached_input` |
| 窗口大小 | 兜底 200K | 兜底 200K | 兜底 200K | 读 transcript 真实值（实测 353K） |
| 配置生效方式 | 热加载 | 热加载 | 热加载 | **需 `/hooks` 一次性信任** |

> 前三家（Claude Code / CodeBuddy / WorkBuddy）的 hook 协议同源，字段名与输出 schema 完全一致，所以适配层几乎是同一份代码。

---

## 安装

```bash
git clone git@github.com:evaevable/skills.git
cd skills/context-guard
./install.sh --dry-run     # 先看会改什么
./install.sh               # 正式安装
```

安装器会：

1. 探测本机已安装的 harness（检查对应配置目录是否存在）
2. 把守卫挂到该 harness 的 hook 配置上，**只追加不覆盖**（你已有的 hook 会被保留）
3. 改动前备份原配置文件（`settings.json.bak-context-guard-<时间戳>`）
4. 幂等 —— 重复执行不会重复添加

只装某一家：

```bash
./install.sh --only codex
```

卸载：

```bash
./install.sh --uninstall
```

---

## 如何测试

分三层，**从下往上做**。前两层完全离线，不需要停会话、也不需要等真实压缩真的发生。

### 第 1 层：自测台（离线，21 项）

```bash
cd context-guard && ./test.sh          # 隔离状态目录，不碰生产数据
./test.sh --live                       # 额外做活性验证
./test.sh --harness codex              # 指定 harness（默认自动探测）
```

原理很简单：**hook 就是一个「读 stdin JSON → 写 stdout JSON」的普通进程**，所以可以直接造事件喂给它，检查输出。

覆盖的断言：

| 组 | 验证什么 |
|---|---|
| 1 语法依赖 | 能编译、Python ≥ 3.9 |
| 2 健壮性 | 空 stdin / 垃圾输入 / 缺字段 / transcript 不存在 —— **全部必须静默且不报错** |
| 3 判级 | 未越线静默、越预警线提醒、越危险线提醒 |
| 4 阈值自适应 | 同样 140K，压缩 0 次只算预警、压缩 3 次已构成危险；冷启动回填计数正确 |
| 5 节流 | 同级别第二次调用必须静默 |
| 6 压缩事件 | `PreCompact` 精确计数，且不与断崖推断重复计数 |
| 7 交接注入 | 注入正文、标记 `.consumed.md`、不重复注入 |
| 8 CLI | `--status` / `--doctor` 可运行 |
| 9 性能 | 单次耗时中位 < 300ms（含解释器冷启动） |

**第 2 组是最该加的一组**：hook 出错会**静默失败**，会话照常进行，你根本不知道守卫已经瞎了。所以"异常输入必须静默"和"正常越线必须提醒"要一起测。

### 第 2 层：自检（跑真实路径）

```bash
/usr/bin/python3 context_guard.py --doctor
```

逐家报告：配置目录是否找到、能否定位 transcript、读出的占用和窗口是多少，并打印各窗口下的阈值表。**这比单测更有价值** —— 像"适配器从 class 改成 dict 后残留 `.root` 属性访问"这类错，只在某条分支触发，静态检查抓不到。

看真实状态：

```bash
/usr/bin/python3 context_guard.py --status          # 最近 5 个会话
/usr/bin/python3 context_guard.py --status <sid>    # 指定会话
```

### 第 3 层：活性验证（唯一可信的方式）

前面两层证明的是"逻辑对"，**不能证明 hook 真被内核调用了**。唯一可信的验证是看状态文件有没有被你**没手动触发**的动作改掉：

```bash
ls -la ~/.<harness>/hooks-state/     # 记下 mtime 和 records_seen
# 然后在会话里发一条消息 / 跑一次工具调用
ls -la ~/.<harness>/hooks-state/     # mtime 刷新、计数 +1 = hook 生效
```

或者 `./test.sh --live` 会直接把各家状态目录的计数和更新时间打出来。

**只看「没报错」是无效验证** —— 脚本出错时通常也是静默的。看计数器增长。

> 实测补充：Claude 系（含 WorkBuddy）**不需要重启、也不需要面板审批**，settings.json 写入后热加载（实测 14:15 写入、14:16 生效）。Codex 需要一次 `/hooks` 信任审批。桌面端文档里"需重启 + `/hooks` 审批"的说法对 Claude 系不适用。

---

## 通用化的边界（这部分最重要）

做通用化时最容易犯的错，是以为"换个配置文件路径就行"。实际上要分三层看：

### 第一层：**完全通用**，跨四家一字不改

- 全部算法逻辑：阈值判级、强节流、断崖检测、冷启动回填、状态持久化
- hook 的 stdin 字段名：`session_id` / `transcript_path` / `cwd` / `hook_event_name`
- hook 的输出 schema：`hookSpecificOutput.additionalContext` / `systemMessage` / `continue`

这层占了代码的绝大部分。四家在这一点上是同源的 —— Codex 明显参考了 Claude Code 的协议。

### 第二层：**必须适配**，收敛在 `ADAPTERS` 注册表里

每家提供五个方法：

| 方法 | 作用 |
|---|---|
| `locate` | 找到当前会话的 transcript 文件 |
| `occupancy` | **算出当前上下文占用了多少 token** |
| `window` | 上下文窗口大小 |
| `history` | 冷启动时回溯历史（压缩次数 / 峰值 / 最后一次用量） |
| `root` | 配置目录名（用于定位状态目录与 transcript） |

### 第三层：**硬差异**，无法用抽象抹平，必须写进文档

- Codex 的 hook 需要用户执行 `/hooks` 逐条信任，否则**静默不触发**（Claude 系是热加载，不需要）
- Codex 多一个 `PostCompact` 事件
- Codex 的前置 feature flag 必须是 `hooks = true`

---

## ⚠️ 最大的坑：两家的 token 语义正好相反

这是做通用化时**一定会踩**的地方，也是这个仓库最值得抄的一条结论。

### Claude 系（Claude Code / CodeBuddy / WorkBuddy）

`input_tokens` **已经包含**了缓存部分，`cache_read_input_tokens` 是它的**子集**。

同一个 `usage` 块的真实数据（WorkBuddy 会话，实测）：

| input_tokens | cache_read_input_tokens | 比值 |
|---|---|---|
| 80,358 | 78,080 | 97% |
| 114,017 | 108,800 | 95% |

两者数值接近 —— 因为绝大部分输入都命中了 prompt cache。

```python
# 正确
occupancy = input_tokens

# 会直接翻倍的错法
occupancy = input_tokens + cache_read_input_tokens   # ❌ 得到一个虚构的 222,817
```

**判定依据**：这个会话的 `input_tokens` 峰值是 168,983，而它的 auto-compact 实际触发点稳定在 150K~165K —— 正好是 200K 窗口的 75%~82%。如果 `input_tokens` 只是"未缓存部分"，它不可能这么稳定地贴着压缩阈值爬升。

### Codex

`last_token_usage.input_tokens` 是**未缓存**输入，`cached_input_tokens` 是缓存输入，两者**并列互斥**。

```json
{"payload": {"type": "token_count", "info": {
  "last_token_usage": {
    "input_tokens": 2500,
    "cached_input_tokens": 290000,
    "output_tokens": 500
  },
  "model_context_window": 353400
}}}
```

```python
# 正确
occupancy = input_tokens + cached_input_tokens    # = 292,500

# 只取 input_tokens 的错法
occupancy = input_tokens                          # ❌ 2,500，差 117 倍
```

**差 117 倍意味着什么**：提醒永远不会触发。你会以为守卫装好了，其实它在每次事件里安静地返回"一切正常"。

两家一个要加、一个不能加，**方向正好相反** —— 所以绝不能写成一份通用逻辑。

---

## 阈值为什么用比例而不是绝对值

第一版写的是绝对阈值（`warn=130000` / `danger=145000`），那是照着 Claude 系 200K 窗口实测出来的。一旦换到 Codex（实测窗口 **353,400**），这套值会让危险线落在窗口的 41% —— 提前得太离谱，等于每次都在会话刚开始时喊"该收尾了"。

所以改成**按窗口比例**：

| 已压缩次数 | warn | danger | 200K 窗口下 | 353K 窗口下 |
|---|---|---|---|---|
| 0 | 68% | 75% | 136K / 150K | 240K / 265K |
| 1 | 60% | 70% | 120K / 140K | 212K / 247K |
| 2 | 52% | 62% | 104K / 124K | 184K / 219K |
| 3+ | 44% | 55% | 88K / 110K | 155K / 194K |

**压缩次数越多，阈值越提前** —— 因为摘要已经叠加了多层，越晚提醒越亏。

校验：0 次压缩时 200K × 0.75 = 150K，与实测触发点 150K~165K 吻合。

> **Codex 侧的阈值尚未用真实长会话校准**（见下方"未验证项"）。比例法能保证量级正确，但 Codex 的实际压缩触发点需要你自己观察后微调。

---

## 设计要点

### 强节流：只在「级别上升」时提醒一次

注入提醒这件事本身会占用上下文、**加速**压缩，形成恶性循环。所以节流策略是硬的：

- 只在 `level > last_level` 且 `level > 0` 时注入
- 跨越预警线提醒一次、跨越危险线再提醒一次，一个周期最多两次
- 压缩发生后 `last_level` 归零，允许下个周期重新计数

### 冷启动要回溯历史

第一次装上守卫时，它**不知道这个会话已经压缩过几次**。不回填的话会用到最宽松那档阈值，文案还会说"尚未压缩过"—— 全是错的。

所以状态文件不存在时，扫一遍 transcript 补齐。这里有两个已修的坑（见下）。

### 交接闭环：只提醒不交接等于负收益

提醒用户"该新开会话了"，结果新会话把进展清零 —— 那这个提醒就是有害的。

所以配套 `SessionStart` 注入：把上一会话留下的 `.context-guard/handoff/latest.md` 读进新会话上下文，然后**重命名**（不是删除）为 `.consumed.md`，内容随时可回溯。

配套 skill 见 [`session-handoff`](../session-handoff/)。

---

## 开发中踩到并修掉的六个真 bug

记在这里是因为**它们会重复发生**。

**① `cache_read_input_tokens` 是 `input_tokens` 的超串。**
用正则抓 `input_tokens` 时如果不加词边界，会把 cache 读数也抓进来。实测把压缩次数从 3 数成 6、峰值从 165,333 读成 165,120。

```python
# 必须带 (?<![_A-Za-z])
_USAGE_RE = re.compile(rb"(?<![_A-Za-z])input_tokens['\"]?\s*:\s*(\d+)")
```

**② 回填状态要用「最后一条用量」，不能用「峰值」。**
用峰值初始化 `last_input`，会让当前值（已经从峰值回落）看起来像一次新的断崖，**凭空多算一次压缩**（实测 3 变 4）。

**③ 适配器从 class 改成 dict 后残留属性访问。**
`.root` 在 dict 上不存在。这类错误只在某条分支上触发，静态检查抓不到 —— 所以 `--doctor` 这种"跑一遍真实路径"的自检比单测更有价值。

**④ `PreCompact` 与断崖推断会双重计数。**
`PreCompact` 精确 +1 之后，用量回落又会被断崖检测再 +1，压缩次数直接翻倍。修法是设一个「这次已经数过了」的标记，让紧接着的那次断崖检测跳过。

**⑤ 空 stdin 会误触发提醒。**
`printf '' | script` 竟然触发了越线提醒 —— 因为空输入下解析出的用量是 0，反而落进了某条判断分支。这违反"绝不误报"原则，必须在入口就挡掉。

**⑥ 用紧凑 JSON 字符串做预筛，遇到带空格格式会静默失效。**
`b'"type":"message"'` 匹配不到 `"type": "message"`（冒号后有空格）。真实 transcript 目前是紧凑格式所以能用，但**不该依赖序列化细节** —— 改成正则容忍空格。这个坑最阴险的地方是：它不会报错，只会让所有统计悄悄变成 0。

---

## 危险状态是怎么判定的

有两级，判定是 `cur >= 阈值` 的简单比较，但**阈值本身不是常数**：

```python
level = 2 if cur >= danger else (1 if cur >= warn else 0)
warn, danger = window * ratio_warn, window * ratio_danger   # 比例，非绝对值
```

所以"危不危险"取决于三个量：

| 量 | 从哪来 | 为什么要这样 |
|---|---|---|
| `cur` 当前占用 | 读 transcript 最后一条带 `usage` 的记录 | 用真实值，不估算 |
| `window` 窗口大小 | Codex 从 `session_meta.context_window` 读真实值；Claude 系用兜底 200K | 实测 Codex 是 **353,400**，写死必错 |
| `ratio` 比例 | 按 `compact_count` 选档（68/75 → 60/70 → 52/62 → 44/55） | 压缩越多、摘要叠层越多，越要提前 |

**关键点：危险不由 token 数单独决定，而是由「占窗口的比例 × 已压缩次数」共同决定。** 同样 140K，在压缩 0 次的会话里只是预警，在压缩 3 次的会话里已经是危险 —— 因为前者的上下文是原始对话，后者已经经过三道有损转换。

判级只在**级别上升**时提醒一次（`level > last_level and level > 0`），所以一个"压缩周期"内最多打扰你两次。压缩发生后 `last_level` 归零，允许下个周期重新计数。

查看当前处于哪一档：

```bash
/usr/bin/python3 context_guard.py --status
# 状态: 危险 —— 已越过危险线 / 预警 —— 越过预警线 / 正常
```

---

## 交接内容是怎么展示的

交接文件落在工作区里，默认 `.context-guard/handoff/latest.md`（兼容老的 `.workbuddy/handoff/` 和 `.codebuddy/handoff/`）。

**新会话启动时**，`SessionStart` 事件把它读进来，作为 `additionalContext` 注入，然后**重命名**为 `latest.consumed.md`：

```
会话启动
   ↓  SessionStart 触发
扫描 cwd/.context-guard/handoff/latest.md
   ↓  找到
整篇读入 → 加上一行抬头 → 注入 additionalContext
   ↓
重命名 latest.md → latest.consumed.md   （不删除，随时可回溯）
   ↓
同一份文件不会再次注入（第二次 SessionStart 静默）
```

实际注入的样子：

```
# 上一会话交接内容（由 context-guard 注入）

# 会话交接 · 2026-09-18 15:52

## 当前任务
把 context-guard 从 WorkBuddy 单平台改成跨 harness 通用版…

## 已完成
- 核心脚本 context_guard.py（1138 行，零依赖，py3.9+），适配 4 家
…

## 已否决的方案（不要重提）
- 用向量库做记忆检索 —— 索引跟不上活跃代码库
- 对 Codex 用 Claude 系 token 语义 —— 会把 292,500 读成 2,500
```

三个设计取舍：

- **用重命名而非删除**做消费标记 —— 注入过什么随时可查，出问题能追溯。
- **超长截断到 12000 字符**，并在文末标出完整文件路径。交接文件是全量注入的，不加限制会一上来就吃掉大块上下文 —— 那和守卫的目标正好相反。
- **模板里必须有「已否决的方案」一节**。这是实践里最容易漏、也最致命的一节：不写，新会话会把你已经论证过不可行的方案再提一遍，然后你得重新论证一遍。模板见 `templates/session-handoff.md`。

配套 skill 见 [`session-handoff`](../session-handoff/)，说一句「交接一下」就生成。

---

## 配置

编辑 `context-guard.json`，**改完立即生效**：

```json
{
  "enabled": true,
  "contextWindow": null,
  "ratios": [
    { "minCompacts": 0, "warn": 0.68, "danger": 0.75 }
  ]
}
```

| 键 | 说明 |
|---|---|
| `enabled` | 总开关 |
| `contextWindow` | 覆盖窗口大小；留 `null` 自动探测 |
| `ratios` | 阈值比例表（推荐方式） |
| `warn` / `danger` | 绝对值覆盖（设了则 `ratios` 失效，用于复刻精确值） |
| `stateDir` | 状态目录，默认 `~/.<harness>/hooks-state` |

也可以用环境变量（优先级更高）：`CONTEXT_GUARD_CONFIG` / `CONTEXT_GUARD_STATE_DIR` / `CONTEXT_GUARD_HARNESS` / `CONTEXT_GUARD_DEBUG`。

---

## 常用命令

```bash
CG=~/.<harness>/hooks-state  # 视 harness 而定

python3 context_guard.py --doctor          # 自检：探测环境与解析器
python3 context_guard.py --status          # 查看最近几个会话的守卫状态
python3 context_guard.py --status <sid>    # 查看指定会话
python3 context_guard.py --reset <sid>     # 重置某会话状态
CONTEXT_GUARD_DEBUG=1 python3 context_guard.py --status   # 带调试日志
```

---

## 排错

| 症状 | 原因与处理 |
|---|---|
| 状态文件 mtime 不刷新 | hook 没被调用。Codex 侧先执行 `/hooks` 信任；其余检查配置路径是否正确、JSON 是否合法 |
| `--doctor` 里 transcript 显示"未找到" | 该 harness 没跑过会话，或 `cwd` 与配置目录 slug 不匹配 |
| 占用值明显偏小 | 检查是否踩了 token 语义坑：Codex 要相加，Claude 系不能相加 |
| 提醒过于频繁 | 调高 `ratios` 的 `warn`/`danger`；确认节流逻辑未被绕过 |
| Codex 无任何反应 | 确认 `codex features list` 里 `hooks` 为 `true` |
| 窗口读到 None | Codex 会从 transcript 读；Claude 系目前用兜底值，可在配置里显式指定 |

---

## 实测记录与未验证项

诚实标注，避免你误以为全部路径都验证过。

**已实测验证（2026-09-18）**：

- ✅ Claude 系解析器在真实 WorkBuddy 会话上正确（压缩 4 次 / 峰值 168,983 / 最后 134,703 / 工具调用 211，与结构化独立解析一致）
- ✅ 四家 harness 配置目录探测正确
- ✅ 阈值比例法输出与首版手调值吻合（200K → 136K/150K vs 原 130K/145K）
- ✅ 安装器 dry-run 正确保留用户已有的 hook（CodeBuddy 的会话采集 hook 未被覆盖）
- ✅ Codex transcript 结构实测：`session_meta.payload.context_window` 读出 **353,400**
- ✅ Codex token 解析用 fixture 验证（292,500 正确算出）

**未验证**：

- ⚠️ **Codex 路径未跑过真实长会话** —— 本机 Codex API key 失效（401），无法生成带 `token_count` 的真实 rollout。解析逻辑只用 fixture 验证过结构，**请在第 0 步先观察一次真实触发**再依赖它
- ⚠️ Codex 的实际 auto-compact 触发点未知，比例阈值按 0.75 推算，需实测校准
- ⚠️ Claude Code / CodeBuddy 两条路径的解析器与 WorkBuddy 同源，但未在真实会话上分别验证

---

## 许可

MIT
