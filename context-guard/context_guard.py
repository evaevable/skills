#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context_guard.py - 跨 harness 的会话上下文守卫

支持: Claude Code / CodeBuddy Code / WorkBuddy / Codex CLI
许可: MIT

它解决什么问题
--------------
所有主流编码 agent 在上下文变长时都会做自动压缩(auto-compact)，把早期对话
换成摘要。压缩是**有损且不可逆**的，而且叠加多次后会出现：

  - 记错前面已经定下的结论
  - 重复提出已经被否决的方案
  - 遗忘早期设定的约束

这个守卫在越线时向 agent 注入一条提醒，让它在回复末尾顺带告诉你
「该收尾并新开会话了」，并在新会话启动时自动加载上一会话的交接文件。

为什么必须是 hook 而不是 skill
------------------------------
skill 是被动加载的指令文本：没有运行时、不挂在任何生命周期事件上，
你不调用它就永远不动。只有 hook 会被内核在每个事件上主动调用。

通用化是怎么做到的（重要）
--------------------------
四家 harness 的 hook 协议高度同源（字段名 session_id / transcript_path /
cwd / hook_event_name 完全一致，输出都认 additionalContext / continue），
真正不同的只有三件事，全部收敛在 ADAPTERS 里：

  1. transcript 在哪、长什么结构
  2. 「当前上下文占用了多少 token」怎么算  ← 最大的坑，见下
  3. 上下文窗口多大

### 最大的坑：两家的 token 语义正好相反

  Claude 系 (claude-code / codebuddy / workbuddy):
      input_tokens 已经**包含**了缓存部分，cache_read_input_tokens 是它的子集。
      实测同一行: input=114,017  cache_read=108,800（比值约 95%）
      → 占用 = input_tokens
      → 若写成 input + cache_read 会直接翻倍

  Codex:
      last_token_usage.input_tokens 是**未缓存**输入，
      cached_input_tokens 是缓存输入，两者并列互斥。
      → 占用 = input_tokens + cached_input_tokens

两者相加/不相加正好相反，这是通用化最容易做错的地方。

设计约束
--------
  1. 极快     PostToolUse 每次工具调用都触发，只读 transcript 尾部。
  2. 强节流   注入提醒本身占用上下文、会加速压缩。只在「级别上升」时提醒一次。
  3. 永不阻塞 任何异常都吞掉并返回 continue:true，绝不阻断会话。
  4. 零依赖   只用标准库，兼容 macOS 自带 /usr/bin/python3 (3.9)。
  5. 自适应   阈值按「窗口百分比」而非绝对值，否则换模型/换 harness 就失效。

用法
----
  hook 模式   cat event.json | python3 context_guard.py
  查看状态    python3 context_guard.py --status [session_id]
  自检        python3 context_guard.py --doctor
  重置状态    python3 context_guard.py --reset <session_id>
"""

import sys
import os
import json
import glob
import time
import ast
import re
import tempfile
import subprocess

HOME = os.path.expanduser('~')

VERSION = '1.0.0'

# ------------------------------------------------------------------ 配置
# 阈值用「占上下文窗口的百分比」表达，不写绝对值 —— 各家窗口差异巨大
# （实测 Claude 系 200K，Codex 365K），写死绝对值换 harness 立刻失效。
#
# 校准依据（Claude 系 200K 窗口实测，2026-09-18）：
#   auto-compact 实际触发点稳定在 150K~165K = 窗口的 75%~82%，
#   压缩后回落到 43K~62K。所以 danger 定 0.75 刚好压在触发点之下，
#   warn 定 0.68 留出可操作的提前量。
#
# 每项: (最小压缩次数, warn 比例, danger 比例)
# 压缩次数越多阈值越提前 —— 摘要已叠加多层，越晚提醒越亏。
DEFAULT_RATIOS = [
    (0, 0.68, 0.75),
    (1, 0.60, 0.70),
    (2, 0.52, 0.62),
    (3, 0.44, 0.55),
]

# 各家上下文窗口的兜底值（读不到时用）
FALLBACK_WINDOW = {
    'claude-code': 200000,
    'codebuddy': 200000,
    'workbuddy': 200000,
    'codex': 272000,
}

TAIL_BYTES = 512 * 1024      # 只读 transcript 尾部 512KB
MAX_SAMPLES = 8              # 保留最近 8 个用量采样用于估算增速
CLIFF_MIN = 80000            # 断崖检测的最低基准，低于此值不判压缩
CLIFF_RATIO = 0.75           # 用量跌破上一次的 75% 视为压缩


def config_path():
    return os.environ.get('CONTEXT_GUARD_CONFIG') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'context-guard.json')


def load_config():
    p = config_path()
    if os.path.isfile(p):
        try:
            with open(p) as f:
                c = json.load(f)
            if isinstance(c, dict):
                return c
        except Exception:
            pass
    return {}


def is_enabled():
    return load_config().get('enabled', True) is not False


def state_dir():
    """状态目录可通过配置或环境变量覆盖，默认放 harness 配置目录下。"""
    cfg = load_config()
    d = (os.environ.get('CONTEXT_GUARD_STATE_DIR')
         or cfg.get('stateDir'))
    if d:
        return os.path.expanduser(d)
    h = current_harness_name()
    root = ADAPTERS.get(h, ADAPTERS['claude-code'])['root']
    return os.path.join(HOME, root, 'hooks-state')


LOG_PATH = lambda: os.path.join(state_dir(), 'context-guard.log')  # noqa: E731


def log(msg):
    if not os.environ.get('CONTEXT_GUARD_DEBUG'):
        return
    try:
        _ensure_dir()
        with open(LOG_PATH(), 'a') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def _ensure_dir():
    d = state_dir()
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            pass


def out(payload):
    """唯一的 stdout 出口，保证永远是合法 JSON。"""
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        pass


def silent():
    out({'continue': True, 'suppressOutput': True})


def read_event():
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    if not raw:
        return {}
    for enc in ('utf-8', 'utf-8-sig', 'gbk'):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        txt = raw.decode('utf-8', 'replace')
    txt = txt.strip().lstrip('\ufeff')
    if not txt:
        return {}
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        log('event parse failed: %r' % txt[:200])
        return {}


def _int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


# ============================================================ 适配器
#
# 每个适配器是一个 dict，提供:
#   root            配置目录名（~ 下），用于定位 transcript 与状态目录
#   window(ev, path)          -> int|None   上下文窗口大小
#   occupancy(path)           -> int|None   当前上下文占用的 token 数
#   history(path)             -> dict       冷启动回溯: compacts/peak/last/records
#   is_record(line)           -> bool       这行是否是一次「模型请求记录」（用于计数工具调用）
#
# 定位 transcript 的逻辑三家同源（projects/<slug>/<sid>.jsonl），
# 由 locate_jsonl_transcript 统一实现；Codex 走独立分支。


def _slug_for(cwd):
    if not cwd:
        return None
    return cwd.strip('/').replace('/', '-')


def _locate_jsonl_transcript(root, cwd, session_id):
    """Claude 系通用定位：~/.<root>/projects/<slug>/<session_id>.jsonl

    按 cwd 找不到时退化为「全局最新」—— 因为手动执行 --status / --doctor 时
    拿不到正确的 cwd，而探测 harness 也需要一个公平的比较基准。
    """
    d = os.path.join(HOME, root, 'projects')
    slug = _slug_for(cwd)
    if slug:
        sd = os.path.join(d, slug)
        if session_id:
            p = os.path.join(sd, session_id + '.jsonl')
            if os.path.isfile(p):
                return p
        try:
            files = glob.glob(os.path.join(sd, '*.jsonl'))
        except Exception:
            files = []
        if files:
            try:
                return max(files, key=os.path.getmtime)
            except Exception:
                return files[0]
    # 全局兜底
    try:
        files = glob.glob(os.path.join(d, '*', '*.jsonl'))
    except Exception:
        files = []
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except Exception:
        return files[0]


_USAGE_RE = re.compile(rb"(?<![_A-Za-z])input_tokens['\"]?\s*:\s*(\d+)")

# 匹配 type 字段时必须容忍冒号两侧的空格：紧凑序列化是 '"type":"message"'，
# 而 json.dumps 默认格式是 '"type": "message"'。只认一种，换 harness 或换写入方
# 之后会静默失效（筛选全部落空 → 用量读不到 → 守卫再无反应）。
_TYPE_MSG_RE = re.compile(rb'"type"\s*:\s*"message"')
_FUNCALL_RE = re.compile(rb'"type"\s*:\s*"function_call"')


def _is_usage_line(raw):
    """这行是否可能带 usage（快速排除 reasoning 等噪声行，避免大行 json.loads）。"""
    return bool(_TYPE_MSG_RE.search(raw) or _FUNCALL_RE.search(raw))


def _parse_claude_usage_line(raw):
    """从一行 JSON 里取出 usage dict（Claude 系）。

    message 字段可能是 dict，也可能是 str（取决于写入方），两种都要认。
    """
    try:
        rec = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return None
    msg = rec.get('message')
    usage = None
    if isinstance(msg, dict):
        usage = msg.get('usage')
    elif isinstance(msg, str):
        try:
            parsed = ast.literal_eval(msg)
            if isinstance(parsed, dict):
                usage = parsed.get('usage')
        except Exception:
            usage = None
    return usage if isinstance(usage, dict) else None


def _claude_occupancy(path):
    """Claude 系：占用 = input_tokens。

    input_tokens 已包含缓存部分（cache_read_input_tokens 是它的子集，
    实测比值约 95%），所以不能再加 cache_read，否则直接翻倍。
    """
    usage = _read_last_usage_claude(path)
    if not usage:
        return None
    cur = _int(usage.get('input_tokens'))
    return cur if cur > 0 else None


def _read_last_usage_claude(path):
    """只 seek 到 transcript 尾部，避免每次 hook 都扫几 MB 历史。"""
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - TAIL_BYTES)
            f.seek(start)
            data = f.read()
    except Exception:
        return None

    lines = data.split(b'\n')
    if start > 0 and lines:
        lines = lines[1:]          # 首行多半被截断，丢掉

    for raw in reversed(lines):
        if b'input_tokens' not in raw:
            continue
        # usage 只出现在这两种 type 上，快速排除 reasoning 等噪声
        if not _is_usage_line(raw):
            continue
        usage = _parse_claude_usage_line(raw)
        if usage and 'input_tokens' in usage:
            return usage
    return None


def _claude_history(path, max_bytes=8 * 1024 * 1024):
    """冷启动回溯。

    不做 json.loads（有些行几百 KB，全量解析太慢），只用正则抓 input_tokens，
    靠「用量断崖」推断压缩。

    注意正则必须带 (?<![_A-Za-z]) 词边界：usage 里同时有 input_tokens 和
    cache_read_input_tokens，后者是前者的超串，不排除会把 cache 读数当成
    input 读数，进而把压缩次数乘二。
    """
    compacts = 0
    peak = 0
    tool_calls = 0
    prev = 0
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢掉被截断的半行
            for raw in f:
                if _FUNCALL_RE.search(raw):
                    tool_calls += 1
                if b'input_tokens' not in raw:
                    continue
                if not _is_usage_line(raw):
                    continue
                hits = _USAGE_RE.findall(raw)
                if not hits:
                    continue
                cur = int(hits[-1])
                if cur <= 0:
                    continue
                if prev > CLIFF_MIN and cur < prev * CLIFF_RATIO:
                    compacts += 1
                if cur > peak:
                    peak = cur
                prev = cur
    except Exception as e:
        log('claude history failed: %s' % e)
    # prev 是「最后一条有效用量」，不是峰值。回填时必须用它初始化 last_input ——
    # 用峰值会让当前值（已从峰值回落）看起来像一次新的断崖，凭空多算一次压缩。
    return {'compacts': compacts, 'peak': peak, 'last': prev, 'records': tool_calls}


# ------------------------------------------------------------ Codex

def _locate_codex_transcript(session_id):
    """Codex 把 rollout 按日期分区存放，文件名含启动时间而非 session_id。"""
    root = os.path.join(HOME, 'codex', 'sessions')
    try:
        files = glob.glob(os.path.join(root, '*', '*', '*', '*.jsonl'))
    except Exception:
        files = []
    if not files:
        return None
    if session_id:
        for p in files:
            if session_id in os.path.basename(p):
                return p
    try:
        return max(files, key=os.path.getmtime)
    except Exception:
        return files[0]


def _read_last_token_count_codex(path):
    """读最后一条 token_count 事件。"""
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - TAIL_BYTES)
            f.seek(start)
            data = f.read()
    except Exception:
        return None

    lines = data.split(b'\n')
    if start > 0 and lines:
        lines = lines[1:]

    for raw in reversed(lines):
        if b'"token_count"' not in raw:
            continue
        try:
            rec = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            continue
        payload = rec.get('payload')
        if not isinstance(payload, dict) or payload.get('type') != 'token_count':
            continue
        info = payload.get('info')
        if isinstance(info, dict):
            return info
    return None


def _codex_occupancy(path):
    """Codex：占用 = input_tokens + cached_input_tokens。

    与 Claude 系相反，Codex 的 last_token_usage.input_tokens 只是「未缓存」
    的那部分，缓存部分单列在 cached_input_tokens 里，两者互斥，必须相加。
    """
    info = _read_last_token_count_codex(path)
    if not info:
        return None
    last = info.get('last_token_usage')
    if not isinstance(last, dict):
        # 极早期或异常记录可能只有累计值，退回用它
        total = info.get('total_token_usage')
        if not isinstance(total, dict):
            return None
        last = total
    cur = _int(last.get('input_tokens')) + _int(last.get('cached_input_tokens'))
    return cur if cur > 0 else None


def _codex_context_window(path):
    """优先从 transcript 里读真实窗口，比兜底猜测可靠得多。"""
    info = _read_last_token_count_codex(path)
    if info:
        w = _int(info.get('model_context_window'))
        if w > 0:
            return w
    # session_meta / task_started 里也带
    try:
        with open(path, 'r', errors='replace') as f:
            for i, line in enumerate(f):
                if i > 40:
                    break
                if 'context_window' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                payload = rec.get('payload')
                if isinstance(payload, dict):
                    w = _int(payload.get('context_window')
                             or payload.get('model_context_window'))
                    if w > 0:
                        return w
    except Exception:
        pass
    return None


def _codex_history(path, max_bytes=16 * 1024 * 1024):
    """Codex 侧回溯：token_count 是「单次请求」快照，不是累计值。

    累计值在 total_token_usage 里，但我们要的是「每次请求的上下文占用」，
    所以取 last_token_usage 的 input+cached。
    """
    compacts = 0
    peak = 0
    tool_calls = 0
    prev = 0
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            for raw in f:
                if b'"function_call"' in raw or b'"command_execution"' in raw:
                    tool_calls += 1
                if b'"token_count"' not in raw:
                    continue
                try:
                    rec = json.loads(raw.decode('utf-8', 'replace'))
                except Exception:
                    continue
                payload = rec.get('payload') or {}
                info = payload.get('info') or {}
                last = info.get('last_token_usage')
                if not isinstance(last, dict):
                    continue
                cur = _int(last.get('input_tokens')) + _int(last.get('cached_input_tokens'))
                if cur <= 0:
                    continue
                if prev > CLIFF_MIN and cur < prev * CLIFF_RATIO:
                    compacts += 1
                if cur > peak:
                    peak = cur
                prev = cur
    except Exception as e:
        log('codex history failed: %s' % e)
    return {'compacts': compacts, 'peak': peak, 'last': prev, 'records': tool_calls}


ADAPTERS = {
    'claude-code': {
        'label': 'Claude Code',
        'root': '.claude',
        'window': lambda ev, p: None,
        'occupancy': _claude_occupancy,
        'history': _claude_history,
        'locate': lambda ev, cwd, sid: (
            ev.get('transcript_path') or _locate_jsonl_transcript('.claude', cwd, sid)),
    },
    'codebuddy': {
        'label': 'CodeBuddy Code',
        'root': '.codebuddy',
        'window': lambda ev, p: None,
        'occupancy': _claude_occupancy,
        'history': _claude_history,
        'locate': lambda ev, cwd, sid: (
            ev.get('transcript_path') or _locate_jsonl_transcript('.codebuddy', cwd, sid)),
    },
    'workbuddy': {
        'label': 'WorkBuddy',
        'root': '.workbuddy',
        'window': lambda ev, p: None,
        'occupancy': _claude_occupancy,
        'history': _claude_history,
        'locate': lambda ev, cwd, sid: (
            ev.get('transcript_path') or _locate_jsonl_transcript('.workbuddy', cwd, sid)),
    },
    'codex': {
        'label': 'Codex CLI',
        'root': '.codex',
        'window': lambda ev, p: _codex_context_window(p) if p else None,
        'occupancy': _codex_occupancy,
        'history': _codex_history,
        'locate': lambda ev, cwd, sid: (
            ev.get('transcript_path') or _locate_codex_transcript(sid)),
    },
}


def detect_harness(ev):
    """判定当前跑在哪家 harness 上。

    优先级:
      1. 环境变量（安装器会把 CONTEXT_GUARD_HARNESS 写进 hook command，最可靠）
      2. 事件里的 transcript_path 路径特征
      3. 「哪家的 transcript 最新」—— 守卫只可能跑在有活跃会话的那家上，
         这比「哪个配置目录存在」公平得多（多数人同时装了多家）
      4. 目录存在性兜底
    """
    h = os.environ.get('CONTEXT_GUARD_HARNESS')
    if h in ADAPTERS:
        return h

    tp = (ev.get('transcript_path') or ev.get('transcriptPath') or '')
    if tp:
        base = os.path.basename(tp)
        if 'rollout-' in base or os.sep + '.codex' + os.sep in tp:
            return 'codex'
        for name, a in ADAPTERS.items():
            if os.sep + a['root'] + os.sep in tp:
                return name

    # 比较各家最新 transcript 的修改时间
    best = None
    for name, a in ADAPTERS.items():
        try:
            p = a['locate']({}, None, '')
        except Exception:
            continue
        if p and os.path.isfile(p):
            try:
                m = os.path.getmtime(p)
            except Exception:
                continue
            if best is None or m > best[0]:
                best = (m, name)
    if best:
        return best[1]

    for name in ('claude-code', 'codebuddy', 'workbuddy', 'codex'):
        if os.path.isdir(os.path.join(HOME, ADAPTERS[name]['root'])):
            return name
    return 'claude-code'


def current_harness_name():
    return detect_harness({})


# ------------------------------------------------------------ 窗口与阈值

def resolve_window(ev, tpath, harness):
    cfg = load_config()
    w = _int(cfg.get('contextWindow'))
    if w <= 0:
        w = _int(ev.get('context_window'))
    if w <= 0 and tpath:
        try:
            w = _int(ADAPTERS[harness]['window'](ev, tpath)) or 0
        except Exception:
            w = 0
    if w <= 0:
        w = FALLBACK_WINDOW.get(harness, 200000)
    return w


def thresholds(compact_count, window):
    """返回 (warn, danger)，按窗口比例算 —— 换模型/换 harness 自动适配。"""
    cfg = load_config()
    tiers = None
    raw = cfg.get('ratios')
    if isinstance(raw, list):
        parsed = []
        for item in raw:
            if isinstance(item, dict) and 'warn' in item and 'danger' in item:
                parsed.append((_int(item.get('minCompacts')), 
                               float(item['warn']), float(item['danger'])))
        if parsed:
            tiers = sorted(parsed, key=lambda t: t[0])
    if not tiers:
        tiers = DEFAULT_RATIOS

    chosen = tiers[0]
    for t in tiers:
        if compact_count >= t[0]:
            chosen = t

    # 绝对覆盖（老配置兼容 / 精确调参用）
    if 'warn' in cfg and 'danger' in cfg and not raw:
        return _int(cfg['warn']), _int(cfg['danger'])

    return int(window * chosen[1]), int(window * chosen[2])


# ------------------------------------------------------------ 状态

def state_path(session_id):
    safe = ''.join(c if (c.isalnum() or c in '-_') else '_'
                   for c in (session_id or 'default'))
    return os.path.join(state_dir(), 'guard-%s.json' % safe)


def load_state(session_id, tpath, harness):
    p = state_path(session_id)
    if os.path.isfile(p):
        try:
            with open(p) as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except Exception:
            pass

    st = {
        'session_id': session_id,
        'harness': harness,
        'compact_count': 0,
        'peak_input': 0,
        'last_input': 0,
        'last_level': 0,
        'notify_count': 0,
        'last_notify_ts': 0,
        'records_seen': 0,
        'created_at': time.time(),
        'backfilled': False,
    }

    # 冷启动：这个会话可能在不装守卫的时候已经压缩过好几次了。
    # 不回填的话阈值会用到最宽松那档，文案还会说「尚未压缩过」——都是错的。
    if tpath and os.path.isfile(tpath):
        try:
            h = ADAPTERS[harness]['history'](tpath)
        except Exception as e:
            log('history failed: %s' % e)
            h = None
        if h:
            st['compact_count'] = h['compacts']
            st['peak_input'] = h['peak']
            st['last_input'] = h['last']
            st['records_seen'] = h['records']
            st['backfilled'] = True
            st['last_level'] = 0          # 回填后允许首次提醒
            log('backfilled harness=%s compacts=%s peak=%s last=%s records=%s'
                % (harness, h['compacts'], h['peak'], h['last'], h['records']))
    return st


def save_state(session_id, st):
    """原子写，避免并发 hook 写坏文件。"""
    _ensure_dir()
    p = state_path(session_id)
    st['updated_at'] = time.time()
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir(), prefix='.guard-')
        with os.fdopen(fd, 'w') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log('save_state failed: %s' % e)


# ------------------------------------------------------------ 提醒文案

def estimate_remaining(st, cur, danger):
    """用采样点粗估还有多少条记录触及压缩阈值。仅作参考，宁可不说。"""
    samples = st.get('samples') or []
    if len(samples) < 3:
        return None
    (l0, t0), (l1, t1) = samples[-3], samples[-1]
    if l1 <= l0 or t1 <= t0:
        return None
    per_record = float(t1 - t0) / (l1 - l0)
    if per_record <= 0:
        return None
    remain = (danger - cur) / per_record
    if remain <= 0:
        return None
    if remain < 15:
        return '十几条记录（约 1-2 轮工具调用）'
    if remain < 60:
        return '约 %d 条记录' % int(remain)
    return None


def _pct_txt(pct):
    """百分比措辞。超过 100% 必须说「已超过」，否则「142%」会被读成「还没到」。"""
    if pct >= 100:
        return '已超过预估触发点 %.0f%%' % (pct - 100)
    return '约为触发点的 %.0f%%' % pct


def build_message(level, st, cur, warn, danger, window, harness):
    """写给 agent 看的指令。要求 agent 转达用户，同时限制篇幅。"""
    n = _int(st.get('compact_count'))
    peak = _int(st.get('peak_input'))
    calls = _int(st.get('records_seen'))
    est = estimate_remaining(st, cur, danger)
    pct = 100.0 * cur / max(1, danger)

    if n == 0:
        head = '本会话上下文 %s tokens（窗口 %s 的 %.0f%%），%s，尚未压缩过。' % (
            f'{cur:,}', f'{window:,}', 100.0 * cur / max(1, window), _pct_txt(pct))
        why = '一旦触发 auto-compact，之前对话的原始细节会被摘要替代，不可逆。'
        ask = '若接下来要开始一个【新任务】，现在开新会话成本最低。'
    elif n == 1:
        head = '本会话已压缩 1 次，上下文又升到 %s tokens（%s）。' % (
            f'{cur:,}', _pct_txt(pct))
        why = '每压缩一次，早期原始细节就被摘要覆盖一层；到第 2 次压缩后开始出现结论漂移。'
        ask = '建议当前任务收尾后就新开会话，别在同一会话里叠新任务。'
    else:
        head = '本会话已压缩 %d 次，上下文又升到 %s tokens（历史峰值 %s，%s）。' % (
            n, f'{cur:,}', f'{peak:,}', _pct_txt(pct))
        why = ('摘要已叠加 %d 层，实测表现为：记错先前结论、重复已被否决的方案、'
               '遗忘早期约束。' % n)
        ask = '强烈建议立刻收尾并新开会话；需要保住当前进展就用交接流程生成交接文件。'

    lines = ['[会话守卫 context-guard · %s] %s' % (ADAPTERS[harness]['label'], head)]
    if calls >= 50:
        lines.append('本会话已执行约 %d 次工具调用。' % calls)
    lines.append('风险：%s' % why)
    if est:
        lines.append('余量：按当前增速约再 %s 后触及压缩点。' % est)
    lines.append('')
    lines.append('**给助手的要求**：在本次回复末尾用 1-2 句话提醒用户以上情况，'
                 '不要展开长篇解释、不要单独起一节。%s' % ask)

    if level >= 2:
        lines.append('（本会话已越过危险线，提醒时请明确给出「现在就该新开」的判断，'
                     '不要用「可以考虑」这类模糊措辞。）')
    return '\n'.join(lines)


# ------------------------------------------------------------ 会话交接

HANDOFF_REL = os.path.join('.context-guard', 'handoff', 'latest.md')
# 兼容老路径（WorkBuddy 首版用的是 .workbuddy/handoff/latest.md）
HANDOFF_LEGACY = [
    os.path.join('.workbuddy', 'handoff', 'latest.md'),
    os.path.join('.codebuddy', 'handoff', 'latest.md'),
]


def _handoff_candidates(cwd):
    out_ = [os.path.join(cwd, HANDOFF_REL)]
    for rel in HANDOFF_LEGACY:
        out_.append(os.path.join(cwd, rel))
    return out_


def handle_session_start(cwd, harness):
    """新会话启动时，把上一会话留下的交接文件注入上下文，然后标记为已消费。

    这是「提醒该新开会话」的闭环另一半：只提醒不交接，用户新开会话等于
    把之前所有进展清零，那提醒就成了负收益。
    """
    if not cwd:
        return silent()
    p = None
    for cand in _handoff_candidates(cwd):
        if os.path.isfile(cand):
            p = cand
            break
    if not p:
        return silent()
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            body = f.read().strip()
    except Exception:
        return silent()
    if not body:
        return silent()
    if len(body) > 12000:
        body = body[:12000] + '\n\n[...交接文件过长已截断，完整内容见 %s]' % p
    # 消费掉：重命名而非删除，内容随时可回溯
    consumed = p[:-3] + '.consumed.md' if p.endswith('.md') else p + '.consumed'
    try:
        os.replace(p, consumed)
    except Exception as e:
        log('handoff rename failed: %s' % e)
    log('SessionStart: injected handoff %s (%d chars)' % (p, len(body)))
    return out({
        'continue': True,
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': '# 上一会话交接内容（由 context-guard 注入）\n\n' + body,
        },
        'systemMessage': '已加载上一会话交接文件',
    })


# ------------------------------------------------------------ 主流程

def handle_event(ev):
    if not is_enabled():
        return silent()

    harness = detect_harness(ev)
    a = ADAPTERS[harness]
    event_name = (ev.get('hook_event_name') or ev.get('hookEventName') or '')
    session_id = (ev.get('session_id') or ev.get('conversationId')
                  or ev.get('sessionId') or '')
    cwd = ev.get('cwd') or os.getcwd()

    if event_name == 'SessionStart':
        return handle_session_start(cwd, harness)

    tpath = ev.get('transcript_path') or ev.get('transcriptPath')
    if not tpath:
        try:
            tpath = a['locate'](ev, cwd, session_id)
        except Exception:
            tpath = None
    if not tpath or not os.path.isfile(tpath):
        log('no transcript; harness=%s event=%s cwd=%s sid=%s'
            % (harness, event_name, cwd, session_id))
        return silent()

    if not session_id:
        session_id = os.path.basename(tpath).rsplit('.', 1)[0]

    st = load_state(session_id, tpath, harness)

    # ---- PreCompact / PostCompact：最硬的压缩信号，直接计数，不靠推断
    if event_name in ('PreCompact', 'PostCompact'):
        st['compact_count'] = _int(st.get('compact_count')) + 1
        st['last_level'] = 0        # 压缩把级别打回 0，允许下个周期重新提醒
        # 关键：告诉后续的断崖检测「这次压缩已经数过了」。
        # 压缩前读到的是高值、压缩后是低值，断崖检测会把同一次压缩再数一遍，
        # 导致压缩次数双倍。这个标记让紧接着的那次断崖检测跳过。
        st['expect_cliff'] = True
        save_state(session_id, st)
        log('%s -> count=%s trigger=%s'
            % (event_name, st['compact_count'], ev.get('trigger')))
        out({
            'continue': True,
            'systemMessage': '上下文守卫：本会话第 %d 次压缩发生，建议收尾后新开会话。'
                             % st['compact_count'],
        })
        return

    # ---- 读当前上下文占用
    try:
        cur = a['occupancy'](tpath)
    except Exception as e:
        log('occupancy failed: %s' % e)
        cur = None
    if not cur:
        return silent()

    # ---- 断崖检测（兜底：万一 PreCompact 没挂上或事件丢失）
    last = _int(st.get('last_input'))
    if st.get('expect_cliff'):
        # 上一次 PreCompact/PostCompact 已经精确计过数了，这次回落不再重复计
        st.pop('expect_cliff', None)
        log('cliff skipped (already counted at compact event): %s -> %s' % (last, cur))
    elif last > CLIFF_MIN and cur < last * CLIFF_RATIO:
        st['compact_count'] = _int(st.get('compact_count')) + 1
        st['last_level'] = 0
        log('cliff detected: %s -> %s, count=%s'
            % (last, cur, st['compact_count']))

    # ---- 采样（只在值变化时追加，避免同轮重复）
    if cur != last:
        samples = st.get('samples') or []
        if not samples or samples[-1][1] != cur:
            samples.append([_count_records(tpath), cur])
            st['samples'] = samples[-MAX_SAMPLES:]

    st['last_input'] = cur
    st['peak_input'] = max(_int(st.get('peak_input')), cur)
    if event_name == 'PostToolUse':
        st['records_seen'] = _int(st.get('records_seen')) + 1

    # ---- 判级 + 节流（只在级别上升时提醒）
    n = _int(st.get('compact_count'))
    window = resolve_window(ev, tpath, harness)
    warn, danger = thresholds(n, window)
    level = 0
    if cur >= danger:
        level = 2
    elif cur >= warn:
        level = 1

    last_level = _int(st.get('last_level'))
    notify = level > last_level and level > 0
    st['last_level'] = level

    if notify:
        st['notify_count'] = _int(st.get('notify_count')) + 1
        st['last_notify_ts'] = time.time()
        save_state(session_id, st)
        msg = build_message(level, st, cur, warn, danger, window, harness)
        log('notify harness=%s level=%s cur=%s/%s count=%s'
            % (harness, level, cur, danger, n))
        out({
            'continue': True,
            'hookSpecificOutput': {
                'hookEventName': event_name or 'UserPromptSubmit',
                'additionalContext': msg,
            },
            'systemMessage': '上下文守卫：%s tokens（窗口 %s 的 %.0f%%，已压缩 %d 次）'
                             % (f'{cur:,}', f'{window:,}',
                                100.0 * cur / max(1, window), n),
        })
        return

    save_state(session_id, st)
    return silent()


def _count_records(path):
    """粗略统计 transcript 行数，用于监控「调用太多了」这个信号。"""
    try:
        with open(path, 'rb') as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


# ------------------------------------------------------------ CLI

def print_status(session_id=None):
    _ensure_dir()
    if session_id:
        files = [state_path(session_id)]
    else:
        files = sorted(glob.glob(os.path.join(state_dir(), 'guard-*.json')),
                       key=os.path.getmtime, reverse=True)[:5]
    print('harness   : %s' % ADAPTERS[current_harness_name()]['label'])
    print('状态目录  : %s' % state_dir())
    if not files:
        print('(没有守卫状态记录 —— 说明 hook 还没被触发过)')
        return
    for p in files:
        if not os.path.isfile(p):
            continue
        try:
            with open(p) as f:
                st = json.load(f)
        except Exception:
            continue
        n = _int(st.get('compact_count'))
        harness = st.get('harness') or current_harness_name()
        window = resolve_window({}, None, harness)
        warn, danger = thresholds(n, window)
        cur = _int(st.get('last_input'))
        print('=' * 62)
        print('会话      : %s' % st.get('session_id'))
        print('harness   : %s' % ADAPTERS.get(harness, {}).get('label', harness))
        print('当前上下文: %s tokens（窗口 %s 的 %.0f%%）'
              % (f'{cur:,}', f'{window:,}', 100.0 * cur / max(1, window)))
        print('历史峰值  : %s tokens' % f'{_int(st.get("peak_input")):,}')
        print('压缩次数  : %d' % n)
        print('当前阈值  : 预警 %s / 危险 %s' % (f'{warn:,}', f'{danger:,}'))
        print('已提醒    : %d 次' % _int(st.get('notify_count')))
        if st.get('backfilled'):
            print('冷启动回填: 是（已回溯历史 transcript）')
        print('工具调用  : 约 %d 次' % _int(st.get('records_seen')))
        if cur >= danger:
            print('状态      : 危险 —— 已越过危险线')
        elif cur >= warn:
            print('状态      : 预警 —— 越过预警线')
        else:
            print('状态      : 正常')
        print('更新时间  : %s' % time.strftime('%Y-%m-%d %H:%M:%S',
                                             time.localtime(st.get('updated_at', 0))))


def doctor():
    """自检：探测环境、跑一遍真实事件、报告每一环是否可用。"""
    print('context-guard %s 自检' % VERSION)
    print('=' * 62)
    print('python    : %s (%s)' % (sys.version.split()[0], sys.executable))
    print('配置文件  : %s %s' % (config_path(),
                                '存在' if os.path.isfile(config_path()) else '不存在(用默认)'))
    print('状态目录  : %s' % state_dir())
    print()
    print('探测到的 harness 配置目录:')
    for name, a in ADAPTERS.items():
        d = os.path.join(HOME, a['root'])
        mark = '✓' if os.path.isdir(d) else '·'
        print('  %s %-14s %s' % (mark, name, d))
    print()
    h = current_harness_name()
    print('当前判定  : %s' % ADAPTERS[h]['label'])
    print()
    print('transcript 探测:')
    for name, a in ADAPTERS.items():
        try:
            p = a['locate']({}, os.getcwd(), '')
        except Exception as e:
            print('  %-14s 定位异常: %s' % (name, e))
            continue
        if p and os.path.isfile(p):
            occ = None
            try:
                occ = a['occupancy'](p)
            except Exception as e:
                occ = '读取异常: %s' % e
            win = None
            try:
                win = a['window']({}, p)
            except Exception:
                pass
            print('  %-14s ✓ %s' % (name, p))
            print('  %-14s   占用=%s  窗口=%s' % ('', occ, win))
        else:
            print('  %-14s · 未找到（该 harness 可能没跑过）' % name)
    print()
    print('阈值（按窗口比例）:')
    for w in (200000, 272000, 400000):
        rows = []
        for n, _, _ in DEFAULT_RATIOS:
            warn, danger = thresholds(n, w)
            rows.append('压缩%d次→%s/%s' % (n, f'{warn // 1000}K', f'{danger // 1000}K'))
        print('  窗口 %-8s %s' % (f'{w // 1000}K', '  '.join(rows)))


def reset(session_id):
    p = state_path(session_id)
    if os.path.isfile(p):
        os.remove(p)
        print('已重置: %s' % p)
    else:
        print('无此状态文件: %s' % p)


def main():
    args = sys.argv[1:]
    if args:
        if args[0] == '--status':
            print_status(args[1] if len(args) > 1 else None)
            return
        if args[0] == '--reset':
            reset(args[1] if len(args) > 1 else 'default')
            return
        if args[0] == '--doctor':
            doctor()
            return
        if args[0] in ('--version', '-v'):
            print('context-guard %s' % VERSION)
            return

    ev = read_event()
    if not ev:
        # 空 stdin 不是合法事件。早退，否则后续会走「自动定位 transcript」
        # 分支，把一次空调用误判成一次真实提醒。
        silent()
        return
    handle_event(ev)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('FATAL %s' % e)
        silent()
