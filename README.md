# skills

个人 skill 大仓 —— 跨 harness 的 agent 扩展集合。

这里放的不是"提示词模板"，而是**带运行时的扩展**：hook 脚本、适配器、安装器，以及配套的 skill 文档。

---

## 仓库约定

每个 skill 一个顶层目录，结构约定：

```
<skill-name>/
├── SKILL.md          # 必需。带 YAML frontmatter，agent 加载入口
├── README.md         # 给人看的详细文档
├── install.sh        # 可选。需要注册到 harness 的 skill 提供一键安装
├── templates/        # 可选。产出的文件模板
└── <核心脚本>         # 可选。实际干活的代码
```

**SKILL.md 的 frontmatter 规范**（各家 harness 基本同源）：

```yaml
---
name: <与目录名一致>
description: <一句话说清「什么时候该用我」，包含触发词>
---
```

`description` 是唯一的检索依据，要写**触发条件**而不是功能简介 —— agent 是靠它判断"现在该不该加载"的。

**SKILL.md 与 README.md 的分工**：

- `SKILL.md` 写给 **agent** 看：怎么用、关键判断规则、硬约束。要短，要可执行。
- `README.md` 写给 **人** 看：完整背景、设计取舍、踩坑记录、排错。可以长。

不要把设计文档塞进 SKILL.md —— 那会白白占用每次加载的上下文。

---

## Skill 清单

### [context-guard](./context-guard/) — 会话上下文守卫

在 agent 即将自动压缩（auto-compact）上下文时提醒你收尾，并在新会话自动接上进展。

**支持 Claude Code / CodeBuddy Code / WorkBuddy / Codex CLI。**

它监控 transcript 里的真实 token 用量与压缩次数，越线时向 agent 注入一条提醒（由它转达你）。因为压缩是有损且不可逆的，叠加多次后会出现"记错结论、重提已否决方案、遗忘早期约束"这类漂移。

**安装与验证**：

```bash
cd context-guard
./install.sh --dry-run && ./install.sh     # 自动探测 + 幂等 + 只追加不覆盖 + 改前备份
./test.sh                                  # 离线自测 21 项，隔离状态目录
/usr/bin/python3 context_guard.py --doctor # 自检，打印各窗口下的阈值表
```

**测试分三层，别跳过第三层**：离线造事件测逻辑 → `--doctor` 跑真实路径 → **看状态文件的计数是否随工具调用递增**。第三层才能证明 hook 真被内核调用了；前两层只证明逻辑对。**只看「没报错」是无效验证 —— hook 失败时通常也是静默的。**

**为什么值得单独做一个**：这件事**只能用 hook 做，skill 做不了** —— skill 是被动加载的文本，没有运行时、不挂在生命周期上，你不调用它就永远不动。

**通用化的三个难点**（细节见 [context-guard/README](./context-guard/README.md)）：

1. **两家的 token 语义正好相反** —— Claude 系的 `input_tokens` 已含缓存，Codex 的要 `input + cached`。一个要加、一个不能加，写错会「静默失效」
2. **窗口大小差异巨大** —— Claude 系 200K，Codex 实测 353K。所以阈值必须按比例而非绝对值
3. **Codex 的 hook 需要一次性 `/hooks` 信任** —— 否则不触发，且不报错

### [session-handoff](./session-handoff/) — 会话交接

`context-guard` 的配套 skill。守卫提醒"该新开会话了"之后，用它把当前进展固化成文件，新会话启动时自动加载。

**为什么必需**：只提醒不交接等于负收益 —— 新会话把进展清零，用户还得重讲一遍背景。

**最有价值的一节是「已否决的方案」**：不写的话，新会话几乎必然把已经被否掉的方案重新提一遍。

---

## 通用安装方式

需要注册到 harness 的 skill，都提供幂等的 `install.sh`：

```bash
./install.sh --dry-run      # 先看会改什么（推荐）
./install.sh                # 正式安装
./install.sh --only codex   # 只装某一家
./install.sh --uninstall    # 卸载
```

安装器共同遵守的约定：

- **只追加不覆盖** —— 你已有的 hook（如自定义的会话采集）会被完整保留
- **改前备份** —— 原配置复制为 `<name>.bak-<skill>-<时间戳>`
- **幂等** —— 重复执行不会重复添加
- **支持 dry-run** —— 先看清楚再做

---

## 各 harness 配置位置速查

| harness | 配置目录 | hook 配置文件 | 生效方式 |
|---|---|---|---|
| Claude Code | `~/.claude/` | `settings.json` | 热加载 |
| CodeBuddy Code | `~/.codebuddy/` | `settings.json` | 热加载 |
| WorkBuddy | `~/.workbuddy/` | `settings.json` | 热加载 |
| Codex CLI | `~/.codex/` | `hooks.json`（或 `config.toml` 内联） | 需 `/hooks` 信任 |

前三家的 hook 协议同源（`session_id` / `transcript_path` / `cwd` / `hook_event_name` 字段一致，输出都认 `hookSpecificOutput.additionalContext`），所以一份脚本可以直接跑在三家上。

---

## 许可

MIT
