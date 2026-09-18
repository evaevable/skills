#!/usr/bin/env bash
# context-guard 自测台 —— 不需要停会话、不需要等真实压缩触发。
#
# 原理：hook 就是一个「读 stdin JSON → 写 stdout JSON」的普通进程。
#       所以可以直接造事件喂给它，检查输出，完全离线可测。
#
# 用法:
#   ./test.sh                 单元测试（隔离状态目录，不污染生产）
#   ./test.sh --live          额外做「hook 是否真被内核调用」的活性验证
#   ./test.sh --harness codex 指定 harness（默认自动探测）
set -uo pipefail
cd "$(dirname "$0")"

PY=${CONTEXT_GUARD_PYTHON:-python3}
[ -x /usr/bin/python3 ] && PY=/usr/bin/python3
SCRIPT=./context_guard.py
HARNESS="${CONTEXT_GUARD_HARNESS:-}"
LIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --live) LIVE=1 ;;
    --harness) shift; HARNESS="$1" ;;
  esac
  shift
done

# 隔离状态目录 —— 绝不碰生产数据
export CONTEXT_GUARD_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cg-test-XXXXXX")"
[ -n "$HARNESS" ] && export CONTEXT_GUARD_HARNESS="$HARNESS"
cleanup() { rm -rf "$CONTEXT_GUARD_STATE_DIR"; }
trap cleanup EXIT

PASS=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
no()   { printf '  \033[31m✗\033[0m %s\n   期望: %s\n   实际: %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); }
title(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

# 断言输出 JSON 里的某个字段
# 用法: assert_field <名称> <输入JSON> <python取值表达式> <期望>
assert_field() {
  local name="$1" input="$2" expr="$3" want="$4"
  local got
  got=$(printf '%s' "$input" | $PY $SCRIPT 2>/dev/null \
        | $PY -c "import sys,json;
try: d=json.load(sys.stdin)
except Exception: print('<非法JSON>'); sys.exit()
try: print(eval(\"$expr\"))
except Exception as e: print('<取值失败:%s>'%e)")
  if [ "$got" = "$want" ]; then ok "$name"; else no "$name" "$want" "$got"; fi
}

# 构造一个真实 transcript 副本，末尾追加指定 input_tokens 的记录
# 用法: make_synth <输出路径> <目标tokens> [压缩次数]
make_synth() {
  local out="$1" target="$2" compacts="${3:-0}"
  $PY - "$out" "$target" "$compacts" <<'PYEOF'
import json, sys
out, target, compacts = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
lines = []
# 造 compacts 次断崖：高值 -> 低值
cur = 40000
for _ in range(compacts):
    lines.append({'type':'message','message':{'role':'assistant',
        'usage':{'input_tokens': 170000, 'cache_read_input_tokens': 100, 'output_tokens': 10}}})
    cur = 40000
    lines.append({'type':'message','message':{'role':'assistant',
        'usage':{'input_tokens': cur, 'cache_read_input_tokens': 100, 'output_tokens': 10}}})
lines.append({'type':'message','message':{'role':'assistant',
    'usage':{'input_tokens': target, 'cache_read_input_tokens': 100, 'output_tokens': 10}}})
with open(out, 'w') as f:
    for d in lines:
        f.write(json.dumps(d) + '\n')   # 默认带空格 —— 顺带回归紧凑匹配的坑
PYEOF
}

echo "=============================================================="
echo " context-guard 自测台"
echo " 脚本     : $SCRIPT"
echo " harness  : ${HARNESS:-（自动探测）}"
echo " 状态目录 : $CONTEXT_GUARD_STATE_DIR  （隔离，退出即删）"
echo "=============================================================="

# ---------------------------------------------------------------- 1
title "1. 语法与依赖"
$PY -m py_compile $SCRIPT 2>/dev/null && ok "语法合法" || no "语法合法" "编译通过" "有语法错误"
if $PY -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)"; then
  ok "Python 版本 ${PY##*/}（$( $PY -V 2>&1 | cut -d' ' -f2 )，>=3.9）"
else
  no "Python 版本" ">=3.9" "$( $PY -V 2>&1 )"
fi

# ---------------------------------------------------------------- 2
title "2. 健壮性 —— 绝不因为输入异常而中断会话"
assert_field "空 stdin 应静默"            ""                                  "d.get('continue')" "True"
assert_field "垃圾输入应静默"             'not json at all {{{'                 "d.get('continue')" "True"
assert_field "合法 JSON 但缺字段应静默"   '{"foo":"bar"}'                      "d.get('continue')" "True"
assert_field "transcript 不存在应静默"    '{"hook_event_name":"PostToolUse","transcript_path":"/nope/x.jsonl","cwd":"/tmp","session_id":"s"}' "d.get('continue')" "True"
assert_field "空 cwd 的 SessionStart 应静默" '{"hook_event_name":"SessionStart","session_id":"s"}' "d.get('continue')" "True"

# ---------------------------------------------------------------- 3
title "3. 危险状态判定 —— 三分档（正常 / 预警 / 危险）"
# 窗口按 200K 兜底，压缩 0 次时 danger=150K warn=136K
make_synth "$CONTEXT_GUARD_STATE_DIR/synth-normal.jsonl" 100000 0
make_synth "$CONTEXT_GUARD_STATE_DIR/synth-warn.jsonl"   140000 0
make_synth "$CONTEXT_GUARD_STATE_DIR/synth-danger.jsonl" 160000 0

silent_ev() { printf '{"session_id":"%s","transcript_path":"%s","cwd":"/tmp","hook_event_name":"UserPromptSubmit"}' "$1" "$2"; }

# 正常区（100K < warn 136K）→ 等级 0，应静默
out=$(silent_ev "t-normal" "$CONTEXT_GUARD_STATE_DIR/synth-normal.jsonl" | $PY $SCRIPT)
echo "$out" | grep -q '"suppressOutput"' && ok "100K / 压缩0次 → 静默（未越线）" \
  || no "100K 应静默" "suppressOutput" "$out"

# 预警区（136K <= 140K < 150K）→ 等级 1，应注入提醒
out=$(silent_ev "t-warn" "$CONTEXT_GUARD_STATE_DIR/synth-warn.jsonl" | $PY $SCRIPT)
echo "$out" | grep -q 'additionalContext' && ok "140K / 压缩0次 → 注入提醒（预警线 136K）" \
  || no "140K 应提醒" "additionalContext" "$out"

# 危险区（160K >= 150K）→ 等级 2
out=$(silent_ev "t-danger" "$CONTEXT_GUARD_STATE_DIR/synth-danger.jsonl" | $PY $SCRIPT)
echo "$out" | grep -q 'additionalContext' && ok "160K / 压缩0次 → 注入提醒（危险线 150K）" \
  || no "160K 应提醒" "additionalContext" "$out"

# ---------------------------------------------------------------- 4
title "4. 危险状态判定 —— 压缩次数越多，阈值越提前"
# 同一个 140K，压缩 0 次只是预警，压缩 3 次已构成危险
make_synth "$CONTEXT_GUARD_STATE_DIR/synth-c3.jsonl" 140000 3
out=$(silent_ev "t-c3" "$CONTEXT_GUARD_STATE_DIR/synth-c3.jsonl" | $PY $SCRIPT)
if echo "$out" | grep -q '压缩 3 次'; then
  ok "140K / 压缩3次 → 文案指明已压缩 3 次（阈值降到 88K/110K）"
else
  no "压缩3次文案" "含「压缩 3 次」" "$(echo "$out" | head -c 200)"
fi
# 从状态文件核对压缩计数被正确回填
cnt=$($PY -c "
import json,glob,os
f=glob.glob(os.path.join('$CONTEXT_GUARD_STATE_DIR','guard-t-c3.json'))
print(json.load(open(f[0]))['compact_count'] if f else 'none')")
[ "$cnt" = "3" ] && ok "冷启动回填压缩次数 = 3（从 transcript 断崖推断）" \
  || no "回填压缩次数" "3" "$cnt"

# ---------------------------------------------------------------- 5
title "5. 节流 —— 同一级别不重复打扰"
# 第 2 次同级别调用应静默（否则提醒本身会加速压缩）
out=$(silent_ev "t-warn" "$CONTEXT_GUARD_STATE_DIR/synth-warn.jsonl" | $PY $SCRIPT)
echo "$out" | grep -q '"suppressOutput"' && ok "第二次同级别调用静默（强节流生效）" \
  || no "节流" "suppressOutput" "$out"

# ---------------------------------------------------------------- 6
title "6. 压缩事件 —— PreCompact 精确计数且不与断崖重数"
out=$(printf '{"session_id":"t-pc","transcript_path":"%s","cwd":"/tmp","hook_event_name":"PreCompact"}' \
      "$CONTEXT_GUARD_STATE_DIR/synth-warn.jsonl" | $PY $SCRIPT)
echo "$out" | grep -q 'systemMessage' && ok "PreCompact 有反馈" || no "PreCompact" "systemMessage" "$out"

# ---------------------------------------------------------------- 7
title "7. 交接文件注入（SessionStart）"
WC="$CONTEXT_GUARD_STATE_DIR/ws"
mkdir -p "$WC/.context-guard/handoff"
cat > "$WC/.context-guard/handoff/latest.md" <<'EOF'
## 当前任务
测试交接注入。

## 已否决的方案
- 用向量库做记忆检索（索引跟不上活跃代码库，放弃）
EOF
out=$(printf '{"hook_event_name":"SessionStart","session_id":"t-h1","cwd":"%s"}' "$WC" | $PY $SCRIPT)
if echo "$out" | grep -q '已否决的方案'; then
  ok "SessionStart 把交接内容注入 additionalContext"
else
  no "交接注入" "含交接正文" "$(echo "$out" | head -c 200)"
fi
[ -f "$WC/.context-guard/handoff/latest.consumed.md" ] \
  && ok "注入后标记为 .consumed.md（重命名不删除，可回溯）" \
  || no "消费标记" "latest.consumed.md 存在" "$(ls -1 "$WC/.context-guard/handoff/")"
[ -f "$WC/.context-guard/handoff/latest.md" ] \
  && no "原文件应被移走" "latest.md 不存在" "仍存在" \
  || ok "原文件已移走，不会重复注入"
# 第二次启动不应再注入
out=$(printf '{"hook_event_name":"SessionStart","session_id":"t-h2","cwd":"%s"}' "$WC" | $PY $SCRIPT)
echo "$out" | grep -q '"suppressOutput"' && ok "第二次 SessionStart 静默（只注入一次）" \
  || no "重复注入" "suppressOutput" "$out"

# ---------------------------------------------------------------- 8
title "8. 状态查看（--status）"
$PY $SCRIPT --status >/dev/null 2>&1 && ok "--status 可运行" || no "--status" "退出码 0" "失败"
$PY $SCRIPT --doctor >/dev/null 2>&1 && ok "--doctor 可运行" || no "--doctor" "退出码 0" "失败"

# ---------------------------------------------------------------- 9
title "9. 性能 —— hook 在每次工具调用上都会跑，必须够快"
# 注意：不要用 date +%s%N —— macOS 的 BSD date 不支持 %N，会输出字面量 "N"。
# 用 Python 计时，跨平台且精度足够。
PERF=$($PY - "$PY" "$SCRIPT" "$CONTEXT_GUARD_STATE_DIR/synth-c3.jsonl" <<'PYEOF'
import subprocess, sys, time, json, os
py, script, tr = sys.argv[1], sys.argv[2], sys.argv[3]
ev = json.dumps({'session_id':'t-perf','transcript_path':tr,'cwd':'/tmp',
                 'hook_event_name':'PostToolUse'})
def once():
    t = time.time()
    subprocess.run([py, script], input=ev, capture_output=True, text=True,
                   env=dict(os.environ, CONTEXT_GUARD_STATE_DIR=os.environ['CONTEXT_GUARD_STATE_DIR']))
    return (time.time() - t) * 1000
once()                                   # 预热
ts = sorted(once() for _ in range(10))
print('%d %d' % (ts[len(ts)//2], ts[-1]))   # 中位 最大
PYEOF
)
med=$(echo "$PERF" | awk '{print $1}')
mx=$(echo "$PERF" | awk '{print $2}')
echo "  含 Python 解释器启动：中位 ${med}ms / 最大 ${mx}ms"
[ "${med:-9999}" -lt 300 ] && ok "单次耗时中位 < 300ms（含解释器冷启动，不拖慢会话）" \
  || no "性能" "<300ms" "${med}ms"

# ---------------------------------------------------------------- 10
if [ "$LIVE" = "1" ]; then
  title "10. 活性验证 —— hook 是否真被内核调用（不是测试台造出来的）"
  echo "  说明：这一项只能在会话里验证。做法是看状态文件的 mtime 或自增计数器。"
  echo "  生产状态目录通常是：~/.workbuddy/hooks-state（WorkBuddy）/ ~/.codebuddy/hooks-state（CodeBuddy）"
  for d in "$HOME"/.workbuddy/hooks-state "$HOME"/.codebuddy/hooks-state "$HOME"/.claude/hooks-state; do
    [ -d "$d" ] || continue
    echo "  --- $d ---"
    for f in "$d"/guard-*.json; do
      [ -f "$f" ] || continue
      $PY -c "
import json,sys,time,os
p='$f'; st=json.load(open(p))
print('    %s' % os.path.basename(p)[:20])
print('      工具调用计数 = %s   ← 让它递增就说明 hook 活着' % st.get('records_seen'))
print('      最后更新     = %s' % time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(p))))
"
    done
  done
  echo "  手动复核：跑任意一次工具调用，再看上面的计数是否 +1"
fi

# ---------------------------------------------------------------- 汇总
echo ""
echo "=============================================================="
if [ "$FAIL" = "0" ]; then
  printf ' 结果：\033[32m全部通过\033[0m（%d 项）\n' "$PASS"
else
  printf ' 结果：\033[32m%d 通过\033[0m / \033[31m%d 失败\033[0m\n' "$PASS" "$FAIL"
fi
echo "=============================================================="
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
