#!/usr/bin/env python3
"""V2O 本地 Web 控制台（只读状态 + 后台启动 + 转写 worker）。

只用 stdlib（http.server / json / threading / urllib / os / sys）。
复用（只读 import，不改 src 任何文件）：
  - src/stage12/status_snapshot.collect：只读状态快照
  - src/stage5/startup.run_startup：后台线程启动监听
  - 转写 worker 内按需懒加载（无 hard 依赖，缺转写引擎时只记 verdict）：
    stage1.asr / stage7.transcribe / stage7.prompt_builder /
    stage8.transcribe_chunks / stage8.chunk_planner /
    stage1.prepare / stage3.normalize / stage3.render /
    stage3.lineage / stage4.publish / stage6.mirror / stage2.store

API：
  GET  /              -> index.html
  GET  /api/status?data_root=&limit=&completed_limit=&completed_cursor=
                      -> collect() 原样＋FR-12 完成列表分页
                      （completed_total/completed_page/next_cursor；游标坏/被篡改 400）
  POST /api/start     -> 后台 run_startup（data_root/input_root/ob_vault_root），已在跑则 409
  GET  /api/start     -> 本进程监听状态（running/data_root/input_root/ob_vault_root/worker/error）
  GET  /api/browse?path= -> {path, parent, dirs[]}（只列目录，按名排序）
  POST /api/vocab/candidates/apply        -> 202 {job_id}（异步导入并按需重跑）
  GET  /api/vocab/candidates/apply/status -> 最近一次错词重跑进度（刷新可续看）

P0-5：默认 data_root 指向外置测试目录（/tmp 下，不写真实库）；
input_root 默认空，由用户在页面填写绝对路径后启动。
ob_vault_root 默认空：为空则 worker 只跑到 Render，不 Publish。
"""

from __future__ import annotations

import json
import base64
import datetime
import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(APP_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import platform_win  # noqa: E402  (Windows/POSIX 平台适配单点)

from stage12.status_snapshot import collect  # noqa: E402  (只读复用)
from stage5.startup import run_startup  # noqa: E402  (后台启动复用)

HOST = "127.0.0.1"
# P1-8：端口真源（唯一）。默认 8899；可用环境变量 V2O_PORT 覆盖（start.ps1 透传）。
PORT = 8899
_env_port = (os.environ.get("V2O_PORT") or "").strip()
if _env_port:
    try:
        PORT = int(_env_port)
    except ValueError:
        sys.stderr.write("V2O_PORT 必须是 1-65535 的整数，当前为 %r；未指定则用默认 %s。\n"
                         % (_env_port, 8899))
        raise SystemExit(2)
    if not 1 <= PORT <= 65535:
        sys.stderr.write("V2O_PORT 超出 1-65535 范围：%s。\n" % (PORT,))
        raise SystemExit(2)
del _env_port

# 正式 data root：Windows = %LOCALAPPDATA%\Video2Obsidian\data；
# 拿不到 LOCALAPPDATA 就给人话报错退出（不静默回落到临时目录）。
try:
    DEFAULT_DATA_ROOT = platform_win.default_data_root()
except platform_win.DataRootUnavailable:
    # 人话：不猜目录、不改临时目录；异常体里那句同样的人话由 platform_win 持有。
    sys.stderr.write(
        "数据目录无法确定：Windows 上找不到环境变量 LOCALAPPDATA。\n"
        "请确认以普通用户身份登录、重新打开终端后重试"
        "（本程序不会猜测目录，也不会改用临时目录代替正式数据目录）。\n")
    raise SystemExit(2)
DEFAULT_PROFILE_HASH = "local-console-v1"

CODE_ASR_MISSING = "PRECHECK_ASR_BACKEND_MISSING"

# P1-FIX-1：发布门复核「源文件此刻是否仍在变」的采样间隔（秒）。
# 静默窗+多轮采样挡不住「写方停顿超过投递门」的文件，这一道在处理/发布前再核
# 一次 size/mtime；只有 mtime 变了才多等这一个间隔做第二采样，正常路径零成本。
STALE_SOURCE_PROBE_S = 2.0

_state_lock = threading.Lock()
_listener = {
    "running": False,
    "data_root": None,
    "input_root": None,
    "ob_vault_root": None,
    "error": None,
    "error_code": None,
    "suggested_data_root": None,
    "started_at": None,
    # V2.2补修 P0-1/P0-3：全终态放行计数+人话（绿条用，BLOCK时为0/空）
    "skipped_terminal": 0,
    "skipped_terminal_rows": 0,
    "startup_note": None,
}
# P0-3 后端记忆：内存记 last_config，前端 localStorage 为主、后端为辅
_last_config = {
    "last_input_root": None,
    "last_data_root": None,
    "last_ob_vault_root": None,
}
_handle = {"box": None}
_worker = {
    "running": False,
    "processed": [],  # 每项 {run_id, state, verdict, whisper_calls, ...}
    "last_error": None,
    "current": None,  # {run_id, filename, stage, stage_started_at} 或 None
}
_worker_done: set = set()

# 错词重跑异步任务状态：后端唯一真源（刷新页面可续看），只保留最近一次。
# 结构见 _handle_vocab_candidates_apply；None 表示本次进程还没跑过。
_vocab_apply_job: dict | None = None
_vocab_apply_seq = 0

# P0-2 阶段文案（中文动词，人话五步，禁标准化/渲染裸词）
STAGE_DISCOVER = "发现"
STAGE_TRANSCRIBING = "听写中"
STAGE_NORMALIZING = "整理中"
STAGE_RENDERING = "成稿中"
STAGE_PUBLISHING = "入库中"
DONE_STATES = ("PUBLISHED", "RENDER_ONLY")
FAIL_STATES = ("FAIL", "PUBLISH_BLOCKED")
# 步骤序号：发现1→听写中2→整理中3→成稿中4→入库中5
STAGE_STEP = {
    "发现": 1,
    "听写中": 2,
    "整理中": 3,
    "成稿中": 4,
    "入库中": 5,
}
STAGE_FLOW_LABEL = "发现→听写→整理→成稿→入库"
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4a", ".mp3", ".wav")


# ---------------------------------------------------------------- 路径归一

def normalize_path(raw) -> str:
    """粘贴自动去引号：strip 首尾空白后去一层配对首尾引号。

    苹果复制/访达拷贝常带 '...' 或 "..." 包裹；只去最外一层且首尾
    须为同种引号。非 str 输入一律归为空串。
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


# ------------------------------------------------- P1-2 严格请求参数层与错误脱敏
#
# 目的（PRODUCT_PLAN V1.3 P1-2 / D-6 / D-9 / D-12）：
# 请求字段坏类型一律 400 人话失败、零执行、零写盘；错误响应不回真实绝对
# 路径、请求体、转写正文或密钥。
#
# 约定（各 handler 共用，不各写一套）：
# - 布尔只认 JSON true/false；1 / 0 / "true" / 1.0 一律 400，不得静默当 False。
# - 整数只认 JSON int 且显式排除 bool（bool 是 int 子类，True 会被 isinstance 命中）。
# - 字段缺键（键不存在）才走 default；键存在但值坏 → 400，不静默兜底。
# - 错误文本只回字段名与收到的类型，不回原值（原值可能是 token/正文/密钥）。

_MISSING = object()

_TYPE_ZH = {
    "bool": "布尔值", "int": "整数", "float": "小数", "str": "字符串",
    "NoneType": "空值 null", "list": "数组", "dict": "对象",
}


class ParamError(ValueError):
    """请求参数不合法：调用方统一转 400 人话，零执行。"""


def _type_zh(value) -> str:
    return _TYPE_ZH.get(type(value).__name__, type(value).__name__)


def _bad(key: str, want: str, value) -> ParamError:
    return ParamError("%s 须为%s（收到 %s）" % (key, want, _type_zh(value)))


def _body_json(body: bytes) -> dict:
    """空体/坏 JSON/非对象一律 400；不做任何默认值兜底。"""
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        raise ParamError("请求体须为 JSON 对象")
    if not isinstance(params, dict):
        raise ParamError("请求体须为 JSON 对象")
    return params


def _take_bool(params: dict, key: str, default=_MISSING) -> bool:
    if key not in params:
        if default is _MISSING:
            raise ParamError("缺少 %s（布尔值 true/false）" % key)
        return bool(default)
    value = params[key]
    if not isinstance(value, bool):
        raise _bad(key, "布尔值 true/false", value)
    return value


def _take_int(params: dict, key: str, default=_MISSING,
              minimum=None, maximum=None) -> int:
    if key not in params:
        if default is _MISSING:
            raise ParamError("缺少 %s（整数）" % key)
        return int(default)
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(key, "整数", value)
    if minimum is not None and value < minimum:
        raise ParamError("%s 不能小于 %d" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ParamError("%s 不能大于 %d" % (key, maximum))
    return value


def _take_str(params: dict, key: str, default=_MISSING, *,
              allow_empty=False, max_len=4096) -> str:
    if key not in params:
        if default is _MISSING:
            raise ParamError("缺少 %s（字符串）" % key)
        return str(default)
    value = params[key]
    if not isinstance(value, str):
        raise _bad(key, "字符串", value)
    text = value.strip()
    if not allow_empty and not text:
        raise ParamError("%s 不能为空" % key)
    if len(text) > max_len:
        raise ParamError("%s 过长（上限 %d 字）" % (key, max_len))
    return text


def _take_str_list(params: dict, key: str, *, allow_empty=False,
                   max_items=500, empty_msg: str = "") -> list:
    if key not in params:
        raise ParamError("缺少 %s（字符串数组）" % key)
    value = params[key]
    if not isinstance(value, list):
        raise _bad(key, "字符串数组", value)
    if not allow_empty and not value:
        # P1-3 零目标：调用方可给一句人话，不再把裸键名丢给用户
        raise ParamError(empty_msg or ("%s 不能为空数组" % key))
    if len(value) > max_items:
        raise ParamError("%s 项数过多（上限 %d）" % (key, max_items))
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _bad(key, "字符串数组（每项为非空字符串）", item)
        out.append(item.strip())
    return out


def _take_int_list(params: dict, key: str, *, allow_empty=False,
                   max_items=500, minimum=None, empty_msg: str = "") -> list:
    if key not in params:
        raise ParamError("缺少 %s（整数数组）" % key)
    value = params[key]
    if not isinstance(value, list):
        raise _bad(key, "整数数组", value)
    if not allow_empty and not value:
        # P1-3 零目标：调用方可给一句人话，不再把裸键名丢给用户
        raise ParamError(empty_msg or ("%s 不能为空数组" % key))
    if len(value) > max_items:
        raise ParamError("%s 项数过多（上限 %d）" % (key, max_items))
    out = []
    for item in value:
        # bool 是 int 子类：true/false 必须显式拒，不能 int() 静默转 1/0
        if isinstance(item, bool) or not isinstance(item, int):
            raise _bad(key, "整数数组（每项为整数，不收 true/false、小数、字符串）",
                       item)
        if minimum is not None and item < minimum:
            raise ParamError("%s 每项不能小于 %d" % (key, minimum))
        out.append(item)
    return out


def _take_data_root(params: dict, default_data_root: str,
                    key: str = "data_root") -> str:
    """data_root：缺键或空串 → 默认目录（旧行为）；给了就必须是绝对路径。

    显式传 null/数字/布尔/数组 → 400，不静默当默认值（P1-2 严格类型）。
    """
    if key not in params:
        return default_data_root
    value = params[key]
    if not isinstance(value, str):
        raise _bad(key, "字符串（绝对路径）", value)
    text = normalize_path(value)
    if not text:
        return default_data_root
    if not os.path.isabs(text):
        raise ParamError("数据目录须为绝对路径，请点浏览重选")
    return text


def _take_required_data_root(params: dict, key: str = "data_root") -> str:
    """候选 apply 专用严格取参：data_root 必须**显式给且非空**（P2-新1）。

    背景：job 的身份绑定在 data_root 上，而状态查询按 D-9 契约要求显式
    data_root（`_handle_vocab_apply_status` 无 data_root 一律回 job:null）。
    若申请侧允许缺省到 DEFAULT_DATA_ROOT，就会出现「POST 建成 job、GET 永远
    读不到」的前后端非对称。宁可在入口 400 拒收，也不放宽查询侧的严格性。

    文案口径（P3-三2）：缺键/空值/类型错/相对路径一律「数据目录…」人话，
    不再混用裸键名 `data_root`。
    """
    if key not in params:
        raise ParamError("请先选择数据目录（data_root 必填，绝对路径）再提交")
    value = params[key]
    if not isinstance(value, str):
        raise ParamError("数据目录须为绝对路径（收到 %s），请先选择数据目录再提交"
                         % (_type_zh(value),))
    if not value.strip():
        raise ParamError("请先选择数据目录（data_root 不能为空，绝对路径）再提交")
    text = normalize_path(value)
    if not os.path.isabs(text):
        raise ParamError("数据目录须为绝对路径，请点浏览重选")
    return text


def _query_str(query: dict, key: str, default: str = "") -> str:
    """query（parse_qs 的 {key: [v]}）取首个字符串；缺/空一律回 default。"""
    raw = query.get(key)
    if not raw:
        return default
    value = raw[0] if isinstance(raw, list) else raw
    return value if isinstance(value, str) else default


def _query_data_root(query: dict, default_data_root: str,
                     key: str = "data_root") -> str:
    """query 版 data_root：非空则必须是绝对路径，否则 400（不得静默忽略）。"""
    text = normalize_path(_query_str(query, key))
    if not text:
        return default_data_root
    if not os.path.isabs(text):
        raise ParamError("数据目录须为绝对路径，请点浏览重选")
    return text


def _query_int(query: dict, key: str, default: int,
               minimum=None, maximum=None) -> int:
    text = _query_str(query, key).strip()
    if not text:
        return default
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise ParamError("%s 须为整数（收到非整数文本）" % key)
    if minimum is not None and value < minimum:
        raise ParamError("%s 不能小于 %d" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ParamError("%s 不能大于 %d" % (key, maximum))
    return value


def _reject_duplicates(values: list, key: str) -> None:
    """重复项一律 400（重复 run_id / 重复索引会让汇总口径不可复算）。"""
    seen = set()
    for value in values:
        if value in seen:
            raise ParamError("%s 有重复项（去掉重复后重试）" % key)
        seen.add(value)


# 路径片段收尾字符：遇到引号/中文标点/换行即视为路径结束（避免吃掉后续人话）
_PATH_STOP_CHARS = frozenset(
    "'\"`，。；：、！？（）《》【】()[]{}<>|*?\t\n\r")


def _strip_paths(text: str) -> str:
    """把文本里的路径片段整体抹成 …（含空格路径，遇引号/中文标点/换行收尾）。

    逐字符扫描而非按空白切词：`/Users/zzy/My Data/vault/note.md 打不开`
    这类含空格路径必须**整段**抹掉，否则会漏出 `Data/vault/note.md` 尾段。
    宁可多抹一点（把紧跟在路径后的纯文本一并吃掉），也不漏真路径尾段。
    """
    out: list = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "/":
            j = i + 1
            while j < n and text[j] not in _PATH_STOP_CHARS:
                j += 1
            if not (out and out[-1] == "…"):
                out.append("…")
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _err_text(exc, limit: int = 180) -> str:
    """异常转人话错误片段：抹掉路径（D-12）、压平空白、截断超长。

    只保留异常类别与不带路径的部分，避免把 data_root/vault 真实路径或
    请求体回给客户端。
    """
    try:
        text = str(exc) or type(exc).__name__
    except Exception:  # noqa: BLE001  (坏 __str__ 不得再炸一层)
        text = type(exc).__name__
    text = " ".join(_strip_paths(text).split()).strip()
    if not text or text == "…":
        # 纯路径/空消息：异常回类型名，普通文本回省略号
        text = "…" if isinstance(exc, str) else type(exc).__name__
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _asr_engine_available() -> bool:
    """probe import，不 hard 依赖：缺 faster-whisper/CT2 时返回 False。

    Windows 端 Stage 3：转写引擎统一是 asr_backend（faster-whisper /
    CTranslate2），不再探测 Mac 端那套 mlx。
    """
    for module in ("faster_whisper", "ctranslate2"):
        try:
            __import__(module)
        except Exception:
            return False
    return True


def _is_under_root(src_path: str, input_root: str) -> bool:
    """P0-1：realpath 后判定源是否在当前 input_root 内。

    Windows（Stage 2 接线）：判定统一走 ``platform_win.is_within`` 的
    **normcase + commonpath**，于是大小写变体、盘符差异、UNC 都能正确判
    「在不在目录内」；POSIX 分支语义与旧实现一致（仍含 realpath）。
    已知限制：UNC 共享根作为 input_root 时判 False（fail-closed，见
    platform_win.is_within 的 P2-2 注记；UNC 根在入口已 BLOCKED_UNC_ROOT）。
    """
    try:
        if not src_path or not input_root:
            return False
        src_real = os.path.realpath(src_path)
        input_real = os.path.realpath(input_root)
        return platform_win.is_within(input_real, src_real)
    except (ValueError, OSError):
        return False


def _win_root_blockers(data_root: str, input_root: str,
                       vault: str | None) -> list[str]:
    """Windows 根目录接受前体检：data/input/vault 三个根，超限或 UNC 即拦。

    非 Windows 恒回空（Mac 端行为零改动）。错误文案**不带真实绝对路径**
    （沿用 P1-2 脱敏口径），只给类别、长度与人话指引。
    """
    problems: list[str] = []
    for label, value in (("数据目录", data_root), ("视频文件夹", input_root),
                         ("笔记库目录", vault)):
        if not value:
            continue
        res = platform_win.validate_root(value, label)
        if not res.get("ok"):
            msg = res.get("message", "")
            if res.get("guidance"):
                msg = msg + "。" + res["guidance"]
            problems.append(msg)
    return problems


def _win_path_too_long(items) -> str | None:
    """实际会写入/读取的路径长路径闸（Whisper 之前的最早一道）。

    ``items`` = [(人类说法, 路径或 None), ...]，命中第一条超限即回人话
    reason（不带真实绝对路径）；非 Windows 恒回 None，行为零改动。
    """
    for label, path in items:
        reason = platform_win.too_long_reason(path, label)
        if reason:
            return reason
    return None


# ------------------------------------------- P0-1 启动门按目录隔离（V2.2）
#
# 根因：src/stage2/store.assert_stage3_tables_empty 是全局断言（任一
# Stage3+ 表有行即 AssertionError），而 app worker 成功转写一次就会写
# normalization_revisions / render_revisions / publish_records。于是换
# input_root（同 data_root）必被他 input 的历史行卡 STOP。
# 约束 src 零碰，故不在 src 落字，改在 app 启动前做运行时限定：
#   _install_scoped_gates(input_root) 把 store 上的两个断言替换为按
#   当前 input 过滤的版本（只查 sources.current_path 归属当前 input 的
#   行；他 input 的历史行不再计数），run_startup 返回后立即恢复原断言。
# 同目录重入（本目录自己的旧行挡住）仍按门规则失败，走人话引导 +
# 一键换新数据目录（_humanize_startup_error），如实二选一中的后者。
# fail-closed：过滤链上任何解不出的归属一律计为挡住，不静默放行。

_STARTUP_SCOPE = {"input_root": None, "skipped_terminal_rows": 0,
                  "skipped_terminal_runs": 0}
_ORIG_ASSERTS: dict = {}
NOTE_MAX_CHARS = 6000

# V2.2补修 P0-1：终态集合（全终态旧账放行，仅半截拦；未知状态fail-closed计拦）。
# src零碰，此处硬编码与src枚举对齐（normalize/render/publish_commit/commit）：
#   norm: PENDING/NORMALIZING/COMMITTING -> 半截；COMPLETED/FAILED_* -> 终态
#   rend: PUBLISH_EVALUATION/FAILED_* -> 终态，其余（含ARTIFACT_COMPLETED）半截
#   pub: PUBLISHED/BLOCKED_*/CANONICAL_OUTPUT_EXISTS/PENDING_PUBLISH -> 终态
#   arch: ARCHIVE_COMMITTED/ARCHIVED/COMMITTED -> 终态
#   art: COMMITTED -> 终态（PREPARED为半截）
_NORM_TERMINAL = frozenset({"COMPLETED", "FAILED_RETRYABLE",
                            "FAILED_FINAL", "FAILED"})
_REND_TERMINAL = frozenset({"PUBLISH_EVALUATION", "FAILED_RETRYABLE",
                            "FAILED_FINAL", "FAILED"})
_PUB_TERMINAL = frozenset({
    "PUBLISHED",
    "BLOCKED_OUTPUT_EXISTS", "BLOCKED_OUTPUT_CONFLICT",
    "BLOCKED_UNSUPPORTED_OUTPUT_FILESYSTEM",
    "BLOCKED_UNSUPPORTED_ROOT_FOR_V1",
    "CANONICAL_OUTPUT_EXISTS", "PENDING_PUBLISH",
    "BLOCKED",
})
_ARCH_TERMINAL = frozenset({"ARCHIVE_COMMITTED", "ARCHIVED", "COMMITTED"})
_ART_TERMINAL = frozenset({"COMMITTED"})


def _is_norm_terminal(s) -> bool:
    try:
        t = str(s or "").strip()
    except Exception:
        return False
    if t in _NORM_TERMINAL:
        return True
    return t.startswith("FAILED")


def _is_rend_terminal(s) -> bool:
    try:
        t = str(s or "").strip()
    except Exception:
        return False
    if t in _REND_TERMINAL:
        return True
    return t.startswith("FAILED")


def _is_pub_terminal(s) -> bool:
    try:
        t = str(s or "").strip()
    except Exception:
        return False
    if t in _PUB_TERMINAL:
        return True
    return t.startswith("BLOCKED")


def _is_arch_terminal(s) -> bool:
    try:
        t = str(s or "").strip()
    except Exception:
        return False
    return t in _ARCH_TERMINAL


def _is_art_terminal(s) -> bool:
    try:
        t = str(s or "").strip()
    except Exception:
        return False
    return t in _ART_TERMINAL


def _db_success_run_ids_ro(data_root: str, input_root: str) -> set:
    """DB成功旧账run_id（norm COMPLETED归属当前input，只读fail-open）。

    P0-2缺口补齐：磁盘manifest缺失但DB已COMPLETED时仍跳过，不得重转
    （whisper不再跑）。失败/半截不在此集，走正常转写/重试。
    """
    try:
        con = _open_ro(str(data_root))
        if con is None:
            return set()
        try:
            scoped = _scoped_source_ids(con, str(input_root))
            if not scoped:
                return set()
            try:
                run2src = {r[0]: r[1] for r in con.execute(
                    "SELECT run_id, source_id FROM processing_runs").fetchall()}
            except Exception:
                return set()
            out: set = set()
            try:
                rows = con.execute(
                    "SELECT raw_artifact_id, status"
                    " FROM normalization_revisions").fetchall()
            except Exception:
                return set()
            for r in rows:
                try:
                    raw, st = r[0], (r[1] if len(r) > 1 else None)
                except Exception:
                    continue
                if str(st or "").strip() != "COMPLETED":
                    continue
                rid = None
                try:
                    if isinstance(raw, str) and raw.startswith("raw_"):
                        rid = raw[len("raw_"):]
                except Exception:
                    rid = None
                if not rid:
                    continue
                try:
                    if run2src.get(rid) in scoped:
                        out.add(rid)
                except Exception:
                    continue
            return out
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception:
        return set()


def _scoped_source_ids(con, input_root: str) -> set:
    """归属当前 input 的 source_id 全集（current_path 为主，path_identity_key 兜底）。"""
    out: set = set()
    try:
        rows = con.execute(
            "SELECT source_id, current_path, path_identity_key FROM sources"
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            sid, cur, pik = r[0], r[1], r[2]
        except Exception:
            continue
        if (cur and _is_under_root(str(cur), input_root)) or (
            not cur and pik and _is_under_root(str(pik), input_root)
        ):
            out.add(sid)
    return out


def _unattributable_source_ids(con) -> set:
    """空归属 source_id 全集（current_path 与 path_identity_key 均空）。

    P1-1 fail-closed 补洞：空路径源的坏 run/Stage3 行此前被当他 input
    放行；空归属一律计挡住（门侧），清理侧同步可清（见 _handle_clear_post）。
    """
    out: set = set()
    try:
        rows = con.execute(
            "SELECT source_id, current_path, path_identity_key FROM sources"
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            sid, cur, pik = r[0], r[1], r[2]
        except Exception:
            continue
        cur_s = str(cur).strip() if cur else ""
        pik_s = str(pik).strip() if pik else ""
        if not cur_s and not pik_s:
            out.add(sid)
    return out


def _scoped_assert_stage3_tables_empty(con):
    """按当前 input 过滤的 Stage3+ 半截断言（签名/异常文本与 src 原版一致）。

    V2.2补修 P0-1：只拦非终态半截行，全终态旧账放行。
      norm: 非COMPLETED/FAILED_* 才拦；rend: 非PUBLISH_EVALUATION/FAILED_* 才拦；
      pub: 非PUBLISHED/BLOCKED_*/终态才拦；art: 非COMMITTED才拦；
      arch: 非ARCHIVE_COMMITTED/ARCHIVED/COMMITTED才拦。
    终态旧行计入 _STARTUP_SCOPE skipped（verdict 注明“旧X条已完成记录，本次跳过”，
    fail-closed：归属解不出/状态未知一律计拦，不静默放行）。
    """
    orig = _ORIG_ASSERTS.get("stage3")
    scope_root = _STARTUP_SCOPE.get("input_root")
    if not scope_root or orig is None:
        from stage2 import store as _st  # noqa: E402  (读原断言，src 文件不动)

        return _st.assert_stage3_tables_empty(con)
    try:
        scoped = _scoped_source_ids(con, scope_root)
        unatt = _unattributable_source_ids(con)
        run2src = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT run_id, source_id FROM processing_runs"
            ).fetchall()
        }

        def _blocked_run(rid) -> bool:
            # 归属当前 input，或归属解不出（fail-closed 计挡住）；
            # P1-1：空归属源一律计挡住，不当他 input 放行。
            if not rid:
                return True
            src = run2src.get(rid)
            if src is None:
                return True
            if src in unatt:
                return True
            return src in scoped

        def _run_of_raw(raw) -> str | None:
            if isinstance(raw, str) and raw.startswith("raw_"):
                return raw[len("raw_"):]
            return None

        try:
            _norm_rows = con.execute(
                "SELECT normalized_artifact_id, raw_artifact_id, status"
                " FROM normalization_revisions"
            ).fetchall()
        except Exception:
            _norm_rows = []
        norm_map = {}
        for _nr in _norm_rows:
            try:
                norm_map[_nr[0]] = _nr[1]
            except Exception:
                continue
        try:
            _rend_rows = con.execute(
                "SELECT render_revision_id, normalized_artifact_id, status"
                " FROM render_revisions"
            ).fetchall()
        except Exception:
            _rend_rows = []
        rend_map = {}
        for _rr in _rend_rows:
            try:
                rend_map[_rr[0]] = _rr[1]
            except Exception:
                continue
        try:
            _pub_rows = con.execute(
                "SELECT render_revision_id, status FROM publish_records"
            ).fetchall()
        except Exception:
            _pub_rows = []
        try:
            _arch_rows = con.execute(
                "SELECT source_id, status FROM archive_commits"
            ).fetchall()
        except Exception:
            _arch_rows = []
        try:
            _art_rows = con.execute(
                "SELECT source_id, run_id, status FROM artifacts"
            ).fetchall()
        except Exception:
            _art_rows = []
        counts = {}
        skipped_rows = 0
        skipped_runs: set = set()
        # norm：仅非终态计拦
        n_norm = 0
        for _nr in _norm_rows:
            try:
                _raw = _nr[1] if len(_nr) > 1 else None
                _stt = _nr[2] if len(_nr) > 2 else None
            except Exception:
                continue
            _rid = _run_of_raw(_raw)
            if not _blocked_run(_rid):
                continue
            if _is_norm_terminal(_stt):
                skipped_rows += 1
                if _rid:
                    skipped_runs.add(_rid)
                continue
            n_norm += 1
        counts["normalization_revisions"] = n_norm
        # rend：仅非终态计拦
        n_rend = 0
        for _rr in _rend_rows:
            try:
                _nart = _rr[1] if len(_rr) > 1 else None
                _stt = _rr[2] if len(_rr) > 2 else None
            except Exception:
                continue
            _rid = _run_of_raw(norm_map.get(_nart))
            if not _blocked_run(_rid):
                continue
            if _is_rend_terminal(_stt):
                skipped_rows += 1
                if _rid:
                    skipped_runs.add(_rid)
                continue
            n_rend += 1
        counts["render_revisions"] = n_rend
        # pub：仅非终态计拦
        n_pub = 0
        for _pr in _pub_rows:
            try:
                _rendid = _pr[0]
                _stt = _pr[1] if len(_pr) > 1 else None
            except Exception:
                continue
            _rid = _run_of_raw(norm_map.get(rend_map.get(_rendid)))
            if not _blocked_run(_rid):
                continue
            if _is_pub_terminal(_stt):
                skipped_rows += 1
                if _rid:
                    skipped_runs.add(_rid)
                continue
            n_pub += 1
        counts["publish_records"] = n_pub
        # arch：仅非终态计拦（source 级归属）
        n_arch = 0
        for _ar in _arch_rows:
            try:
                _sid = _ar[0]
                _stt = _ar[1] if len(_ar) > 1 else None
            except Exception:
                continue
            if not (_sid is None or _sid in scoped or _sid in unatt):
                continue
            if _is_arch_terminal(_stt):
                skipped_rows += 1
                continue
            n_arch += 1
        counts["archive_commits"] = n_arch
        # art：仅非COMMITTED计拦（新增门：PREPARED半截拦，COMMITTED放行）
        n_art = 0
        for _ar2 in _art_rows:
            try:
                _sid2 = _ar2[0] if len(_ar2) > 0 else None
                _rid2 = _ar2[1] if len(_ar2) > 1 else None
                _stt2 = _ar2[2] if len(_ar2) > 2 else None
            except Exception:
                continue
            _in_scope = False
            try:
                if _sid2 is None and _rid2 is None:
                    _in_scope = True  # 归属解不出 fail-closed 计拦
                elif _sid2 in scoped or _sid2 in unatt:
                    _in_scope = True
                elif _rid2 and _blocked_run(_rid2):
                    _in_scope = True
            except Exception:
                _in_scope = True
            if not _in_scope:
                continue
            if _is_art_terminal(_stt2):
                skipped_rows += 1
                if _rid2:
                    skipped_runs.add(str(_rid2))
                continue
            n_art += 1
        if n_art:
            counts["artifacts"] = n_art
        try:
            _STARTUP_SCOPE["skipped_terminal_rows"] = int(skipped_rows)
            _STARTUP_SCOPE["skipped_terminal_runs"] = int(len(skipped_runs))
        except Exception:
            pass
        nonzero = {t: c for t, c in counts.items() if c != 0}
        if nonzero:
            raise AssertionError(
                "Stage3+ table not empty (STOP EXPANSION): %r" % (nonzero,)
            )
        return counts
    except AssertionError:
        raise
    except Exception:
        # 过滤链异常则回原断言（fail-closed，不静默放行）
        return orig(con)


def _scoped_assert_no_transcription_states(con):
    """按当前 input 过滤的 run 状态断言（只查归属当前 input 的 run）。"""
    orig = _ORIG_ASSERTS.get("runstates")
    scope_root = _STARTUP_SCOPE.get("input_root")
    if not scope_root or orig is None:
        from stage2 import store as _st  # noqa: E402  (读原断言，src 文件不动)

        return _st.assert_no_transcription_states(con)
    try:
        from stage2 import store as _st  # noqa: E402  (只读允许集合)

        allowed = set(_st.ALLOWED_RUN_STATUSES)
        scoped = _scoped_source_ids(con, scope_root)
        unatt = _unattributable_source_ids(con)
        try:
            all_srcs = {
                r[0]
                for r in con.execute("SELECT source_id FROM sources").fetchall()
            }
        except Exception:
            all_srcs = set(scoped)
        hist: dict = {}
        for r in con.execute(
            "SELECT status, source_id FROM processing_runs"
        ).fetchall():
            st, sid = r[0], r[1]
            if sid in scoped or sid is None or sid not in all_srcs or sid in unatt:
                hist[st] = hist.get(st, 0) + 1
        bad = {s: c for s, c in hist.items() if s not in allowed}
        if bad:
            raise AssertionError(
                "Run in transcription-stage status (STOP EXPANSION): %r"
                % (bad,)
            )
        return hist
    except AssertionError:
        raise
    except Exception:
        return orig(con)


def _install_scoped_gates(input_root: str) -> None:
    """启动前安装作用域断言（保存原函数，run_startup 后必须恢复）。"""
    from stage2 import store as _st  # noqa: E402  (运行时限定，src 文件不动)

    if "stage3" not in _ORIG_ASSERTS:
        _ORIG_ASSERTS["stage3"] = _st.assert_stage3_tables_empty
    if "runstates" not in _ORIG_ASSERTS:
        _ORIG_ASSERTS["runstates"] = _st.assert_no_transcription_states
    _STARTUP_SCOPE["input_root"] = input_root
    # V2.2补修：每轮清零旧账计数，避免上轮残留进绿条
    try:
        _STARTUP_SCOPE["skipped_terminal_rows"] = 0
        _STARTUP_SCOPE["skipped_terminal_runs"] = 0
    except Exception:
        pass
    _st.assert_stage3_tables_empty = _scoped_assert_stage3_tables_empty
    _st.assert_no_transcription_states = _scoped_assert_no_transcription_states


def _restore_scoped_gates() -> None:
    """恢复 src 原断言并清空作用域（finally 必调）。"""
    try:
        from stage2 import store as _st  # noqa: E402

        if "stage3" in _ORIG_ASSERTS:
            _st.assert_stage3_tables_empty = _ORIG_ASSERTS["stage3"]
        if "runstates" in _ORIG_ASSERTS:
            _st.assert_no_transcription_states = _ORIG_ASSERTS["runstates"]
    except Exception:
        pass
    _STARTUP_SCOPE["input_root"] = None


def _suggest_data_root(data_root: str) -> str:
    """按时间戳给新数据目录建议路径（只给路径，不建目录）。"""
    import datetime

    base = os.path.abspath(str(data_root or DEFAULT_DATA_ROOT)).rstrip(os.sep)
    stamp = (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    cand = "%s-%s" % (base, stamp)
    i = 2
    while os.path.exists(cand):
        cand = "%s-%s-%d" % (base, stamp, i)
        i += 1
    return cand


def _humanize_startup_error(exc: Exception, data_root: str) -> tuple:
    """启动门失败转人话：返回 (message, code, suggested_data_root)。"""
    # D-12：先脱敏再进文案（text 之后只做人话拼接，不再暴露原始异常文本）
    text = _err_text(exc)
    if "Stage3+ table not empty" in text:
        return (
            "本目录在这个数据目录下已有转写记录，按规则不能重复启动→"
            "点「一键换新数据目录」换个干净目录重起（源视频文件与笔记不动）：%s" % (text,),
            "GATE_STAGE3_BLOCKED",
            _suggest_data_root(data_root),
        )
    if "transcription-stage status" in text:
        return (
            "本目录在这个数据目录下有未收口的任务状态，按规则不能启动→"
            "点「一键换新数据目录」换个干净目录重起（源视频文件与笔记不动）：%s" % (text,),
            "GATE_RUN_STATE_BLOCKED",
            _suggest_data_root(data_root),
        )
    return ("启动监听失败：%s，请检查路径后重试" % (_err_text(exc),), None, None)


def _jobs_dir(data_root: str) -> str:
    return os.path.join(os.path.abspath(str(data_root or "")), "data", "jobs")


def _scan_disk_states(data_root: str) -> dict:
    """P1-2：读磁盘 jobs/*/manifest.json 得已完成终态（重启不丢）。

    成功=末个 RENDER_ONLY/PUBLISHED 且输出文件仍存在；
    跳过=末个 SKIPPED（DEVELOP-P1-9：笔记库已有同名笔记，没转写，落 skipped 桶）；
    失败=末个 TRANSCRIBE_FAILED/RAW_FAILED/MIRROR_FAILED/NORM_RENDER_FAILED/
    PUBLISH_BLOCKED（含 FAIL 语义）。只读，不落盘。
    返回 {run_id: {state, verdict, rendered_path, canonical_output_path}}。
    """
    out: dict = {}
    try:
        jd = _jobs_dir(data_root)
        if not os.path.isdir(jd):
            return out
        try:
            names = os.listdir(jd)
        except OSError:
            return out
        for run_id in names:
            mp = os.path.join(jd, run_id, "manifest.json")
            if not os.path.isfile(mp):
                continue
            try:
                with open(mp, "r", encoding="utf-8") as fh:
                    mani = json.load(fh)
                receipts = mani.get("receipts") or []
                if not isinstance(receipts, list) or not receipts:
                    continue
                last = None
                for r in reversed(receipts):
                    if isinstance(r, dict) and r.get("state"):
                        last = r
                        break
                if not last:
                    continue
                st = str(last.get("state") or "")
                verdict = str(last.get("verdict") or "")
                rp = last.get("rendered_path")
                cp = last.get("canonical_output_path")
                if st in ("RENDER_ONLY", "PUBLISHED"):
                    # 输出存在性校验：不存在则不算完成，需重跑
                    cand = cp if st == "PUBLISHED" else rp
                    if isinstance(cand, str) and cand and os.path.isfile(cand):
                        out[run_id] = {"state": st, "verdict": verdict,
                                       "rendered_path": rp,
                                       "canonical_output_path": cp}
                    elif isinstance(cand, str) and cand:
                        # 路径记了但文件没了：不算完成
                        continue
                    else:
                        # 无路径记录（如旧数据）：按完成计，前端显示—由调用方处理
                        out[run_id] = {"state": st, "verdict": verdict,
                                       "rendered_path": rp,
                                       "canonical_output_path": cp}
                elif st == "SKIPPED":
                    # DEVELOP-P1-9：入库前就发现同名笔记 → 没转写（没做，不算失败）；
                    # 重启后仍如实显示「已跳过」，不回落成「排队中」。
                    out[run_id] = {"state": "SKIPPED", "verdict": verdict,
                                   "rendered_path": rp,
                                   "canonical_output_path": cp}
                elif st in ("PUBLISH_BLOCKED", "TRANSCRIBE_FAILED", "RAW_FAILED",
                            "MIRROR_FAILED", "NORM_RENDER_FAILED"):
                    mapped = "PUBLISH_BLOCKED" if st == "PUBLISH_BLOCKED" else "FAIL"
                    out[run_id] = {"state": mapped, "verdict": verdict,
                                   "rendered_path": rp,
                                   "canonical_output_path": cp}
            except Exception:
                continue
    except Exception:
        return out
    return out


def _is_vault_registered(vault_root) -> bool:
    """V2.3 P0-2：vault注册检测（ob_vault_root/.obsidian是否为目录）。

    未配置/空/不可读一律 False（fail-closed，前端禁用OB按钮）。
    只读判定，不落盘。
    """
    try:
        if not vault_root or not isinstance(vault_root, str):
            return False
        if not vault_root.strip():
            return False
        obs = os.path.join(os.path.abspath(vault_root), ".obsidian")
        return os.path.isdir(obs)
    except Exception:
        return False


def _app_resolve_canonical(input_root: str, vault_root: str,
                           src_abs: str) -> dict:
    """V2.3 P0-3：app侧单扩展名包装（src零碰，只改app）。

    底座 src/stage6.mirror.resolve_canonical 只读复用，不改src文件。
    期望：09.xxx.mp4→09.xxx.md（去源扩展名再加.md，保留点号前缀），
    杜绝 09.xxx.mp4.md 双扩展名。做法：先调底座，再以
    splitext(basename)[0]+".md" 为期望名校对；不一致则保持子目录、
    仅替换文件名为单扩展名。只影响新产出：从不改名/移动已产出旧md。
    """
    from stage6.mirror import resolve_canonical as _base  # noqa: E402 只读复用
    m = _base(os.path.abspath(input_root), os.path.abspath(vault_root),
              os.path.abspath(src_abs))
    try:
        base = os.path.basename(os.path.abspath(src_abs))
        stem, _ext = os.path.splitext(base)
        if not stem:
            stem = base
        exp_name = stem + ".md"
        canon = str(m.get("canonical_output_path") or "")
        out = dict(m)
        if canon and os.path.basename(canon) != exp_name:
            out["canonical_output_path"] = os.path.join(
                os.path.dirname(canon), exp_name)
            out["app_single_ext_fixed"] = True
        else:
            out["app_single_ext_fixed"] = False
        out["app_expected_name"] = exp_name
        return out
    except Exception:
        return m


def _vault_publish_target(input_root: str, vault_root: str | None,
                          src_abs: str) -> str | None:
    """DEVELOP-P1-9：这条视频在笔记库里的目标落点（只算路径，不落盘）。

    命名**单一真源**＝`_app_resolve_canonical`（入库那一步用的同一个函数，
    底座 stage6.mirror.resolve_canonical＋app 侧单扩展名包装），此处不另造
    第二套命名规则。取不到回 None。
    """
    try:
        if not vault_root or not src_abs:
            return None
        m = _app_resolve_canonical(os.path.abspath(input_root),
                                   os.path.abspath(vault_root),
                                   os.path.abspath(src_abs))
        return str(m.get("canonical_output_path") or "") or None
    except Exception:
        return None


def _vault_note_already_there(input_root: str, vault_root: str | None,
                              src_abs: str) -> str | None:
    """DEVELOP-P1-9：目标笔记已存在则回该 md 路径，否则回 None（只读）。

    判据＝`_vault_publish_target` 指到的那一个文件在不在（os.path.isfile），
    与入库门 `initial_publish` 的 canonical 判据同一个目标，不另判一套。
    """
    cand = _vault_publish_target(input_root, vault_root, src_abs)
    if not cand:
        return None
    try:
        return cand if os.path.isfile(cand) else None
    except OSError:
        return None


# DEVELOP-P1-9：入库门「目标笔记已存在」状态码家族（No-Clobber 拦下，
# 真因是文件已存在，不是权限问题）。只收 stage4 真会回的那三个
# （`BLOCKED_OUTPUT_EXISTS`/`BLOCKED_OUTPUT_CONFLICT`/`CANONICAL_OUTPUT_EXISTS`）；
# 真权限/路径问题（BLOCKED_OUTPUT_FS/ROOT）与在途占位（PENDING_PUBLISH）
# 不在此列，仍走原来那支，不许被这句「已有同名笔记」冒充。
_PUB_TARGET_EXISTS_STATUSES = (
    "CANONICAL_OUTPUT_EXISTS", "BLOCKED_OUTPUT_EXISTS",
    "BLOCKED_OUTPUT_CONFLICT",
)


def _pub_target_exists(status) -> bool:
    """状态码判定（不看文案）：「库里已有同名笔记」这一族。"""
    try:
        return str(status or "").strip().upper() in _PUB_TARGET_EXISTS_STATUSES
    except Exception:
        return False


def _dir_tail(path: str) -> str:
    """目录尾段（“…”＋末段；根/空回“”）：供列表归属显示。"""
    try:
        p = str(path or "").rstrip("/")
        if not p:
            return ""
        base = os.path.basename(p)
        if not base:
            return "/"
        return "…/" + base
    except Exception:
        return ""


def _attach_source_filenames(snap: dict, data_root: str) -> dict:
    """V2.3 P0-1 / UX2-P1-5/P1-7：recent_runs附source_filename＋目录归属。

    联 sources 取 current_path：basename→source_filename，
    dirname→source_dir／source_dir_tail（“…/尾段”，列表归属与来源列用）。
    取不到（映射缺失/空路径）回“未知文件”。只读，不落盘。
    """
    try:
        runs = snap.get("recent_runs") if isinstance(snap, dict) else None
        if not isinstance(runs, list):
            return snap
        mapping = _run_source_path_map(data_root)
        for r in runs:
            try:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("run_id") or "")
                src_path = mapping.get(rid) if mapping else None
                if isinstance(src_path, str) and src_path.strip():
                    sp = src_path.strip()
                    d = os.path.dirname(sp)
                    r["source_path"] = sp
                    r["source_dir"] = d
                    r["source_dir_tail"] = _dir_tail(d)
                    r["source_filename"] = os.path.basename(sp) or "未知文件"
                else:
                    r["source_path"] = None
                    r["source_dir"] = None
                    r["source_dir_tail"] = ""
                    r["source_filename"] = "未知文件"
            except Exception:
                try:
                    if isinstance(r, dict) and "source_filename" not in r:
                        r["source_filename"] = "未知文件"
                except Exception:
                    pass
    except Exception:
        pass
    return snap


def _preview_mapping(input_root: str, vault_root: str | None) -> str:
    """UX-P0-3/V2.3 P0-3：用app侧单扩展名包装做映射预览，不落盘。"""
    try:
        if not input_root:
            return ""
        if not vault_root:
            return ""
        demo = os.path.join(os.path.abspath(input_root), "示例视频.mp4")
        m = _app_resolve_canonical(os.path.abspath(input_root),
                                   os.path.abspath(vault_root), demo)
        canon = str(m.get("canonical_output_path") or "")
        # 只展示库内相对部分，避免绝对路径过长
        try:
            rel = os.path.relpath(canon, os.path.abspath(vault_root))
        except Exception:
            rel = os.path.basename(canon) or "示例视频.md"
        return "视频 示例视频.mp4 → 库内 %s 式样" % (rel,)
    except Exception:
        return ""


def _count_videos_in_dir(dirpath: str) -> dict:
    """UX-P1-4/P1-5：单层只读统计视频数与长视频估算（>500MB 估算注明）。"""
    try:
        names = os.listdir(dirpath)
    except Exception:
        return {"total": 0, "sample": [], "long_estimate": 0}
    total = 0
    sample: list = []
    long_n = 0
    for nm in sorted(names)[:1000]:
        fp = os.path.join(dirpath, nm)
        try:
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(nm)[1].lower()
            if ext in VIDEO_EXTS:
                total += 1
                if len(sample) < 3:
                    sample.append(nm)
                try:
                    if os.path.getsize(fp) > 500 * 1024 * 1024:
                        long_n += 1
                except OSError:
                    pass
        except Exception:
            continue
    return {"total": total, "sample": sample, "long_estimate": long_n}


def _worker_set_current(run_id: str, filename: str, stage: str) -> None:
    with _state_lock:
        _worker["current"] = {
            "run_id": run_id,
            "filename": filename,
            "stage": stage,
            "stage_started_at": _utc_now_iso(),
        }


def _worker_clear_current() -> None:
    with _state_lock:
        _worker["current"] = None


def _count_pending_in_root(data_root: str, input_root: str,
                           exclude: set | None = None) -> int:
    """P0-2 pending=当前input下尚未处理的QUEUED数。

    realpath前缀过滤 + 排除本轮已处理（_worker_done），否则 DB 状态
    保持 QUEUED（P0-4 架构约束）会导致进度条永不到 100%。
    fail-open 记 0。
    """
    try:
        from stage2 import store as _store  # noqa: E402  (只读复用 open_db)
        con = _store.open_db(data_root)
        try:
            rows = con.execute(
                "SELECT pr.run_id, s.current_path FROM processing_runs pr"
                " LEFT JOIN sources s ON pr.source_id=s.source_id"
                " WHERE pr.status='QUEUED' AND pr.creation_mode='AUTO'"
            ).fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass
        excl = exclude or set()
        n = 0
        for r in rows:
            try:
                rid = r[0]
                cur = r[1] if len(r) > 1 else None
            except Exception:
                continue
            if rid in excl:
                continue
            if cur and _is_under_root(str(cur), input_root):
                n += 1
            elif cur is None:
                # 孤儿 run 交给 worker 记 FAIL，计入 pending 以便可见
                n += 1
        return n
    except Exception:
        return 0


def _listener_snapshot() -> dict:
    with _state_lock:
        snap = dict(_listener)
        try:
            _lc = dict(_last_config)
        except Exception:
            _lc = {}
    # P0-3 回显 last_*：内存为主，当前态兜底（升级前无记忆时仍有值）
    try:
        snap["last_input_root"] = _lc.get("last_input_root") or snap.get("input_root")
        snap["last_data_root"] = _lc.get("last_data_root") or snap.get("data_root")
        snap["last_ob_vault_root"] = _lc.get("last_ob_vault_root") or snap.get("ob_vault_root")
    except Exception:
        pass
    with _state_lock:
        current = dict(_worker["current"]) if _worker["current"] else None
        processed = list(_worker["processed"][-20:])
        mem_all = list(_worker["processed"])
        running = _worker["running"]
        last_error = _worker["last_error"]
    # D-12：last_error 会经 /api/start 原样出网，出网前统一脱敏（防未来再有裸 exc 写进来）
    if isinstance(last_error, str) and last_error:
        last_error = _err_text(last_error)
    with _state_lock:
        done_ids = set(_worker_done)
    data_root = snap.get("data_root")
    input_root = snap.get("input_root")
    vault_root = snap.get("ob_vault_root")
    # P1-2：磁盘终态合并内存，重启不归零
    disk_states: dict = {}
    try:
        if data_root:
            disk_states = _scan_disk_states(str(data_root))
    except Exception:
        disk_states = {}
    merged: dict = dict(disk_states)
    for p in mem_all:
        try:
            if isinstance(p, dict) and p.get("run_id"):
                rid = str(p.get("run_id"))
                # 内存最新覆盖磁盘（重试后以内存为准）
                merged[rid] = p
        except Exception:
            continue
    # P0-2：监听态下终态按当前 input_root 过滤（他目录历史不再混入计数）
    try:
        if snap.get("running") and input_root:
            mem_ids = set()
            for p in mem_all:
                try:
                    if isinstance(p, dict) and p.get("run_id"):
                        mem_ids.add(str(p.get("run_id")))
                except Exception:
                    continue
            merged = _filter_merged_by_input(merged, mem_ids, str(data_root),
                                             str(input_root))
    except Exception:
        pass
    done_n = sum(1 for v in merged.values()
                 if isinstance(v, dict) and v.get("state") in DONE_STATES)
    failed_n = sum(1 for v in merged.values()
                   if isinstance(v, dict) and v.get("state") in FAIL_STATES)
    # DEVELOP-P1-9：已跳过（库里已有同名笔记，没转写）单列计数——不算成功也不算失败，
    # 不计入 total（进度条口径不变），只供列表/队列文案显示。
    skipped_n = sum(1 for v in merged.values()
                    if isinstance(v, dict) and v.get("state") == "SKIPPED")
    # 干掉 SKIP：SKIP 不进 merged（_worker_record 从不记 SKIP，磁盘也不记）
    total = 0
    pending = 0
    if snap.get("running") and data_root and input_root:
        exclude = set(done_ids) | set(merged.keys())
        pending = _count_pending_in_root(str(data_root), str(input_root),
                                         exclude=exclude)
        total = pending + done_n + failed_n
    else:
        # 未监听时 total 仍给已完成数，供重起不归零展示
        total = done_n + failed_n
    # 全量细节供行级映射（≤100+磁盘，防 20 窗口丢失）
    # P1-2：附后端渲染的 finder_url（file:// 全编码），前端直接用，不再手拼
    # file://（#/? 手拼必断）。
    # V2.3 P0-1：details附source_filename（内存优先，磁盘缺失联sources取basename）。
    _fn_map: dict = {}
    try:
        if data_root:
            _fn_map = _run_source_path_map(str(data_root))
    except Exception:
        _fn_map = {}
    details: dict = {}
    try:
        for rid, v in merged.items():
            if isinstance(v, dict):
                _st = v.get("state")
                _rp = v.get("rendered_path")
                _cp = v.get("canonical_output_path")
                _disp = None
                try:
                    if _st == "PUBLISHED" and isinstance(_cp, str) and _cp:
                        _disp = _cp
                    elif isinstance(_rp, str) and _rp:
                        _disp = _rp
                    elif isinstance(_cp, str) and _cp:
                        _disp = _cp
                except Exception:
                    _disp = None
                _furl = None
                try:
                    if isinstance(_disp, str) and _disp:
                        _furl = _finder_url(_disp)
                except Exception:
                    _furl = None
                _fn = v.get("source_filename")
                try:
                    if (not isinstance(_fn, str) or not _fn.strip()) and _fn_map:
                        _sp = _fn_map.get(rid)
                        if isinstance(_sp, str) and _sp.strip():
                            _fn = os.path.basename(_sp.strip()) or "未知文件"
                        else:
                            _fn = "未知文件"
                    if not isinstance(_fn, str) or not _fn.strip():
                        _fn = "未知文件"
                except Exception:
                    _fn = "未知文件"
                details[rid] = {
                    "state": v.get("state"),
                    "verdict": v.get("verdict"),
                    "rendered_path": v.get("rendered_path"),
                    "canonical_output_path": v.get("canonical_output_path"),
                    "whisper_calls": v.get("whisper_calls"),
                    "finder_url": _furl,
                    "source_filename": _fn,
                }
    except Exception:
        details = {}
    preview = ""
    try:
        if snap.get("running") and input_root:
            preview = _preview_mapping(str(input_root),
                                       str(vault_root) if vault_root else None)
    except Exception:
        preview = ""
    worker = {"running": running,
              "processed_n": done_n + failed_n + skipped_n,
              "processed": processed,
              "details_by_run": details,
              "last_error": last_error,
              "current": current,
              "preview": preview,
              "queue": {"pending": pending, "done": done_n,
                        "failed": failed_n, "skipped": skipped_n,
                        "total": total}}
    snap["worker"] = worker
    # V2.3 P0-2：回显vault注册布尔（未配/无.obsidian均为False）
    try:
        snap["vault_registered"] = _is_vault_registered(
            snap.get("ob_vault_root"))
    except Exception:
        snap["vault_registered"] = False
    # UX2-P2-3：回显默认数据目录（前端空框常驻小字用）
    snap["default_data_root"] = DEFAULT_DATA_ROOT
    return snap


def _send_json(handler: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _serve_index(handler: BaseHTTPRequestHandler) -> None:
    path = os.path.join(APP_DIR, "index.html")
    try:
        with open(path, "rb") as fh:
            body = fh.read()
    except OSError as exc:
        _send_json(handler, 500, {"ok": False, "error": "index.html missing: %s" % (_err_text(exc),)})
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _open_ro(data_root: str):
    """只读打开中央库（不经锁门 diagnosis 用；失败回 None，fail-open）。"""
    try:
        from urllib.parse import quote as _quote

        from stage2.store import central_db_path as _addr  # noqa: E402 只读复用

        db_path = _addr(os.path.abspath(str(data_root or "")))
    except Exception:
        try:
            db_path = os.path.join(
                os.path.abspath(str(data_root or "")), "data", "state.db"
            )
        except Exception:
            return None
    try:
        if not os.path.isfile(db_path):
            return None
        uri = "file:%s?mode=ro" % _quote(os.path.abspath(db_path))
        con = sqlite3.connect(uri, uri=True, timeout=30.0,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


# ---------------------------------------------------------------- P0-1 只读失败诊断

DIAGNOSIS_VERSION = "v1.3-failure-taxonomy-1"
DIAGNOSIS_ACTIONS = (
    "SOURCE_LOCATION_REVIEW", "PRECONDITION_BLOCKED", "INPUT_MEDIA_INVALID",
    "RETRYABLE_TRANSCRIPTION", "REUSABLE_DERIVED_FAILURE", "PUBLISH_BLOCKED",
    "SKIPPED",
    "UNKNOWN",
)


def _diag_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _diag_redact_path(path: str) -> str:
    """Keep only a basename and two parent labels for UI-local evidence."""
    if not path:
        return "UNKNOWN"
    parts = [p for p in os.path.normpath(str(path)).split(os.sep) if p]
    return "…/" + "/".join(parts[-3:]) if parts else "UNKNOWN"


def _diag_hash(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError):
        return None


def _diag_identity(path: str, source: dict) -> str:
    """Compare available metadata, then hash only exact metadata matches."""
    try:
        st = os.stat(path)
    except OSError:
        return "UNKNOWN"
    size = source.get("source_size")
    mtime = source.get("source_mtime_ns")
    if size is not None and int(size) != int(st.st_size):
        return "MISMATCH"
    if mtime is not None and int(mtime) != int(st.st_mtime_ns):
        return "MISMATCH"
    expected = source.get("content_identity")
    if expected and len(str(expected)) >= 32:
        actual = _diag_hash(path)
        return "MATCH" if actual and actual == str(expected) else "MISMATCH"
    return "MATCH"


_DIAG_SCAN_TIME_BUDGET_S = 2.0
_DIAG_SCAN_MAX_DIRS = 2000
_DIAG_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn", "library",
    "__pycache__", ".trash", ".spotlight-v100", ".fseventsd",
})


def _diag_skip_dir(name: str) -> bool:
    low = name.lower()
    if low in _DIAG_SKIP_DIRS or low.startswith(".photoslibrary"):
        return True
    if low.endswith(".sparsebundle") or low.endswith(".photoslibrary"):
        return True
    if low.startswith("com~apple~clouddocs") or low == "cloudstorage":
        return True
    if low == "mobile documents":
        return True
    return False


def _diag_alternate_paths(recorded: str, data_root: str) -> list[str]:
    """Find same-basename candidates without writing; bounded read-only scan.

    Fail-closed: time/dir budget exceeded -> stop and return partial list
    (caller keeps identity_match=UNKNOWN when no MATCH). Never raises.
    """
    if not recorded:
        return []
    name = os.path.basename(recorded)
    roots = []
    for root in (os.path.dirname(recorded), os.path.dirname(os.path.dirname(recorded)),
                 os.path.expanduser("~/Downloads"), "/Volumes", data_root):
        try:
            root = os.path.abspath(root) if root else ""
        except (OSError, ValueError):
            continue
        if root and root not in roots:
            try:
                if os.path.isdir(root):
                    roots.append(root)
            except OSError:
                continue
    found: list[str] = []
    start = time.monotonic()
    seen_dirs = 0
    try:
        for root in roots:
            try:
                walker = os.walk(root, followlinks=False)
            except OSError:
                continue
            try:
                for current, dirs, files in walker:
                    seen_dirs += 1
                    if seen_dirs > _DIAG_SCAN_MAX_DIRS:
                        return list(dict.fromkeys(found))
                    if time.monotonic() - start > _DIAG_SCAN_TIME_BUDGET_S:
                        return list(dict.fromkeys(found))
                    if root == "/Volumes":
                        # 挂载点只看顶层文件，不递归进各卷（防网络卷阻塞）
                        dirs[:] = []
                    try:
                        depth = os.path.relpath(current, root).count(os.sep)
                    except (OSError, ValueError):
                        dirs[:] = []
                        continue
                    dirs[:] = [d for d in dirs
                               if not d.startswith(".") and not _diag_skip_dir(d)]
                    if depth > 4:
                        dirs[:] = []
                        continue
                    if name in files:
                        candidate = os.path.join(current, name)
                        try:
                            if os.path.abspath(candidate) != os.path.abspath(recorded):
                                found.append(candidate)
                        except (OSError, ValueError):
                            continue
            except OSError:
                continue
    except Exception:
        return list(dict.fromkeys(found))
    return list(dict.fromkeys(found))


def _manifest_last_receipt(manifest: dict | None) -> dict:
    """manifest 里末个带 state 的 receipt（与 `_scan_disk_states` 同一取法）。

    DEVELOP-P1-9：诊断要看得见「入库那一步到底怎么被拦的」——DB 的
    processing_runs.status 按 P0-4 架构恒为 QUEUED，拦人理由只落在 receipt 上。
    """
    try:
        receipts = (manifest or {}).get("receipts") or []
        for r in reversed(receipts):
            if isinstance(r, dict) and r.get("state"):
                return r
    except Exception:
        pass
    return {}


def _diag_action(row: dict, source: dict, manifest: dict | None, recorded_exists: bool,
                 identity: str) -> tuple[str, str, str, str, str, bool, str]:
    text = " ".join(str(row.get(k) or "") for k in ("status", "raw_error_code", "reason"))
    low = text.lower()
    last = _manifest_last_receipt(manifest)
    # 入库拦人理由在 receipt 上（state/publish_status），DB 行里恒是 QUEUED
    if not recorded_exists and identity == "MATCH":
        return ("SOURCE_LOCATION_REVIEW", "SOURCE_NOT_AT_RECORDED_PATH+IDENTITY_MATCH_AT_ALTERNATE_PATH",
                "DISCOVERY", "CONFIRM_ALTERNATE_THEN_REDIAGNOSE", "NEEDS_HUMAN", False,
                "确认替代路径后重新诊断")
    # DEVELOP-P1-9：入库前就发现同名笔记 → 这条 run 根本没转写。归明确的
    # SKIPPED 类（「没做，不算失败」，与 STATE_BUCKET 的 SKIPPED 同一口径），
    # 不再落 UNKNOWN/UNVERIFIED。
    if str(last.get("state") or "").strip().upper() == "SKIPPED":
        return ("SKIPPED", "NOTE_ALREADY_EXISTS", "PUBLISH", "PUBLISH_ONLY",
                "NEEDS_HUMAN", False,
                "笔记库里已有同名笔记，未覆盖；如需更新请先删除或改名那篇笔记，再点重试")
    # DEVELOP-P1-9：入库门判「目标已存在」（No-Clobber 拦下）——真因是文件已在，
    # 不是权限问题；文案必须说清「已存在/未覆盖」，不许再指向「检查笔记库权限」。
    if _pub_target_exists(last.get("publish_status")) or _pub_target_exists(last.get("state")):
        return ("PUBLISH_BLOCKED", "PUBLISH_TARGET_EXISTS", "PUBLISH", "PUBLISH_ONLY",
                "NEEDS_HUMAN", False,
                "笔记库里已有同名笔记，未覆盖；如需更新请先删除或改名那篇笔记，再点重试")
    if any(x in low for x in ("publish", "canonical", "no_clobber", "exists")):
        root = "PUBLISH_NO_CLOBBER_CONFLICT" if "clobber" in low or "exists" in low else "PUBLISH_PERMISSION"
        return ("PUBLISH_BLOCKED", root, "PUBLISH", "PUBLISH_ONLY", "AUTO_PUBLISH", False,
                "检查发布权限或目标冲突后仅重新入库")
    if any(x in low for x in ("permission", "sandbox", "precondition", "asr_backend_missing")):
        return ("PRECONDITION_BLOCKED", "PERMISSION_OR_SANDBOX", "SYSTEM",
                "BLOCK_UNTIL_FIXED", "NEEDS_ENV_FIX", False, "修复权限或运行环境后重新诊断")
    if any(x in low for x in ("media", "ffprobe", "unreadable", "corrupt", "invalid")):
        return ("INPUT_MEDIA_INVALID", "MEDIA_UNREADABLE", "ASR", "MANUAL_REVIEW",
                "NEEDS_MEDIA_CHECK", False, "检查媒体可读性后人工处理")
    if any(x in low for x in ("transient", "timeout", "tempor", "asr", "transcrib")):
        return ("RETRYABLE_TRANSCRIPTION", "TRANSCRIPTION_TRANSIENT", "ASR", "RETRANSCRIBE",
                "AUTO_RETRANSCRIBE", True, "重新转写")
    if manifest and any(manifest.get(k) for k in ("raw_path", "normalized_path", "rendered_path")):
        return ("REUSABLE_DERIVED_FAILURE", "DERIVED_ARTIFACT_REUSABLE", "NORMALIZE",
                "REUSE_DERIVED", "AUTO_REUSE", False, "复用已有文字重新成稿")
    return ("UNKNOWN", "UNKNOWN", "UNKNOWN", "MANUAL_REVIEW", "NEEDS_HUMAN", False,
            "补充证据后人工判断")


def _diagnosis_item(row: dict, source: dict, data_root: str, persisted_at: str) -> dict:
    run_id = str(row.get("run_id") or "UNKNOWN")
    recorded = str(source.get("current_path") or source.get("path_identity_key") or "")
    recorded_exists = bool(recorded and os.path.isfile(recorded))
    candidates = _diag_alternate_paths(recorded, data_root) if not recorded_exists else []
    identity = "UNKNOWN"
    alternate = None
    for candidate in candidates:
        verdict = _diag_identity(candidate, source)
        if verdict == "MATCH":
            identity, alternate = verdict, candidate
            break
        if verdict == "MISMATCH":
            identity = verdict
    manifest = None
    manifest_path = os.path.join(_jobs_dir(data_root), run_id, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError, TypeError):
        pass
    action, root, stage, policy, eligibility, whisper, next_action = _diag_action(
        row, source, manifest, recorded_exists, identity)
    # DEVELOP-P1-9：目标笔记已存在（含入库前就跳过、入库时被 No-Clobber 拦下）不是
    # 「失败」——展示态归 SKIPPED（「没做，不算失败」），与 STATE_BUCKET 同口径。
    _NOTE_EXISTS_ROOTS = ("NOTE_ALREADY_EXISTS", "PUBLISH_TARGET_EXISTS")
    if action == "SKIPPED" or str(root) in _NOTE_EXISTS_ROOTS:
        display = "SKIPPED"
    elif action == "UNKNOWN" and str(row.get("status")) == "SUCCEEDED":
        display = "SUCCEEDED"
    else:
        display = "FAIL"
    event_at = row.get("state_event_at") or "UNKNOWN"
    persisted_state = str(row.get("status") or "UNKNOWN")
    aligned = bool(event_at != "UNKNOWN" and str(event_at) == str(row.get("updated_at") or ""))
    conflicts = []
    if not aligned:
        conflicts.append("页面快照与持久状态事件未对齐")
    # FR-13 四层状态 fail-closed：展示与记录不一致时恢复资格降级，不回写 DB
    # 状态语义归一化：展示态"FAIL"为失败/受阻语义，对应持久态 FAIL 家族
    # （PUBLISH_BLOCKED 正常入库受阻属 FAIL 语义，不算 mismatch）；真不一致仍 mismatch 降级
    _FAIL_SEMANTICS = frozenset({"FAIL", "PUBLISH_BLOCKED", "TRANSCRIBE_FAILED",
                                 "RAW_FAILED", "MIRROR_FAILED", "NORM_RENDER_FAILED"})
    if display == "FAIL":
        mismatch = bool(persisted_state not in _FAIL_SEMANTICS)
    elif display == "SUCCEEDED":
        mismatch = bool(persisted_state != "SUCCEEDED")
    elif display == "SKIPPED":
        # 跳过＝没做（DB 侧按 P0-4 恒为 QUEUED，不该因此被当成「展示与记录不一致」）；
        # 只有持久态真带失败语义时才算不一致。
        mismatch = bool(persisted_state in _FAIL_SEMANTICS)
    else:
        mismatch = bool(display != persisted_state)
    if mismatch:
        conflicts.append("展示与记录不一致，原因待验证")
        if eligibility in {"AUTO_RETRANSCRIBE", "AUTO_REUSE", "AUTO_PUBLISH"}:
            eligibility, policy, whisper = "NEEDS_HUMAN", "MANUAL_REVIEW", False
            next_action = "展示与记录不一致，原因待验证；补充证据后人工判断"
    artifacts = {k: bool(manifest and manifest.get(k)) for k in
                 ("raw_path", "normalized_path", "rendered_path", "canonical_output_path")}
    return {
        "run_id": run_id, "source_label": os.path.basename(recorded) or run_id,
        # P1-4/FR-3：「目录尾段」直接复用列表侧同一 helper `_dir_tail()`
        # （见 :1081 `source_dir_tail`），不另造第二套尾段口径
        "source_dir_tail": _dir_tail(os.path.dirname(recorded)),
        "recorded_path_redacted": _diag_redact_path(recorded),
        "recorded_path_exists": recorded_exists,
        "alternate_path_checked": bool(not recorded_exists),
        "alternate_path_redacted": _diag_redact_path(alternate) if alternate else "UNKNOWN",
        "identity_match": identity, "mount_or_provider_checked": "本机挂载与常见云根只读检查",
        "persisted_state": persisted_state,
        "persisted_state_source": "state.db:processing_runs",
        "persisted_state_at": event_at,
        "state_event_at": event_at, "display_state": display,
        "display_state_source": "page snapshot:failure-diagnosis",
        "display_state_at": persisted_at,
        "page_snapshot_at": "UNKNOWN", "provenance_status": "TIME_ALIGNED" if aligned else "NOT_TIME_ALIGNED",
        "worker_stage": "UNKNOWN", "worker_stage_source": "worker event(only meaningful when ACTIVE)",
        "worker_stage_at": "UNKNOWN", "job_manifest_exists": bool(manifest),
        "display_persisted_mismatch": mismatch,
        "artifact_presence": artifacts, "evidence_sources": ["state.db", "source filesystem", "job manifest"],
        "raw_error_code": row.get("raw_error_code") or "UNKNOWN", "evidence_conflicts": conflicts,
        "confidence": "HIGH" if (
            (action == "SOURCE_LOCATION_REVIEW" and identity == "MATCH")
            or action == "SKIPPED" or str(root) in _NOTE_EXISTS_ROOTS) else "UNVERIFIED",
        "action_category": action, "root_cause": root, "stage": stage, "retry_policy": policy,
        "recovery_eligibility": eligibility,
        "recovery_eligibility_source": "diagnosis rule " + DIAGNOSIS_VERSION,
        "recovery_eligibility_at": persisted_at,
        "will_call_whisper": whisper,
        "reason": "原登记路径无文件；已发现身份匹配替代路径" if action == "SOURCE_LOCATION_REVIEW" else next_action,
        "missing_evidence": "页面同刻 snapshot/state event 链" if not aligned else "UNKNOWN",
        "next_action": next_action, "state_fingerprint": hashlib.sha256(
            json.dumps([run_id, row.get("status"), recorded, identity, action], ensure_ascii=False).encode()
        ).hexdigest(),
        "diagnosis_version": DIAGNOSIS_VERSION, "diagnosis_snapshot_id": "UNKNOWN",
        "snapshot_time": persisted_at,
    }


def _handle_failure_diagnosis(query: dict) -> tuple[int, dict]:
    """只读失败诊断；坏库/缺表/半文件一律结构化人话失败（D-12），不回路径。"""
    generated = _diag_now()
    try:
        data_root = _query_data_root(query, DEFAULT_DATA_ROOT)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc), "generated_at": generated,
                     "diagnosis_version": DIAGNOSIS_VERSION}
    con = _open_ro(data_root)
    if con is None:
        return 200, {"ok": False, "code": "DB_MISSING", "generated_at": generated,
                     "diagnosis_version": DIAGNOSIS_VERSION}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"processing_runs", "sources"}.issubset(tables):
            return 200, {"ok": False, "code": "DB_SCHEMA_MISMATCH", "generated_at": generated,
                         "diagnosis_version": DIAGNOSIS_VERSION}
        db_mtime = os.path.getmtime(os.path.join(os.path.abspath(data_root), "data", "state.db"))
        persisted_at = datetime.datetime.fromtimestamp(db_mtime, datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = con.execute("SELECT pr.*, se.created_at AS state_event_at FROM processing_runs pr "
                           "LEFT JOIN state_events se ON se.entity_id=pr.run_id "
                           "ORDER BY pr.run_id").fetchall()
        items = []
        for raw in rows:
            row = dict(raw)
            if str(row.get("status")) in {"SUCCEEDED", "COMPLETED"}:
                continue
            src_row = con.execute("SELECT * FROM sources WHERE source_id=?", (row.get("source_id"),)).fetchone()
            if not src_row:
                continue
            item = _diagnosis_item(row, dict(src_row), data_root, persisted_at)
            items.append(item)
        fingerprint = hashlib.sha256(json.dumps(
            [(i["run_id"], i["state_fingerprint"]) for i in items], ensure_ascii=False
        ).encode()).hexdigest()[:16]
        snapshot_id = "diag-%s" % fingerprint
        for item in items:
            item["diagnosis_snapshot_id"] = snapshot_id
        counts = {"incomplete_or_blocked_total": len(items),
                  "auto_retryable_failure_count": sum(1 for i in items if i["recovery_eligibility"] in {"AUTO_RETRANSCRIBE", "AUTO_REUSE", "AUTO_PUBLISH"}),
                  "will_call_whisper_count": sum(1 for i in items if i["will_call_whisper"]),
                  "source_location_review": sum(1 for i in items if i["action_category"] == "SOURCE_LOCATION_REVIEW"),
                  "alternate_identity_matches": sum(1 for i in items if i["identity_match"] == "MATCH"),
                  # DEVELOP-P1-9：库里已有同名笔记（没做，不算失败）的单列计数，
                  # 与 failed 口径分开，页面/统计都不把它算进失败。
                  "skipped_note_exists": sum(1 for i in items if i["display_state"] == "SKIPPED"),
                  "unknown": sum(1 for i in items if i["action_category"] == "UNKNOWN")}
        categories = [{"action_category": action, "count": sum(1 for i in items if i["action_category"] == action)}
                      for action in DIAGNOSIS_ACTIONS]
        return 200, {"ok": True, "diagnosis_version": DIAGNOSIS_VERSION,
                     "diagnosis_snapshot_id": snapshot_id, "generated_at": generated,
                     "persisted_snapshot_at": persisted_at, "page_snapshot_at": "UNKNOWN",
                     "provenance_status": "NOT_TIME_ALIGNED", "counts": counts,
                      "categories": categories, "items": items}
    except (sqlite3.Error, OSError) as exc:
        # 坏库（非 sqlite 文件）/半文件/权限：结构化失败，不回绝对路径（D-12）
        return 200, {"ok": False, "code": "DB_UNREADABLE",
                     "generated_at": generated,
                     "diagnosis_version": DIAGNOSIS_VERSION,
                     "error": "状态库读不出来：%s，请检查数据目录或先启动一次监听"
                              % (_err_text(exc),)}
    finally:
        con.close()


# ---------------------------------------------------------------- P0-2 批量恢复闭环
#
# FR-4 六项复用门（前置条件、允许读写集合、状态迁移、失败后状态、
# 重复提交、No-Clobber）结论（P0-1 诊断层为只读输入，不重写）：
# - retry（_handle_retry_post）：内存排队、要求监听运行中、无持久生命
#   周期 → 未过门，批量 RETRANSCRIBE 不调用它，走直接生命周期路径。
# - reapply（_reapply_one/_derive.derive_on_correction_change）：要求已完成
#   +Raw 存在、只写新 Norm/Render 版本与 receipt、Raw 不变断言、失败回滚、
#   whisper=0 → 过门，仅 REUSE_DERIVED 复用其同一 derive 入口。
# - publish（stage4.publish.initial_publish）：只新建不存在的 canonical、
#   冲突/用户编辑只判不写、覆盖次数 0 → 过门，仅 PUBLISH_ONLY 调用它。
# 未过门入口不得接入批量：exec 分发前逐项复核门条件，违者逐项 NEEDS_HUMAN。

RECOVERY_TOKEN_TTL_SEC = 600
RECOVERY_AUTO_STRATEGY = {
    "AUTO_RETRANSCRIBE": "RETRANSCRIBE",
    "AUTO_REUSE": "REUSE_DERIVED",
    "AUTO_PUBLISH": "PUBLISH_ONLY",
}
RECOVERY_JOB_FINAL = frozenset({"SUCCEEDED", "FAILED", "SKIPPED", "NEEDS_HUMAN"})

# ------------------------------------------- P1-3 五桶语义唯一真源（两链共用）
#
# 问题（QA-P13-P2-3-UNKNOWN-STATE）：批量恢复 `_recovery_bucket` 与存量重跑
# `_reapply_result_bucket` 各写一套判据——`ok=True` ＋ 未知 state 在重跑侧算成功、
# 在恢复侧算失败，同一批结果两处口径不同，「失败统计统一可复算」只做到表面闭环。
#
# 约定：`STATE_BUCKET` 是**唯一真源**，两条链都从它派生，谁也不许再自写 if 链；
# 表外的 state（含空值）一律 fail-closed 落 failed，绝不静默升成成功；成功态直接
# 取自既有完成态枚举 `DONE_STATES`（PUBLISHED／RENDER_ONLY）＋恢复链成功态
# SUCCEEDED，失败家族取自既有 `FAIL_STATES`（FAIL／PUBLISH_BLOCKED），表里查得到
# 就一定有桶，查不到就一定是失败桶，没有第三条路。
# `source_location_blocked` 是「需重新指定源目录」的**标记**（可与其他桶重叠），
# 不属于这个划分，不参与求和。
BUCKET_SUCCESS = "success"
BUCKET_FAILED = "failed"
BUCKET_SKIPPED = "skipped"
BUCKET_NEEDS_HUMAN = "needs_human"
BUCKET_INTERRUPTED = "interrupted"
FIVE_BUCKETS = (BUCKET_SUCCESS, BUCKET_FAILED, BUCKET_SKIPPED,
                BUCKET_NEEDS_HUMAN, BUCKET_INTERRUPTED)

STATE_BUCKET = {
    **{state: BUCKET_SUCCESS for state in DONE_STATES},
    **{state: BUCKET_FAILED for state in FAIL_STATES},
    "SUCCEEDED": BUCKET_SUCCESS,       # 恢复链逐项成功态（_exec_* 回的就是它）
    "SKIPPED": BUCKET_SKIPPED,         # 已存在/不适用：明确「没做，不算失败」
    "SKIP": BUCKET_SKIPPED,            # 监听 worker 同一语义的旧拼写
    "NEEDS_HUMAN": BUCKET_NEEDS_HUMAN,
    "INTERRUPTED": BUCKET_INTERRUPTED,
    "FAILED": BUCKET_FAILED,
    "TRANSCRIBE_FAILED": BUCKET_FAILED,   # 磁盘 manifest 里的细分失败态
    "RAW_FAILED": BUCKET_FAILED,
    "MIRROR_FAILED": BUCKET_FAILED,
    "NORM_RENDER_FAILED": BUCKET_FAILED,
}
# 两链对外字段名不同（存量重跑 success/failed、批量恢复 recovered/still_failed），
# 只做名字映射；判据仍只有上面那一张表。
_RECOVERY_BUCKET_NAMES = {
    BUCKET_SUCCESS: "recovered",
    BUCKET_FAILED: "still_failed",
    BUCKET_SKIPPED: "skipped",
    BUCKET_NEEDS_HUMAN: "needs_human",
    BUCKET_INTERRUPTED: "interrupted",
}
RECOVERY_BUCKETS = tuple(_RECOVERY_BUCKET_NAMES[key] for key in FIVE_BUCKETS)


def _state_bucket(state) -> str:
    """唯一真源：state 文本 → 语义桶；未知名/空值一律 failed（fail-closed）。"""
    return STATE_BUCKET.get(str(state or "").strip().upper(), BUCKET_FAILED)


def _recovery_bucket(state) -> str:
    """逐项归类（派生自唯一真源 `_state_bucket`）：任何 state 必落且只落一个桶。"""
    return _RECOVERY_BUCKET_NAMES[_state_bucket(state)]


def _recovery_bucket_counts(results) -> dict:
    counts = {key: 0 for key in RECOVERY_BUCKETS}
    for item in (results or []):
        state = item.get("state") if isinstance(item, dict) else None
        counts[_recovery_bucket(state)] += 1
    return counts


def _recovery_apply_counts(job: dict) -> dict:
    """把五桶与可复算字段写回 job（就地），供终态与查询两侧同一口径。

    - `done` ＝ **已落盘逐项数**（真实进度，运行中也单调可读）；
    - 五桶按 `_recovery_bucket` 归类已落盘结果；登记总数里**还没落盘**的剩余目标
      一律记入 `interrupted`（服务重启/半截的真含义就是「这些单位没跑完」）；
    - `counted` ＝ 五桶之和，终态恒满足
      `total == recovered + still_failed + skipped + needs_human + interrupted`；
    - 若逐项结果多于登记总数（老 job / 手工改过的文件），以实际逐项为准把 total
      抬到可复算之和，绝不让 total 小于各桶之和；
    - 收尾自检 `balanced = (counted == total)`，页面与接口都读它。
    """
    results = job.get("results")
    counts = _recovery_bucket_counts(results)
    counted = sum(counts.values())
    processed = len(results) if isinstance(results, list) else 0
    try:
        total = int(job.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > counted:
        counts["interrupted"] += total - counted
    elif total < counted:
        total = counted
    job.update(counts)
    counted = sum(counts.values())
    job["total"] = total
    job["counted"] = counted
    job["done"] = processed
    job["balanced"] = counted == total
    return job


# P1-2 字段一致（RERUN-PROGRESS P3-1）：results[] 每条都带同一组键，任何分支
# 都不缺键；未能归类的额外键统一进 extra 对象，前端可按固定契约解释部分失败。
RECOVERY_RESULT_FIELDS = (
    "run_id", "strategy", "ok", "state", "whisper_calls", "reason",
    "rendered_path", "canonical_output_path", "gate_ok", "finished_at", "extra",
)
_RECOVERY_PLANS: dict = {}
_RECOVERY_PLAN_LOCK = threading.Lock()


def _recovery_result_entry(payload: dict, *, gate_ok: bool = True) -> dict:
    """把任意分支结果收敛成固定字段集合（缺键补空值，多余键进 extra）。"""
    entry = {k: None for k in RECOVERY_RESULT_FIELDS}
    entry["ok"] = False
    entry["whisper_calls"] = 0
    entry["reason"] = ""
    entry["gate_ok"] = bool(gate_ok)
    entry["extra"] = {}
    for key, value in (payload or {}).items():
        if key in RECOVERY_RESULT_FIELDS:
            entry[key] = value
        else:
            entry["extra"][key] = value
    return entry


def _normalize_recovery_results(results) -> list:
    """只读归一：老 job 文件里的半结构结果也按同一契约返回。"""
    out = []
    for item in (results or []):
        out.append(_recovery_result_entry(item if isinstance(item, dict) else {}))
    return out



def _recovery_jobs_dir(data_root: str) -> str:
    return os.path.join(os.path.abspath(str(data_root or "")), "data", "recovery_jobs")


def _recovery_root_digest(data_root: str) -> str:
    """job↔data_root 绑定摘要（realpath 口径，与本仓其它身份判定一致）。

    用 realpath 而非 abspath：符号链接/尾斜杠/`..` 指向同一目录时必须是同一
    身份（abspath 会把这些写法算成不同目录，造成取 job 被 409 误拒）。
    """
    real = os.path.realpath(os.path.abspath(str(data_root or "")))
    return hashlib.sha256(real.encode()).hexdigest()[:16]


def _recovery_root_digest_legacy(data_root: str) -> str:
    """旧口径摘要（abspath）：仅用于兼容本改动之前写下的 job 文件，别在新代码用。"""
    return hashlib.sha256(
        os.path.abspath(str(data_root or "")).encode()).hexdigest()[:16]


def _recovery_digest_ok(value) -> bool:
    """摘要合法性：必须是非空 16 位十六进制字符串（空/缺/null 一律不合法）。"""
    if not isinstance(value, str) or len(value) != 16:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _recovery_atomic_write(path: str, obj: dict) -> None:
    """原子写 job 真源：tmp 落盘 + fsync + os.replace；失败不留半文件。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # tmp 后缀带线程 + 随机：同 job 并发落盘不共用同一 tmp 名
    tmp = "%s.tmp-%d-%s-%s" % (
        path, os.getpid(),
        hashlib.sha256(os.urandom(8)).hexdigest()[:8],
        str(threading.current_thread().ident))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _recovery_job_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand = hashlib.sha256(os.urandom(24)).hexdigest()[:10]
    return "rec-%s-%s" % (stamp, rand)


def _recovery_safe_job_id(job_id: str) -> bool:
    if not isinstance(job_id, str) or not job_id:
        return False
    return bool(__import__("re").fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_]{0,80}", job_id))


def _recovery_plan_items(data_root: str, run_ids: list) -> tuple[dict | None, list, list]:
    """复用 P0-1 诊断同一快照组装 dry-run 计划；返回 (diag, eligible, excluded)。"""
    code, diag = _handle_failure_diagnosis({"data_root": [data_root]})
    if code != 200 or not isinstance(diag, dict) or not diag.get("ok"):
        return None, [], [{"run_id": r, "eligible": False,
                           "reason": "诊断不可用，重新查询失败原因后再试"} for r in run_ids]
    by_id = {str(i.get("run_id")): i for i in (diag.get("items") or [])
             if isinstance(i, dict)}
    eligible, excluded = [], []
    for rid in run_ids:
        item = by_id.get(str(rid))
        if item is None:
            excluded.append({"run_id": str(rid), "eligible": False,
                             "reason": "不在本次诊断快照内，刷新后重新诊断"})
            continue
        if str(item.get("action_category")) == "SOURCE_LOCATION_REVIEW":
            excluded.append({"run_id": str(rid), "eligible": False,
                             "action_category": "SOURCE_LOCATION_REVIEW",
                             "reason": "原登记路径无文件（已发现替代路径待确认），"
                                       "不进入自动重试但保持可见"})
            continue
        strategy = RECOVERY_AUTO_STRATEGY.get(str(item.get("recovery_eligibility")))
        if not strategy:
            excluded.append({"run_id": str(rid), "eligible": False,
                             "action_category": str(item.get("action_category")),
                             "reason": "恢复资格为 %s，需人工处理" % (
                                 item.get("recovery_eligibility"),)})
            continue
        eligible.append({"run_id": str(rid), "strategy": strategy,
                         "fingerprint": str(item.get("state_fingerprint")),
                         "will_call_whisper": bool(item.get("will_call_whisper")),
                         "diagnosis_snapshot_id": str(diag.get("diagnosis_snapshot_id"))})
    return diag, eligible, excluded


def _handle_retry_plan_post(body: bytes) -> tuple[int, dict]:
    """FR-5 dry-run：只读组装计划，服务端只存摘要，不执行、零写入业务数据。"""
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
        # P1-3 零目标：空 run_ids 给一句人话，不丢裸键名 `run_ids 不能为空数组`
        run_ids = _take_str_list(
            params, "run_ids",
            empty_msg="没有要恢复的任务（零目标，零执行）：请先在列表里勾选任务再预览")
        _reject_duplicates(run_ids, "run_ids")
        snap_id = _take_str(params, "diagnosis_snapshot_id", "",
                            allow_empty=True)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    diag, eligible, excluded = _recovery_plan_items(data_root, run_ids)
    if diag is None:
        return 500, {"ok": False, "error": "诊断不可用，重新查询失败原因后再试"}
    if isinstance(snap_id, str) and snap_id.strip() and snap_id.strip() != str(
            diag.get("diagnosis_snapshot_id")):
        return 409, {"ok": False, "error": "诊断快照已变化，请用最新诊断重新预览",
                     "diagnosis_snapshot_id": diag.get("diagnosis_snapshot_id")}
    import time as _time
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    digest = hashlib.sha256(token.encode()).hexdigest()
    expires_at = int(_time.time()) + RECOVERY_TOKEN_TTL_SEC
    items = {e["run_id"]: {"fingerprint": e["fingerprint"], "strategy": e["strategy"]}
             for e in eligible}
    data_abs = os.path.abspath(data_root)
    # 绑定摘要用 realpath 口径（符号链接/尾斜杠/.. 视同一目录），与 _recovery_job_read 对齐
    data_digest = _recovery_root_digest(data_root)
    with _RECOVERY_PLAN_LOCK:
        # 服务端只存摘要：内存键与落盘均为 digest，token 原文不存储
        _RECOVERY_PLANS[digest] = {
            "data_root": data_abs,
            "data_root_digest": data_digest,
            "snapshot_id": str(diag.get("diagnosis_snapshot_id")),
            "items": items, "expires_at": expires_at, "job_id": None,
        }
    by_strategy: dict = {}
    for e in eligible:
        by_strategy[e["strategy"]] = by_strategy.get(e["strategy"], 0) + 1
    summary = {"selected": len(run_ids), "eligible": len(eligible),
               "excluded": len(excluded),
               "retranscribe": by_strategy.get("RETRANSCRIBE", 0),
               "reuse_derived": by_strategy.get("REUSE_DERIVED", 0),
               "publish_only": by_strategy.get("PUBLISH_ONLY", 0),
               "will_call_whisper": sum(1 for e in eligible if e["will_call_whisper"])}
    # D-4 可复算：汇总恒等于逐项 recompute，不手写第二口径
    assert summary["eligible"] == len(eligible)
    assert summary["excluded"] == len(excluded)
    return 200, {"ok": True, "dry_run": True, "data_root": data_root,
                 "data_root_digest": data_digest,
                 "diagnosis_snapshot_id": str(diag.get("diagnosis_snapshot_id")),
                 "eligible": eligible, "excluded": excluded, "summary": summary,
                 "eta": "未验证", "plan_token": token, "expires_at": expires_at,
                 "message": "预览：选中 %d，可恢复 %d，排除 %d；确认后才执行" % (
                     len(run_ids), len(eligible), len(excluded))}


def _recovery_gate_ok(strategy: str, item: dict, data_root: str) -> tuple[bool, str]:
    """FR-4 六项门逐项复核（执行前，fail-closed）；ok 即允许调用对应旧入口。"""
    run_id = str(item.get("run_id"))
    # 前置：run 仍在快照指纹上（漂移已在 batch 层判，此处再卡一次）
    code, diag = _handle_failure_diagnosis({"data_root": [data_root]})
    if code != 200 or not (diag or {}).get("ok"):
        return False, "诊断不可用，重新诊断后再试"
    cur = {str(i.get("run_id")): i for i in (diag.get("items") or [])}.get(run_id)
    if cur is None:
        return False, "该任务已不在失败/受阻快照内（可能已恢复），刷新后重看"
    if str(cur.get("state_fingerprint")) != str(item.get("fingerprint")):
        return False, "任务状态已漂移，重新诊断后再试"
    if str(cur.get("action_category")) == "SOURCE_LOCATION_REVIEW":
        return False, "原登记路径无文件待确认，不自动重试"
    if strategy == "REUSE_DERIVED":
        # 复用门：_reapply_one 同一 derive 入口；前置 Raw 存在由 exec 内再验
        if str(cur.get("recovery_eligibility")) not in {"AUTO_REUSE", "AUTO_RETRANSCRIBE"}:
            return False, "当前恢复资格不可复用已有文字"
        return True, ""
    if strategy == "PUBLISH_ONLY":
        # 复用门：initial_publish；前置 Render/lineage 有效由 exec 内再验
        if str(cur.get("recovery_eligibility")) not in {"AUTO_PUBLISH", "AUTO_REUSE"}:
            return False, "当前恢复资格不可仅重新入库"
        return True, ""
    if strategy == "RETRANSCRIBE":
        # retry 旧入口未过门（内存排队/需监听中），走直接生命周期路径
        if str(cur.get("recovery_eligibility")) != "AUTO_RETRANSCRIBE":
            return False, "当前恢复资格不可重新转写"
        src = _run_source_path_map(data_root).get(run_id) or ""
        if not (src and os.path.isfile(src)):
            return False, "源文件当前不可读，先确认源位置后再试"
        return True, ""
    return False, "未知恢复策略"


def _exec_retranscribe(data_root: str, run_id: str, work_dir: str) -> dict:
    """RETRANSCRIBE 直接路径（不调用未过门的 retry 内存排队入口）。

    真执行语义：源只读校验 → 落生命周期事件 QUEUED→ACTIVE → 尝试引擎；
    合成/非媒体源如实 FAILED（whisper_calls=0），不冒充成功。
    """
    src = _run_source_path_map(data_root).get(run_id) or ""
    if not (src and os.path.isfile(src)):
        return {"ok": False, "state": "FAILED", "strategy": "RETRANSCRIBE",
                "whisper_calls": 0, "reason": "源文件当前不可读，未调用转写引擎"}
    if os.path.getsize(src) <= 0:
        return {"ok": False, "state": "FAILED", "strategy": "RETRANSCRIBE",
                "whisper_calls": 0, "reason": "源文件为空，引擎未调用"}
    if not _asr_engine_available():
        return {"ok": False, "state": "NEEDS_HUMAN", "strategy": "RETRANSCRIBE",
                "whisper_calls": 0,
                "reason": "本机转写引擎不可用（faster-whisper/CT2 缺失），修复环境后重新诊断"}
    # 引擎在位但批量通道不做整片重转写冒充：如实记录需转监听通道处理
    return {"ok": False, "state": "NEEDS_HUMAN", "strategy": "RETRANSCRIBE",
            "whisper_calls": 0,
            "reason": "源可读但批量通道不代跑整片转写，请走单条重试（监听中）"}


def _exec_reuse_derived(data_root: str, run_id: str, work_dir: str) -> dict:
    """REUSE_DERIVED：复用过门的 reapply 同一 derive 入口，whisper 恒 0。"""
    res = _reapply_one(os.path.abspath(data_root), run_id, None)
    if res.get("ok"):
        return {"ok": True, "state": "SUCCEEDED", "strategy": "REUSE_DERIVED",
                "whisper_calls": 0,
                "rendered_path": res.get("rendered_path"),
                "reason": str(res.get("note") or "已用已有文字重新成稿")}
    if res.get("skipped"):
        return {"ok": False, "state": "SKIPPED", "strategy": "REUSE_DERIVED",
                "whisper_calls": 0, "reason": str(res.get("reason") or "已跳过")}
    return {"ok": False, "state": "FAILED", "strategy": "REUSE_DERIVED",
            "whisper_calls": 0, "reason": str(res.get("error") or "重成稿失败")}


def _exec_publish_only(data_root: str, run_id: str, work_dir: str,
                       vault: str | None = None) -> dict:
    """PUBLISH_ONLY：只调过门的 initial_publish；No-Clobber 五类保护。

    五类：①源视频只读（本函数永不写源）；②旧 Raw/Norm/Render 不覆盖不删除
    （只读 manifest 定位 Render，不写旧版本）；③既有 canonical 永不覆盖
    （存在即 SKIPPED，initial_publish 再判一次）；④用户编辑字节差异最高保护
    （CONFLICT→NEEDS_HUMAN）；⑤DB/历史只追加 receipt，不删改成功记录。
    缺失 vault 不创建 → NEEDS_HUMAN。
    """
    job_dir = os.path.join(_jobs_dir(os.path.abspath(data_root)), run_id)
    manifest_path = os.path.join(job_dir, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return {"ok": False, "state": "FAILED", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0, "reason": "任务产物清单缺失，无法仅重新入库"}
    rend_rev = str(manifest.get("render_revision_id") or "")
    rendered = None
    for cand in (manifest.get("rendered_path"),
                 os.path.join(job_dir, "render", "%s.md" % rend_rev) if rend_rev else None):
        if isinstance(cand, str) and cand and os.path.isfile(cand):
            rendered = os.path.abspath(cand)
            break
    if not rendered:
        return {"ok": False, "state": "FAILED", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0, "reason": "成稿文件缺失，无法仅重新入库"}
    if not (isinstance(vault, str) and vault.strip() and os.path.isdir(vault)):
        return {"ok": False, "state": "NEEDS_HUMAN", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0,
                "reason": "未配置可用笔记库（缺失不创建），配置后再试"}
    try:
        con = _open_rw(os.path.abspath(data_root))
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "state": "FAILED", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0, "reason": "状态库不可读：%s" % (_err_text(exc),)}
    try:
        from stage4.publish import initial_publish  # noqa: E402
        # ③预判：目标已存在只判不写（initial_publish 内再判一次，双保险）
        vault_real = os.path.realpath(vault)
        try:
            rows = con.execute(
                "SELECT canonical_output_path FROM publish_records"
                " WHERE render_revision_id=? ORDER BY rowid DESC LIMIT 1",
                (rend_rev,)).fetchone() if rend_rev else None
        except (sqlite3.Error, OSError):
            rows = None
        if rows and rows[0] and os.path.exists(str(rows[0])):
            _append_manifest_receipt(job_dir, {
                "stage": "recovery-batch", "state": "PUBLISH_BLOCKED",
                "run_id": run_id, "verdict": "目标已存在，未覆盖",
                "created_at": _utc_now_iso(), "whisper_calls": 0,
                "rendered_path": rendered,
                "canonical_output_path": str(rows[0])})
            return {"ok": False, "state": "SKIPPED", "strategy": "PUBLISH_ONLY",
                    "whisper_calls": 0, "reason": "目标笔记已存在，未覆盖"}
        source_rel = os.path.relpath(
            rendered, vault_real) if os.path.commonpath(
                [vault_real, os.path.realpath(rendered)]) == vault_real else None
        if source_rel is None:
            # 成稿在数据目录内：按文件名映射到库根（永不覆盖既有文件）
            source_rel = os.path.basename(rendered)
        target = os.path.join(vault_real, source_rel)
        if os.path.exists(target):
            return {"ok": False, "state": "SKIPPED", "strategy": "PUBLISH_ONLY",
                    "whisper_calls": 0, "reason": "目标笔记已存在，未覆盖"}
        pub = initial_publish(con, job_dir, rend_rev, vault_real, source_rel)
        status = str(pub.get("status") or "")
        if status == "PUBLISHED":
            _append_manifest_receipt(job_dir, {
                "stage": "recovery-batch", "state": "PUBLISHED",
                "run_id": run_id,
                "verdict": "批量仅重新入库：%s" % (pub.get("canonical_output_path"),),
                "created_at": _utc_now_iso(), "whisper_calls": 0,
                "rendered_path": rendered,
                "canonical_output_path": pub.get("canonical_output_path")})
            return {"ok": True, "state": "SUCCEEDED", "strategy": "PUBLISH_ONLY",
                    "whisper_calls": 0,
                    "canonical_output_path": pub.get("canonical_output_path"),
                    "reason": "已新建入库，未覆盖既有笔记"}
        writes = int(pub.get("canonical_writes") or 0)
        if writes != 0:
            return {"ok": False, "state": "NEEDS_HUMAN",
                    "strategy": "PUBLISH_ONLY", "whisper_calls": 0,
                    "reason": "入库写断言异常（writes!=0），已拦截"}
        if status == "BLOCKED_OUTPUT_CONFLICT":
            return {"ok": False, "state": "NEEDS_HUMAN",
                    "strategy": "PUBLISH_ONLY", "whisper_calls": 0,
                    "reason": "库内笔记你改过（字节差异），未覆盖，需人工确认"}
        return {"ok": False, "state": "SKIPPED", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0,
                "reason": "入库跳过（%s），库内未动" % (status or "BLOCKED",)}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "state": "FAILED", "strategy": "PUBLISH_ONLY",
                "whisper_calls": 0, "reason": "入库失败：%s" % (_err_text(exc),)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _handle_retry_batch_post(body: bytes) -> tuple[int, dict]:
    """FR-6 确认执行：confirm:true + token + 指纹复核；幂等同 token 单 job。"""
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
        confirm = _take_bool(params, "confirm", False)
        token = _take_str(params, "plan_token", "", allow_empty=True)
        run_ids = _take_str_list(params, "run_ids", allow_empty=True)
        _reject_duplicates(run_ids, "run_ids")
        vault_s = _take_str(params, "ob_vault_root", "", allow_empty=True) or None
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if confirm is not True:
        return 400, {"ok": False,
                     "error": "批量恢复须在预览后确认（confirm:true），请先预览再确认"}
    if not token:
        return 400, {"ok": False, "error": "缺少预览令牌，请先预览再确认"}
    run_ids = [str(r).strip() for r in run_ids]
    import time as _time
    import hashlib as _hl
    digest = _hl.sha256(token.strip().encode()).hexdigest()
    with _RECOVERY_PLAN_LOCK:
        plan = _RECOVERY_PLANS.get(digest)
        if plan is not None and plan.get("job_id"):
            existing = str(plan["job_id"])
        else:
            existing = None
            if plan is not None:
                _reserved = _recovery_job_id()
                _RECOVERY_PLANS[digest] = {**plan, "job_id": _reserved}
                plan = _RECOVERY_PLANS[digest]
    data_abs = os.path.abspath(data_root)
    if plan is None:
        return 409, {"ok": False,
                     "error": "预览已过期或不存在（令牌未知），请重新预览"}
    if int(_time.time()) > int(plan.get("expires_at") or 0):
        with _RECOVERY_PLAN_LOCK:
            _RECOVERY_PLANS.pop(digest, None)
        return 409, {"ok": False, "error": "预览已过期（10 分钟），请重新预览"}
    # 目录身份用 realpath 口径：符号链接/尾斜杠/.. 指向同一目录不该误判成“换了目录”
    if os.path.realpath(str(plan.get("data_root"))) != os.path.realpath(data_abs):
        return 409, {"ok": False, "error": "数据目录与预览不一致，零执行；请重选目录后重新预览"}
    if existing:
        code, job = _recovery_job_read(data_abs, existing)
        if code == 200:
            job = dict(job)
            job["idempotent"] = True
            return 202, {"ok": True, **job,
                         "message": "该预览已执行过，返回既有任务（未重复执行）"}
        # 已占位但文件尚不可读 = 同 token 另一次执行正在进行：409，不建第二个 job
        return 409, {"ok": False,
                     "error": "该预览正在执行中（零新增），请稍后用任务状态查询"}
    if set(run_ids) != set(plan.get("items") or {}):
        return 409, {"ok": False,
                     "error": "执行集合与预览不一致（零执行），请用预览返回的集合确认"}
    # 指纹复核：逐项重算，漂移即 409 零执行
    code, diag = _handle_failure_diagnosis({"data_root": [data_root]})
    if code != 200 or not (diag or {}).get("ok"):
        return 409, {"ok": False, "error": "诊断不可用（零执行），重新诊断后再试"}
    by_id = {str(i.get("run_id")): i for i in (diag.get("items") or [])}
    for rid, want in (plan.get("items") or {}).items():
        cur = by_id.get(str(rid))
        if cur is None or str(cur.get("state_fingerprint")) != str(
                want.get("fingerprint")):
            return 409, {"ok": False,
                         "error": "任务 %s 状态已漂移（零执行），请重新预览" % (rid,)}
    job_id = str(plan.get("job_id")) or _recovery_job_id()
    job_path = os.path.join(_recovery_jobs_dir(data_abs), "%s.json" % job_id)
    job: dict = {
        "job_id": job_id, "data_root_digest": str(plan.get("data_root_digest")),
        "diagnosis_snapshot_id": str(plan.get("snapshot_id")),
        "token_digest": digest, "state": "RUNNING",
        "total": len(run_ids), "done": 0, "recovered": 0, "still_failed": 0,
        "source_location_blocked": 0, "skipped": 0, "needs_human": 0,
        # P1-3 可复算自检：刚建 job 时逐项结果为 0，只有「本来就零目标」才 balanced
        "interrupted": 0, "whisper_calls": 0, "counted": 0,
        "balanced": len(run_ids) == 0,
        "started_at": _diag_now(), "finished_at": None, "error": None,
        "results": [], "current": None,
    }
    try:
        _recovery_atomic_write(job_path, job)
    except OSError as exc:
        with _RECOVERY_PLAN_LOCK:
            cur = _RECOVERY_PLANS.get(digest)
            if cur is not None and str(cur.get("job_id")) == job_id:
                _RECOVERY_PLANS[digest] = {**cur, "job_id": None}
        return 500, {"ok": False,
                     "error": "批量任务落盘失败（零执行）：%s，检查磁盘空间/目录权限后重试"
                              % (_err_text(exc),)}
    work_base = os.path.join(_recovery_jobs_dir(data_abs), job_id, "work")
    for rid in run_ids:
        want = plan["items"][rid]
        strategy = str(want.get("strategy"))
        job["current"] = rid
        gate_ok, gate_msg = _recovery_gate_ok(strategy, {"run_id": rid, **want},
                                              data_abs)
        if not gate_ok:
            entry = _recovery_result_entry({
                "run_id": rid, "strategy": strategy, "ok": False,
                "state": "NEEDS_HUMAN", "whisper_calls": 0,
                "reason": "复用门未过：%s" % gate_msg,
                "finished_at": _diag_now()}, gate_ok=False)
        else:
            work_dir = os.path.join(work_base, rid)
            try:
                os.makedirs(work_dir, exist_ok=True)
            except OSError:
                pass
            try:
                if strategy == "RETRANSCRIBE":
                    entry = _recovery_result_entry({
                        "run_id": rid, **_exec_retranscribe(
                            data_abs, rid, work_dir),
                        "finished_at": _diag_now()})
                elif strategy == "REUSE_DERIVED":
                    entry = _recovery_result_entry({
                        "run_id": rid, **_exec_reuse_derived(
                            data_abs, rid, work_dir),
                        "finished_at": _diag_now()})
                else:
                    entry = _recovery_result_entry({
                        "run_id": rid, **_exec_publish_only(
                            data_abs, rid, work_dir, vault_s),
                        "finished_at": _diag_now()})
            except Exception as exc:  # noqa: BLE001  (逐项异常不炸整批)
                entry = _recovery_result_entry({
                    "run_id": rid, "strategy": strategy, "ok": False,
                    "state": "FAILED", "whisper_calls": 0,
                    "reason": "执行异常：%s" % (_err_text(exc),),
                    "finished_at": _diag_now()}, gate_ok=False)
        job["results"].append(entry)
        job["whisper_calls"] = sum(int(r.get("whisper_calls") or 0)
                                   for r in job["results"])
        # D-4/D-9 可复算：五桶由唯一归类函数复算，恒等于逐项 recompute；
        # `done` 同步为逐项计数（= 各桶之和），total 独立登记供自检。
        _recovery_apply_counts(job)
        try:
            _recovery_atomic_write(job_path, job)
        except OSError:
            pass
    job["current"] = None
    job["state"] = "SUCCEEDED" if (
        job["still_failed"] == 0 and job["needs_human"] == 0) else "DONE_PARTIAL"
    job["finished_at"] = _diag_now()
    try:
        _recovery_atomic_write(job_path, job)
    except OSError as exc:
        job = dict(job)
        job["error"] = "终态落盘失败：%s（逐项结果仍以此前落盘为准）" % (_err_text(exc),)
    out = dict(job)
    out["ok"] = True
    return 202, out


def _recovery_job_read(data_root: str, job_id: str) -> tuple[int, dict]:
    data_abs = os.path.abspath(str(data_root or ""))
    if not _recovery_safe_job_id(job_id):
        return 400, {"ok": False, "error": "任务编号不合法"}
    path = os.path.join(_recovery_jobs_dir(data_abs), "%s.json" % job_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            job = json.load(fh)
    except FileNotFoundError:
        return 404, {"ok": False, "error": "批量任务不存在（可能跨目录），请检查数据目录"}
    except (OSError, ValueError) as exc:
        # 半文件/不可读 → INTERRUPTED，不自动续跑。
        # P1-3 统计统一：原始目标数已读不出来，按「至少 1 个未跑完单位」记；
        # done=0（读到 0 条逐项）＋ counted=1（1 条计入中断）＋ balanced=True。
        return 200, {"ok": True, "job_id": job_id, "state": "INTERRUPTED",
                     "total": 1, "done": 0, "recovered": 0, "still_failed": 0,
                     "source_location_blocked": 0, "skipped": 0,
                     "needs_human": 0, "interrupted": 1,
                     "counted": 1, "balanced": True,
                     "results": [], "current": None,
                     "elapsed_seconds": 0,
                     "error": "任务文件不完整，标为中断（不自动续跑）：%s"
                              % (_err_text(exc),)}
    if not isinstance(job, dict):
        return 200, {"ok": True, "job_id": job_id, "state": "INTERRUPTED",
                     "total": 1, "done": 0, "recovered": 0, "still_failed": 0,
                     "skipped": 0, "needs_human": 0, "interrupted": 1,
                     "counted": 1, "balanced": True,
                     "results": [], "current": None, "elapsed_seconds": 0,
                     "error": "任务文件不是对象，标为中断（不自动续跑）"}
    # P1-2/D-9 目录绑定：job 自带的 data_root 摘要必须与本次请求的数据目录一致，
    # 否则一律结构化失败且不返回任务内容（防串目录/被搬过来的 job 文件）。
    # 摘要缺失/空/null/非法一律 409（不做“空值=放行”的旁路）；realpath 口径
    # 命中同一目录的符号链接/尾斜杠/`..` 写法；旧 abspath 口径仅作显式兼容白名单。
    stored_digest = job.get("data_root_digest")
    if not _recovery_digest_ok(stored_digest):
        return 409, {"ok": False, "job_id": job_id,
                     "error": "任务文件缺少数据目录绑定（零渲染），请重新预览后新建任务"}
    if str(stored_digest) not in (_recovery_root_digest(data_abs),
                                  _recovery_root_digest_legacy(data_abs)):
        return 409, {"ok": False, "job_id": job_id,
                     "error": "该任务属于另一个数据目录（零渲染），请核对数据目录后重查"}
    if str(job.get("state")) not in RECOVERY_JOB_FINAL and job.get("state") != "DONE_PARTIAL":
        # 未终态（服务重启遗留 RUNNING）→ INTERRUPTED，不自动续跑；尽力持久化。
        # P1-3：`interrupted` 由 `_recovery_apply_counts` 按「登记目标 − 已落盘逐项」
        # 复算（不再 `or 1` 硬塞），所以 job 级状态与单位计数不再互相打架。
        job = dict(job)
        job["state"] = "INTERRUPTED"
        job["error"] = "服务重启前未终态，标为中断（不自动续跑），重新诊断后新建计划"
        _recovery_apply_counts(job)
        try:
            _recovery_atomic_write(path, job)
        except OSError:
            pass
    out = dict(job)
    # P1-2 字段一致：老 job 文件也按固定契约返回 results[]
    out["results"] = _normalize_recovery_results(job.get("results"))
    # P1-3 统计统一：老 job 文件也走同一归类函数复算，终态恒满足
    # total == recovered + still_failed + skipped + needs_human + interrupted
    _recovery_apply_counts(out)
    out["elapsed_seconds"] = _elapsed_seconds(job.get("started_at"),
                                             job.get("finished_at"))
    out["ok"] = True
    return 200, out


def _handle_retry_batch_status(query: dict) -> tuple[int, dict]:
    """按 data_root + job_id 双键取 job；缺任一或跨目录一律结构化人话失败。"""
    try:
        data_root = _query_data_root(query, "", "data_root")
        job_id = _query_str(query, "job_id").strip()
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if not data_root:
        return 400, {"ok": False,
                     "error": "缺少数据目录 data_root，无法定位批量任务（零渲染）"}
    if not job_id:
        return 400, {"ok": False, "error": "缺少任务编号 job_id（零渲染）"}
    return _recovery_job_read(data_root, job_id)


def _run_source_path_map(data_root: str) -> dict:
    """{run_id: current_path}（只读；sources 缺失/不可读回空，fail-open）。"""
    con = _open_ro(data_root)
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT pr.run_id, s.current_path, s.path_identity_key"
            " FROM processing_runs pr LEFT JOIN sources s"
            " ON pr.source_id=s.source_id"
        ).fetchall()
        out = {}
        for r in rows:
            try:
                out[str(r[0])] = r[1] or r[2]
            except Exception:
                continue
        return out
    except Exception:
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _filter_status_runs(snap: dict, data_root: str, input_root: str) -> dict:
    """P0-2：recent_runs 只留归属当前 input 的行（孤儿 run 可见，fail-open）。"""
    try:
        runs = snap.get("recent_runs") or []
        mapping = _run_source_path_map(data_root)
        if not mapping:
            snap["input_root"] = input_root
            snap["filtered"] = False
            return snap
        kept = []
        for r in runs:
            try:
                rid = str(r.get("run_id"))
            except Exception:
                continue
            if rid not in mapping:
                kept.append(r)  # 映射缺失时可见，不静默丢
                continue
            if _is_under_root(str(mapping[rid] or ""), input_root):
                kept.append(r)
        snap["recent_runs"] = kept
        snap["input_root"] = input_root
        snap["filtered"] = True
        return snap
    except Exception:
        try:
            snap["filtered"] = False
        except Exception:
            pass
        return snap


def _filter_merged_by_input(merged: dict, mem_ids: set, data_root: str,
                            input_root: str) -> dict:
    """P0-2：worker 终态按当前 input 过滤（磁盘孤儿排除，内存孤儿保留）。"""
    try:
        mapping = _run_source_path_map(data_root)
        if not mapping:
            return merged
        out = {}
        for rid, v in merged.items():
            if rid in mapping:
                if _is_under_root(str(mapping[rid] or ""), input_root):
                    out[rid] = v
            elif rid in mem_ids:
                out[rid] = v  # 本 session 内存真相保留
        return out
    except Exception:
        return merged


def _count_job_dirs(data_root: str, run_ids) -> int:
    """本 data_root 下 jobs/<run_id> 目录计数（清空代价预览用，只读）。"""
    try:
        base = _jobs_dir(data_root)
        n = 0
        for rid in run_ids:
            try:
                if os.path.isdir(os.path.join(base, str(rid))):
                    n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


CLEAR_AVG_SEC_PER_VIDEO = 120  # 清空代价估算：每条视频重转均耗时（秒，估算值）


def _est_retranscribe_minutes(n_runs) -> int:
    """按条数估重转分钟（ceil；估算值，前端明示“约”）。"""
    try:
        n = int(n_runs or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0
    return int((n * CLEAR_AVG_SEC_PER_VIDEO + 59) // 60)


def _clear_plan(con, data_root: str, input_root: str,
                only_failed: bool = False) -> dict:
    """UX2-P0-2：只读计算清空计划（不写库、不删盘），预览与实清共用。

    返回 exists=False 表示本目录无任何可清记录；否则给出去重后的待删 id：
      run_ids / srcs_to_del / cand_ids / norm_ids / rend_ids / pub_ids /
      art_ids / null_arch_ids / entity_ids，以及
      total/success/failed/pending/fail_run_ids/kept_success 计数。
    only_failed=True 时只圈失败 run 及其修订链；同日录源若另有成功 run，
    该源保留（成功记录与产物不动）。
    """
    out = {"exists": False, "run_ids": [], "srcs_to_del": set(),
           "cand_ids": [], "norm_ids": [], "rend_ids": [], "pub_ids": [],
           "art_ids": [], "null_arch_ids": [], "entity_ids": set(),
           "total": 0, "success": 0, "failed": 0, "pending": 0,
           "fail_run_ids": [], "kept_success": 0}
    try:
        path_rows = con.execute(
            "SELECT source_id, current_path, path_identity_key FROM sources"
        ).fetchall()
    except Exception:
        return out
    scope_srcs: set = set()
    unatt_srcs: set = set()
    for r in path_rows:
        try:
            cur, pik = r[1], r[2]
            if (cur and _is_under_root(str(cur), input_root)) or (
                    not cur and pik and _is_under_root(str(pik), input_root)):
                scope_srcs.add(r[0])
            cur_s = str(cur).strip() if cur else ""
            pik_s = str(pik).strip() if pik else ""
            if not cur_s and not pik_s:
                unatt_srcs.add(r[0])
        except Exception:
            continue
    eff_srcs = set(scope_srcs) | set(unatt_srcs)
    try:
        all_src_ids = {rr[0] for rr in con.execute(
            "SELECT source_id FROM sources").fetchall()}
    except Exception:
        all_src_ids = set(eff_srcs)
    try:
        run_rows = con.execute(
            "SELECT run_id, source_id, status FROM processing_runs").fetchall()
    except Exception:
        run_rows = []
    run_status: dict = {}
    source_runs: dict = {}
    orphan_run_ids: list = []
    for rr in run_rows:
        try:
            rid = str(rr[0])
            sid = rr[1] if len(rr) > 1 else None
            status = str(rr[2] or "") if len(rr) > 2 else ""
        except Exception:
            continue
        run_status[rid] = status
        if sid is None or sid not in all_src_ids:
            orphan_run_ids.append(rid)
        else:
            source_runs.setdefault(sid, []).append(rid)
    try:
        disk = _scan_disk_states(str(data_root))
    except Exception:
        disk = {}

    def _run_failed(rid: str) -> bool:
        d = disk.get(rid)
        if isinstance(d, dict) and str(d.get("state") or "") in FAIL_STATES:
            return True
        s = run_status.get(rid, "")
        return s.startswith("FAILED") or s == "NO_SPEECH_DETECTED"

    def _run_done(rid: str) -> bool:
        d = disk.get(rid)
        return bool(isinstance(d, dict)
                    and str(d.get("state") or "") in DONE_STATES)

    scope_run_ids: list = []
    for sid in eff_srcs:
        scope_run_ids.extend(source_runs.get(sid, []))
    scope_run_ids.extend(orphan_run_ids)
    fail_set = {rid for rid in scope_run_ids if _run_failed(rid)}
    done_set = {rid for rid in scope_run_ids if _run_done(rid)}
    out["total"] = len(scope_run_ids)
    out["success"] = len(done_set)
    out["failed"] = len(fail_set)
    _pend = len(scope_run_ids) - len(fail_set) - len(done_set)
    out["pending"] = _pend if _pend > 0 else 0
    out["fail_run_ids"] = [rid for rid in scope_run_ids if rid in fail_set]
    out["kept_success"] = len(done_set)
    if only_failed:
        out["run_ids"] = list(out["fail_run_ids"])
        failed_srcs = {sid for sid, rids in source_runs.items()
                       if sid in eff_srcs and any(rid in fail_set for rid in rids)}
        keep_srcs = {sid for sid in failed_srcs
                     if any(rid not in fail_set for rid in source_runs.get(sid, []))}
        out["srcs_to_del"] = failed_srcs - keep_srcs
    else:
        out["run_ids"] = list(scope_run_ids)
        out["srcs_to_del"] = set(eff_srcs)
    runs_del = set(out["run_ids"])
    if not runs_del and not out["srcs_to_del"]:
        return out
    # 修订链归属：raw_artifact_id = raw_<run_id>
    try:
        norm_rows = con.execute(
            "SELECT normalization_revision_id, normalized_artifact_id,"
            " raw_artifact_id FROM normalization_revisions").fetchall()
    except Exception:
        norm_rows = []
    norm_ids: list = []
    norm_artifacts: list = []
    for r in norm_rows:
        try:
            raw = r[2]
            rid = (raw[len("raw_"):]
                   if isinstance(raw, str) and raw.startswith("raw_") else None)
        except Exception:
            rid = None
        if rid in runs_del:
            norm_ids.append(r[0])
            norm_artifacts.append(r[1])
    out["norm_ids"] = norm_ids
    try:
        rend_rows = con.execute(
            "SELECT render_revision_id FROM render_revisions"
            " WHERE normalized_artifact_id IN (%s)"
            % ",".join("?" for _ in norm_artifacts),
            norm_artifacts).fetchall() if norm_artifacts else []
    except Exception:
        rend_rows = []
    out["rend_ids"] = [r[0] for r in rend_rows]
    try:
        pub_rows = con.execute(
            "SELECT publish_record_id FROM publish_records"
            " WHERE render_revision_id IN (%s)"
            % ",".join("?" for _ in out["rend_ids"]),
            out["rend_ids"]).fetchall() if out["rend_ids"] else []
    except Exception:
        pub_rows = []
    out["pub_ids"] = [r[0] for r in pub_rows]
    # artifacts：全清按源或 run 归属；只清失败只按 run 归属（不误伤成功源产物）
    try:
        if only_failed:
            art_rows = con.execute(
                "SELECT artifact_id FROM artifacts WHERE run_id IN (%s)"
                % ",".join("?" for _ in out["run_ids"]),
                list(out["run_ids"])).fetchall() if out["run_ids"] else []
        elif eff_srcs:
            qmarks = ",".join("?" for _ in eff_srcs)
            art_rows = con.execute(
                "SELECT artifact_id FROM artifacts WHERE source_id IN (%s)%s"
                % (qmarks,
                   (" OR run_id IN (%s)" % ",".join("?" for _ in out["run_ids"]))
                   if out["run_ids"] else ""),
                list(eff_srcs) + list(out["run_ids"])).fetchall()
        elif out["run_ids"]:
            art_rows = con.execute(
                "SELECT artifact_id FROM artifacts WHERE run_id IN (%s)"
                % ",".join("?" for _ in out["run_ids"]),
                list(out["run_ids"])).fetchall()
        else:
            art_rows = []
    except Exception:
        art_rows = []
    out["art_ids"] = [r[0] for r in art_rows]
    # discovery_candidates：全清按源或路径归属；只清失败只按待删源归属（保守）
    try:
        cand_rows = con.execute(
            "SELECT candidate_id, path_identity_key, source_id"
            " FROM discovery_candidates").fetchall()
    except Exception:
        cand_rows = []
    cand_ids: list = []
    for r in cand_rows:
        try:
            sid, pik = r[2], r[1]
        except Exception:
            continue
        if only_failed:
            if sid in out["srcs_to_del"]:
                cand_ids.append(r[0])
        elif sid in eff_srcs or (pik and _is_under_root(str(pik), input_root)):
            cand_ids.append(r[0])
    out["cand_ids"] = cand_ids
    # NULL archive：仅全清带走（门清一致）；只清失败不动
    null_arch_ids: list = []
    if not only_failed:
        try:
            null_arch_rows = con.execute(
                "SELECT archive_commit_id FROM archive_commits"
                " WHERE source_id IS NULL").fetchall()
            null_arch_ids = [rr[0] for rr in null_arch_rows]
        except Exception:
            null_arch_ids = []
    out["null_arch_ids"] = null_arch_ids
    out["entity_ids"] = (set(cand_ids) | set(out["srcs_to_del"])
                         | set(out["run_ids"]) | set(norm_ids)
                         | set(out["rend_ids"]) | set(out["pub_ids"])
                         | set(out["art_ids"]) | set(null_arch_ids))
    out["exists"] = bool(out["run_ids"] or out["srcs_to_del"]
                         or out["cand_ids"] or null_arch_ids)
    return out


def _scoped_runs_summary(data_root: str, input_root: str) -> dict:
    """UX2-P0-1：全量（不受 limit 限制）任务分桶 + 分目录，供列表头。

    total=当前范围全部任务数；done/failed/pending 按磁盘终态优先、DB
    状态兜底分桶；groups 按输入目录尾段分组（未监听展示归属用）。
    与 _filter_status_runs 同口径：孤儿/映射缺失的 run 视为可见（不静默丢）。
    只读 fail-open：任何异常回空壳（total=0），不拦接口。
    """
    out = {"total": 0, "done": 0, "failed": 0, "pending": 0,
           "groups": [], "shown": 0, "truncated": False}
    con = None
    try:
        mapping = _run_source_path_map(data_root)
        con = _open_ro(data_root)
        if con is None:
            return out
        rows = con.execute(
            "SELECT run_id, source_id, status FROM processing_runs").fetchall()
        try:
            disk = _scan_disk_states(data_root)
        except Exception:
            disk = {}
        allow_all = not input_root
        groups: dict = {}
        total = done = failed = pending = 0
        for r in rows:
            try:
                rid = str(r[0])
                status = str(r[2] or "")
            except Exception:
                continue
            path = mapping.get(rid) if mapping else None
            if (not allow_all and path is not None
                    and not _is_under_root(str(path or ""), input_root)):
                continue
            total += 1
            st = None
            d = disk.get(rid)
            if isinstance(d, dict):
                st = str(d.get("state") or "")
            if st in DONE_STATES:
                bucket = "done"
            elif st in FAIL_STATES:
                bucket = "failed"
            elif st is None and (status.startswith("FAILED")
                                 or status == "NO_SPEECH_DETECTED"):
                bucket = "failed"
            else:
                bucket = "pending"
            if bucket == "done":
                done += 1
            elif bucket == "failed":
                failed += 1
            else:
                pending += 1
            dt = (_dir_tail(os.path.dirname(str(path)))
                  if path else "（无归属目录）")
            g = groups.get(dt)
            if g is None:
                g = {"dir_tail": dt, "total": 0, "done": 0,
                     "failed": 0, "pending": 0}
                groups[dt] = g
            g["total"] += 1
            g[bucket] += 1
        out.update({"total": total, "done": done, "failed": failed,
                    "pending": pending, "groups": list(groups.values())})
        return out
    except Exception:
        return out
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# ------------------------------------------ FR-12/HD-6=A 完成列表分层与 cursor 历史

COMPLETED_DEFAULT_LIMIT = 20
COMPLETED_MAX_LIMIT = 200
_CURSOR_SIG_PREFIX = "v2o-cursor:"


def _completed_cursor_encode(finished_at: str, updated_at: str,
                             run_id: str) -> str:
    """FR-12：cursor 编码排序键（finished_at|updated_at + run_id），带校验和。

    不用易漂移 offset；payload 校验和不匹配即视为被篡改（decode 侧 400）。
    """
    payload = json.dumps(
        {"f": str(finished_at or ""), "u": str(updated_at or ""),
         "r": str(run_id or "")},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hashlib.sha256((_CURSOR_SIG_PREFIX + raw).encode("utf-8")).hexdigest()[:12]
    return raw + "." + sig


def _completed_cursor_decode(token: str) -> tuple:
    """校验并解出 cursor 排序键（finished_at, updated_at, run_id）；坏/篡改 400。"""
    text = str(token or "")
    if "." not in text:
        raise ParamError("完成列表游标格式不正确，请回列表点「展开更早」重新加载")
    raw, sig = text.rsplit(".", 1)
    expect = hashlib.sha256(
        (_CURSOR_SIG_PREFIX + raw).encode("utf-8")).hexdigest()[:12]
    if not hmac.compare_digest(str(sig), expect):
        raise ParamError("完成列表游标校验失败（可能被改动），请回列表点「展开更早」重新加载")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        f = str(payload.get("f") or "")
        u = str(payload.get("u") or "")
        r = str(payload.get("r") or "")
    except Exception:
        raise ParamError("完成列表游标解析失败，请回列表点「展开更早」重新加载")
    if not r:
        raise ParamError("完成列表游标缺少排序键，请回列表点「展开更早」重新加载")
    return f, u, r


def _completed_runs_view(data_root: str, input_root: str) -> list:
    """FR-12：全量完成项视图（不受 limit 限制），按 (eff_ts, run_id) DESC 排。

    完成=磁盘终态 PUBLISHED/RENDER_ONLY（与 _scoped_runs_summary 的 done
    分桶同口径，DONE_STATES）；finished_at 取库列 completed_at，缺了回退
    updated_at；并列时间戳由 run_id 决胜。范围过滤同口径：孤儿（映射缺失）
    可见，不静默丢。只读，fail-open（库打不开回空列表）。
    """
    con = _open_ro(data_root)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT run_id, source_id, status, created_at, updated_at,"
            " completed_at FROM processing_runs"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass
    try:
        disk = _scan_disk_states(data_root)
    except Exception:
        disk = {}
    try:
        mapping = _run_source_path_map(data_root)
    except Exception:
        mapping = {}
    allow_all = not input_root
    out: list = []
    for r in rows:
        try:
            rid = str(r[0])
        except Exception:
            continue
        d = disk.get(rid)
        if not (isinstance(d, dict) and str(d.get("state") or "") in DONE_STATES):
            continue
        path = mapping.get(rid) if mapping else None
        if (not allow_all and path is not None
                and not _is_under_root(str(path or ""), input_root)):
            continue
        finished_at = str(r[5] or "")
        updated_at = str(r[4] or "")
        out.append({
            "run_id": rid, "source_id": r[1], "status": r[2],
            "created_at": r[3], "updated_at": updated_at,
            "completed_at": finished_at or None,
            "finished_at": finished_at,
            "state": str(d.get("state") or ""),
            "verdict": str(d.get("verdict") or ""),
            "rendered_path": d.get("rendered_path"),
            "canonical_output_path": d.get("canonical_output_path"),
            "source_path": path,
            "_eff": finished_at or updated_at,
        })
    out.sort(key=lambda e: (e["_eff"], e["run_id"]), reverse=True)
    return out


def _attach_completed_view(snap: dict, data_root: str, input_root: str,
                           query: dict) -> dict:
    """FR-12/D-15：完成项单独分页＋recent_runs 完成行打标。

    recent_runs 原样返回（当前/排队/失败/受阻始终全量可见，不受 limit 影响），
    只给完成行补 completed:true 标记供前端分层；分页字段
    completed_total/completed_limit/completed_page/next_cursor 挂在返回 dict。
    坏/被篡改 completed_cursor 抛 ParamError（do_GET 统一回 400，不静默）。
    completed_limit 非整数同样 400；越界按既有 limit 口径钳制（1..200）。
    """
    limit = _query_int(query, "completed_limit", COMPLETED_DEFAULT_LIMIT)
    limit = max(1, min(limit, COMPLETED_MAX_LIMIT))
    cursor_token = _query_str(query, "completed_cursor").strip()
    entries = _completed_runs_view(data_root, input_root)
    total = len(entries)
    done_ids = {e["run_id"] for e in entries}
    try:
        runs = snap.get("recent_runs")
        if isinstance(runs, list):
            for r in runs:
                if isinstance(r, dict) and str(r.get("run_id") or "") in done_ids:
                    r["completed"] = True
    except Exception:
        pass
    if cursor_token:
        f, u, rid = _completed_cursor_decode(cursor_token)
        cur_eff = f or u
        after = [e for e in entries if (e["_eff"], e["run_id"]) < (cur_eff, rid)]
    else:
        after = entries
    page = after[:limit]
    next_cursor = None
    if page and len(after) > len(page):
        last = page[-1]
        next_cursor = _completed_cursor_encode(
            last["finished_at"], last["updated_at"], last["run_id"])
    page_rows = []
    for e in page:
        row = {k: e[k] for k in ("run_id", "source_id", "status",
                                 "created_at", "updated_at", "completed_at",
                                 "state", "verdict", "rendered_path",
                                 "canonical_output_path")}
        row["completed"] = True
        sp = e.get("source_path")
        if isinstance(sp, str) and sp.strip():
            sp = sp.strip()
            d = os.path.dirname(sp)
            row["source_path"] = sp
            row["source_dir"] = d
            row["source_dir_tail"] = _dir_tail(d)
            row["source_filename"] = os.path.basename(sp) or "未知文件"
        else:
            row["source_filename"] = "未知文件"
        page_rows.append(row)
    return {"completed_total": total, "completed_limit": limit,
            "completed_page": page_rows, "next_cursor": next_cursor}


def _finder_url(abs_path: str) -> str:
    """P0-4：在访达中打开（file:// + 全编码，复制路径兜底由前端做）。"""
    return "file://" + urllib.parse.quote(os.path.abspath(abs_path), safe="/:")


def _obsidian_url(vault_root: str | None,
                  abs_path: str | None) -> tuple:
    """P0-4：在 OB 中打开。返回 (url|None, reason|None)。

    obsidian://open?vault={vault名}&file={相对路径}，vault 名取
    ob_vault_root basename；未配 vault / 非入库输出均回 reason。
    """
    if not vault_root:
        return None, "未配置笔记库，在上方填入笔记库目录后重起即用"
    if not abs_path:
        return None, "该任务还没有可打开的笔记"
    try:
        vault_real = os.path.realpath(vault_root)
        path_real = os.path.realpath(abs_path)
        if os.path.commonpath([vault_real, path_real]) != vault_real:
            return None, "该稿尚未入库（还在数据目录），入库后可用"
        rel = os.path.relpath(path_real, vault_real)
        vault_name = os.path.basename(os.path.abspath(vault_real))
        url = ("obsidian://open?vault=%s&file=%s"
               % (urllib.parse.quote(vault_name, safe=""),
                  urllib.parse.quote(rel, safe="")))
        return url, None
    except (ValueError, OSError) as exc:
        return None, "路径不在同一本机盘：%s" % (_err_text(exc),)


def _note_entry_for_run(run_id: str, data_root: str | None) -> tuple:
    """P0-3：找该 run 的终态条目。返回 (entry|None, from_mem: bool)。"""
    with _state_lock:
        mem_all = list(_worker["processed"])
        cur = dict(_worker["current"]) if _worker["current"] else None
    for p in reversed(mem_all):
        try:
            if isinstance(p, dict) and str(p.get("run_id")) == run_id:
                return dict(p), True
        except Exception:
            continue
    if data_root:
        try:
            disk = _scan_disk_states(str(data_root))
            if run_id in disk:
                return dict(disk[run_id]), False
        except Exception:
            pass
    return None, False


def _stage_text_zh(run_id: str, entry: dict | None,
                   data_root: str | None) -> str:
    """P0-3：无 md 时的所处阶段人话。"""
    with _state_lock:
        cur = dict(_worker["current"]) if _worker["current"] else None
    if cur and cur.get("run_id") == run_id:
        stage = str(cur.get("stage") or "")
        if stage == "发现":
            return "正在发现这个视频，稍等即开始听写"
        if stage in ("听写中", "整理中", "成稿中", "入库中"):
            return "正在%s：%s（第 %s 步/共 5 步：发现→听写→整理→成稿→入库）" % (
                stage, cur.get("filename") or run_id,
                STAGE_STEP.get(stage, "?"))
        return "正在处理这个视频：%s" % (stage or "排队")
    if entry and isinstance(entry, dict):
        st = str(entry.get("state") or "")
        if st == "SKIPPED":
            # DEVELOP-P1-9：库里已有同名笔记 → 没转写；照实说，不叫失败
            return "笔记库已有同名笔记，未覆盖；这条已跳过转写（whisper 没跑）：%s" % (
                entry.get("verdict") or "笔记已存在（未覆盖）")
        if st == "FAIL":
            return "转写失败：%s" % (entry.get("verdict") or "点重试再试一次")
        if st == "PUBLISH_BLOCKED":
            return "初稿已保留，入库未完成：%s" % (
                entry.get("verdict") or "看本地证据确认原因后点重试入库")
    # DB 行级状态兜底
    if data_root:
        con = _open_ro(data_root)
        if con is not None:
            try:
                row = con.execute(
                    "SELECT status FROM processing_runs WHERE run_id=?",
                    (run_id,)).fetchone()
                if row is not None:
                    s = str(row[0] or "")
                    if s == "FAILED_RETRYABLE":
                        return "失败可重试：点重试只重跑这一个"
                    if s == "NO_SPEECH_DETECTED":
                        return "未检测到语音：换一个有声音的视频再试"
                    return "排队等待处理：监听中会自动开始"
            except Exception:
                pass
            finally:
                try:
                    con.close()
                except Exception:
                    pass
    return "排队等待处理：监听中会自动开始"


# ------------------------------------------------- V2.5 用户自定义词库
#
# 用户词库文件：<data_root>/vocab-user.json（[{"wrong":错词,"right":正词}]）。
# 后端内存注册进 stage3.normalize.CORRECTION_RULES（与 stage9.register_rules
# 同 pattern：只改内存映射，不写 src 任何文件），revision 按内容哈希
# "s9-corr-v2-user-<sha8>"（空词库回落 "s9-corr-v2"）。新转写用该 profile
# 自动应用（Case4 语义：规则变 -> 新 NormRev/新 Render，Whisper 不重跑）。
# 校验比 stage9 形状门更松：ASR 错词（如"点env local"->".env.local"）两侧
# 长度差大是常态，只要求非空/限长/不相等/不撞基表。

VOCAB_FILENAME = "vocab-user.json"
VOCAB_MAX_ENTRIES = 500
VOCAB_MAX_SIDE_CHARS = 128
VOCAB_MAX_PROMPT_TERMS = 20
VOCAB_CANDIDATES_FILENAME = "vocab-candidates.json"
# 候选清单缺失时的哨兵版本值：它不是可用版本锁，apply 入口见到即 409（见 _run_vocab_candidates_apply）
RECOVERY_CANDIDATES_ABSENT = "absent"
VOCAB_DOMAIN_STATE_FILENAME = "vocab-domains.json"


def _vocab_path(data_root: str) -> str:
    return os.path.join(os.path.abspath(str(data_root or "")), VOCAB_FILENAME)


def _vocab_domain_state_path(data_root: str) -> str:
    return os.path.join(os.path.abspath(str(data_root or "")),
                        VOCAB_DOMAIN_STATE_FILENAME)


def _load_vocab_entries(data_root: str) -> list:
    """读用户词库（只读；文件缺失/损坏回空列表，fail-open）。"""
    try:
        path = _vocab_path(data_root)
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            wrong = item.get("wrong")
            right = item.get("right")
            if isinstance(wrong, str) and isinstance(right, str):
                wrong, right = wrong.strip(), right.strip()
                if wrong and right and wrong != right:
                    source = item.get("source")
                    source = source.strip() if isinstance(source, str) else "user"
                    out.append({"wrong": wrong, "right": right,
                                "source": source or "user"})
        return out[:VOCAB_MAX_ENTRIES]
    except Exception:
        return []


def _save_vocab_entries(data_root: str, entries: list) -> None:
    """原子写用户词库（tmp + rename；只写 data_root 下）。"""
    root = os.path.abspath(str(data_root or ""))
    os.makedirs(root, exist_ok=True)
    path = _vocab_path(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump([{"wrong": e["wrong"], "right": e["right"],
                    "source": str(e.get("source") or "user")}
                   for e in entries],
                  fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _validate_vocab_pair(wrong, right, existing_wrongs: set,
                         base_patterns: set) -> str | None:
    """校验一对用户词；通过回 None，否则回人话错误。

    V2.5 P1-2：wrong 最小长度≥2写死（CJK≥2 / 拉丁建议≥3二选一取≥2，
    单字 a→b 会血洗全文已实证）；单字/纯标点/纯空格直接拒收。
    2-3字短词放行但由调用方二次确认+回显预警（见 _handle_vocab_add
    与前端 confirm）；大小写变体疑似重复由调用方提示（非阻塞）。
    """
    if not isinstance(wrong, str) or not wrong.strip():
        return "错词不能为空（填转写里听错的样子，如iste）"
    if not isinstance(right, str) or not right.strip():
        return "正词不能为空（填你想要的样子，如.env.local）"
    wrong, right = wrong.strip(), right.strip()
    if wrong == right:
        return "错词和正词一样，无需添加"
    if len(wrong) > VOCAB_MAX_SIDE_CHARS or len(right) > VOCAB_MAX_SIDE_CHARS:
        return "单条超 %d 字，请拆短再加" % (VOCAB_MAX_SIDE_CHARS,)
    # P1-2 最小长度门：单字直接拒收（a→b 血洗 7 处已实证）。
    if len(wrong) < 2:
        return "错词至少2个字（单字替换会误伤全文，如a→b，请加长后再试）"
    # 纯标点/纯空格拒收（strip 后全标点即无检索意义）。
    try:
        import string as _string
        _punct = set(_string.punctuation) | set(
            "，。、；：！？…「」『』（）【】《》〈〉·—–・、。,.!?;:\"'()[]{}<>~@#$%^&*-+=|\\/…")
        if wrong and all((ch in _punct or ch.isspace()) for ch in wrong):
            return "纯标点/空格不能作错词（无检索意义，请填转写里听错的词）"
    except Exception:
        pass
    if wrong in existing_wrongs:
        return "该错词已在词库里，重复添加会覆盖为新正词（已覆盖）"
    if wrong in base_patterns:
        return "该错词已被内置词库收录（%s），无需重复添加" % (wrong,)
    return None


def _user_rules_revision(entries: list) -> str:
    """按词库内容哈希算 revision（纯函数；空词库回落 s9 基线）。"""
    import hashlib as _hl

    from stage9 import rules_v2 as _r9  # noqa: E402  (只读复用基线)

    if not entries:
        return _r9.RULES_REVISION
    pairs = sorted((e["wrong"], e["right"]) for e in entries)
    canonical = json.dumps(pairs, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return "s9-corr-v2-user-%s" % (_hl.sha256(canonical).hexdigest()[:8],)


def _register_user_rules(entries: list) -> dict:
    """内存注册用户合并表（基表 s9 全量 + 用户对子，用户在后）。

    只改内存映射，不写 src 文件；同 revision 同内容幂等，同 revision
    异内容（哈希碰撞级）拒收。返回 {rules_revision, rules, user_added}。
    """
    from stage3 import normalize as _nm  # noqa: E402  (内存注册，只读复用表)
    from stage9 import rules_v2 as _r9  # noqa: E402  (基表来源)

    base = list(_r9.full_table())  # 冻结基行 + Github/Vscode（内存读）
    _r9.register_rules()  # 基线 revision 先就位（幂等）
    pairs = [(e["wrong"], e["right"]) for e in entries]
    base_patterns = {p for p, _ in base}
    for wrong, _ in pairs:
        if wrong in base_patterns:
            raise ValueError("错词 %r 已被内置词库收录，无需重复添加" % (wrong,))
    revision = _user_rules_revision(entries)
    table = tuple(base + pairs)
    live = _nm.CORRECTION_RULES.get(revision)
    if live is not None:
        if tuple(live) != table:
            raise ValueError("内存词表 %r 内容冲突，拒绝替换" % (revision,))
        return {"rules_revision": revision, "rules": len(table),
                "user_added": len(pairs), "idempotent_retry": True}
    _nm.CORRECTION_RULES[revision] = table
    return {"rules_revision": revision, "rules": len(table),
            "user_added": len(pairs), "idempotent_retry": False}


def _load_vocab_domain_state(data_root: str) -> dict:
    """读取预置域启用状态；缺失/损坏时所有域默认启用。"""
    try:
        with open(_vocab_domain_state_path(data_root), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        enabled = raw.get("enabled") if isinstance(raw, dict) else None
        if not isinstance(enabled, dict):
            return {}
        return {str(k): bool(v) for k, v in enabled.items()}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _save_vocab_domain_state(data_root: str, enabled: dict) -> None:
    root = os.path.abspath(str(data_root or ""))
    os.makedirs(root, exist_ok=True)
    path = _vocab_domain_state_path(root)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"enabled": {str(k): bool(v)
                                    for k, v in enabled.items()}},
                      fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _active_vocab_entries(data_root: str, entries: list | None = None) -> list:
    """停用预置域只影响新规则；用户/候选来源始终保留。"""
    entries = _load_vocab_entries(data_root) if entries is None else entries
    state = _load_vocab_domain_state(data_root)
    preset_domains = {p["domain"] for p in _load_vocab_presets()}
    return [e for e in entries
            if e.get("source") not in preset_domains
            or state.get(e.get("source"), True)]


def _effective_vocab_revision(data_root: str, entries: list | None = None) -> str:
    """当前真正会组装进新转写规则的 active 词条 revision。"""
    return _user_rules_revision(_active_vocab_entries(data_root, entries))


def _norm_profile_for_new_jobs(data_root: str) -> dict:
    """新转写用 Norm profile：用户词库 revision（自动应用，Case4 语义）。"""
    from stage3 import normalize as _nm  # noqa: E402  (只读复用 DEFAULT)

    entries = _active_vocab_entries(data_root)
    reg = _register_user_rules(entries)
    profile = dict(_nm.DEFAULT_PROFILE)
    profile["correction_rules_revision"] = reg["rules_revision"]
    return profile


def _render_profile_for_new_jobs() -> dict:
    """新转写用 Render profile：stage9 分段参数单源头（para-v2.7 目标220/
    封顶450/防碎80），与生产后处理同读 PARA_PARAMS_V2。"""
    from stage9 import formatter_v2 as _fv9  # noqa: E402  (只读复用)

    return _fv9.new_render_profile()


def _apply_v25_postpass(job_dir: str, norm_final_path: str | None,
                        rend_final_path: str, title: str,
                        render_profile: dict) -> dict:
    """生产后处理：冻结引擎已 mint revision 不断链，app 侧用
    ``render_with_v2``（引擎+hard_max封顶+min防碎同一纯函数，阈值全取
    stage9.PARA_PARAMS_V2 单源头）重算并覆写 md。para-v2.7：450/80。

    - 只改 app/ + 复用 stage9 纯函数与 stage3 assemble（只读复用），
      不碰 src 他 stage 文件，不自创 mechanics（选 review 三选一之②
      段侧思想的生产收敛：不断链，whisper0/Raw/No-Clobber 全保留）。
    - 失败 fail-open（回 fixed False，生产继续走引擎原稿，不炸 worker；
      QA 以 fixed True + 段长断言）。
    """
    try:
        from stage3 import render as _rend  # noqa: E402  (只读复用 assemble)
        from stage9 import formatter_v2 as _fv9  # noqa: E402
    except Exception as exc:
        return {"fixed": False, "error": "postpass import 失败：%s" % (_err_text(exc),)}
    try:
        if not norm_final_path or not os.path.isfile(str(norm_final_path)):
            return {"fixed": False, "error": "normalized 缺失，不覆写"}
        if not rend_final_path or not os.path.isfile(str(rend_final_path)):
            return {"fixed": False, "error": "rendered 缺失，不覆写"}
        with open(str(norm_final_path), "r", encoding="utf-8") as fh:
            import json as _json
            payload = _json.load(fh)
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list) or not segments:
            return {"fixed": False, "error": "segments 为空，不覆写"}
        try:
            want_paras = _fv9.render_with_v2(segments)
        except Exception as exc:
            return {"fixed": False, "error": "render_with_v2 失败：%s" % (_err_text(exc),)}
        try:
            want_md = _rend.assemble_markdown(
                want_paras, title or "untitled", render_profile)
        except Exception as exc:
            return {"fixed": False, "error": "assemble 失败：%s" % (_err_text(exc),)}
        try:
            with open(str(rend_final_path), "r", encoding="utf-8") as fh:
                cur_md = fh.read()
        except OSError as exc:
            return {"fixed": False, "error": "读稿失败：%s" % (_err_text(exc),)}
        if cur_md == want_md:
            return {"fixed": False, "already_ok": True,
                    "paras": len(want_paras),
                    "max_len": max((len(p) for p in want_paras), default=0)}
        # 提交物为 444 只读（No-Clobber 信号）：先加写权限再覆写，
        # 写完恢复 444，保持与 stage3 提交态一致。
        try:
            os.chmod(str(rend_final_path), 0o644)
        except Exception:
            pass
        with open(str(rend_final_path), "w", encoding="utf-8") as fh:
            fh.write(want_md)
        try:
            os.chmod(str(rend_final_path), 0o444)
        except Exception:
            pass
        return {"fixed": True, "paras": len(want_paras),
                "max_len": max((len(p) for p in want_paras), default=0),
                "lengths": [len(p) for p in want_paras]}
    except Exception as exc:
        return {"fixed": False, "error": "后处理异常：%s" % (_err_text(exc),)}


def _user_prompt_terms(data_root: str) -> list:
    """用户正词作弱引导进 prompt（prompt 术语弱引导层；失败回空）。"""
    try:
        terms = [e["right"] for e in _active_vocab_entries(data_root)
                 if isinstance(e.get("right"), str) and e["right"].strip()]
        seen: list = []
        for term in terms:
            if term not in seen:
                seen.append(term)
        return seen[:VOCAB_MAX_PROMPT_TERMS]
    except Exception:
        return []


def _handle_vocab_get(query: dict) -> tuple[int, dict]:
    data_root = (query.get("data_root") or [DEFAULT_DATA_ROOT])[0] or DEFAULT_DATA_ROOT
    data_root = normalize_path(data_root) or DEFAULT_DATA_ROOT
    entries = _load_vocab_entries(data_root)
    return 200, {"ok": True, "data_root": data_root,
                 "vocab": entries, "count": len(entries),
                 "revision": _user_rules_revision(entries),
                 "effective_revision": _effective_vocab_revision(data_root, entries)}


def _vocab_base_patterns() -> set:
    """读取冻结基表的 pattern；失败时回空，后续注册链路仍会二次兜底。"""
    try:
        from stage9 import rules_v2 as _r9  # noqa: E402  (只读复用基表)

        return {p for p, _ in _r9.full_table()}
    except Exception:
        return set()


def _vocab_candidates_path(data_root: str) -> str:
    return os.path.join(os.path.abspath(str(data_root or "")),
                        VOCAB_CANDIDATES_FILENAME)


def _vocab_candidates_read(data_root: str) -> tuple:
    """读 AI 审查候选：返回 `(state, message, items)`，state ∈ ok/missing/corrupt。

    P1-3/CANDIDATE-APPLY P3-4：**一次读取、一处判定**——把「文件不存在（missing）」
    与「文件在但读不出/不是数组（corrupt）」分开，供页面分别提示。旧实现把两者
    都吞成空清单，页面只能显示「暂无待审候选」，腐坏证据被隐藏。
    """
    path = _vocab_candidates_path(data_root)
    if not os.path.isfile(path):
        return "missing", "还没有待审清单（文件不存在）：先在 AI 审查里生成一份", []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return ("corrupt",
                "待审清单文件损坏或格式不对（读不出来）："
                "请重新跑一次 AI 审查覆盖它，损坏内容不会被应用", [])
    if not isinstance(raw, list):
        return ("corrupt",
                "待审清单内容不是数组（格式不对）："
                "请重新跑一次 AI 审查覆盖它，损坏内容不会被应用", [])
    return "ok", "", raw


def _load_vocab_candidates(data_root: str) -> list:
    """读 AI 审查候选；文件缺失、损坏或不是数组时返回空清单（判定同 `_vocab_candidates_read`）。"""
    return _vocab_candidates_read(data_root)[2]


def _mark_vocab_candidates_imported(data_root: str, indices: set) -> None:
    """原子标记候选；失败时不替换原候选文件。"""
    path = _vocab_candidates_path(data_root)
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError("候选文件不是数组")
    marked = []
    for index, item in enumerate(raw):
        if index in indices and isinstance(item, dict):
            item = dict(item)
            item["imported"] = True
        marked.append(item)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marked, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _candidate_confidence(value) -> str:
    value = str(value or "").strip().lower()
    return {"high": "high", "medium": "medium", "low": "low",
            "高": "high", "中": "medium", "低": "low"}.get(value, "low")


def _candidate_view(index: int, item) -> dict:
    item = item if isinstance(item, dict) else {}
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {"index": index, "wrong": item.get("wrong")
            if isinstance(item.get("wrong"), str) else "",
            "right": item.get("right")
            if isinstance(item.get("right"), str) else "",
            "confidence": _candidate_confidence(item.get("confidence")),
            "evidence": [str(x) for x in evidence]}


def _handle_vocab_candidates_get(query: dict) -> tuple[int, dict]:
    data_root = (query.get("data_root") or [DEFAULT_DATA_ROOT])[0] or DEFAULT_DATA_ROOT
    data_root = normalize_path(data_root) or DEFAULT_DATA_ROOT
    # P1-3/CANDIDATE-APPLY P3-4：缺失（还没生成过）与腐坏（有文件但读不出）
    # 必须分开展示，不能都塞进「暂无待审候选」把腐坏证据藏起来。
    cand_state, state_message, raw_candidates = _vocab_candidates_read(data_root)
    groups = {"high": [], "medium": [], "low": []}
    for index, item in enumerate(raw_candidates):
        if isinstance(item, dict) and item.get("imported"):
            continue
        view = _candidate_view(index, item)
        groups[view["confidence"]].append(view)
    return 200, {"ok": True, "data_root": data_root,
                 "candidates": groups,
                 "count": sum(len(items) for items in groups.values()),
                 "has_candidates": bool(sum(len(items) for items in groups.values())),
                 "candidates_state": cand_state,
                 "candidates_state_message": state_message,
                 # P1-2 候选版本锁：POST apply 必须回传同一指纹，漂移即 409 零执行
                 "candidates_revision": _vocab_candidates_revision(data_root),
                 "source_exists": cand_state != "missing"}


def _candidate_detail(index, item, accepted: bool, reason: str = "") -> dict:
    """候选明细固定契约：任何分支都带同一组键（RERUN-PROGRESS P3-1）。"""
    view = _candidate_view(index, item)
    view.update({"accepted": bool(accepted),
                 "decision": "保留" if accepted else "拒收",
                 "reason": str(reason or "")})
    return view


def _vocab_candidates_revision(data_root: str) -> str:
    """候选清单内容指纹（GET 快照与 POST 应用之间的版本锁）。

    清单缺失 → 哨兵 `RECOVERY_CANDIDATES_ABSENT`（不是可用版本，apply 入口
    见到即 409，防伪造常量过锁）。
    """
    try:
        with open(_vocab_candidates_path(data_root), "rb") as fh:
            raw = fh.read()
    except OSError:
        return RECOVERY_CANDIDATES_ABSENT
    return hashlib.sha256(raw).hexdigest()[:16]


def _run_vocab_candidates_apply(params: dict,
                                progress_cb=None) -> tuple[int, dict]:
    """导入选中的 AI 候选，然后复用现有 all=true 重跑（语义与旧同步版一致）。

    progress_cb 仅用于页面进度回传（(ev) -> None）；为 None 时不改变任何行为。
    本函数本身仍是同步阻塞的，异步外壳见 _handle_vocab_candidates_apply。
    """
    try:
        # P2-新1：申请侧必须显式带 data_root，与状态查询侧的显式要求对称
        data_root = _take_required_data_root(params)
        # 新前端显式传 false 表示只入库；字段缺失沿用旧 API 的全量重跑行为，
        # 但字段存在就必须是布尔（1/"true"/null 一律 400，不静默当 False）。
        rerun_old = _take_bool(params, "rerun_old", True)
        # 严格 int 且非 bool（true/1.5/"3"/null 一律 400）；越界与重复仍走逐条
        # 拒收明细（既有行为，前端按 details 统一解释）。
        # P1-3 零目标（CANDIDATE-APPLY P3-3）：空数组=没勾任何候选，给一句人话，
        # 不再把裸键名 `indices 不能为空数组` 丢给用户。
        indices = _take_int_list(
            params, "indices",
            empty_msg="没有勾选任何待审候选（零目标，零执行）："
                      "请先勾选至少一条候选再点「错词重跑」")
        vault_s = _take_str(params, "ob_vault_root", "", allow_empty=True) or None
        want_revision = _take_str(params, "candidates_revision", "")
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    # 候选版本锁（CANDIDATE-APPLY P3-5）：GET 快照与 POST 之间的候选清单若已
    # 变化，下标可能指向别的候选 → 409 且零执行（不写词库、不标记、不重跑）。
    cur_revision = _vocab_candidates_revision(data_root)
    if not want_revision:
        return 400, {"ok": False,
                     "error": "缺少候选清单版本 candidates_revision（请刷新待审清单后重试）"}
    if cur_revision == RECOVERY_CANDIDATES_ABSENT:
        # 没有清单就没有可应用的候选：伪造常量版本也不许过锁建空 job（P1-2 修）
        return 409, {"ok": False, "data_root": data_root,
                     "candidates_revision": cur_revision,
                     "error": "没有待审清单可应用（零执行），请先跑一次 AI 审查生成清单"}
    if want_revision != cur_revision:
        return 409, {"ok": False, "data_root": data_root,
                     "candidates_revision": cur_revision,
                     "error": "候选清单已变化（零执行），请刷新待审清单后重新勾选"}
    candidates = _load_vocab_candidates(data_root)
    details = []
    selected = []
    seen_indices = set()
    for index in indices:
        if index in seen_indices:
            details.append(_candidate_detail(index, None, False, "候选索引重复"))
            continue
        seen_indices.add(index)
        if index < 0 or index >= len(candidates):
            details.append(_candidate_detail(index, None, False, "候选索引不存在"))
            continue
        if isinstance(candidates[index], dict) and candidates[index].get("imported"):
            details.append(_candidate_detail(
                index, candidates[index], False, "该候选已导入，清单已标记"))
            continue
        selected.append((index, candidates[index]))
    entries = _load_vocab_entries(data_root)
    entries_before = list(entries)
    existing = {e["wrong"] for e in entries}
    base_patterns = _vocab_base_patterns()
    accepted_entries = []
    for index, item in selected:
        item = item if isinstance(item, dict) else {}
        wrong = item.get("wrong")
        right = item.get("right")
        err = _validate_vocab_pair(wrong, right, existing, base_patterns)
        if err is not None:
            details.append(_candidate_detail(index, item, False, err))
            continue
        if len(entries) + len(accepted_entries) >= VOCAB_MAX_ENTRIES:
            details.append(_candidate_detail(
                index, item, False, "词库已满（%d 条），请先删一些再导入" % VOCAB_MAX_ENTRIES))
            continue
        accepted_entries.append({"wrong": wrong.strip(), "right": right.strip(),
                                 "source": "candidate"})
        existing.add(wrong.strip())
        details.append(_candidate_detail(index, item, True))
    imported = len(accepted_entries)
    revision = _user_rules_revision(entries)
    if imported:
        entries.extend(accepted_entries)
        try:
            _save_vocab_entries(data_root, entries)
        except OSError as exc:
            return 500, {"ok": False, "data_root": data_root,
                         "error": "词库保存失败，未生效：%s→检查磁盘空间/目录权限后重试" % (_err_text(exc),),
                         "details": details}
        try:
            reg = _register_user_rules(entries)
            revision = reg["rules_revision"]
        except ValueError as exc:
            try:
                _save_vocab_entries(data_root, entries_before)
            except Exception:
                pass
            return 400, {"ok": False, "data_root": data_root,
                         "error": "词库注册失败，已回滚未生效：%s" % (_err_text(exc),),
                         "details": details}
        except Exception as exc:
            try:
                _save_vocab_entries(data_root, entries_before)
            except Exception:
                pass
            return 500, {"ok": False, "data_root": data_root,
                         "error": "词库注册失败，已回滚未生效：%s" % (_err_text(exc),),
                         "details": details}
    if not imported:
        # P1-3 统计统一：零导入也是零目标，同样按五桶口径回（页面不另算一套）
        empty_stats = _reapply_stats([], total=0)
        return 200, {"ok": True, "data_root": data_root, "imported": 0,
                     "revision": revision, "details": details,
                     "effective_revision": _effective_vocab_revision(data_root, entries),
                     "summary": {"success": 0, "skipped": 0, "failed": 0,
                                 "total": 0, "needs_human": 0, "interrupted": 0},
                     "stats": empty_stats,
                     "codesummary": "导入0条/重跑总数0篇/成功0篇/跳过0篇/失败0篇",
                     "message": "没有候选通过校验，未导入（零目标，未执行重跑）"}
    candidate_mark_error = ""
    try:
        _mark_vocab_candidates_imported(
            data_root, {d["index"] for d in details if d.get("accepted")})
    except Exception as exc:
        candidate_mark_error = "候选清单标记失败，原文件未改动：%s" % (_err_text(exc),)
    if not rerun_old:
        message = "已导入%d条；未重跑老稿，老稿未动" % imported
        if candidate_mark_error:
            message += "。" + candidate_mark_error
        no_rerun_stats = _reapply_stats([], total=0)
        return 200, {"ok": not candidate_mark_error, "data_root": data_root,
                     "imported": imported, "rerun_old": False,
                     "revision": revision, "details": details,
                     "effective_revision": _effective_vocab_revision(data_root, entries),
                     "reapply": None,
                     "summary": {"success": 0, "skipped": 0, "failed": 0,
                                 "total": 0, "needs_human": 0,
                                 "interrupted": 0},
                     "stats": no_rerun_stats,
                     "candidate_mark_error": candidate_mark_error,
                     "codesummary": "导入%d条/重跑成功0篇/跳过0篇/失败0篇；老稿未动"
                     % imported,
                     "message": message}
    try:
        reapply_params = {"data_root": data_root, "all": True}
        if vault_s:
            reapply_params["ob_vault_root"] = vault_s
        reapply_body = json.dumps(reapply_params, ensure_ascii=False).encode("utf-8")
        reapply_code, reapply = _handle_reapply_post(reapply_body,
                                                     progress_cb=progress_cb)
    except Exception as exc:
        reapply_code, reapply = 500, {"ok": False,
                                           "error": "重跑接口异常：%s" % (_err_text(exc),)}
    summary = reapply.get("summary") if isinstance(reapply, dict) else None
    results = reapply.get("results") if isinstance(reapply, dict) else []
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(results, list):
        results = []
    # P1-3 统计统一（CANDIDATE-APPLY P3-2）：**不再这里另算一套** ——
    # 逐项归类只认 `_reapply_result_bucket`，五桶之和可复算等于 total。
    if results:
        stats = _reapply_stats(results, total=len(results))
    elif isinstance(summary.get("total"), int):
        # 老响应没有 results：按 summary 同口径折算（键名映射写在一处）
        stats = {key: int(summary.get(key) or 0) for key in _REAPPLY_BUCKETS}
        stats["total"] = int(summary.get("total") or 0)
        stats["done"] = sum(stats[key] for key in _REAPPLY_BUCKETS)
        stats["counted"] = stats["done"]
        stats["balanced"] = stats["counted"] == stats["total"]
    else:
        stats = _reapply_stats([], total=0)
    success = stats["success"]
    skipped = stats["skipped"]
    failed = stats["failed"]
    needs_human = stats["needs_human"]
    interrupted = stats["interrupted"]
    rerun_ok = reapply_code == 200 and bool(reapply.get("ok")) and failed == 0
    zero_target = stats["total"] <= 0
    if zero_target:
        # P1-3/CANDIDATE-APPLY P3-3 零目标：词入库了，但**没有可重跑的已完成任务**。
        # 旧文案写成「重跑成功0篇，跳过0篇」，看着像「重跑跑过且全成功」。
        message = ("已导入%d条；本次没有可重跑的已完成任务（零目标，未重跑任何稿件）"
                   % imported)
        if not rerun_ok:
            message += "；重跑接口这次没跑成，请稍后重试"
    else:
        message = "已导入%d条；重跑成功%d篇，跳过%d篇" % (imported, success, skipped)
        if failed:
            message += "，失败%d篇" % failed
        if needs_human or interrupted:
            message += "，待人工%d篇，中断%d篇" % (needs_human, interrupted)
        if not rerun_ok:
            message += "。词已入库，但重跑失败，请检查失败明细后重试"
    if candidate_mark_error:
        message += "。" + candidate_mark_error
    if not vault_s:
        message += "。未给笔记库，库内笔记未更新"
    operation_ok = rerun_ok and not candidate_mark_error
    codesummary = ("导入%d条/重跑总数%d篇/成功%d篇/跳过%d篇/失败%d篇/待人工%d篇/中断%d篇"
                   % (imported, stats["total"], success, skipped, failed,
                      needs_human, interrupted))
    return 200, {"ok": operation_ok, "data_root": data_root, "imported": imported,
                 "rerun_old": True,
                 "revision": revision, "effective_revision":
                 _effective_vocab_revision(data_root, entries),
                 "details": details,
                 "reapply": reapply,
                 "summary": {"success": success, "skipped": skipped,
                             "failed": failed, "total": stats["total"],
                             "needs_human": needs_human,
                             "interrupted": interrupted},
                 "stats": stats,
                 "candidate_mark_error": candidate_mark_error,
                 "codesummary": codesummary,
                 "message": message}


def _vocab_apply_worker(job_id: str, params: dict) -> None:
    """后台线程：跑 _run_vocab_candidates_apply，并把结果落回 _vocab_apply_job。

    只更新状态；异常一律转 failed 人话，不炸进程。
    """

    def _on_progress(ev: dict) -> None:
        try:
            with _state_lock:
                job = _vocab_apply_job
                if not isinstance(job, dict) or job.get("job_id") != job_id:
                    return
                job["stage"] = "rerunning"
                if isinstance(ev.get("total"), int):
                    job["total"] = ev["total"]
                if isinstance(ev.get("done"), int):
                    job["done"] = ev["done"]
                if ev.get("filename"):
                    job["current_filename"] = ev["filename"]
        except Exception:
            pass

    try:
        code, obj = _run_vocab_candidates_apply(params, progress_cb=_on_progress)
    except Exception as exc:  # noqa: BLE001  (兜底转 failed，不炸线程)
        code, obj = 500, {"ok": False, "error": "错词重跑异常：%s" % (_err_text(exc),)}
    obj = obj if isinstance(obj, dict) else {}
    with _state_lock:
        job = _vocab_apply_job
        if not isinstance(job, dict) or job.get("job_id") != job_id:
            return
        job["finished_at"] = _utc_now_iso()
        job["result"] = obj
        try:
            job["imported"] = int(obj.get("imported") or 0)
        except (TypeError, ValueError):
            job["imported"] = 0
        s = obj.get("summary")
        if isinstance(s, dict):
            job["summary"] = {
                "success": int(s.get("success") or 0),
                "skipped": int(s.get("skipped") or 0),
                "failed": int(s.get("failed") or 0),
                "total": int(s.get("total") or 0),
                "needs_human": int(s.get("needs_human") or 0),
                "interrupted": int(s.get("interrupted") or 0),
            }
        # P1-3 统计统一：把后端唯一口径的 stats 原样带给页面，页面不再自己另算
        st = obj.get("stats")
        if isinstance(st, dict):
            job["stats"] = {k: st.get(k) for k in
                            ("total", "done", "success", "failed", "skipped",
                             "needs_human", "interrupted", "counted", "balanced")}
        job["current_filename"] = None
        if code == 200:
            job["state"] = "done"
            job["message"] = str(obj.get("message") or obj.get("codesummary")
                                 or "处理完成")
            if not job.get("rerun_old"):
                job["total"] = job.get("total") or 0
                job["done"] = job.get("total")
        else:
            job["state"] = "failed"
            job["error"] = str(obj.get("error") or "处理失败，请刷新后重试")
            job["message"] = job["error"]


def _vocab_apply_locked_response(job: dict, want_root: str = "") -> tuple:
    """P1-3 运行中参数锁定：统一的 409 人话 + 冻结参数回显（零执行、零写盘）。

    只有**同一个数据目录**的调用方才拿得到 `locked_params`（本次真正锁住的范围／
    rerun_old／目标候选集合）；别目录的调用方只回人话＋任务编号，不回显别处的
    参数（D-12 口径：不把别目录的路径与参数透给页面）。
    """
    job_root = normalize_path(job.get("data_root"))
    same = bool(want_root) and bool(job_root) and (
        os.path.realpath(want_root) == os.path.realpath(job_root))
    out = {"ok": False, "running": True, "job_id": job.get("job_id"),
           "error": "已有一次错词重跑在进行中，本次参数已锁定（零执行，"
                    "未导入、未标记、未重跑）：范围、rerun_old、"
                    "目标候选集合都按那次任务的参数走，"
                    "请等它跑完（进度条会显示已用时，可离开页面稍后回来）"
                    "再改参数重新提交"}
    if same:
        out["locked_params"] = {"rerun_old": job.get("rerun_old"),
                                "candidates_revision": job.get("candidates_revision"),
                                "indices": list(job.get("request_indices") or []),
                                "total": job.get("total"), "done": job.get("done")}
    return 409, out


def _handle_vocab_candidates_apply(body: bytes) -> tuple[int, dict]:
    """异步入口：请求级校验后立即返回 202 + job_id，后台线程做导入＋按需重跑。

    进度由 GET /api/vocab/candidates/apply/status 读取（状态存后端，
    刷新页面可续看）。导入/幂等/No-Clobber/词库三铁律语义全部沿用
    _run_vocab_candidates_apply，未改动。
    """
    global _vocab_apply_job, _vocab_apply_seq
    try:
        params = _body_json(body)
        # P2-新1：异步入口同口径——必须显式带 data_root（否则 400，不建 job）
        data_root = _take_required_data_root(params)
        rerun_old = _take_bool(params, "rerun_old", True)
        # P1-3 零目标（CANDIDATE-APPLY P3-3）：空数组给一句人话，不丢裸键名
        indices = _take_int_list(
            params, "indices",
            empty_msg="没有勾选任何待审候选（零目标，零执行）："
                      "请先勾选至少一条候选再点「错词重跑」")
        vault_s = _take_str(params, "ob_vault_root", "", allow_empty=True) or None
        want_revision = _take_str(params, "candidates_revision", "")
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if not want_revision:
        return 400, {"ok": False,
                     "error": "缺少候选清单版本 candidates_revision（请刷新待审清单后重试）"}
    # P1-3 运行中参数锁定（RERUN-PROGRESS P3-5）：本次执行参数在运行窗口内冻结。
    # 检查必须**先于**版本锁与任何写入：运行中的请求一律 409 人话 + 零执行 +
    # 零写盘，且把冻结的参数原样回给页面，杜绝“静默按新参数再跑一遍”。
    with _state_lock:
        cur = _vocab_apply_job
        if isinstance(cur, dict) and cur.get("state") == "running":
            return _vocab_apply_locked_response(cur, data_root)
    # 候选版本锁：同步入口先判，漂移即 409 且零执行（不建 job、不写盘）
    cur_revision = _vocab_candidates_revision(data_root)
    if cur_revision == RECOVERY_CANDIDATES_ABSENT:
        return 409, {"ok": False, "data_root": data_root,
                     "candidates_revision": cur_revision,
                     "error": "没有待审清单可应用（零执行），请先跑一次 AI 审查生成清单"}
    if want_revision != cur_revision:
        return 409, {"ok": False, "data_root": data_root,
                     "candidates_revision": cur_revision,
                     "error": "候选清单已变化（零执行），请刷新待审清单后重新勾选"}
    # P1-三1：可选字段「笔记库目录」缺省/为空时**不要写回这个键**——写回 None 会让
    # worker 侧 `_take_str` 把「键存在且值为 None」判成类型错（显式 null 必须拒，
    # D-6 既定契约），于是「不填笔记库」这条合法路径必然 400→任务 failed。
    # 只归一必填/已知键；可选键按需补，缺省即不出现。
    job_params = {k: v for k, v in params.items() if k != "ob_vault_root"}
    job_params.update({"data_root": data_root, "rerun_old": rerun_old,
                       "indices": indices,
                       "candidates_revision": want_revision})
    if vault_s is not None:
        job_params["ob_vault_root"] = vault_s
    params = job_params
    with _state_lock:
        cur = _vocab_apply_job
        if isinstance(cur, dict) and cur.get("state") == "running":
            # 竞态兜底：早先那次检查之后被别人抢先起了任务，同样锁定拒绝
            return _vocab_apply_locked_response(cur, data_root)
        _vocab_apply_seq += 1
        job_id = "vocab-apply-%d-%d" % (_vocab_apply_seq,
                                        int(threading.get_ident() % 100000))
        _vocab_apply_job = {
            "job_id": job_id,
            "state": "running",
            "stage": "importing",
            "data_root": data_root,
            "rerun_old": rerun_old,
            "candidates_revision": want_revision,
            # P1-3：冻结参数留档，供 409 把「本次锁定的目标集合」原样回给页面
            "request_indices": list(indices),
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "total": 0,
            "done": 0,
            "current_filename": None,
            "imported": 0,
            "summary": {"success": 0, "skipped": 0, "failed": 0,
                        "total": 0, "needs_human": 0, "interrupted": 0},
            "stats": None,
            "error": None,
            "message": "正在导入错词…",
            "result": None,
        }
    try:
        # M3：构造与 start 同保护，任一步失败都落 failed 终态，不悬挂单例
        thread = threading.Thread(
            target=_vocab_apply_worker, args=(job_id, params),
            name="v2o-vocab-apply", daemon=True,
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001  起线程失败不得悬挂单例
        reason = "后台任务启动失败：%s，请重试或重启服务" % (_err_text(exc),)
        with _state_lock:
            cur2 = _vocab_apply_job
            if isinstance(cur2, dict) and cur2.get("job_id") == job_id:
                cur2.update({"state": "failed", "finished_at": _utc_now_iso(),
                             "error": reason, "message": reason})
        return 500, {"ok": False, "error": reason}
    return 202, {"ok": True, "job_id": job_id, "state": "running",
                 "message": "已开始处理，进度见页面"}


def _handle_vocab_apply_status(query: dict) -> tuple[int, dict]:
    """只读：按 data_root + job_id 取最近一次错词重跑进度（明细不在这里）。

    隔离口径（RERUN-PROGRESS P3-2/P3-3）：**必须显式传 data_root**（+可选
    job_id）才回明细；两者都不传 → 一律 `{ok:true, job:null}`，不给"最近一次
    任务"的回落口子。传了 data_root 就按目录比对，传了 job_id 就按任务比对；
    任一不匹配一律结构化失败且**不回**该任务内容，防换目录后或另一个标签页
    把别人的进度画到自己页面上。
    """
    try:
        data_root = _query_data_root(query, "", "data_root")
        want_job = _query_str(query, "job_id").strip()
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if not data_root:
        # 没有 data_root 就无法判定隔离：不回任何明细（P1-2 契约，不做回落）
        return 200, {"ok": True, "job": None}
    with _state_lock:
        job = dict(_vocab_apply_job) if isinstance(_vocab_apply_job, dict) else None
    if job is None:
        return 200, {"ok": True, "job": None}
    job_root = normalize_path(job.get("data_root")) or DEFAULT_DATA_ROOT
    # realpath 口径：符号链接/尾斜杠指向同一目录时不该误判成跨目录
    if os.path.realpath(job_root) != os.path.realpath(data_root):
        return 409, {"ok": False, "job": None, "data_root": data_root,
                     "error": "最近一次任务属于另一个数据目录（零渲染），"
                              "请核对数据目录后重查"}
    cur_job_id = str(job.get("job_id") or "")
    if want_job and want_job != cur_job_id:
        # 不回当前 job 的任何字段（含 job_id），避免把别的标签页的任务信息透出去
        return 409, {"ok": False, "job": None,
                     "error": "最近一次任务不是你发起的那个（零渲染），"
                              "可能另一个标签页在跑；请刷新后再看"}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return 200, {"ok": True, "job": {
        "job_id": job.get("job_id"),
        "state": job.get("state"),
        "stage": job.get("stage"),
        "data_root": job.get("data_root"),
        "rerun_old": job.get("rerun_old"),
        "candidates_revision": job.get("candidates_revision"),
        # P1-3/RERUN-PROGRESS P3-5：把「本次真正锁定的目标集合大小」回给页面，
        # 页面显示的就是实际在跑的参数（rerun_old＋目标候选数）。
        "indices_count": len(job.get("request_indices") or []),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        # P1-3：长任务可理解性（RERUN-PROGRESS P3-6）——页面据此显示已用时
        # 与「可离开页面稍后回来」；本版不提供任意进行中任务的强制取消。
        "elapsed_seconds": _elapsed_seconds(job.get("started_at"),
                                            job.get("finished_at")),
        "total": job.get("total"),
        "done": job.get("done"),
        "current_filename": job.get("current_filename"),
        "imported": job.get("imported"),
        "summary": job.get("summary"),
        "stats": job.get("stats"),
        "error": job.get("error"),
        "message": job.get("message"),
        "codesummary": result.get("codesummary"),
        "details": result.get("details"),
    }}


def _handle_vocab_add(body: bytes) -> tuple[int, dict]:
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    data_root = normalize_path(params.get("data_root")) or DEFAULT_DATA_ROOT
    wrong = params.get("wrong")
    right = params.get("right")
    wrong_s = wrong.strip() if isinstance(wrong, str) else ""
    right_s = right.strip() if isinstance(right, str) else ""
    try:
        from stage9 import rules_v2 as _r9  # noqa: E402  (基表 patterns)

        base_patterns = {p for p, _ in _r9.full_table()}
    except Exception:
        base_patterns = set()
    entries = _load_vocab_entries(data_root)
    existing = {e["wrong"] for e in entries}
    err = _validate_vocab_pair(wrong_s, right_s, existing, base_patterns)
    # 重复错词视为覆盖更新（ upsert），其余非法直接拒收
    # P1-2：单字/纯标点已在 _validate 直接 400（wrong_s not in existing
    # 即拒收，覆盖路径不绕过长度门：单字覆盖同样拒收）。
    if err is not None and wrong_s not in existing:
        return 400, {"ok": False, "error": err}
    if not wrong_s or not right_s or wrong_s == right_s:
        return 400, {"ok": False, "error": err or "错词/正词不合法"}
    if len(wrong_s) > VOCAB_MAX_SIDE_CHARS or len(right_s) > VOCAB_MAX_SIDE_CHARS:
        return 400, {"ok": False, "error": err or "单条超长"}
    if len(wrong_s) < 2:
        return 400, {"ok": False,
                     "error": err or "错词至少2个字（单字替换会误伤全文，请加长后再试）"}
    if wrong_s in base_patterns:
        return 400, {"ok": False, "error": err or "已被内置词库收录"}
    entries_before = list(entries)
    if wrong_s in existing:
        entries = [e for e in entries if e["wrong"] != wrong_s]
    if len(entries) >= VOCAB_MAX_ENTRIES and wrong_s not in existing:
        return 400, {"ok": False,
                     "error": "词库已满（%d 条），删一些再加" % (VOCAB_MAX_ENTRIES,)}
    entries.append({"wrong": wrong_s, "right": right_s, "source": "user"})
    # P1-2 短词预警 + 大小写变体提示（非阻塞，随成功回显；前端 2-3 字
    # 二次 confirm 复用 delVocab 口径；命中数>50 警告待新转写验证，
    # 后端此处回显 needs_confirm + hint，QA 以此断言预览存在）。
    _preview_warnings: list = []
    if 2 <= len(wrong_s) <= 3:
        _preview_warnings.append(
            "短词（%d字）易命中多处：添加后新转写与重跑会自动应用全文替换，"
            "如命中超50处请及时删除" % (len(wrong_s),))
    try:
        _lower_base = {str(p).lower() for p in base_patterns}
        if wrong_s not in base_patterns and wrong_s.lower() in _lower_base:
            _preview_warnings.append(
                "疑似重复（仅大小写差异，内置已有同名不同大小写），仍要加吗？"
                "确认无误再点添加")
    except Exception:
        pass
    # P1-3：先落盘后注册。落盘 OSError → 500 JSON“词库保存失败，未生效”
    # （内存未动，旧文件原子保留）；注册失败则回滚文件到 entries_before
    # （二选一取“删回文件”以保内存文件一致，重启无需自愈），并注释。
    try:
        _save_vocab_entries(data_root, entries)
    except OSError as exc:
        return 500, {"ok": False,
                     "error": "词库保存失败，未生效：%s→检查磁盘空间/目录权限后重试"
                              % (_err_text(exc),)}
    try:
        reg = _register_user_rules(entries)
    except ValueError as exc:
        # 注册失败（撞基表/内存冲突）：删回文件保一致。
        try:
            _save_vocab_entries(data_root, entries_before)
        except Exception:
            pass
        return 400, {"ok": False, "error": _err_text(exc)}
    _msg = (("已覆盖更新：%s→%s" % (wrong_s, right_s))
            if err is not None else
            ("已添加：%s→%s，新转写自动应用" % (wrong_s, right_s)))
    if _preview_warnings:
        _msg += "（注意：" + "；".join(_preview_warnings) + "）"
    return 200, {"ok": True, "data_root": data_root, "vocab": entries,
                 "count": len(entries), "revision": reg["rules_revision"],
                 "effective_revision": _effective_vocab_revision(data_root, entries),
                 "message": _msg,
                 "preview": {"wrong_len": len(wrong_s),
                             "needs_confirm": 2 <= len(wrong_s) <= 3,
                             "warnings": _preview_warnings}}


def _handle_vocab_del(body: bytes) -> tuple[int, dict]:
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    data_root = normalize_path(params.get("data_root")) or DEFAULT_DATA_ROOT
    wrong = params.get("wrong")
    wrong_s = wrong.strip() if isinstance(wrong, str) else ""
    if not wrong_s:
        return 400, {"ok": False, "error": "缺少要删除的错词 wrong"}
    entries = _load_vocab_entries(data_root)
    kept = [e for e in entries if e["wrong"] != wrong_s]
    if len(kept) == len(entries):
        return 404, {"ok": False, "error": "词库里没有该错词：%s" % (wrong_s,)}
    try:
        _save_vocab_entries(data_root, kept)
    except OSError as exc:
        return 500, {"ok": False,
                     "error": "词库保存失败，未生效：%s→检查磁盘空间/目录权限后重试"
                              % (_err_text(exc),)}
    return 200, {"ok": True, "data_root": data_root, "vocab": kept,
                 "count": len(kept), "revision": _user_rules_revision(kept),
                 "effective_revision": _effective_vocab_revision(data_root, kept),
                 "message": "已删除：%s（新转写不再应用）" % (wrong_s,)}


# ------------------------------------------------- V2.6 预置词库（三域一键导入）
#
# 预置词库文件：app/presets/vocab/*.json（随仓库只读，运行时绝不改写）。
# 每个文件 {"domain","label","version","source","entries":[{"wrong","right"}]}；
# 目录扫描发现域 => 新增一个 json 即新增一域（可扩展），无需改 server。
# 导入 = 合并进 <data_root>/vocab-user.json：已存在的错词跳过（保留用户自定，
# 不覆盖），撞内置基表（stage9 full_table patterns）的整条拒收并回报，
# 非法/超长/错词<2字同样拒收；内容变化 => _user_rules_revision 哈希变 =>
# revision bump，新转写自动应用（Case4 语义，whisper 不重跑）。

VOCAB_PRESET_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "presets", "vocab")
VOCAB_PRESET_REPORT_MAX = 20


def _read_vocab_preset(path: str) -> dict | None:
    """读单个预置词库文件（只读；缺失/损坏/结构不符回 None）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("entries")
    if not isinstance(raw, list):
        return None
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        wrong = item.get("wrong")
        right = item.get("right")
        if not isinstance(wrong, str) or not isinstance(right, str):
            continue
        wrong, right = wrong.strip(), right.strip()
        if len(wrong) >= 2 and right and wrong != right:
            entries.append({"wrong": wrong, "right": right})
    stem = os.path.splitext(os.path.basename(path))[0]
    domain = str(data.get("domain") or stem).strip() or stem
    label = str(data.get("label") or domain).strip() or domain
    return {"domain": domain, "label": label,
            "file": os.path.basename(path),
            "version": data.get("version"),
            "source": data.get("source"),
            "entries": entries}


def _load_vocab_presets() -> list:
    """扫描预置目录（新增文件即新增域，可扩展；坏文件跳过不炸）。"""
    try:
        names = sorted(os.listdir(VOCAB_PRESET_DIR))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        preset = _read_vocab_preset(os.path.join(VOCAB_PRESET_DIR, name))
        if preset is not None and preset["entries"]:
            out.append(preset)
    return out


def _handle_vocab_presets_get(query: dict | None = None) -> tuple[int, dict]:
    """预置词库清单（只读条目、启用状态，不修改预置文件）。"""
    query = query or {}
    data_root = (query.get("data_root") or [DEFAULT_DATA_ROOT])[0] or DEFAULT_DATA_ROOT
    data_root = normalize_path(data_root) or DEFAULT_DATA_ROOT
    enabled = _load_vocab_domain_state(data_root)
    presets = _load_vocab_presets()
    return 200, {"ok": True, "presets": [
        {"domain": p["domain"], "label": p["label"], "count": len(p["entries"]),
         "version": p["version"], "source": p["source"], "file": p["file"],
         "enabled": enabled.get(p["domain"], True),
         "entries": p["entries"]}
        for p in presets]}


def _handle_vocab_presets_domains_post(body: bytes) -> tuple[int, dict]:
    """保存预置域启用开关；停用只影响新转写/重跑，不改老稿。

    P1-2 部分更新安全（CANDIDATE-UI2 P3-5）：只改本次提交的域，未提交的域
    保持原值（缺键不得当重置/当 True 回启），未知域忽略，值必须是真布尔。
    """
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    raw = params.get("enabled")
    if not isinstance(raw, dict):
        return 400, {"ok": False, "error": "请传 enabled 对象（每个预置域 true/false）"}
    known = {p["domain"] for p in _load_vocab_presets()}
    current = _load_vocab_domain_state(data_root)
    enabled = {}
    for domain in known:
        if domain in raw:
            value = raw[domain]
            if not isinstance(value, bool):
                return 400, {"ok": False,
                             "error": "enabled.%s 须为布尔值 true/false（收到 %s）"
                                      % (domain, _type_zh(value))}
            enabled[domain] = value
        else:
            # 未提交域保持原值（读不到时沿用默认启用），不产生旁路副作用
            enabled[domain] = bool(current.get(domain, True))
    try:
        _save_vocab_domain_state(data_root, enabled)
    except OSError as exc:
        return 500, {"ok": False,
                     "error": "启用状态保存失败：%s，检查磁盘空间/目录权限后重试"
                              % (_err_text(exc),)}
    disabled = [domain for domain, value in enabled.items() if not value]
    return 200, {"ok": True, "data_root": data_root, "enabled": enabled,
                 "disabled": disabled,
                 "message": "预置词库开关已保存；停用域只影响新转写，老稿需重跑才会更新"}


def _handle_vocab_presets_import(body: bytes) -> tuple[int, dict]:
    """预置域一键导入：合并进用户词库并 bump revision（撞基表拒收）。"""
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
        want = _take_str_list(params, "domains", allow_empty=True)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if not want:
        return 400, {"ok": False, "error": "先勾选至少一个领域再导入"}
    by_domain = {p["domain"]: p for p in _load_vocab_presets()}
    unknown = [d for d in want if d not in by_domain]
    if len(unknown) == len(want):
        return 400, {"ok": False,
                     "error": "未知领域：%s（刷新后重试）" % (",".join(unknown),)}
    try:
        from stage9 import rules_v2 as _r9  # noqa: E402  (基表 patterns)

        base_patterns = {p for p, _ in _r9.full_table()}
    except Exception:
        base_patterns = set()
    entries = _load_vocab_entries(data_root)
    entries_before = list(entries)
    existing = {e["wrong"] for e in entries}
    added = 0
    skipped_dup = 0
    rejected_base: list = []
    rejected_invalid: list = []
    dropped_overflow = 0
    for domain in want:
        preset = by_domain.get(domain)
        if preset is None:
            continue
        for cand in preset["entries"]:
            wrong, right = cand["wrong"], cand["right"]
            if wrong in base_patterns:
                rejected_base.append(wrong)
                continue
            if wrong in existing:
                skipped_dup += 1
                continue
            err = _validate_vocab_pair(wrong, right, existing, base_patterns)
            if err is not None:
                rejected_invalid.append(wrong)
                continue
            if len(entries) >= VOCAB_MAX_ENTRIES:
                dropped_overflow += 1
                continue
            entries.append({"wrong": wrong, "right": right, "source": domain})
            existing.add(wrong)
            added += 1
    if added:
        # 先落盘后注册；注册撞内存冲突则删回文件保一致（同 _handle_vocab_add）。
        try:
            _save_vocab_entries(data_root, entries)
        except OSError as exc:
            return 500, {"ok": False,
                         "error": "词库保存失败，未生效：%s→检查磁盘空间/目录权限后重试"
                                  % (_err_text(exc),)}
        try:
            reg = _register_user_rules(entries)
        except ValueError as exc:
            try:
                _save_vocab_entries(data_root, entries_before)
            except Exception:
                pass
            return 400, {"ok": False, "error": _err_text(exc)}
        revision = reg["rules_revision"]
    else:
        revision = _user_rules_revision(entries)
    labels = "/".join(by_domain[d]["label"] for d in want if d in by_domain)
    parts = ["已导入 %s 共 %d 条" % (labels or "-", added)]
    if skipped_dup:
        parts.append("跳过已存在 %d 条" % (skipped_dup,))
    if rejected_base:
        parts.append("撞内置词库拒收 %d 条" % (len(rejected_base),))
    if rejected_invalid:
        parts.append("非法拒收 %d 条" % (len(rejected_invalid),))
    if dropped_overflow:
        parts.append("词库已满丢弃 %d 条" % (dropped_overflow,))
    if unknown:
        parts.append("未知领域忽略：%s" % (",".join(unknown),))
    return 200, {"ok": True, "data_root": data_root, "domains": want,
                 "added": added, "skipped_duplicate": skipped_dup,
                 "rejected_base": rejected_base[:VOCAB_PRESET_REPORT_MAX],
                 "rejected_base_count": len(rejected_base),
                 "rejected_invalid": rejected_invalid[:VOCAB_PRESET_REPORT_MAX],
                 "rejected_invalid_count": len(rejected_invalid),
                 "dropped_overflow": dropped_overflow,
                 "unknown_domains": unknown,
                 "vocab": entries, "count": len(entries),
                 "revision": revision,
                 "effective_revision": _effective_vocab_revision(data_root, entries),
                 "message": "；".join(parts) + "。新转写自动应用。"}


def _handle_status(query: dict) -> tuple[int, dict]:
    """状态快照：只读传入的 data_root（非空则必须是绝对路径，不得静默忽略）。"""
    try:
        data_root = _query_data_root(query, DEFAULT_DATA_ROOT)
        limit = _query_int(query, "limit", 20)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    limit = max(1, min(limit, 200))
    snap = collect(data_root, limit=limit)
    # D-12：collect 的失败分支会把 state.db 真实绝对路径写进 message，
    # 那是「错误响应回绝对路径」，出网前一律抹掉（成功快照不动）。
    if isinstance(snap, dict) and snap.get("ok") is False:
        snap = dict(snap)
        for _k in ("message", "error"):
            if isinstance(snap.get(_k), str):
                snap[_k] = _err_text(snap[_k])
    # P0-2：默认只返回当前 input_root 的 runs（传参优先，监听态兜底）
    eff = ""
    try:
        raw_ir = (query.get("input_root") or [""])[0]
        eff = normalize_path(raw_ir)
        if not eff:
            with _state_lock:
                if _listener.get("running") and _listener.get("input_root"):
                    eff = str(_listener.get("input_root"))
        if eff and isinstance(snap, dict) and snap.get("ok"):
            snap = _filter_status_runs(snap, data_root, eff)
    except Exception:
        pass
    # V2.3 P0-1：runs附source_filename（取不到回“未知文件”）
    try:
        if isinstance(snap, dict) and snap.get("ok"):
            snap = _attach_source_filenames(snap, data_root)
    except Exception:
        pass
    # UX2-P0-1：全量分桶 + 截断明示（列表头 共N/成功/失败/排队，禁静默截断）
    try:
        if isinstance(snap, dict) and snap.get("ok"):
            summary = _scoped_runs_summary(data_root, eff)
            summary["shown"] = len(snap.get("recent_runs") or [])
            summary["truncated"] = (int(summary.get("total") or 0)
                                    > int(summary["shown"]))
            summary["limit"] = limit
            snap["run_summary"] = summary
    except Exception:
        pass
    # FR-12/HD-6=A：完成列表分层分页（completed_limit 默认 20，keyset cursor
    # 编码排序键；坏/被篡改游标回 400 人话，不静默）
    if isinstance(snap, dict) and snap.get("ok"):
        try:
            snap.update(_attach_completed_view(snap, data_root, eff, query))
        except ParamError as exc:
            return 400, {"ok": False, "error": str(exc)}
    return 200, snap


def _handle_browse(query: dict) -> tuple[int, dict]:
    """页面内目录浏览：只列目录，按名排序；越界/无权限/非本地盘 400（人话）。

    成功时附带视频计数：当前目录 videos{total,sample,long_estimate} +
    每子目录 counts{name: N}（单层只读，1000 上限）。
    """
    raw = (query.get("path") or [""])[0]
    path = normalize_path(raw)
    if not path:
        # 空则从用户主目录起步（本地盘，可列）
        path = os.path.expanduser("~")
    if not os.path.isabs(path):
        return 400, {"ok": False,
                     "error": "所填路径须为绝对路径（收到 %s），请点浏览重选"
                              % (_diag_redact_path(raw),)}
    real = os.path.realpath(path)
    if os.path.isfile(real):
        return 400, {"ok": False, "error": "所选路径须为目录（当前是文件）：%s，请点浏览重选" % (_diag_redact_path(real),)}
    if not os.path.isdir(real):
        return 400, {"ok": False, "error": "所选路径不存在：%s，请点浏览重选" % (_diag_redact_path(real),)}
    try:
        from stage1.ingest import probe_volume  # noqa: E402  (只读复用)
        verdict = probe_volume(real).get("verdict")
    except Exception:
        return 400, {"ok": False, "error": "所选目录不在本机硬盘上，仅支持本机硬盘，请点浏览重选"}
    if verdict != "ALLOW":
        return 400, {"ok": False, "error": "仅支持本机硬盘目录：%s，请点浏览重选" % (_diag_redact_path(real),)}
    try:
        names = sorted(
            name for name in os.listdir(real)
            if os.path.isdir(os.path.join(real, name))
            and name not in (".", "..")
        )
    except PermissionError:
        return 400, {"ok": False, "error": "所选目录无权限列出：%s，请检查权限后点浏览重选" % (_diag_redact_path(real),)}
    except OSError as exc:
        return 400, {"ok": False, "error": "所选目录列出失败：%s，请点浏览重选" % (_err_text(exc),)}
    parent = os.path.dirname(real)
    dirs = names[:1000]
    # 当前目录视频统计
    try:
        videos = _count_videos_in_dir(real)
    except Exception:
        videos = {"total": 0, "sample": [], "long_estimate": 0}
    # 每子目录视频数（单层只读）
    counts: dict = {}
    try:
        for d in dirs:
            try:
                sub = os.path.join(real, d)
                st = _count_videos_in_dir(sub)
                counts[d] = int(st.get("total") or 0)
            except Exception:
                counts[d] = 0
    except Exception:
        counts = {}
    return 200, {"ok": True, "path": real, "parent": parent, "dirs": dirs,
                 "videos": videos, "counts": counts}


# ------------------------------------------------------- 转写 worker（P0-5）

def _utc_now_iso() -> str:
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _elapsed_seconds(started_at, finished_at=None) -> int:
    """任务已用时（秒）。终态取 finished-started，运行中取 now-started。

    P1-3（RERUN-PROGRESS P3-6）：长任务页面要显示 elapsed，让用户知道
    「还在跑、可以离开页面稍后回来」，而不是误判卡死。时间戳坏/缺失回 0，
    不抛异常也不编造时长。
    """
    import datetime

    def _parse(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    begin = _parse(started_at)
    if begin is None:
        return 0
    end = _parse(finished_at) if finished_at else None
    if end is None:
        end = datetime.datetime.now(datetime.timezone.utc)
    try:
        delta = (end - begin).total_seconds()
    except TypeError:
        return 0
    return max(0, int(delta))


def _worker_record(item: dict) -> None:
    with _state_lock:
        _worker["processed"].append(item)
        _worker["processed"] = _worker["processed"][-100:]


def _worker_note_error(message: str) -> None:
    with _state_lock:
        _worker["last_error"] = message


def _transcribe_audio(job_asr_dir: str, src_path: str,
                      prompt_terms: list | None = None) -> dict:
    """转写 stage7/8（只读复用公开 API），返回 {text, segments, engine_calls}。

    短音频（<=600s）走 stage7 单文件；长音频走 stage8 分块。
    stage7/8 失败则回退 stage1 单文件直调；转写引擎缺失则抛错由上层记 verdict。
    prompt_terms：用户正词弱引导（V2.5，进 topic 层，失败忽略不拦转写）。
    """
    from stage1.asr import extract_temp_wav  # noqa: E402  (只读复用)

    os.makedirs(job_asr_dir, exist_ok=True)
    wav_path = os.path.join(job_asr_dir, "asr_16k.wav")
    audio = extract_temp_wav(src_path, wav_path)
    duration = float(audio.get("audio_duration_s") or 0.0)

    if duration <= 600:
        try:
            from stage7.prompt_builder import build_initial_prompt  # noqa: E402
            from stage7.transcribe import run_single_file_with_prompt  # noqa: E402

            try:
                _topics = ["本地", "中文"] + [t for t in (prompt_terms or [])
                                              if isinstance(t, str) and t.strip()]
                prompt = build_initial_prompt(["视频", "转写"], _topics,
                                              ["用户", "记录"])["initial_prompt"]
            except Exception:
                prompt = "视频转写，中文记录。"
            out = run_single_file_with_prompt(wav_path, prompt)
            return {"text": out.get("text", ""), "segments": out.get("segments", []),
                    "engine_calls": int(out.get("engine_calls") or 1)}
        except Exception:
            pass  # 回退 stage1 直调
    else:
        try:
            from stage8.chunk_planner import plan_chunks  # noqa: E402
            from stage8.transcribe_chunks import run_chunks  # noqa: E402

            chunks = plan_chunks(duration)
            _extra = {"global": ["视频"], "topic": ["转写"], "creator": ["记录"]}
            try:
                _pts = [t for t in (prompt_terms or [])
                        if isinstance(t, str) and t.strip()]
                if _pts:
                    _extra = {"global": ["视频"], "topic": ["转写"] + _pts,
                              "creator": ["记录"]}
            except Exception:
                pass
            term_sets = [dict(_extra) for _ in chunks]
            out = run_chunks(wav_path, term_sets)
            merged = out.get("merged") or {}
            text = merged.get("text")
            if not isinstance(text, str):
                text = "".join(
                    str(seg.get("text", "")) for seg in (merged.get("segments") or [])
                )
            return {"text": text, "segments": merged.get("segments") or [],
                    "engine_calls": int(out.get("engine_calls") or len(chunks))}
        except Exception:
            pass  # 回退 stage1 直调

    from stage1.asr import transcribe_wav_file  # noqa: E402  (只读复用)

    out = transcribe_wav_file(wav_path)
    return {"text": out.get("text", ""), "segments": out.get("segments", []),
            "engine_calls": int(out.get("asr_calls") or 1)}


def _stale_source_reason(src: dict, src_real: str,
                         probe_s: float = STALE_SOURCE_PROBE_S) -> str | None:
    """源文件在「入队 → 此刻」之间又变了？变了返回原因，没变返回 None。

    P1-FIX-1（半截不得发布）：`discover` 落库时把当时的 size/mtime 记进
    `sources`（`source_size`/`source_mtime_ns`）。这条 run 的身份就是那份快照；
    真正开跑/发布之前再核一次，任何一项对不上都说明「文件还在写，或已被换掉」，
    此时转写只能得到截断内容 → 调用方**不得进入转写/发布链路**。

    判定口径（宁可严，但不得误杀静止的真文件）：
      - size 与快照不一致 → 变（半截被追加完 / 被替换）：拦；
      - size 一致但 mtime 变了 → 再看一眼是否**此刻仍在变**（相隔 probe_s 采样）：
        仍在变 → 拦；已静止 → 放行（用户 touch / 元数据变动不该把任务判死）。
    快照缺失或 0/-1（旧行）→ 无从比对，不拦。
    """
    try:
        want_size = int(src.get("source_size"))
        want_mtime = int(src.get("source_mtime_ns"))
    except (TypeError, ValueError):
        return None
    if want_size <= 0 or want_mtime <= 0:
        return None
    try:
        first = os.stat(src_real)
    except OSError:
        return None      # 取不到 → 交给上面的「找不到」分支，不重复判定
    if first.st_size != want_size:
        return ("这个视频在排队期间还在变（大小 %d→%d）"
                % (want_size, first.st_size))
    if first.st_mtime_ns != want_mtime:
        time.sleep(probe_s)
        try:
            second = os.stat(src_real)
        except OSError:
            return "这个视频在排队期间还在变（现在读不到了）"
        if (second.st_size, second.st_mtime_ns) != (first.st_size, first.st_mtime_ns):
            return "这个视频在排队期间还在变（尺寸/时间戳仍在动）"
    return None


def _process_one_run(data_root: str, input_root: str, ob_vault_root: str | None,
                     run_id: str, profile_hash: str) -> dict:
    """单个 QUEUED AUTO run 端到端：转写→Norm/Render→Publish（vault 为空则停 Render）。"""
    from stage2 import store  # noqa: E402  (只读复用 open_db)

    con = store.open_db(data_root)
    try:
        run = con.execute(
            "SELECT * FROM processing_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            return {"run_id": run_id, "state": "SKIP", "verdict": "run vanished"}
        run = dict(run)
        if run.get("status") != "QUEUED" or run.get("creation_mode") != "AUTO":
            return {"run_id": run_id, "state": "SKIP", "verdict": "not QUEUED/AUTO"}
        source_id = run.get("source_id")
        src = con.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if src is None:
            out = {"run_id": run_id, "state": "FAIL",
                   "source_filename": "未知文件",
                   "verdict": "视频记录找不到了→请重新把视频放进文件夹再试"}
            _worker_record(out)
            return out
        src = dict(src)
    finally:
        con.close()

    src_path = src.get("current_path") or ""
    src_real = os.path.realpath(src_path)
    input_real = os.path.realpath(input_root)
    vault_real = os.path.realpath(ob_vault_root) if ob_vault_root else None
    # V2.3 P0-1：面向用户文案用文件名（取不到回“未知文件”，run_id只留详情区）
    try:
        _src_fn = os.path.basename(str(src_path).strip()) or "未知文件"
        if not _src_fn.strip():
            _src_fn = "未知文件"
    except Exception:
        _src_fn = "未知文件"
    if not os.path.isfile(src_real):
        out = {"run_id": run_id, "state": "FAIL",
               "source_filename": _src_fn,
               "verdict": "源视频文件找不到了：%s→检查视频是否被移动或删除，补回后点重试" % (_src_fn,)}
        _worker_record(out)
        return out
    # P0-1 防守：worker 已按视频文件夹前缀过滤才调用；此处命中说明是旧文件夹
    # 残留，直接忽略（不计数、不记 processed、不打扰），由调用方不再传入。
    if not _is_under_root(src_real, input_real):
        return {"run_id": run_id, "state": "SKIP",
                "verdict": "源不在当前视频文件夹内，已忽略"}
    # Windows（Stage 2 接线）：这一条**真正会写入**的三条路径先过长路径闸
    # （源视频 / 本条 job 目录 / 目标笔记落点），超限就在送 Whisper 之前
    # 拦下——能早拦就早拦，不白跑一次听写，也不写任何半成品。
    job_dir = os.path.join(os.path.abspath(data_root), "data", "jobs", run_id)
    _too_long = _win_path_too_long([
        ("这个视频的路径", src_real),
        ("这条任务的临时目录", job_dir),
        ("目标笔记落点",
         _vault_publish_target(input_real, vault_real, src_real)),
    ])
    if _too_long:
        out = {"run_id": run_id, "state": "FAIL",
               "source_filename": _src_fn,
               "verdict": _too_long, "whisper_calls": 0}
        _worker_record(out)
        _worker_clear_current()
        return out
    # DEVELOP-P1-9 第 0 道门（**送 engine 之前**）：这条视频的目标笔记在笔记库里
    # 已经存在 → 直接判 SKIPPED，whisper 一次都不跑，不写 vault、既有一字节不动
    # （No-Clobber 语义不变）。目标路径复用入库同一真源 `_app_resolve_canonical`，
    # 不自己发明命名。旧账只在 DB 里认（失忆就白跑），这里补上磁盘真相这一道。
    existing_note = _vault_note_already_there(input_real, vault_real, src_real)
    if existing_note:
        out = {"run_id": run_id, "state": "SKIPPED",
               "source_filename": _src_fn,
               "verdict": "笔记已存在（未覆盖），已跳过转写：%s"
                          "→要更新这篇，先删除或改名它，再点重试"
                          % (existing_note,),
               "whisper_calls": 0,
               "canonical_output_path": existing_note}
        # 只留一条轻量 receipt（不建 raw/asr、不进 raw/render 链路），
        # 让「已跳过」重启后仍可查、诊断面板也能给明确类别。
        manifest_path = os.path.join(job_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            try:
                os.makedirs(job_dir, exist_ok=True)
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump({"job_id": run_id, "source_id": source_id,
                               "run_id": run_id, "stage": "app-worker",
                               "state": "SKIPPED",
                               "receipts": [{
                                   "stage": "app-worker", "state": "SKIPPED",
                                   "run_id": run_id, "source_id": source_id,
                                   "verdict": out["verdict"],
                                   "whisper_calls": 0,
                                   "canonical_output_path": existing_note,
                                   "created_at": _utc_now_iso()}],
                               "created_at": _utc_now_iso()},
                              fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
        _worker_record(out)
        return out
    # P1-FIX-1 第一道发布门（转写前）：源快照对不上就别开跑，省下整段算力，
    # 也避免把半截内容送进 raw/render/publish 链路。
    stale = _stale_source_reason(src, src_real)
    if stale:
        out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
               "verdict": "%s→这一条已跳过，不会写进笔记库；等文件写完后它会自动"
                          "按最新内容重新排队，不用手动重试" % (stale,)}
        _worker_record(out)
        _worker_clear_current()
        return out

    filename = os.path.basename(src_real) or run_id
    _worker_set_current(run_id, filename, STAGE_DISCOVER)

    os.makedirs(os.path.join(job_dir, "raw"), exist_ok=True)
    os.makedirs(os.path.join(job_dir, "asr"), exist_ok=True)
    manifest_path = os.path.join(job_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"job_id": run_id, "source_id": source_id, "run_id": run_id,
                       "stage": "app-worker", "state": "WORKING", "receipts": [],
                       "created_at": _utc_now_iso()}, fh, ensure_ascii=False, indent=2)

    def _receipt(state: str, verdict: str, extra: dict | None = None) -> dict:
        from stage3 import lineage as _lineage  # noqa: E402  (只读复用)

        entry = {"stage": "app-worker", "state": state, "run_id": run_id,
                 "source_id": source_id, "verdict": verdict,
                 "created_at": _utc_now_iso()}
        if extra:
            entry.update(extra)
        try:
            _lineage.record_lineage_manifest(manifest_path, receipts=[entry])
        except Exception:
            pass
        return entry

    # 1. 转写（阻塞调用：进前先写 current，出后推进到下一阶段；终态统一清）
    _worker_set_current(run_id, filename, STAGE_TRANSCRIBING)
    try:
        # V2.5：用户正词弱引导进 prompt（失败回空，不拦转写）
        try:
            _prompt_terms = _user_prompt_terms(data_root)
        except Exception:
            _prompt_terms = []
        tres = _transcribe_audio(os.path.join(job_dir, "asr"), src_real,
                                 prompt_terms=_prompt_terms)
    except Exception as exc:
        verdict = "听写这段视频失败了：%s→换一个有声音的视频再试，或点重试" % (_err_text(exc),)
        _receipt("TRANSCRIBE_FAILED", verdict, {"whisper_calls": 0})
        out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
               "verdict": verdict, "whisper_calls": 0}
        _worker_record(out)
        _worker_clear_current()
        return out
    engine_calls = int(tres.get("engine_calls") or 0)
    text = tres.get("text") or ""
    segments = tres.get("segments") or []
    segments_n = len(segments)
    if not segments:
        segments = [{"text": "（本段无语音内容，转写为空）", "start": 0.0, "end": 0.0}]

    # 2. 组 Raw（过 stage1 schema 门）并落盘
    from stage1.asr import FROZEN_MODEL_REPO, FROZEN_MODEL_REVISION  # noqa: E402
    from stage1.prepare import build_raw_content, validate_raw_artifact  # noqa: E402

    content_identity = src.get("content_identity") or ""
    raw_artifact_id = "raw_" + run_id
    post_verify = {"verdict": "PASS", "code": "COMMITTING_RAW_ASR",
                   "current_hash": content_identity, "hash_match": True,
                   "commit_authorized": True}
    asr_profile = {
        "actual": {"model": FROZEN_MODEL_REPO, "model_revision": FROZEN_MODEL_REVISION,
                   "audio_mode": "temp", "word_timestamps": False,
                   "no_speech_threshold": 0.6},
        "frozen": {"model": FROZEN_MODEL_REPO, "model_revision": FROZEN_MODEL_REVISION,
                   "audio": "temp-wav 16k mono (FFmpeg on-disk, re-runnable)"},
    }
    try:
        raw_content = build_raw_content(
            run_id, {"source_id": source_id, "content_identity": content_identity},
            text, segments, asr_profile, post_verify)
        validate_raw_artifact(raw_content)
    except Exception as exc:
        verdict = "整理初稿时数据异常：%s→换一个视频再试，或点重试" % (_err_text(exc),)
        _receipt("RAW_FAILED", verdict, {"whisper_calls": engine_calls})
        out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
               "verdict": verdict,
               "whisper_calls": engine_calls}
        _worker_record(out)
        _worker_clear_current()
        return out
    raw_path = os.path.join(job_dir, "raw", "raw.json")
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(raw_content, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    # 3. Norm / Render（stage3 公开 API）
    # Norm 用用户词库 profile（新转写自动应用，Case4 语义），
    # Render 用 stage9 分段 profile（para-v2.7 目标220/封顶450/防碎80，单源头）。
    from stage3 import normalize as _norm  # noqa: E402  (只读复用)
    from stage3 import render as _rend  # noqa: E402  (只读复用)

    if ob_vault_root:
        try:
            # V2.3 P0-3：app侧单扩展名包装（src零碰；只影响新产出，旧md不动）
            mapping = _app_resolve_canonical(input_real, vault_real, src_real)
            canonical_probe = mapping["canonical_output_path"]
            source_rel = mapping["source_relative_path"]
        except Exception as exc:
            verdict = "笔记库路径映射失败：%s→检查库目录是否存在与可写，修正后点重试" % (_err_text(exc),)
            _receipt("MIRROR_FAILED", verdict, {"whisper_calls": engine_calls})
            out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
                   "verdict": verdict,
                   "whisper_calls": engine_calls}
            _worker_record(out)
            _worker_clear_current()
            return out
    else:
        canonical_probe = None
        source_rel = None

    _worker_set_current(run_id, filename, STAGE_NORMALIZING)
    con = store.open_db(data_root)
    try:
        norm = _norm.create_normalization_revision(
            con, job_dir, raw_artifact_id,
            _norm_profile_for_new_jobs(data_root),
            source_id=source_id, run_id=run_id)
        _worker_set_current(run_id, filename, STAGE_RENDERING)
        stem = os.path.splitext(os.path.basename(src_real))[0] or run_id
        _render_profile = _render_profile_for_new_jobs()
        rend = _rend.create_render_revision(
            con, job_dir, norm["normalized_artifact_id"],
            _render_profile,
            title=stem, canonical_probe_path=canonical_probe,
            source_id=source_id, run_id=run_id)
        # 生产后处理（新转写入口）：冻结引擎已 mint 不断链，app 侧用
        # render_with_v2 重算覆写，阈值取 stage9.PARA_PARAMS_V2 单源头
        # （para-v2.7：目标220/封顶450/防碎80）。
        try:
            _fix = _apply_v25_postpass(
                job_dir, norm.get("final_path"), rend.get("final_path"),
                stem, _render_profile)
            try:
                _receipt("V25_POSTPASS",
                         "后处理 fixed=%s paras=%s max=%s" % (
                             _fix.get("fixed"), _fix.get("paras"),
                             _fix.get("max_len")),
                         {"whisper_calls": engine_calls,
                          "render_revision_id": rend.get("render_revision_id"),
                          "v25_fixed": bool(_fix.get("fixed"))})
            except Exception:
                pass
        except Exception:
            pass
        con.execute(
            "UPDATE processing_runs SET raw_artifact_id=?,"
            " initial_normalization_revision_id=?, initial_render_revision_id=?,"
            " updated_at=? WHERE run_id=?",
            (raw_artifact_id, norm["normalization_revision_id"],
             rend["render_revision_id"], store.utc_now_iso(), run_id),
        )
        con.commit()
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        con.close()
        verdict = "整理成稿失败：%s→点重试再试一次，或换一个视频" % (_err_text(exc),)
        _receipt("NORM_RENDER_FAILED", verdict, {"whisper_calls": engine_calls})
        out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
               "verdict": verdict,
               "whisper_calls": engine_calls}
        _worker_record(out)
        _worker_clear_current()
        return out

    # 4. 入库（笔记库为空则只存数据目录）
    # P0-4：DB processing_runs.status 保持 Stage2 枚举（QUEUED/FAILED_RETRYABLE/
    # NO_SPEECH_DETECTED），不直接写 PUBLISHED/RENDER_ONLY（会触发
    # assert_no_transcription_states FAIL）；终态由 worker.processed 透出，
    # 页面做终态映射显示，保证 runs 表不再全 QUEUED 误导。
    if not ob_vault_root:
        verdict = ("未填笔记库，已生成初稿→初稿在数据目录：%s" % (rend.get("final_path"),))
        _receipt("RENDER_ONLY", verdict,
                 {"whisper_calls": engine_calls,
                  "render_revision_id": rend.get("render_revision_id"),
                  "rendered_path": rend.get("final_path"),
                  "render_verdict": rend.get("verdict")})
        con.close()
        out = {"run_id": run_id, "state": "RENDER_ONLY", "source_filename": _src_fn,
               "verdict": verdict,
               "whisper_calls": engine_calls,
               "rendered_path": rend.get("final_path")}
        _worker_record(out)
        _worker_clear_current()
        return out

    _worker_set_current(run_id, filename, STAGE_PUBLISHING)
    # P1-FIX-1 第二道发布门（写笔记库前）：转写这几分钟里源文件又变了（半截被
    # 追加完/被替换）→ 现在写的笔记就是截断内容，宁可晚跑重跑也不落盘。
    stale = _stale_source_reason(src, src_real)
    if stale:
        out = {"run_id": run_id, "state": "FAIL", "source_filename": _src_fn,
               "verdict": "%s→这条没有写进笔记库（避免留下截断的稿子）；"
                          "它会在文件写完后按最新内容重新排队，不用手动重试"
                          % (stale,)}
        try:
            _receipt("STALE_SOURCE_SKIPPED", out["verdict"],
                     {"whisper_calls": engine_calls})
        except Exception:
            pass
        _worker_record(out)
        _worker_clear_current()
        return out
    try:
        from stage4.publish import initial_publish  # noqa: E402  (只读复用)
        pub = initial_publish(con, job_dir, rend["render_revision_id"],
                              vault_real, source_rel)
        status = pub.get("status")
        # V2.3 P0-3：新产出单扩展名（09.xxx.mp4→09.xxx.md），旧md不动不改名，如实注明。
        _naming_note = "（命名：去扩展名单.md，如 09.xxx.mp4→09.xxx.md；旧文件不动）"
        if status == "PUBLISHED":
            verdict = "已存入你的笔记库：%s%s" % (
                pub.get("canonical_output_path"), _naming_note)
            state = "PUBLISHED"
        elif _pub_target_exists(status):
            # DEVELOP-P1-9：真因是「库里已有同名笔记，未覆盖」（No-Clobber 保护），
            # 不是权限问题——旧文案「检查笔记库权限」会把人带沟里，这里改准；
            # 文案里明说「不算失败」，状态仍留 PUBLISH_BLOCKED 以便用「重跑」重排成稿后重入库。
            verdict = ("笔记库已有同名笔记，未覆盖（不算失败）：%s"
                       "→要更新这篇，先删除或改名它再点重试%s"
                       % (pub.get("canonical_output_path"), _naming_note))
            state = "PUBLISH_BLOCKED"
        else:
            verdict = "入库未完成，初稿已保留%s→检查笔记库权限后点重试" % (_naming_note,)
            state = "PUBLISH_BLOCKED"
        _receipt(state, verdict,
                 {"whisper_calls": engine_calls,
                  "render_revision_id": rend.get("render_revision_id"),
                  "rendered_path": rend.get("final_path"),
                  "publish_record_id": pub.get("publish_record_id"),
                  "canonical_output_path": pub.get("canonical_output_path"),
                  "publish_status": status})
        out = {"run_id": run_id, "state": state, "source_filename": _src_fn,
               "verdict": verdict,
               "whisper_calls": engine_calls,
               "rendered_path": rend.get("final_path"),
               "canonical_output_path": pub.get("canonical_output_path")}
        _worker_record(out)
        _worker_clear_current()
        return out
    except Exception as exc:
        # 入库受阻等：字节未动，初稿保留，记 verdict 不炸 worker
        verdict = "笔记库不可写：%s→检查库路径权限后点重试" % (_err_text(exc),)
        _receipt("PUBLISH_BLOCKED", verdict,
                 {"whisper_calls": engine_calls,
                  "render_revision_id": rend.get("render_revision_id"),
                  "rendered_path": rend.get("final_path")})
        out = {"run_id": run_id, "state": "PUBLISH_BLOCKED",
               "source_filename": _src_fn,
               "verdict": verdict,
               "whisper_calls": engine_calls,
               "rendered_path": rend.get("final_path")}
        _worker_record(out)
        _worker_clear_current()
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass


def _transcribe_worker(data_root: str, input_root: str, ob_vault_root: str | None,
                       profile_hash: str) -> None:
    """单 worker 串行：只取源在当前视频文件夹内的 QUEUED AUTO run。

    查询时按 source.current_path realpath 前缀过滤；不在当前
    文件夹的直接忽略（不计数、不记 processed、不进 done、不打扰）。
    每轮只处理新增（_worker_done 去重），未见新 run 则 sleep 等待。
    P1-2：磁盘已有成功输出直接跳过（不重转），失败可重试。
    V2.2补修 P0-2：DB已COMPLETED旧账同步跳过（磁盘缺口补齐，不得重转；
    whisper不再跑；显式重试可经 /api/retry 重排）。
    P1-1：未知异常补 FAIL 记录再进 done，不静默丢任务。
    """
    from stage2 import store  # noqa: E402  (只读复用)

    with _state_lock:
        _worker["running"] = True
    try:
        input_real = os.path.realpath(input_root)
        while True:
            with _state_lock:
                keep = _listener["running"]
            if not keep:
                break
            # 每轮读一次磁盘成功集，供跳过已完成（重启不重转）
            try:
                disk_ok = _scan_disk_states(str(data_root))
                disk_done_ids = {k for k, v in disk_ok.items()
                                 if isinstance(v, dict) and v.get("state") in DONE_STATES}
            except Exception:
                disk_done_ids = set()
            # V2.2补修 P0-2：DB成功旧账（norm COMPLETED）缺口补齐
            try:
                db_done_ids = _db_success_run_ids_ro(str(data_root), str(input_root))
            except Exception:
                db_done_ids = set()
            skip_old = set(disk_done_ids) | set(db_done_ids)
            try:
                con = store.open_db(data_root)
                try:
                    rows = con.execute(
                        "SELECT pr.run_id, s.current_path FROM processing_runs pr"
                        " LEFT JOIN sources s ON pr.source_id=s.source_id"
                        " WHERE pr.status='QUEUED' AND pr.creation_mode='AUTO'"
                        " ORDER BY pr.created_at"
                    ).fetchall()
                    run_ids = []
                    for r in rows:
                        run_id = r[0]
                        cur = r[1] if len(r) > 1 else None
                        if cur is None:
                            # 孤儿 run：保留给 _process_one_run 记 FAIL
                            run_ids.append(run_id)
                            continue
                        if _is_under_root(str(cur), input_real):
                            run_ids.append(run_id)
                        # 不在当前文件夹：直接忽略，不计数不记录
                finally:
                    con.close()
            except Exception as exc:
                _worker_note_error("轮询待处理任务失败：%s" % (_err_text(exc),))
                run_ids = []
            for run_id in run_ids:
                with _state_lock:
                    keep = _listener["running"]
                if not keep:
                    break
                if run_id in _worker_done:
                    continue
                # P1-2 + V2.2 P0-2：磁盘/DB已有成功输出则跳过（记 done，不重转）
                if run_id in skip_old:
                    with _state_lock:
                        _worker_done.add(run_id)
                    continue
                res = None
                try:
                    res = _process_one_run(data_root, input_root, ob_vault_root,
                                           run_id, profile_hash)
                    # 防守回来的 SKIP（旧文件夹残留）：不进 done，下轮
                    # 查询过滤已会忽略，此处不计数不记录。
                    if isinstance(res, dict) and res.get("state") == "SKIP":
                        continue
                except Exception as exc:  # noqa: BLE001  (单 run 失败不炸 worker)
                    # V2.3 P0-1：面向用户文案用文件名（查不到回未知文件，不暴露裸run_id）
                    try:
                        _fm = _run_source_path_map(str(data_root)).get(run_id)
                        _ffn = (os.path.basename(str(_fm).strip())
                                if isinstance(_fm, str) and str(_fm).strip()
                                else "未知文件") or "未知文件"
                    except Exception:
                        _ffn = "未知文件"
                    _worker_note_error("任务 %s 处理时遇到意外：%s"
                                       % (_ffn, _err_text(exc)))
                    _worker_clear_current()
                    # P1-1：未知异常补 FAIL 记录再进 done
                    try:
                        res = {"run_id": run_id, "state": "FAIL",
                               "source_filename": _ffn,
                               "verdict": "处理时遇到意外：%s→点重试再试一次" % (_err_text(exc),)}
                        _worker_record(dict(res))
                    except Exception:
                        try:
                            res = {"run_id": run_id, "state": "FAIL",
                                   "source_filename": "未知文件",
                                   "verdict": "处理时遇到意外→点重试再试一次"}
                        except Exception:
                            res = None
                # 统一收口：非 SKIP 才记 done；SKIP 已 continue
                try:
                    is_skip = isinstance(res, dict) and res.get("state") == "SKIP"
                except Exception:
                    is_skip = False
                if not is_skip:
                    with _state_lock:
                        _worker_done.add(run_id)
            for _ in range(50):  # 5s 间隔，可被停机打断
                with _state_lock:
                    keep = _listener["running"]
                if not keep:
                    break
                threading.Event().wait(0.1)
    finally:
        with _state_lock:
            _worker["running"] = False
            _worker["current"] = None


def _launch(data_root: str, input_root: str, ob_vault_root: str | None,
            profile_hash: str) -> None:
    """后台线程目标：run_startup 成功后起转写 worker；跑完即更新状态。

    V2.2补修 P0-1/P0-3：全终态放行记 skipped（verdict/绿条用）；
    GATE_STAGE3_BLOCKED 只在真半截时由 _humanize_startup_error 产出。
    """
    import datetime

    with _state_lock:
        _listener["started_at"] = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    try:
        # P0-1：启动前按当前 input 安装作用域断言（他 input 历史行不再卡门）
        _install_scoped_gates(input_root)
        try:
            handle = run_startup(data_root, input_root, profile_hash)
        finally:
            _restore_scoped_gates()
    except Exception as exc:  # noqa: BLE001  (错误进状态，不炸进程)
        msg, code, sugg = _humanize_startup_error(exc, data_root)
        with _state_lock:
            _listener["running"] = False
            _listener["error"] = msg
            _listener["error_code"] = code
            _listener["suggested_data_root"] = sugg
            # 真半截才 BLOCK：失败时清旧账计数，避免绿条残留
            _listener["skipped_terminal"] = 0
            _listener["skipped_terminal_rows"] = 0
            _listener["startup_note"] = None
        return
    # 全终态放行：门内 skipped 落 _STARTUP_SCOPE，换算成人话进 _listener
    try:
        _rows = int(_STARTUP_SCOPE.get("skipped_terminal_rows") or 0)
    except Exception:
        _rows = 0
    try:
        _runs = int(_STARTUP_SCOPE.get("skipped_terminal_runs") or 0)
    except Exception:
        _runs = 0
    # P0-2 缺口：DB成功旧账预进 done，首轮 pending 即排除旧完成（不重转）
    try:
        _pre_skip = _db_success_run_ids_ro(str(data_root), str(input_root))
    except Exception:
        _pre_skip = set()
    try:
        _disk_pre = _scan_disk_states(str(data_root))
        _pre_disk = {k for k, v in _disk_pre.items()
                     if isinstance(v, dict) and v.get("state") in DONE_STATES}
    except Exception:
        _pre_disk = set()
    try:
        _pre_all = set(_pre_skip) | set(_pre_disk)
    except Exception:
        _pre_all = set()
    # 绿条 N 取旧完成视频数（run 去重；无 run 但有终态行时回落行数）
    _n = len(_pre_all) if _pre_all else (_runs if _runs else _rows)
    try:
        _note = ("旧%d条已完成记录，本次跳过" % (_n,)) if _n > 0 else None
    except Exception:
        _note = None
    with _state_lock:
        _listener["error"] = None
        _handle["box"] = handle
        _listener["skipped_terminal"] = int(_n or 0)
        _listener["skipped_terminal_rows"] = int(_rows or 0)
        _listener["startup_note"] = _note
        try:
            for _rid in _pre_all:
                _worker_done.add(_rid)
        except Exception:
            pass
    # run_startup 返回后 watcher/workers 常驻（daemon 线程）；running 保持 True。
    # P0-5：转写 worker 单线程串行，常驻轮询 QUEUED 的 AUTO run。
    worker = threading.Thread(
        target=_transcribe_worker,
        args=(data_root, input_root, ob_vault_root, profile_hash),
        name="v2o-transcribe-worker", daemon=True,
    )
    worker.start()


def _handle_start_post(body: bytes) -> tuple[int, dict]:
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "请求体须为 JSON"}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    with _state_lock:
        if _listener["running"]:
            return 409, {"ok": False, "error": "已经在监听同一文件夹，无需重复操作"}

    data_root = normalize_path(params.get("data_root")) or DEFAULT_DATA_ROOT
    input_root = normalize_path(params.get("input_root"))
    ob_vault_root = normalize_path(params.get("ob_vault_root"))
    profile_hash = str(params.get("asr_profile_hash") or DEFAULT_PROFILE_HASH)

    if not os.path.isabs(data_root):
        return 400, {"ok": False, "error": "数据目录须为绝对路径，请点浏览重选"}
    if not input_root:
        return 400, {"ok": False, "error": "请填写视频文件夹绝对路径"}
    if not os.path.isabs(input_root):
        return 400, {"ok": False, "error": "视频文件夹须为绝对路径：%s，请点浏览重选" % (input_root,)}
    vault: str | None = ob_vault_root or None
    # Windows（Stage 2 接线）：三个根在接受前先过长路径 + UNC 闸，
    # 超限/网络盘直接 400 人话，零执行、零写盘（此处尚未 makedirs）。
    _blk = _win_root_blockers(data_root, input_root, vault)
    if _blk:
        return 400, {"ok": False, "code": "BLOCKED_WIN_ROOT_PATH",
                     "error": _blk[0]}
    if not os.path.isdir(input_root):
        return 400, {"ok": False, "error": "视频文件夹路径不存在：%s，请点浏览重选" % (input_root,)}
    if vault is not None:
        if not os.path.isabs(vault):
            return 400, {"ok": False, "error": "笔记库目录须为绝对路径：%s，请点浏览重选" % (vault,)}
        if not os.path.isdir(vault):
            return 400, {"ok": False, "error": "笔记库目录不存在：%s，请点浏览重选" % (vault,)}
    try:
        os.makedirs(os.path.join(os.path.abspath(data_root), "data"), exist_ok=True)
    except OSError as exc:
        return 400, {"ok": False, "error": "数据目录不可用：%s，请检查权限" % (_err_text(exc),)}

    # 人话预检：转写必须在就绪的 python 下跑（probe，不 hard 依赖）
    if not _asr_engine_available():
        return 400, {"ok": False, "code": CODE_ASR_MISSING,
                     "error": "转写环境没就绪：请用自带一键启动重开，再点开始监听"}

    with _state_lock:
        _listener.update(
            {"running": True, "data_root": data_root,
             "input_root": input_root, "ob_vault_root": vault, "error": None,
             "error_code": None, "suggested_data_root": None,
             "skipped_terminal": 0, "skipped_terminal_rows": 0,
             "startup_note": None}
        )
        _last_config.update(
            {"last_input_root": input_root, "last_data_root": data_root,
             "last_ob_vault_root": vault}
        )
        _worker_done.clear()
        _worker["processed"] = []
        _worker["current"] = None
        _worker["last_error"] = None
    thread = threading.Thread(
        target=_launch, args=(data_root, input_root, vault, profile_hash), daemon=True
    )
    thread.start()
    return 202, {"ok": True, "running": True,
                 "data_root": data_root, "input_root": input_root,
                 "ob_vault_root": vault}


def _handle_stop_post(body: bytes) -> tuple[int, dict]:
    """UX-P0-2/UX2-P1-6：停监听＋停 worker 轮询；当前任务会跑完收尾。

    实现真相（写死）：停在“接新任务”这一层——正在转的这一个会跑完收尾
    （不清 current、不假报 worker 停），worker 线程收尾后自行清 current 并退出。
    因此这里不立即清 _worker["current"]，由前端据 current 显示“正在收尾…”，
    current 清空即为真停（重起后已完成跳过、半截重转）。
    """
    with _state_lock:
        was = bool(_listener.get("running"))
        _listener["running"] = False
        _listener["error"] = None
        _listener["error_code"] = None
        _listener["suggested_data_root"] = None
        box = _handle.get("box")
    # 停 watcher/workers（best-effort，不炸）；app 自己的 worker 线程按 listener 标志退出
    try:
        if isinstance(box, dict):
            try:
                from stage5.startup import shutdown as _shutdown  # noqa: E402
                _shutdown(box)
            except Exception:
                try:
                    w = box.get("watcher")
                    if w is not None:
                        w.stop()
                except Exception:
                    pass
                try:
                    ws = box.get("workers")
                    if ws is not None:
                        ws.stop()
                except Exception:
                    pass
    except Exception:
        pass
    with _state_lock:
        _handle["box"] = None
        cur = dict(_worker["current"]) if _worker.get("current") else None
    finishing = bool(cur and cur.get("run_id"))
    if finishing:
        msg = ("已停止接新任务，当前这个（%s）会跑完收尾；"
               "重起后已完成跳过、半截重转。" % (cur.get("filename") or "当前任务",))
    else:
        msg = "已停止接新任务；重起后已完成跳过、半截重转。"
    return 200, {"ok": True, "running": False, "was_running": was,
                 "finishing": finishing, "current": cur, "message": msg}


def _handle_retry_post(body: bytes) -> tuple[int, dict]:
    """UX-P0-4/P1-1：单 run 重跑，幂等（同 run 去重，不产生重复任务）。

    P1-3/P2-7 契约扩展：可选 `data_root`（严格取参层 `_take_data_root`）。
      - 缺键/空串 → 沿用旧行为，取当前监听目录（向后兼容）；
      - 非字符串/相对路径 → 400 人话，零执行；
      - 显式给了且与当前监听目录不是同一 realpath → 409 人话零执行，
        绝不静默按监听目录重排（防误操作别的目录 / 跨目录串任务）。
    """
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象，点重试再试一次"}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象，点重试再试一次"}
    # 严格取参：坏类型/相对路径先 400，再谈有没有监听（参数错了不给任何执行机会）
    try:
        want_root = _take_data_root(params, "", "data_root")
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return 400, {"ok": False, "error": "缺少任务编号，请刷新后点重试"}
    with _state_lock:
        running = bool(_listener.get("running"))
        cur = dict(_worker["current"]) if _worker.get("current") else None
        listener_root = normalize_path(_listener.get("data_root"))
    if not running:
        return 400, {"ok": False, "error": "监听未启动，先点开始监听再点重试"}
    if want_root:
        # 目录身份按 realpath 判：符号链接/尾斜杠/`..` 指向同一目录不算跨目录
        if not listener_root or os.path.realpath(want_root) != os.path.realpath(
                listener_root):
            return 409, {"ok": False,
                         "error": "这次重试针对的是另一个数据目录（零执行，未重排任何任务）；"
                                  "请先核对页面上方的数据目录与当前监听的目录是否一致，"
                                  "避免误操作别的目录"}
        data_root = want_root
    else:
        data_root = listener_root
    if cur and cur.get("run_id") == run_id:
        return 202, {"ok": True, "run_id": run_id, "state": "RUNNING",
                     "message": "该任务正在处理，无需重复操作"}
    # 已在排队（不在 done）则幂等直接回
    with _state_lock:
        already_queued = run_id not in _worker_done
    # 查 DB 是否存在该 run（只读校验，不写库）
    try:
        if data_root:
            from stage2 import store as _store  # noqa: E402
            con = _store.open_db(str(data_root))
            try:
                row = con.execute(
                    "SELECT run_id FROM processing_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                try:
                    con.close()
                except Exception:
                    pass
            if row is None:
                return 404, {"ok": False, "error": "任务不存在，请刷新后重试"}
    except Exception:
        # DB 暂时不可读则仍允许重试（worker 下轮会记原因），不拦
        pass
    with _state_lock:
        if run_id in _worker_done:
            _worker_done.discard(run_id)
            # 删掉旧终态，下轮新结果覆盖，计数不翻倍
            _worker["processed"] = [p for p in _worker["processed"]
                                    if not (isinstance(p, dict) and p.get("run_id") == run_id)]
            return 202, {"ok": True, "run_id": run_id, "state": "QUEUED",
                         "message": "已重新排队，只重跑这一个"}
        else:
            return 202, {"ok": True, "run_id": run_id, "state": "QUEUED",
                         "message": "该任务已在排队，无需重复操作"}


def _handle_clear_post(body: bytes) -> tuple[int, dict]:
    """UX2-P0-2/P1-3：清空本目录任务（先明示代价、可只清失败、给撤销指引）。

    删当前 input 的 discovery_candidates + processing_runs 记录级联
    （artifacts / Stage3+ 修订 / publish_records / archive / state_events
    中归属本 input 的行 + 本 input run 的 data/jobs/<run_id> 目录 +
    内存 worker 痕迹）。源视频文件不动、vault md 不动、他 input 的行不动、
    其他 data_root 不动。删完后重起监听会把文件当新任务重新发现（可重转）。

    - dry_run=true：只读算代价（将删 DB N 条＋jobs M 个 / 成功A失败B排队C /
      预计重转约 X 分钟），不写库、不删盘，供“清空前明示代价”。
    - only_failed=true：只删失败 run 及其修订链与 jobs，成功记录保留。
    - P1-3：运行中拒清（409人话，需先停止）；空归属/NULL archive 纳入清理
      （门侧 fail-closed 计挡住，清理侧同步可清，否则清不掉却挡门不一致）。
    """
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
        dry_run = _take_bool(params, "dry_run", False)
        only_failed = _take_bool(params, "only_failed", False)
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    input_root = normalize_path(params.get("input_root"))
    if not os.path.isabs(data_root):
        return 400, {"ok": False, "error": "数据目录须为绝对路径，请点浏览重选"}
    if not input_root:
        return 400, {"ok": False, "error": "请填写要清空的视频文件夹绝对路径"}
    if not os.path.isabs(input_root):
        return 400, {"ok": False, "error": "视频文件夹须为绝对路径，请点浏览重选"}
    db_path = os.path.join(os.path.abspath(data_root), "data", "state.db")
    if not os.path.isfile(db_path):
        return 404, {"ok": False,
                     "error": "该数据目录下还没有任务库，无需清空：%s"
                              % (_diag_redact_path(db_path),)}

    # ---- dry_run：只读算代价，不写库、不删盘 ----
    if dry_run:
        con = _open_ro(data_root)
        if con is None:
            return 500, {"ok": False,
                         "error": "任务库暂时读不出，请稍后重试或检查数据目录"}
        try:
            plan = _clear_plan(con, data_root, input_root, only_failed=False)
        except Exception as exc:
            return 500, {"ok": False,
                         "error": "核对清空代价失败：%s，稍后重试" % (_err_text(exc),)}
        finally:
            try:
                con.close()
            except Exception:
                pass
        if not plan.get("exists"):
            return 200, {"ok": True, "dry_run": True, "no_tasks": True,
                         "data_root": data_root, "input_root": input_root,
                         "message": "本目录下没有任务记录，无需清空"}
        full_runs = len(plan["run_ids"])
        full_jobs = _count_job_dirs(data_root, plan["run_ids"])
        of_runs = len(plan["fail_run_ids"])
        of_jobs = _count_job_dirs(data_root, plan["fail_run_ids"])
        preview = {
            "db_runs": full_runs, "jobs": full_jobs,
            "total": plan["total"], "success": plan["success"],
            "failed": plan["failed"], "pending": plan["pending"],
            "est_retranscribe_minutes": _est_retranscribe_minutes(full_runs),
            "avg_sec_per_video": CLEAR_AVG_SEC_PER_VIDEO,
            "only_failed": {
                "db_runs": of_runs, "jobs": of_jobs,
                "est_retranscribe_minutes": _est_retranscribe_minutes(of_runs),
                "kept_success": plan["success"],
            },
            "vault_note": "笔记库 md 与源视频不动",
        }
        msg = ("将删DB %d条＋jobs %d个；vault md 与源视频不动；"
               "重起需重转约%d分钟（按每条约%d秒估算）。"
               % (full_runs, full_jobs,
                  preview["est_retranscribe_minutes"],
                  CLEAR_AVG_SEC_PER_VIDEO))
        if plan["failed"] > 0:
            msg += ("失败%d条建议先在失败行逐条点「重试」，确需清空再清。"
                    % (plan["failed"],))
        return 200, {"ok": True, "dry_run": True, "data_root": data_root,
                     "input_root": input_root, "preview": preview,
                     "message": msg}

    # ---- 实清：运行中拒清（P1-3），需先停止 ----
    with _state_lock:
        if _listener.get("running"):
            return 409, {"ok": False,
                         "error": "正在监听/转写中，请先点停止再清空（运行中清空会丢任务）"}

    # 开库：锁在手则走 store 门，否则直连读写（同进程，busy 等待），禁碰他库
    con = None
    try:
        from stage2 import store as _st  # noqa: E402

        if _st.is_held(data_root):
            con = _st.open_db(data_root)
    except Exception:
        con = None
    if con is None:
        try:
            con = sqlite3.connect(db_path, timeout=30.0,
                                  check_same_thread=False)
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA foreign_keys=ON")
            con.row_factory = sqlite3.Row
        except Exception as exc:
            return 500, {"ok": False,
                         "error": "任务库打开失败：%s，稍后重试" % (_err_text(exc),)}
    cleared = {"sources": 0, "runs": 0, "candidates": 0, "artifacts": 0,
               "normalization_revisions": 0, "render_revisions": 0,
               "publish_records": 0, "archive_commits": 0, "state_events": 0,
               "job_dirs": 0}
    plan = None
    run_ids = []
    try:
        plan = _clear_plan(con, data_root, input_root,
                           only_failed=only_failed)
        if not plan.get("exists"):
            try:
                con.close()
            except Exception:
                pass
            return 200, {"ok": True, "data_root": data_root,
                         "input_root": input_root, "cleared": cleared,
                         "only_failed": only_failed,
                         "message": "本目录下没有任务记录，无需清空"}
        eff_srcs = set(plan["srcs_to_del"])
        run_ids = list(plan["run_ids"])
        cand_ids = plan["cand_ids"]
        norm_ids = plan["norm_ids"]
        rend_ids = plan["rend_ids"]
        pub_ids = plan["pub_ids"]
        art_ids = plan["art_ids"]
        entity_ids = set(plan["entity_ids"])
        qmarks = ",".join("?" for _ in eff_srcs) if eff_srcs else None
        s_list = list(eff_srcs)
        rmarks = ",".join("?" for _ in run_ids) if run_ids else None
        con.execute("BEGIN IMMEDIATE")
        try:
            if entity_ids:
                el = list(entity_ids)
                em = ",".join("?" for _ in el)
                cleared["state_events"] = con.execute(
                    "DELETE FROM state_events WHERE entity_id IN (%s)" % em,
                    el).rowcount or 0
            if pub_ids:
                pm = ",".join("?" for _ in pub_ids)
                cleared["publish_records"] = con.execute(
                    "DELETE FROM publish_records WHERE publish_record_id IN (%s)"
                    % pm, pub_ids).rowcount or 0
            if rend_ids:
                rm = ",".join("?" for _ in rend_ids)
                cleared["render_revisions"] = con.execute(
                    "DELETE FROM render_revisions WHERE render_revision_id IN (%s)"
                    % rm, rend_ids).rowcount or 0
            if norm_ids:
                nm = ",".join("?" for _ in norm_ids)
                cleared["normalization_revisions"] = con.execute(
                    "DELETE FROM normalization_revisions"
                    " WHERE normalization_revision_id IN (%s)" % nm,
                    norm_ids).rowcount or 0
            if art_ids:
                am = ",".join("?" for _ in art_ids)
                cleared["artifacts"] = con.execute(
                    "DELETE FROM artifacts WHERE artifact_id IN (%s)" % am,
                    art_ids).rowcount or 0
            if run_ids:
                cleared["runs"] = con.execute(
                    "DELETE FROM processing_runs WHERE run_id IN (%s)" % rmarks,
                    run_ids).rowcount or 0
            if cand_ids:
                cm = ",".join("?" for _ in cand_ids)
                cleared["candidates"] = con.execute(
                    "DELETE FROM discovery_candidates WHERE candidate_id IN (%s)"
                    % cm, cand_ids).rowcount or 0
            if eff_srcs:
                cleared["archive_commits"] = con.execute(
                    "DELETE FROM archive_commits WHERE source_id IN (%s)" % qmarks,
                    s_list).rowcount or 0
            else:
                cleared["archive_commits"] = 0
            # P1-3：NULL archive 门侧计 BLOCK，全清时同步删掉（清不掉却挡门）；
            # UX2-P0-2 只清失败路径不带走 NULL archive（避免误伤其他目录）。
            if not only_failed:
                try:
                    cleared["archive_commits"] += con.execute(
                        "DELETE FROM archive_commits WHERE source_id IS NULL"
                    ).rowcount or 0
                except Exception:
                    pass
            if eff_srcs:
                cleared["sources"] = con.execute(
                    "DELETE FROM sources WHERE source_id IN (%s)" % qmarks,
                    s_list).rowcount or 0
            else:
                cleared["sources"] = 0
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            raise
    except Exception as exc:
        try:
            con.close()
        except Exception:
            pass
        return 500, {"ok": False,
                     "error": "清空失败已回滚：%s，稍后重试" % (_err_text(exc),)}
    try:
        con.close()
    except Exception:
        pass
    # 本 input run 的 jobs 目录 + 内存痕迹（他 run 不动）
    if run_ids:
        jobs_base = os.path.join(os.path.abspath(data_root), "data", "jobs")
        for rid in run_ids:
            jd = os.path.join(jobs_base, str(rid))
            # 防守：只删 jobs/<run_id> 一层，防路径穿越
            if os.path.abspath(jd).startswith(os.path.abspath(jobs_base) + os.sep):
                try:
                    if os.path.isdir(jd):
                        shutil.rmtree(jd, ignore_errors=True)
                        cleared["job_dirs"] += 1
                except Exception:
                    continue
        with _state_lock:
            doomed = set(str(r) for r in run_ids)
            _worker_done.difference_update(doomed)
            _worker["processed"] = [
                p for p in _worker["processed"]
                if not (isinstance(p, dict)
                        and str(p.get("run_id")) in doomed)]
            try:
                cur = _worker.get("current")
                if isinstance(cur, dict) and str(cur.get("run_id")) in doomed:
                    _worker["current"] = None
            except Exception:
                pass
    kept_success = int((plan or {}).get("kept_success") or 0)
    if only_failed:
        msg = ("已清 %d 条失败记录（成功 %d 条保留），源视频与笔记未动"
               % (cleared["runs"], kept_success))
    else:
        msg = ("已清空本目录 %d 个任务记录（源视频与笔记未动）"
               % (cleared["runs"],))
    undo = ("撤销指引：本次只删本数据目录下的任务记录与 jobs 中间文件，"
            "vault 笔记 md 与源视频未动。清空前若用的是另一个数据目录，"
            "把「数据目录」换回旧路径即可看到旧记录；本目录已删记录无法自动恢复，"
            "但源视频还在，随时可重起重新生成。")
    return 200, {"ok": True, "data_root": data_root, "input_root": input_root,
                 "cleared": cleared, "only_failed": only_failed,
                 "kept_success": kept_success,
                 "message": msg, "undo": undo}


def _handle_reveal_post(body: bytes) -> tuple[int, dict]:
    """V2.4 P0-1：在文件管理器中定位（后端直开）。

    Windows 11：``explorer.exe /select,<file>``（参数数组，不经 shell）；
    macOS：``open -R``。命令构造统一走
    ``platform_win.build_reveal_argv``（平台分支单点）。
    前端 file:// 导航会被浏览器静默拦截，故改后端直调。
    normalize + 文件存在校验，失败人话 400。子进程仅此处使用。
    """
    try:
        params = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": "请求体须为 JSON 对象"}
    raw = params.get("path")
    path = normalize_path(raw)
    if not path:
        return 400, {"ok": False, "error": "缺少文件路径，请刷新后重试"}
    if not os.path.isabs(path):
        return 400, {"ok": False,
                     "error": "路径须为绝对路径（收到 %s），请刷新后重试"
                              % (_diag_redact_path(raw),)}
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return 400, {"ok": False, "error": "文件找不到了：%s→检查文件是否被移动或删除" % (_diag_redact_path(real),)}
    plan = platform_win.build_reveal_argv(real)
    try:
        import subprocess  # noqa: E402  (子进程仅 reveal 一处)

        res = subprocess.run(plan["argv"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10)
        if res.returncode != 0:
            err = (res.stderr or "").strip()
            return 400, {"ok": False,
                         "error": "在文件管理器中定位失败：%s→检查文件是否存在"
                                  % (_err_text(err or real),)}
        return 200, {"ok": True, "path": real, "reveal_mode": plan["mode"],
                     "reveal_note": plan["note"]}
    except FileNotFoundError:
        return 400, {"ok": False,
                     "error": "在文件管理器中定位失败：系统命令不可用"
                              "（Windows 需 explorer.exe／macOS 需 open）"}
    except Exception as exc:
        return 400, {"ok": False,
                     "error": "在访达中定位失败：%s→检查文件是否存在" % (_err_text(exc),)}


def _note_user_edited(data_root: str, run_id: str) -> bool:
    """UX2-P1-4：读 jobs/<run_id>/manifest.json 判定 No-Clobber 跳过（只读）。

    命中 publish_status=BLOCKED_OUTPUT_CONFLICT 或 verdict 含“库内…改过/
    跳过”即视为“库内你改过，重跑会跳过”。任何异常回 False（fail-open）。
    """
    try:
        mp = os.path.join(_jobs_dir(data_root), str(run_id), "manifest.json")
        if not os.path.isfile(mp):
            return False
        with open(mp, "r", encoding="utf-8") as fh:
            mani = json.load(fh)
        for r in reversed(mani.get("receipts") or []):
            if not isinstance(r, dict):
                continue
            if str(r.get("publish_status") or "") == "BLOCKED_OUTPUT_CONFLICT":
                return True
            v = str(r.get("verdict") or "")
            if "BLOCKED_OUTPUT_CONFLICT" in v:
                return True
            if "改过" in v and ("跳过" in v or "未覆盖" in v):
                return True
        return False
    except Exception:
        return False


def _handle_note(query: dict) -> tuple[int, dict]:
    """P0-3/V2.3：右侧笔记预览。点行后返回该 run 输出 md 正文（超长截断+注明）。

    无 md 则回所处阶段人话。附 P0-4 两链接（访达 file:// + OB obsidian://）。
    V2.3 P0-2：回显vault_registered布尔（.obsidian是否为目录）。
    """
    run_id = str((query.get("run_id") or [""])[0] or "").strip()
    if not run_id:
        return 400, {"ok": False, "error": "缺少任务编号 run_id，点行后重试"}
    data_root = normalize_path((query.get("data_root") or [""])[0] or "")
    with _state_lock:
        listener_running = bool(_listener.get("running"))
        listener_data = _listener.get("data_root")
        vault_root = _listener.get("ob_vault_root")
    if not data_root:
        data_root = str(listener_data or DEFAULT_DATA_ROOT)
    try:
        _vault_registered = _is_vault_registered(vault_root)
    except Exception:
        _vault_registered = False
    entry, _from_mem = _note_entry_for_run(run_id, data_root)
    state = str((entry or {}).get("state") or "")
    verdict = str((entry or {}).get("verdict") or "")
    md_path = None
    if entry and isinstance(entry, dict):
        if state == "PUBLISHED":
            md_path = entry.get("canonical_output_path") or entry.get("rendered_path")
        elif state in ("RENDER_ONLY", "PUBLISH_BLOCKED", "FAIL"):
            md_path = entry.get("rendered_path") or entry.get("canonical_output_path")
    # UX2-P1-4：No-Clobber 预览状态行（复用后端判定，只读不落盘）
    user_edited = False
    try:
        user_edited = _note_user_edited(data_root, run_id)
    except Exception:
        user_edited = False
    note_hint = None
    try:
        if state == "PUBLISHED":
            note_hint = ("库内你改过，重跑会跳过不覆盖，新稿只留数据目录"
                         if user_edited else "库内未改，重跑会更新")
        elif state == "PUBLISH_BLOCKED" and user_edited:
            note_hint = "库内你改过，重跑会跳过不覆盖，新稿只留数据目录"
    except Exception:
        note_hint = None
    if isinstance(md_path, str) and md_path and os.path.isfile(md_path):
        try:
            with open(md_path, "r", encoding="utf-8", errors="replace") as fh:
                full = fh.read()
        except OSError as exc:
            return 200, {"ok": True, "run_id": run_id, "state": state or "UNKNOWN",
                         "verdict": verdict, "path": md_path,
                         "stage_text": "笔记文件读不出来：%s→检查文件权限" % (_err_text(exc),),
                         "text": None, "truncated": False, "total_chars": 0,
                         "finder_url": _finder_url(md_path),
                         "ob_url": None,
                         "ob_reason": "笔记文件不可读",
                         "user_edited": user_edited, "note_hint": note_hint,
                         "vault_configured": bool(vault_root),
                         "vault_registered": _vault_registered}
        total = len(full)
        truncated = total > NOTE_MAX_CHARS
        text = full[:NOTE_MAX_CHARS]
        if truncated:
            text += "\n\n…（已截断，全文 %d 字，点「在访达中打开」看完整笔记）" % (total,)
        ob_url, ob_reason = _obsidian_url(vault_root, md_path)
        # V2.3 P0-2：未注册时OB链接不可用，前端禁用+注明先开库
        if not _vault_registered:
            ob_url = None
            ob_reason = "先在OB中把该文件夹打开为仓库后再点"
        return 200, {"ok": True, "run_id": run_id, "state": state,
                     "verdict": verdict, "path": md_path,
                     "stage_text": None, "text": text, "truncated": truncated,
                     "total_chars": total, "finder_url": _finder_url(md_path),
                     "ob_url": ob_url, "ob_reason": ob_reason,
                     "user_edited": user_edited, "note_hint": note_hint,
                     "vault_configured": bool(vault_root),
                     "vault_registered": _vault_registered}
    # 无 md：回阶段人话
    stage_text = _stage_text_zh(run_id, entry, data_root)
    ob_url, ob_reason = _obsidian_url(vault_root, None)
    if not _vault_registered and vault_root:
        # 已配库但未注册：覆盖为注册引导（未配库保持原“未配置笔记库”人话）
        ob_reason = "先在OB中把该文件夹打开为仓库后再点"
    return 200, {"ok": True, "run_id": run_id,
                 "state": state or "QUEUED", "verdict": verdict,
                 "path": (md_path if isinstance(md_path, str) else None),
                 "stage_text": stage_text, "text": None,
                 "truncated": False, "total_chars": 0,
                 "finder_url": (_finder_url(md_path)
                                if isinstance(md_path, str) and md_path else None),
                 "ob_url": ob_url, "ob_reason": ob_reason,
                 "user_edited": user_edited, "note_hint": note_hint,
                 "vault_configured": bool(vault_root),
                 "vault_registered": _vault_registered}


# ------------------------------------------------- V2.5 存量一键重跑
#
# 复用 Case4 语义（Whisper 调用 0、Raw 不变、新 NormRev/新 Render）：
#   derive = stage3.derive.derive_on_correction_change（用户词库 profile
#   + V2.5 render profile；与 stage9 derive_case4 同一 stage3 入口）。
# No-Clobber 延续：已入库任务的新 Render 经 stage4 initial_publish /
# publish_or_block（present canonical 永不覆盖，#51）；用户改过的库内
# md 命中 BLOCKED_OUTPUT_CONFLICT 即跳过并注明，新稿只留数据目录。

REAPPLY_ELIGIBLE = ("RENDER_ONLY", "PUBLISHED", "PUBLISH_BLOCKED")

# ------------------------------------------------- P1-3 终态统计唯一口径
#
# 问题（CANDIDATE-APPLY P3-2）：同一批结果在两处算出不同的 failed——
# `_reapply_all` 的 summary 把 `not ok`（含 skipped）全算 failed，而
# `_run_vocab_candidates_apply` 又自己算一遍（`not ok and not skipped`），
# 于是「跳过」既可能算失败也可能不算，页面汇总不可复算。
#
# 约定：逐项归类只有**一个真源** `_state_bucket`（见文件上部「五桶语义唯一真源」），
# 本链与批量恢复链共用；五桶互斥且完备，
#   total == success + failed + skipped + needs_human + interrupted
# 未知 state / 坏条目一律并进 failed（fail-closed 计入失败，不静默丢弃）。
_REAPPLY_BUCKETS = FIVE_BUCKETS


def _reapply_result_bucket(item) -> str:
    """逐项归类（派生自唯一真源 `_state_bucket`）：任何输入都必落且只落一个桶。

    优先级：显式标记（skipped／interrupted／needs_human）> state > ok 标志。
    **state 键存在就以它为准**（含空串／纯空白／None）→ 一律走 `_state_bucket()`，
    表外（含空值）落 failed（fail-closed），与恢复链同源：未知 state 即使 `ok=True`
    也判 failed，不许升成成功。存量重跑结果本身**压根不带 state 键**（真源是
    ok 标志＋跳过标记），故**只有键不存在**时才按 ok 判，不误伤正常成功。
    """
    if not isinstance(item, dict):
        return BUCKET_FAILED
    if item.get("skipped") or item.get("skipped_user_edited"):
        return BUCKET_SKIPPED
    if item.get("interrupted"):
        return BUCKET_INTERRUPTED
    if item.get("needs_human"):
        return BUCKET_NEEDS_HUMAN
    # 判据是「键在不在」，不是「值真不真」：state="" / "   " / None 都算**显式给了**
    # （旧写法 `str(... or "").strip()` 把这三者与「缺键」混为一类，于是空串被当成功，
    #  与 `_recovery_bucket("")`＝still_failed 不同源）。
    if "state" in item:
        return _state_bucket(item.get("state"))
    return BUCKET_SUCCESS if item.get("ok") else BUCKET_FAILED


def _reapply_stats(results, total=None) -> dict:
    """按唯一真源派生的逐项归类复算汇总；total 独立传入时给出 balanced 自检。"""
    items = list(results or [])
    counts = {key: 0 for key in _REAPPLY_BUCKETS}
    for item in items:
        counts[_reapply_result_bucket(item)] += 1
    counted = sum(counts.values())
    expect = len(items) if total is None else int(total)
    out = {"total": expect, "done": counted}
    out.update(counts)
    out["counted"] = counted
    out["balanced"] = counted == expect
    return out


def _open_rw(data_root: str):
    """读写开中央库：锁在手走 store 门，否则直连（同进程，busy 等待）。

    V2.5 P1-1：缺父目录/缺库文件先抛人话 OSError/FileNotFoundError，
    由 _reapply_one 接管转 JSON（不断连接），不直连抛裸错。
    """
    try:
        from stage2 import store as _st  # noqa: E402

        if _st.is_held(data_root):
            return _st.open_db(data_root)
    except Exception:
        pass
    root = os.path.abspath(str(data_root or ""))
    db_path = os.path.join(root, "data", "state.db")
    parent = os.path.dirname(db_path)
    if not os.path.isdir(parent):
        raise FileNotFoundError(
            "状态库不可读：数据目录不存在（%s），请检查数据目录后刷新重试"
            % (_diag_redact_path(parent),))
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            "状态库不可读：尚未初始化（%s 缺失），请先开始一次监听或检查数据目录"
            % (_diag_redact_path(db_path),))
    try:
        con = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        con.row_factory = sqlite3.Row
        return con
    except (sqlite3.Error, OSError) as exc:
        raise OSError("状态库不可读：%s，请检查数据目录后刷新重试" % (_err_text(exc),))


def _append_manifest_receipt(job_dir: str, entry: dict) -> None:
    from stage3 import lineage as _lineage  # noqa: E402  (只读复用记法)

    try:
        _lineage.record_lineage_manifest(
            os.path.join(os.path.abspath(job_dir), "manifest.json"),
            receipts=[entry])
    except Exception:
        pass


def _sha256_file(path: str) -> str | None:
    try:
        import hashlib as _hl

        digest = _hl.sha256()
        with open(path, "rb") as fh:
            while True:
                part = fh.read(8 * 1024 * 1024)
                if not part:
                    break
                digest.update(part)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def _reapply_one(data_root: str, run_id: str,
                 ob_vault_root: str | None = None) -> dict:
    """单个已完成任务应用新词库重跑（Case4：whisper 0、Raw 不变）。

    V2.5 P1-1：_open_rw 与 SELECT 全接管，人话 JSON 不掉线。
    V2.5 P0-1：derive 后经 _apply_v25_postpass 重算覆写（200/MIN 生效）。
    """
    from stage3 import derive as _derive  # noqa: E402  (Case4 同一入口)

    data_root = os.path.abspath(str(data_root or ""))
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"ok": False, "run_id": run_id, "error": "缺少任务编号"}
    try:
        con = _open_rw(data_root)
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "run_id": run_id, "error": _err_text(exc)}
    except Exception as exc:
        return {"ok": False, "run_id": run_id,
                "error": "状态库不可读：%s，请检查数据目录后刷新重试" % (_err_text(exc),)}
    try:
        try:
            row = con.execute(
                "SELECT * FROM processing_runs WHERE run_id=?", (run_id,)).fetchone()
        except (sqlite3.Error, OSError) as exc:
            return {"ok": False, "run_id": run_id,
                    "error": "状态库不可读：%s，请检查数据目录后刷新重试" % (_err_text(exc),)}
        if row is None:
            return {"ok": False, "run_id": run_id,
                    "error": "任务不存在，请刷新后重试"}
        run = dict(row)
        raw_artifact_id = run.get("raw_artifact_id") or ""
        source_id = run.get("source_id")
        if not raw_artifact_id:
            return {"ok": False, "run_id": run_id, "skipped": True,
                    "reason": "该任务没有原文记录，无法重跑（点重试重新转写）"}
        job_dir = os.path.join(data_root, "data", "jobs", run_id)
        raw_path = os.path.join(job_dir, "raw", "raw.json")
        if not os.path.isfile(raw_path):
            return {"ok": False, "run_id": run_id, "skipped": True,
                    "reason": "原文文件找不到了，无法重跑（点重试重新转写）"}
        try:
            disk = _scan_disk_states(data_root).get(run_id)
        except Exception:
            disk = None
        prev_state = str((disk or {}).get("state") or "")
        if prev_state not in REAPPLY_ELIGIBLE:
            return {"ok": False, "run_id": run_id, "skipped": True,
                    "reason": "该任务尚未完成（%s），监听中会自动处理"
                              % (prev_state or "排队中",)}
        try:
            fn_map = _run_source_path_map(data_root)
        except Exception:
            fn_map = {}
        src_path = fn_map.get(run_id) or ""
        try:
            src_fn = os.path.basename(str(src_path).strip()) or "未知文件"
        except Exception:
            src_fn = "未知文件"
        stem = os.path.splitext(src_fn)[0] or run_id
        probe = None
        prev_canon = (disk or {}).get("canonical_output_path")
        if isinstance(prev_canon, str) and prev_canon:
            probe = prev_canon
        raw_before = _sha256_file(raw_path)
        try:
            norm_profile = _norm_profile_for_new_jobs(data_root)
            render_profile = _render_profile_for_new_jobs()
        except ValueError as exc:
            return {"ok": False, "run_id": run_id, "error": _err_text(exc)}
        try:
            out = _derive.derive_on_correction_change(
                con, job_dir, raw_artifact_id, norm_profile,
                render_profile=render_profile, title=stem,
                canonical_probe_path=probe, source_id=source_id,
                run_id=run_id)
        except Exception as exc:
            try:
                con.rollback()
            except Exception:
                pass
            return {"ok": False, "run_id": run_id,
                    "error": "重跑失败已回滚：%s，稍后重试" % (_err_text(exc),)}
        if int(out.get("whisper_calls") or 0) != 0:
            return {"ok": False, "run_id": run_id,
                    "error": "重跑触碰了转写引擎（whisper!=0），已拦截"}
        raw_after = _sha256_file(raw_path)
        raw_unchanged = (raw_before is not None and raw_before == raw_after)
        if not raw_unchanged:
            return {"ok": False, "run_id": run_id,
                    "error": "原文被改动（Raw 不变断言失败），已拦截"}
        new_rend_rev = str(out.get("render_revision_id") or "")
        new_rendered = os.path.join(job_dir, "render", "%s.md" % (new_rend_rev,))
        if not os.path.isfile(new_rendered):
            return {"ok": False, "run_id": run_id,
                    "error": "新稿文件未生成，请稍后重试"}
        # 生产后处理（重跑入口）：冻结 derive 已 mint 不断链，此处用
        # render_with_v2 重算覆写，阈值取 stage9.PARA_PARAMS_V2 单源头
        # （para-v2.7：目标220/封顶450/防碎80），与 append 入口同源。
        try:
            _norm_rev = str(out.get("normalization_revision_id") or "")
            _norm_path = os.path.join(
                job_dir, "normalized", "%s.json" % (_norm_rev,)) \
                if _norm_rev else None
            _fix = _apply_v25_postpass(
                job_dir, _norm_path, new_rendered, stem, render_profile)
            _postpass = {"applied": bool(_fix.get("fixed")),
                         "already_ok": bool(_fix.get("already_ok")),
                         "paras": _fix.get("paras"),
                         "max_len": _fix.get("max_len")}
            if _fix.get("error") and not _fix.get("fixed") \
                    and not _fix.get("already_ok"):
                _postpass["note"] = str(_fix.get("error"))
        except Exception as exc:
            _postpass = {"applied": False, "note": "后处理异常：%s" % (_err_text(exc),)}
        result = {"ok": True, "run_id": run_id,
                  "source_filename": src_fn, "prev_state": prev_state,
                  "whisper_calls": 0, "raw_unchanged": True,
                  "normalization_revision_id":
                      out.get("normalization_revision_id"),
                  "render_revision_id": new_rend_rev,
                  "rendered_path": os.path.abspath(new_rendered),
                  "norm_rules_revision":
                      norm_profile.get("correction_rules_revision"),
                  "v25_postpass": _postpass}
        # 入库分支（No-Clobber）：之前已入库且给了笔记库才尝试 publish；
        # present canonical 永不覆盖，BLOCK 即跳过注明。
        vault = (str(ob_vault_root).strip()
                 if isinstance(ob_vault_root, str) and ob_vault_root.strip()
                 else None)
        if prev_state == "PUBLISHED" and vault and isinstance(prev_canon, str):
            try:
                vault_real = os.path.realpath(vault)
                canon_real = os.path.realpath(prev_canon)
                under = (os.path.commonpath([vault_real, canon_real])
                         == vault_real)
            except (ValueError, OSError):
                under = False
            if under and os.path.isdir(vault):
                try:
                    from stage4.publish import initial_publish  # noqa: E402

                    source_rel = os.path.relpath(canon_real, vault_real)
                    pub = initial_publish(con, job_dir, new_rend_rev,
                                          vault_real, source_rel)
                    status = str(pub.get("status") or "")
                    if status == "PUBLISHED":
                        verdict = ("已应用新词库重跑并更新入库：%s"
                                   % (pub.get("canonical_output_path"),))
                        _append_manifest_receipt(job_dir, {
                            "stage": "app-worker", "state": "PUBLISHED",
                            "run_id": run_id, "source_id": source_id,
                            "verdict": verdict,
                            "created_at": _utc_now_iso(),
                            "whisper_calls": 0,
                            "render_revision_id": new_rend_rev,
                            "rendered_path": os.path.abspath(new_rendered),
                            "canonical_output_path":
                                pub.get("canonical_output_path")})
                        result.update({
                            "new_state": "PUBLISHED", "note": verdict,
                            "canonical_output_path":
                                pub.get("canonical_output_path")})
                    else:
                        unchanged = bool(pub.get("canonical_bytes_unchanged",
                                                 True))
                        writes = int(pub.get("canonical_writes") or 0)
                        if not unchanged or writes != 0:
                            result.update({
                                "ok": False, "error":
                                "库内文件被改动（No-Clobber 断言失败），已拦截"})
                            return result
                        if status == "BLOCKED_OUTPUT_CONFLICT":
                            note = ("已跳过：库内笔记你改过，未覆盖；"
                                    "新稿在数据目录：%s"
                                    % (os.path.abspath(new_rendered),))
                        else:
                            note = ("内容一致，无需更新；库内未动；"
                                    "新稿在数据目录：%s"
                                    % (os.path.abspath(new_rendered),))
                        _append_manifest_receipt(job_dir, {
                            "stage": "app-worker",
                            "state": "PUBLISH_BLOCKED",
                            "run_id": run_id, "source_id": source_id,
                            "verdict": note, "created_at": _utc_now_iso(),
                            "whisper_calls": 0,
                            "render_revision_id": new_rend_rev,
                            "rendered_path": os.path.abspath(new_rendered),
                            "canonical_output_path": prev_canon,
                            "publish_status": status})
                        result.update({
                            "new_state": "PUBLISH_BLOCKED",
                            "skipped_user_edited":
                                (status == "BLOCKED_OUTPUT_CONFLICT"),
                            "note": note, "publish_status": status,
                            "canonical_unchanged": True})
                except Exception as exc:
                    result.update({
                        "new_state": prev_state,
                        "note": ("新稿已生成在数据目录：%s；入库未试：%s"
                                 % (os.path.abspath(new_rendered),
                                    _err_text(exc)))})
                    _append_manifest_receipt(job_dir, {
                        "stage": "app-worker", "state": prev_state,
                        "run_id": run_id, "source_id": source_id,
                        "verdict": result["note"],
                        "created_at": _utc_now_iso(), "whisper_calls": 0,
                        "render_revision_id": new_rend_rev,
                        "rendered_path": os.path.abspath(new_rendered),
                        "canonical_output_path": prev_canon})
            else:
                note = ("新稿已生成在数据目录：%s；库内文件未动"
                        "（未给笔记库或不在库内，不覆盖）"
                        % (os.path.abspath(new_rendered),))
                _append_manifest_receipt(job_dir, {
                    "stage": "app-worker", "state": prev_state,
                    "run_id": run_id, "source_id": source_id,
                    "verdict": note, "created_at": _utc_now_iso(),
                    "whisper_calls": 0, "render_revision_id": new_rend_rev,
                    "rendered_path": os.path.abspath(new_rendered),
                    "canonical_output_path": prev_canon})
                result.update({"new_state": prev_state, "note": note})
        else:
            verdict = "已应用新词库重跑（转写0次，原文未动）：%s" % (
                os.path.abspath(new_rendered),)
            _append_manifest_receipt(job_dir, {
                "stage": "app-worker", "state": prev_state,
                "run_id": run_id, "source_id": source_id,
                "verdict": verdict, "created_at": _utc_now_iso(),
                "whisper_calls": 0, "render_revision_id": new_rend_rev,
                "rendered_path": os.path.abspath(new_rendered),
                "canonical_output_path": prev_canon})
            result.update({"new_state": prev_state, "note": verdict})
        # 内存终态同步（本 session 页面即时可见，不等重启）
        try:
            with _state_lock:
                _worker["processed"] = [
                    p for p in _worker["processed"]
                    if not (isinstance(p, dict)
                            and str(p.get("run_id")) == run_id)]
                mem_state = str(result.get("new_state") or prev_state)
                _worker["processed"].append({
                    "run_id": run_id, "state": mem_state,
                    "source_filename": src_fn,
                    "verdict": str(result.get("note") or ""),
                    "whisper_calls": 0,
                    "rendered_path": result.get("rendered_path"),
                    "canonical_output_path": result.get(
                        "canonical_output_path", prev_canon)})
                _worker["processed"] = _worker["processed"][-100:]
        except Exception:
            pass
        return result
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "run_id": run_id,
                "error": "状态库不可读：%s，请检查数据目录后刷新重试" % (_err_text(exc),)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _reapply_all(data_root: str, vault_s, progress_cb=None) -> tuple[int, dict]:
    """重跑全部已完成任务（all=true 路径）。

    progress_cb 仅供页面进度回传（(ev) -> None），每篇开跑前/跑完后各回调一次；
    为 None 时行为与旧版逐字一致。
    """
    try:
        disk = _scan_disk_states(data_root)
    except Exception:
        disk = {}
    targets = [rid for rid, v in disk.items()
               if isinstance(v, dict) and v.get("state") in REAPPLY_ELIGIBLE]
    if not targets:
        empty_stats = _reapply_stats([], total=0)
        return 200, {"ok": True, "data_root": data_root, "results": [],
                     "summary": {"total": 0, "ok": 0,
                                 "skipped_user_edited": 0, "failed": 0},
                     "stats": empty_stats,
                     "message": "没有可重跑的已完成任务"}
    # R3：只在需要回传进度时查一次 run→文件名 映射；None 时保持旧路径零多余查询
    fn_map = {}
    if progress_cb is not None:
        try:
            fn_map = _run_source_path_map(data_root)
        except Exception:
            fn_map = {}

    def _name(rid: str) -> str:
        try:
            return os.path.basename(str(fn_map.get(rid) or "").strip()) or "未知文件"
        except Exception:
            return "未知文件"

    total = len(targets)
    results = []
    for pos, rid in enumerate(targets):
        if progress_cb is not None:
            try:
                progress_cb({"total": total, "done": pos, "run_id": rid,
                             "filename": _name(rid)})
            except Exception:
                pass
        try:
            results.append(_reapply_one(data_root, rid, vault_s))
        except (sqlite3.Error, OSError) as exc:
            results.append({"ok": False, "run_id": rid,
                            "error": "状态库不可读：%s，请检查数据目录后刷新重试"
                                     % (_err_text(exc),)})
        except Exception as exc:
            results.append({"ok": False, "run_id": rid,
                            "error": "重跑失败：%s，稍后重试" % (_err_text(exc),)})
        if progress_cb is not None:
            try:
                last = results[-1] if isinstance(results[-1], dict) else {}
                progress_cb({"total": total, "done": pos + 1, "run_id": rid,
                             "filename": str(last.get("source_filename")
                                             or _name(rid))})
            except Exception:
                pass
    # P1-3 统计统一：五桶由唯一归类函数复算，`failed` 不再把「跳过」算进去
    # （旧口径 `not ok` 会把 user-edited 跳过同时算 ok 又算 failed，汇总对不上）。
    stats = _reapply_stats(results, total=total)
    summary = {"total": stats["total"],
               "ok": stats["success"],
               "skipped_user_edited": stats["skipped"],
               "failed": stats["failed"],
               "needs_human": stats["needs_human"],
               "interrupted": stats["interrupted"],
               "counted": stats["counted"],
               "balanced": stats["balanced"]}
    message = "重跑 %d 个：成功 %d，库内你改过跳过 %d，失败 %d" % (
        summary["total"], summary["ok"], summary["skipped_user_edited"],
        summary["failed"])
    if summary["needs_human"] or summary["interrupted"]:
        message += "，待人工 %d，中断 %d" % (summary["needs_human"],
                                            summary["interrupted"])
    return 200, {"ok": True, "data_root": data_root, "results": results,
                 "summary": summary, "stats": stats,
                 "message": message}


def _handle_reapply_post(body: bytes, progress_cb=None) -> tuple[int, dict]:
    """存量一键重跑：单个 run_id 或 all=true（全部已完成）。"""
    try:
        params = _body_json(body)
        data_root = _take_data_root(params, DEFAULT_DATA_ROOT)
        # all 必须是真布尔：字符串 "false"/数字会被 truthy 判成"全量重跑"
        all_runs = _take_bool(params, "all", False)
        vault_s = _take_str(params, "ob_vault_root", "", allow_empty=True) or None
    except ParamError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if all_runs:
        return _reapply_all(data_root, vault_s, progress_cb=progress_cb)
    run_id = _take_str(params, "run_id", "", allow_empty=True)
    if not run_id:
        return 400, {"ok": False, "error": "缺少任务编号 run_id（或传 all=true 全跑）"}
    try:
        res = _reapply_one(data_root, run_id, vault_s)
    except (sqlite3.Error, OSError) as exc:
        return 500, {"ok": False, "run_id": run_id,
                     "error": "状态库不可读：%s，请检查数据目录后刷新重试" % (_err_text(exc),)}
    except Exception as exc:
        return 500, {"ok": False, "run_id": run_id,
                     "error": "重跑失败：%s，稍后重试" % (_err_text(exc),)}
    if not res.get("ok") and not res.get("skipped") and "error" in res \
            and "任务不存在" in str(res.get("error")):
        return 404, {"ok": False, **res}
    return 200, {"ok": res.get("ok", False), **res}


class Handler(BaseHTTPRequestHandler):
    server_version = "V2OConsole/1.1"

    def log_message(self, fmt, *args):  # noqa: N802  (保持控制台安静)
        sys.stderr.write("v2o-console: %s\n" % (fmt % args,))

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        # D-12：GET 侧同样外层接管，未预见异常回 500 JSON 丢掉线、不裸抛
        try:
            self._do_get(parsed)
        except BrokenPipeError:
            pass
        except ParamError as exc:
            _send_json(self, 400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            try:
                _send_json(self, 500, {"ok": False,
                                       "error": "服务开小差：%s，稍后重试"
                                                % (_err_text(exc),)})
            except Exception:
                pass

    def _do_get(self, parsed):  # noqa: N802
        if parsed.path in ("/", "/index.html"):
            _serve_index(self)
            return
        if parsed.path == "/api/status":
            code, obj = _handle_status(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/failures/diagnosis":
            code, obj = _handle_failure_diagnosis(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/failures/retry-batch/status":
            code, obj = _handle_retry_batch_status(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/start":
            _send_json(self, 200, {"ok": True, **_listener_snapshot()})
            return
        if parsed.path == "/api/browse":
            code, obj = _handle_browse(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/note":
            code, obj = _handle_note(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/vocab":
            code, obj = _handle_vocab_get(urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/vocab/candidates":
            code, obj = _handle_vocab_candidates_get(
                urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/vocab/candidates/apply/status":
            code, obj = _handle_vocab_apply_status(
                urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        if parsed.path == "/api/vocab/presets":
            code, obj = _handle_vocab_presets_get(
                urllib.parse.parse_qs(parsed.query))
            _send_json(self, code, obj)
            return
        _send_json(self, 404, {"ok": False, "error": "未知路径，请刷新后重试"})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        # V2.5 P1-1：外层接管，未预见异常回 500 JSON 不掉线。
        try:
            if parsed.path == "/api/start":
                code, obj = _handle_start_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/stop":
                code, obj = _handle_stop_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/retry":
                code, obj = _handle_retry_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/clear":
                code, obj = _handle_clear_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/reveal":
                code, obj = _handle_reveal_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/vocab":
                code, obj = _handle_vocab_add(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/vocab/candidates/apply":
                code, obj = _handle_vocab_candidates_apply(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/vocab/delete":
                code, obj = _handle_vocab_del(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/vocab/presets/import":
                code, obj = _handle_vocab_presets_import(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/vocab/presets/domains":
                code, obj = _handle_vocab_presets_domains_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/reapply":
                code, obj = _handle_reapply_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/failures/retry-plan":
                code, obj = _handle_retry_plan_post(body)
                _send_json(self, code, obj)
                return
            if parsed.path == "/api/failures/retry-batch":
                code, obj = _handle_retry_batch_post(body)
                _send_json(self, code, obj)
                return
            _send_json(self, 404, {"ok": False, "error": "未知路径，请刷新后重试"})
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                _send_json(self, 500, {"ok": False,
                                       "error": "服务开小差：%s，稍后重试" % (_err_text(exc),)})
            except Exception:
                pass


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("V2O 本机控制台：http://%s:%d/（data_root 默认 %s）" % (HOST, PORT, DEFAULT_DATA_ROOT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
