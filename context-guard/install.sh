#!/usr/bin/env bash
# context-guard 安装器
#
# 自动探测本机已安装的 harness，把守卫挂到各家的 hook 配置上。
# 幂等：重复执行不会重复添加。会先备份被改动的配置文件。
#
# 用法:
#   ./install.sh              # 只装探测到的 harness
#   ./install.sh --dry-run    # 只打印将要做的改动
#   ./install.sh --only codex # 只装指定 harness
#   ./install.sh --uninstall  # 卸载

set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*"; }
dim()  { printf '%s%s%s\n' "$DIM" "$*" "$RST"; }

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE="$SELF_DIR/context_guard.py"
CONF="$SELF_DIR/context-guard.json"

DRY=0
ONLY=""
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --only)      : ;;
    codex|claude-code|codebuddy|workbuddy) ONLY="$arg" ;;
    --only=*)    ONLY="${arg#--only=}" ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) warn "未知参数: $arg" ;;
  esac
done

[ -f "$CORE" ] || { err "找不到核心脚本: $CORE"; exit 1; }

# ---- 选一个可靠的 python3（hook 每次调用都要起它，必须绝对路径且稳定）
PY=""
for cand in /usr/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null)"; do
  [ -n "$cand" ] && [ -x "$cand" ] && { PY="$cand"; break; }
done
[ -n "$PY" ] || { err "找不到 python3"; exit 1; }

# macOS 自带 python3 需要 Command Line Tools；如果它只是个 stub，换别的
if [ "$PY" = "/usr/bin/python3" ] && ! "$PY" -c 'import json' >/dev/null 2>&1; then
  warn "/usr/bin/python3 不可用（可能没装 Command Line Tools），尝试其它解释器"
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -x "$cand" ] && { PY="$cand"; break; }
  done
fi
"$PY" -c 'import json' >/dev/null 2>&1 || { err "没有可用的 python3"; exit 1; }

say "context-guard 安装"
say "核心脚本 : $CORE"
say "解释器   : $PY  ($("$PY" -V 2>&1))"
say ""

HARNESSES=()
if [ -n "$ONLY" ]; then
  [ -d "$HOME/.${ONLY}" ] || warn "~/.${ONLY} 不存在，仍按你指定继续"
  HARNESSES=("$ONLY")
else
  [ -d "$HOME/.claude" ]    && HARNESSES+=(claude-code)
  [ -d "$HOME/.codebuddy" ] && HARNESSES+=(codebuddy)
  [ -d "$HOME/.workbuddy" ] && HARNESSES+=(workbuddy)
  [ -d "$HOME/.codex" ]     && HARNESSES+=(codex)
fi
[ ${#HARNESSES[@]} -gt 0 ] || { err "没有探测到任何 harness 配置目录"; exit 1; }
say "探测到: ${HARNESSES[*]}"
say ""

# ---- 事件到 hook 的挂载表
# Claude 系与 Codex 事件名同源，差别只在配置文件的格式与位置。
EVENTS_4="SessionStart UserPromptSubmit PostToolUse PreCompact"
# Codex 额外有 PostCompact；Claude 系没有
EVENTS_CODEX="SessionStart UserPromptSubmit PostToolUse PreCompact PostCompact"

build_events() {
  local h="$1"
  if [ "$h" = "codex" ]; then echo "$EVENTS_CODEX"; else echo "$EVENTS_4"; fi
}

# 生成 hook command：把 harness 名字固化进去，脚本就不必靠猜
hook_cmd() {
  local h="$1"
  printf 'CONTEXT_GUARD_HARNESS=%s %s %s' "$h" "$PY" "$CORE"
}

# ---- Claude 系：合并 ~/.<root>/settings.json 的 hooks 键（结构化，保证 JSON 合法）
install_claude_like() {
  local h="$1" root="$2" ev="$3"
  local cfg="$HOME/${root}/settings.json"
  local cmd; cmd="$(hook_cmd "$h")"

  "$PY" - "$cfg" "$cmd" "$ev" "$UNINSTALL" "$DRY" <<'PYEOF'
import json, os, sys, shutil, time

cfg, cmd, events, uninstall, dry = sys.argv[1], sys.argv[2], sys.argv[3].split(), \
                                   sys.argv[4] == '1', sys.argv[5] == '1'

data = {}
if os.path.isfile(cfg):
    try:
        with open(cfg) as f:
            data = json.load(f)
    except Exception as e:
        print('  \033[31m✗\033[0m 现有 settings.json 不是合法 JSON，拒绝改动: %s' % e)
        sys.exit(1)
    if not isinstance(data, dict):
        print('  \033[31m✗\033[0m 现有 settings.json 顶层不是对象，拒绝改动')
        sys.exit(1)

hooks = data.get('hooks')
if not isinstance(hooks, dict):
    hooks = {}

MARK = 'context_guard'

def strip(entry_list):
    """移除本守卫添加的条目，保留用户自己写的。"""
    kept = []
    for grp in entry_list:
        if not isinstance(grp, dict):
            kept.append(grp); continue
        inner = grp.get('hooks')
        if not isinstance(inner, list):
            kept.append(grp); continue
        inner = [x for x in inner if not (
            isinstance(x, dict) and
            (MARK in str(x.get('metadata', {})) or 'context_guard.py' in str(x.get('command', ''))))]
        if inner:
            grp = dict(grp); grp['hooks'] = inner
            kept.append(grp)
    return kept

if uninstall:
    for ev in list(hooks.keys()):
        if isinstance(hooks[ev], list):
            hooks[ev] = strip(hooks[ev])
            if not hooks[ev]:
                del hooks[ev]
    data['hooks'] = hooks
    print('  \033[32m✓\033[0m 已从 hooks 中移除 context-guard')
else:
    for ev in events:
        arr = hooks.get(ev)
        if not isinstance(arr, list):
            arr = []
        arr = strip(arr)
        arr.append({
            'matcher': '',
            'hooks': [{
                'type': 'command',
                'command': cmd,
                'timeout': 10,
                'metadata': {
                    'managed_by': MARK,
                    'description': '会话上下文守卫：接近 auto-compact 时提醒收尾。删除本条即可停用。',
                },
            }],
        })
        hooks[ev] = arr
    data['hooks'] = hooks
    print('  \033[32m✓\033[0m 已写入 %d 个事件: %s' % (len(events), ' '.join(events)))

if dry:
    print('  \033[2m(dry-run，未写入)\033[0m')
    print(json.dumps({'hooks': {k: hooks[k] for k in events if k in hooks}},
                     ensure_ascii=False, indent=2)[:800])
    sys.exit(0)

if os.path.isfile(cfg):
    bak = cfg + '.bak-context-guard-' + time.strftime('%Y%m%d%H%M%S')
    shutil.copy2(cfg, bak)
else:
    os.makedirs(os.path.dirname(cfg), exist_ok=True)

tmp = cfg + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
os.replace(tmp, cfg)
print('  \033[32m✓\033[0m 已写入 %s' % cfg)
PYEOF
}

# ---- Codex: 写 ~/.codex/hooks.json（JSON 形式比改 TOML 更安全）
install_codex() {
  local ev="$1"
  local cfg="$HOME/.codex/hooks.json"
  local cmd; cmd="$(hook_cmd codex)"

  "$PY" - "$cfg" "$cmd" "$ev" "$UNINSTALL" "$DRY" <<'PYEOF'
import json, os, sys, shutil, time

cfg, cmd, events, uninstall, dry = sys.argv[1], sys.argv[2], sys.argv[3].split(), \
                                   sys.argv[4] == '1', sys.argv[5] == '1'

data = {}
if os.path.isfile(cfg):
    try:
        with open(cfg) as f:
            data = json.load(f)
    except Exception as e:
        print('  \033[31m✗\033[0m 现有 hooks.json 不是合法 JSON，拒绝改动: %s' % e)
        sys.exit(1)
    if not isinstance(data, dict):
        print('  \033[31m✗\033[0m 现有 hooks.json 顶层不是对象，拒绝改动')
        sys.exit(1)

hooks = data.get('hooks')
if not isinstance(hooks, dict):
    hooks = {}

def strip(entry_list):
    kept = []
    for grp in entry_list:
        if not isinstance(grp, dict):
            kept.append(grp); continue
        inner = grp.get('hooks')
        if not isinstance(inner, list):
            kept.append(grp); continue
        inner = [x for x in inner if not (
            isinstance(x, dict) and 'context_guard.py' in str(x.get('command', '')))]
        if inner:
            grp = dict(grp); grp['hooks'] = inner
            kept.append(grp)
    return kept

if uninstall:
    for ev in list(hooks.keys()):
        if isinstance(hooks[ev], list):
            hooks[ev] = strip(hooks[ev])
            if not hooks[ev]:
                del hooks[ev]
    data['hooks'] = hooks
    print('  \033[32m✓\033[0m 已从 hooks 中移除 context-guard')
else:
    for ev in events:
        arr = hooks.get(ev)
        if not isinstance(arr, list):
            arr = []
        arr = strip(arr)
        arr.append({
            'matcher': '',
            'hooks': [{
                'type': 'command',
                'command': cmd,
                'timeout': 10,
                'statusMessage': '检查上下文用量',
            }],
        })
        hooks[ev] = arr
    data['hooks'] = hooks
    print('  \033[32m✓\033[0m 已写入 %d 个事件: %s' % (len(events), ' '.join(events)))

if dry:
    print('  \033[2m(dry-run，未写入)\033[0m')
    sys.exit(0)

if os.path.isfile(cfg):
    shutil.copy2(cfg, cfg + '.bak-context-guard-' + time.strftime('%Y%m%d%H%M%S'))
else:
    os.makedirs(os.path.dirname(cfg), exist_ok=True)

tmp = cfg + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
os.replace(tmp, cfg)
print('  \033[32m✓\033[0m 已写入 %s' % cfg)
PYEOF

  # Codex 的 hooks 由 feature flag 控制，确认它是开的
  if command -v codex >/dev/null 2>&1; then
    local st
    st="$(codex features list 2>/dev/null | awk '$1=="hooks"{print $3}')"
    if [ "$st" = "true" ]; then
      dim "  codex feature 'hooks' = true（已启用）"
    else
      warn "codex feature 'hooks' 未启用，请执行: codex features enable hooks"
    fi
  fi
  warn "Codex 需要一次性信任：启动 codex 后执行 /hooks，逐个批准 context_guard 条目，否则不会触发"
}

for h in "${HARNESSES[@]}"; do
  evs="$(build_events "$h")"
  case "$h" in
    claude-code) say "[claude-code] ~/.claude/settings.json";   install_claude_like claude-code .claude   "$evs" ;;
    codebuddy)   say "[codebuddy]   ~/.codebuddy/settings.json"; install_claude_like codebuddy   .codebuddy "$evs" ;;
    workbuddy)   say "[workbuddy]   ~/.workbuddy/settings.json"; install_claude_like workbuddy   .workbuddy "$evs" ;;
    codex)       say "[codex]       ~/.codex/hooks.json";        install_codex "$evs" ;;
    *)           warn "跳过未知 harness: $h" ;;
  esac
  say ""
done

if [ "$UNINSTALL" = "1" ]; then
  say "已卸载。状态文件仍在各自 hooks-state 目录下，可手动删除。"
else
  say "安装完成。验证方式:"
  dim "  1) 跑一次工具调用或发一条消息"
  dim "  2) 看状态文件的修改时间是否刷新: ls -la ~/.<harness>/hooks-state/"
  dim "  3) 或直接查状态: $PY $CORE --status"
  dim ""
  dim "  自检（推荐先跑）: $PY $CORE --doctor"
  dim "  调阈值: 编辑 $CONF 后立即生效"
fi
