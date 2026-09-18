---
name: context-guard
description: 会话上下文守卫。监控 agent 会话的上下文用量与 auto-compact 次数，在越线时提醒用户收尾并新开会话，避免因多次压缩导致结论漂移。当用户提到「上下文快满了」「会话压缩」「上下文偏移」「该开新会话了吗」「context guard」「会话守卫」，或询问如何跨 Claude Code / CodeBuddy / WorkBuddy / Codex 统一监控上下文时使用本 skill。也适用于：给现有 hook 排错、调整压缩预警阈值、诊断为什么守卫没触发。
---

# context-guard

跨 harness 的会话上下文守卫。支持 Claude Code / CodeBuddy Code / WorkBuddy / Codex CLI。

## 这个 skill 是什么形态

**它不是一个纯文本 skill，而是一套 hook 安装包。** skill 本身只是文档入口，真正干活的是 `context_guard.py`，它必须被挂到 harness 的 hook 配置上才会运行。

这一点很重要：**如果有人问"能不能只装 skill 就生效"，答案是不能。** 必须执行 `install.sh` 写 hook 配置。

## 什么时候用它

**用户想装/修这个守卫时**：

```bash
cd <skill 目录>
./install.sh --dry-run    # 先给用户看会改什么
./install.sh
/usr/bin/python3 context_guard.py --doctor    # 自检
```

**用户问"我的会话是不是快满了"时**：

```bash
/usr/bin/python3 context_guard.py --status
```

输出会给出当前上下文占用、窗口占比、压缩次数、当前阈值、是否越线。

**用户问"为什么守卫没提醒"时**，按这个顺序查：

1. 状态文件 mtime 是否刷新（`ls -la ~/.<harness>/hooks-state/`）—— 不刷新就是 hook 没被调用
2. **Codex 用户** → 是否在 `codex` 里执行过 `/hooks` 并信任？未信任则静默不触发
3. Codex → `codex features list` 里 `hooks` 是否为 `true`
4. 配置文件 JSON 是否合法
5. `--doctor` 里 transcript 能否定位到

## 关键知识（排查时必用）

### 两家的 token 语义正好相反

这是最容易搞错、且错了会「静默失效」的地方：

| harness | 上下文占用怎么算 |
|---|---|
| Claude Code / CodeBuddy / WorkBuddy | `input_tokens`（**已含**缓存，cache_read 是子集，**不能相加**） |
| Codex | `input_tokens + cached_input_tokens`（两者互斥，**必须相加**） |

- Claude 系写成相加 → 数值翻倍 → 疯狂误报
- Codex 写成只取 input → 数值差两个数量级 → **永远不触发**，且看起来一切正常

### 阈值是窗口比例，不是绝对值

各家窗口差异巨大（Claude 系 200K vs Codex 实测 353K）。阈值按 `窗口 × 比例` 算：

- 0 次压缩：warn 68% / danger 75%
- 压缩越多越提前（摘要已叠多层）

用户报"提醒太早/太晚"，改 `context-guard.json` 的 `ratios`，不要改代码。

### 需要提醒用户的一件事

守卫提醒本身占用上下文。所以它做了强节流：**只在级别上升时提醒一次**，一个压缩周期最多打扰两次。用户若抱怨"只提醒一次就不提了"，这是设计而非故障。

## 交付物清单

| 文件 | 作用 |
|---|---|
| `context_guard.py` | 核心脚本（自包含、零依赖、py3.9+） |
| `context-guard.json` | 阈值配置，改完立即生效 |
| `install.sh` | 安装器，自动探测 harness + 幂等写配置 + 备份 |
| `README.md` | 完整文档：支持矩阵、通用化边界、踩坑记录、排错 |

## 硬约束

- **绝不能阻塞会话**：任何异常必须吞掉并返回 `{"continue": true}`
- **绝不能覆盖用户已有的 hook**：安装器只追加（用户可能有自己的 hook，如会话采集）
- **必须快**：`PostToolUse` 每次工具调用都触发，只读 transcript 尾部 512KB
- **兼容 macOS 自带 python3 3.9**：不用 3.10+ 语法
