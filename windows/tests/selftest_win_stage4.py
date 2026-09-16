#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage 4 自测：端到端集成 + 崩溃恢复语义（真链路 + 桩引擎）。

本机是 macOS，没有 CUDA、装不了 faster-whisper/ctranslate2，所以转写引擎
用桩顶住（monkeypatch ``asr_backend.transcribe_file`` / ``load_model`` /
``resolve_runtime`` / ``load_tokenizer``）；链路上其余每一环都是**真实现**：

  监听发现（真 watchdog watcher + 四道投递门）→ 入库（真 discover/promote/
  AUTO run）→ ffmpeg 抽 16k mono wav（真 ffmpeg，本机 homebrew）→ 转写（桩）
  → stage8 分块（真切块/合并）→ stage3 Norm/Render + stage9 后处理（真）
  → stage4 发布进 tmp 测试 vault（真 No-Clobber/提交链）。

模型供应链闸也走真实现：tmp 伪造模型目录 + 真 SHA-256 清单（V2O_MODEL_MANIFEST
指向 tmp），manifest 校验原样跑。不联网、不下载任何模型/依赖；测试只用外置
tmp + 合成数据，不碰用户真实目录（首行断言 tmp 根在系统临时目录下）。

跑法（在仓库根）：
    .venv/bin/python windows/tests/selftest_win_stage4.py   # rc=0 即通过

覆盖清单：
  A. 端到端（真监听链）：短音频 stage7 单文件路径 → 恰好 1 篇 md；
     长音频（601s）stage8 分块路径（2 块）→ 恰好 1 篇 md；
     目标笔记已存在 → SKIPPED（whisper 0 次），既有笔记一字节不动；
     源文件保留；run 状态枚举不被污染（P0-4）；publish_records 全 PUBLISHED；
     盘上无半成品（vault 只有预期 md）。
  B2. 崩溃恢复（转写中途强杀）：桩在转写调用里抛 KeyboardInterrupt 模拟
     进程死亡 → 重启扫描后同一 run 幂等重跑 → 恰好 1 篇 md、Source=1、
     AUTO Run=1、publish PUBLISHED=1、无半成品。
  B1. 崩溃恢复（发布中断）：发布一步抛 OSError → PUBLISH_BLOCKED；重启
     扫描**不重转**（norm COMPLETED 已是跳过口径，不重烧 GPU）、不重复建
     Source/Run、无半成品发布、成稿仍保留在数据目录（可显式恢复）。
  C. 周期 reconcile 幂等：同一文件 initial_reconcile 两遍 → Source=1、
     AUTO Run=1（Lost/Duplicate=0）。
  D. 半截源不发布：入队后源文件又变大 → FAIL（人话），不转写、不发布、
     源文件保留。

反向证伪（teeth，改坏实现 → 对应断言必须变红；仍绿=无牙=失败）：
  T1 No-Clobber 前置门改坏（_vault_note_already_there 恒 None）→ 已存在
     笔记场景不再 SKIPPED（真跑转写、状态变 BLOCKED）→ 测红；同时如实
     记录：提交链的「绝不覆盖」结构性防线即使此时仍守住（笔记字节不动）。
  T2 重启扫描误重复建 run（get_or_create_auto_run 改成每次都建新 run）→
     AUTO Run 数 >1 → 测红。
  T3 「已完成跳过」三道防线：①磁盘 receipt 扫描 ②DB 成功旧账 ③提交链
     真值闸（publish_records 行 PUBLISHED 而盘上 final 缺失 → 拒绝伪造
     字节）。正向：删已发布笔记 + 重启 = 不重转、不重建；改坏①② → 重转
     发生但③仍拒绝静默重建（PUBLISH_BLOCKED + refusing to fabricate）；
     ①②③全改坏 → 静默重建发生 → 测红（三道防线各自有牙）。
  T4 半截源快照复核改坏（_stale_source_reason 恒 None）→ 源变大的 run
     照样发布 → 测红。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_ROOT = os.path.dirname(HERE)          # windows/
SRC = os.path.join(WIN_ROOT, "src")
APP = os.path.join(WIN_ROOT, "app")
for _p in (SRC, APP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TMP_ROOT = tempfile.mkdtemp(prefix="v2o-win-stage4-")

# 铁律首检：data_root 只许落在系统临时目录下（调任何 handler 之前断言）。
assert os.path.realpath(TMP_ROOT).startswith(
    os.path.realpath(tempfile.gettempdir())
), "tmp root escaped system temp dir: %r" % (TMP_ROOT,)

import asr_backend  # noqa: E402
import platform_win  # noqa: E402
from stage2 import instance as s2_instance  # noqa: E402
from stage2 import runs as s2_runs  # noqa: E402
from stage2 import store as s2_store  # noqa: E402
from stage2.candidate import discover as s2_discover  # noqa: E402
from stage4 import publish as s4_publish  # noqa: E402
from stage4 import publish_commit as s4_pc  # noqa: E402
from stage5.reconcile import initial_reconcile  # noqa: E402
import server  # noqa: E402  (windows/app/server.py，import 无副作用)

PROFILE_HASH = "stage4-selftest-hash"
STUB_TEXT = "这是合成转写的中文测试文本。"


# ---- 计数与断言 ---------------------------------------------------------
class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.failed: list[str] = []
        self.teeth_total = 0
        self.teeth_sharp = 0

    def ok(self, cond: bool, label: str) -> bool:
        self.total += 1
        if cond:
            print("  PASS  %s" % (label,))
        else:
            self.failed.append(label)
            print("  FAIL  %s" % (label,))
        return bool(cond)

    def tooth(self, label: str, red: bool) -> bool:
        """改坏实现后断言变红=有牙；仍绿=无牙（计失败）。"""
        self.teeth_total += 1
        if red:
            self.teeth_sharp += 1
            print("  牙    [有牙] %s（改坏后断言变红）" % (label,))
        else:
            self.failed.append("无牙: " + label)
            print("  牙    [无牙] %s（改坏后断言仍绿！）" % (label,))
        return red


T = Checker()


# ---- 桩：引擎 / 分词器 / ffmpeg / manifest -------------------------------
ENGINE_CALLS: list[str] = []          # 每次桩转写调用的 wav 路径
CRASH = {"armed": False}              # 置 True 时下一次转写调用模拟强杀


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return round(wf.getnframes() / float(wf.getframerate() or 1), 2)


def _stub_transcribe_file(wav_path, model=None, initial_prompt=None,
                          language=None, word_timestamps=False,
                          no_speech_threshold=0.6, decode=None, config=None,
                          environ=None, _import=None):
    if not isinstance(wav_path, str) or not os.path.isfile(wav_path):
        raise asr_backend.AsrBlock("BLOCKED_AUDIO_MISSING", "音频不存在", {})
    ENGINE_CALLS.append(os.path.abspath(wav_path))
    if CRASH["armed"]:
        CRASH["armed"] = False
        raise KeyboardInterrupt("simulated hard kill mid-run (stage4 selftest)")
    dur = _wav_duration(wav_path)
    segments = [{"start": 0.0, "end": max(dur, 0.1), "text": STUB_TEXT}]
    cfg = config or {"device": "cuda", "compute_type": "int8_float16",
                     "batch_size": 1, "num_workers": 1}
    return {
        "text": STUB_TEXT, "segments": segments,
        "info": {"language": "zh", "language_probability": 0.99,
                 "duration": dur},
        "engine": asr_backend.BACKEND_ID, "library": asr_backend.LIBRARY_ID,
        "model": asr_backend.MODEL_ID, "config": cfg, "decode": {},
        "initial_prompt": initial_prompt, "prompt_chars": len(initial_prompt or ""),
        "word_timestamps": bool(word_timestamps),
        "no_speech_threshold": float(no_speech_threshold),
        "monotonic": True, "asr_calls": 1, "engine_calls": 1,
        "transcribe_s": 0.01,
    }


class _StubTokenizer:
    def encode(self, text):
        return [0] * max(1, len(" " + str(text).strip()))


_ORIG = {
    "transcribe_file": asr_backend.transcribe_file,
    "load_model": asr_backend.load_model,
    "resolve_runtime": asr_backend.resolve_runtime,
    "load_tokenizer": asr_backend.load_tokenizer,
    "asr_available": server._asr_engine_available,
}


def install_stubs() -> None:
    asr_backend.transcribe_file = _stub_transcribe_file
    asr_backend.load_model = lambda *a, **k: object()   # 桩路径不真正用模型
    asr_backend.resolve_runtime = lambda *a, **k: {
        "device": "cuda", "compute_type": "int8_float16", "batch_size": 1,
        "num_workers": 1, "explicit_cpu": False, "gpu_available": True,
        "ab_approved": False, "fallback": None}
    asr_backend.load_tokenizer = lambda *a, **k: _StubTokenizer()
    server._asr_engine_available = lambda: True


def restore_stubs() -> None:
    asr_backend.transcribe_file = _ORIG["transcribe_file"]
    asr_backend.load_model = _ORIG["load_model"]
    asr_backend.resolve_runtime = _ORIG["resolve_runtime"]
    asr_backend.load_tokenizer = _ORIG["load_tokenizer"]
    server._asr_engine_available = _ORIG["asr_available"]


def setup_ffmpeg_env() -> bool:
    """本机真 ffmpeg（Mac 宿主跑 ffmpeg 抽 wav 是真链路的一部分）。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    os.environ["V2O_FFMPEG"] = exe
    return True


def setup_fake_model_manifest() -> str:
    """tmp 伪造模型目录 + 真 SHA-256 清单：供应链闸走真实现。"""
    model_dir = os.path.join(TMP_ROOT, "fake-model")
    os.makedirs(model_dir, exist_ok=True)
    import hashlib

    files = []
    for name, payload in (("model.bin", b"stub-ct2-model-bytes"),
                          ("tokenizer.json", b'{"stub": true}'),
                          ("config.json", b'{"stub": true}')):
        full = os.path.join(model_dir, name)
        with open(full, "wb") as fh:
            fh.write(payload)
        files.append({"path": name, "sha256": hashlib.sha256(payload).hexdigest()})
    manifest = {
        "schema": "v2o-model-manifest/v1",
        "models": [{
            "model_id": asr_backend.MODEL_ID,
            "source": "https://huggingface.co/" + asr_backend.MODEL_ID,
            "revision": "1234567890abcdef1234567890abcdef12345678",
            "license": "mit",
            "local_path": model_dir,
            "files": files,
        }],
    }
    mpath = os.path.join(TMP_ROOT, "MODEL_MANIFEST.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.environ["V2O_MODEL_MANIFEST"] = mpath
    return mpath


# ---- 合成数据 -----------------------------------------------------------
def make_wav_mp4(path: str, seconds: float, rate: int = 16000) -> str:
    """合成「视频」文件：真 wav 字节 + .mp4 后缀（ffmpeg 按内容探测解码）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        total = int(seconds * rate)
        chunk = bytearray()
        for i in range(total):
            if seconds <= 10:
                v = int(6000 * math.sin(2 * math.pi * 220 * i / rate))
            else:
                v = 0  # 长音频用静音，写快
            chunk += int(v).to_bytes(2, "little", signed=True)
        wf.writeframes(bytes(chunk))
    return path


# ---- 基础设施 -----------------------------------------------------------
class Scene:
    """一个场景一套 tmp 根：data_root / input_root / vault。"""

    def __init__(self, name: str) -> None:
        self.root = os.path.join(TMP_ROOT, name)
        self.data_root = os.path.join(self.root, "data-root")
        self.input_root = os.path.join(self.root, "输入文件夹")
        self.vault = os.path.join(self.root, "测试库")
        for d in (self.data_root, self.input_root, self.vault):
            os.makedirs(d, exist_ok=True)


def start_listener(scene: Scene) -> None:
    code, body = server._handle_start_post(json.dumps({
        "data_root": scene.data_root,
        "input_root": scene.input_root,
        "ob_vault_root": scene.vault,
        "asr_profile_hash": PROFILE_HASH,
    }).encode("utf-8"))
    if not T.ok(code == 202, "start 监听接受 (code=%s)" % (code,)):
        raise SystemExit(2)
    deadline = time.time() + 60
    while time.time() < deadline:
        box = server._handle.get("box")
        if isinstance(box, dict) and box.get("running") and \
                not server._listener.get("error"):
            return
        if server._listener.get("error"):
            T.ok(False, "run_startup 报错: %r" % (server._listener.get("error"),))
            raise SystemExit(2)
        time.sleep(0.2)
    T.ok(False, "run_startup 60s 未进入 RUNNING")
    raise SystemExit(2)


def stop_listener(wait_worker: bool = True) -> None:
    server._handle_stop_post(b"")
    if wait_worker:
        deadline = time.time() + 30
        while time.time() < deadline:
            if not server._worker.get("running") and \
                    not server._worker.get("current"):
                return
            time.sleep(0.2)


def wait_processed(run_id: str, states: tuple, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in reversed(server._worker.get("processed") or []):
            if isinstance(entry, dict) and entry.get("run_id") == run_id \
                    and entry.get("state") in states:
                return dict(entry)
        time.sleep(0.3)
    return None


def wait_run_created(scene: Scene, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        con = s2_store.open_db(scene.data_root)
        try:
            row = con.execute(
                "SELECT run_id FROM processing_runs WHERE creation_mode='AUTO'"
                " ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if row is not None:
            return row[0]
        time.sleep(0.3)
    return None


def db_snapshot(data_root: str) -> dict:
    con = s2_store.open_db(data_root)
    try:
        out = {
            "sources": con.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "auto_runs": con.execute(
                "SELECT COUNT(*) FROM processing_runs WHERE"
                " creation_mode='AUTO'").fetchone()[0],
            "runs_status": dict(con.execute(
                "SELECT status, COUNT(*) FROM processing_runs"
                " GROUP BY status").fetchall()),
            "pub_published": con.execute(
                "SELECT COUNT(*) FROM publish_records WHERE"
                " status='PUBLISHED'").fetchone()[0],
            "pub_rows": [dict(r) for r in con.execute(
                "SELECT * FROM publish_records").fetchall()],
            "runs": [dict(r) for r in con.execute(
                "SELECT * FROM processing_runs").fetchall()],
        }
        return out
    finally:
        con.close()


def calls_since(n: int) -> int:
    return len(ENGINE_CALLS) - n


def wait_until(fn, timeout: float, tick: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(tick)
    return fn()


def vault_files(vault: str) -> list[str]:
    out = []
    for dirpath, _dirs, names in os.walk(vault):
        for n in names:
            out.append(os.path.join(dirpath, n))
    return sorted(out)


def half_products(paths: list[str]) -> list[str]:
    bad = (".tmp", ".part", ".partial", ".crdownload", ".swp")
    return [p for p in paths if p.lower().endswith(bad)]


# ---- 场景 A：端到端（真监听链） -------------------------------------------
def scenario_a() -> None:
    print("== 场景 A：端到端 监听发现→入库→转写→成稿→发布 ==")
    sc = Scene("a-e2e")
    short_name = "会议纪要 09.合成测试.mp4"
    long_name = "长会议 601秒.mp4"
    dup_name = "重复笔记 视频.mp4"
    install_stubs()
    try:
        start_listener(sc)

        # A1 短音频：stage7 单文件路径
        n0 = len(ENGINE_CALLS)
        make_wav_mp4(os.path.join(sc.input_root, short_name), 2.0)
        run1 = wait_run_created(sc, 60)
        T.ok(run1 is not None, "watcher 发现并建 AUTO run（短音频）")
        res1 = wait_processed(run1, ("PUBLISHED", "RENDER_ONLY", "FAIL",
                                     "PUBLISH_BLOCKED"), 120)
        T.ok(res1 is not None and res1.get("state") == "PUBLISHED",
             "短音频 run 端到端 PUBLISHED（%r）" % (res1 and res1.get("verdict"),))
        T.ok(calls_since(n0) == 1, "短音频引擎恰好调 1 次（stage7 路径）")
        note1 = os.path.join(sc.vault, "会议纪要 09.合成测试.md")
        T.ok(os.path.isfile(note1), "恰出一篇 md（单扩展名 09.xxx.mp4→09.xxx.md）")
        if os.path.isfile(note1):
            with open(note1, "r", encoding="utf-8") as fh:
                md1 = fh.read()
            T.ok(STUB_TEXT in md1, "md 含转写文本")
            T.ok("会议纪要 09.合成测试" in md1, "md 标题=源文件去扩展名")
        T.ok(os.path.isfile(os.path.join(sc.input_root, short_name)),
             "源文件保留（短音频）")

        # A2 长音频（601s > 600s 冻结块长）：stage8 分块路径
        n_long0 = len(ENGINE_CALLS)
        make_wav_mp4(os.path.join(sc.input_root, long_name), 601.0)
        deadline = time.time() + 60
        run2 = None
        while time.time() < deadline and run2 is None:
            con = s2_store.open_db(sc.data_root)
            try:
                row = con.execute(
                    "SELECT pr.run_id FROM processing_runs pr JOIN sources s"
                    " ON pr.source_id=s.source_id WHERE s.current_path LIKE ?"
                    " ORDER BY pr.created_at DESC LIMIT 1",
                    ("%" + long_name,)).fetchone()
            finally:
                con.close()
            run2 = row[0] if row else None
            if run2 is None:
                time.sleep(0.5)
        T.ok(run2 is not None, "watcher 发现并建 AUTO run（长音频）")
        res2 = wait_processed(run2, ("PUBLISHED", "RENDER_ONLY", "FAIL",
                                     "PUBLISH_BLOCKED"), 180)
        T.ok(res2 is not None and res2.get("state") == "PUBLISHED",
             "长音频 run 端到端 PUBLISHED（%r）" % (res2 and res2.get("verdict"),))
        T.ok(calls_since(n_long0) == 2,
             "长音频引擎恰好调 2 次（stage8 分块 600/2）")
        note2 = os.path.join(sc.vault, "长会议 601秒.md")
        T.ok(os.path.isfile(note2), "长音频恰出一篇 md")

        # A3 No-Clobber：目标笔记已存在 → SKIPPED，零转写、零覆盖
        note3 = os.path.join(sc.vault, "重复笔记 视频.md")
        sentinel = "已有内容，一字节都不许动。"
        with open(note3, "w", encoding="utf-8") as fh:
            fh.write(sentinel)
        make_wav_mp4(os.path.join(sc.input_root, dup_name), 1.0)
        deadline = time.time() + 60
        run3 = None
        while time.time() < deadline and run3 is None:
            con = s2_store.open_db(sc.data_root)
            try:
                row = con.execute(
                    "SELECT pr.run_id FROM processing_runs pr JOIN sources s"
                    " ON pr.source_id=s.source_id WHERE s.current_path LIKE ?",
                    ("%" + dup_name,)).fetchone()
            finally:
                con.close()
            run3 = row[0] if row else None
            if run3 is None:
                time.sleep(0.5)
        T.ok(run3 is not None, "watcher 发现并建 AUTO run（已存在笔记场景）")
        res3 = wait_processed(run3, ("SKIPPED", "PUBLISHED", "FAIL",
                                     "PUBLISH_BLOCKED"), 120)
        T.ok(res3 is not None and res3.get("state") == "SKIPPED",
             "目标笔记已存在 → SKIPPED 不覆盖")
        T.ok((res3 or {}).get("whisper_calls") == 0,
             "SKIPPED 场景 whisper 0 次调用")
        with open(note3, "r", encoding="utf-8") as fh:
            T.ok(fh.read() == sentinel, "既有笔记一字节未动")

        # A4 全局账目与盘面
        stop_listener()
        snap = db_snapshot(sc.data_root)
        T.ok(snap["sources"] == 3, "Source 恰 3（三个文件）")
        T.ok(snap["auto_runs"] == 3, "AUTO Run 恰 3")
        T.ok(set(snap["runs_status"]) <= {"QUEUED", "FAILED_RETRYABLE",
                                          "NO_SPEECH_DETECTED"},
             "run 状态枚举不被转写态污染（P0-4）: %r" % (snap["runs_status"],))
        T.ok(snap["pub_published"] == 2, "publish_records PUBLISHED 恰 2")
        vfiles = vault_files(sc.vault)
        T.ok(vfiles == sorted([note1, note2, note3]),
             "vault 恰 3 篇预期 md、无其他文件: %r" % (vfiles,))
        T.ok(not half_products(vfiles), "vault 无半成品文件")
        T.ok(not server._worker.get("current"), "收工后 worker current 清空")
    finally:
        restore_stubs()

    # T1 反向证伪：前置门改坏 → 不再 SKIPPED（真跑转写 → 状态变 BLOCKED）。
    # 如实记录：提交链「绝不覆盖」的结构性防线此时仍守住（笔记字节不动）。
    print("  牙    T1：改坏 _vault_note_already_there（恒 None）")
    if not T.ok(run3 is not None, "T1 前置：run3 存在"):
        restore_stubs()
        return
    install_stubs()
    orig_gate = server._vault_note_already_there
    try:
        s2_instance.acquire(sc.data_root)
        server._vault_note_already_there = lambda *a, **k: None
        res = server._process_one_run(sc.data_root, sc.input_root, sc.vault,
                                      run3, PROFILE_HASH)
        red = (res or {}).get("state") != "SKIPPED"
        T.tooth("No-Clobber 前置门（SKIPPED）有牙", red)
        with open(note3, "r", encoding="utf-8") as fh:
            T.ok(fh.read() == sentinel,
                 "结构性防线：即使前置门失守，提交链仍不覆盖既有笔记")
    finally:
        server._vault_note_already_there = orig_gate
        restore_stubs()
        try:
            s2_instance.release(sc.data_root)
        except Exception:
            pass


# ---- 场景 B：崩溃恢复 -----------------------------------------------------
def scenario_b() -> None:
    print("== 场景 B2：转写中途强杀 → 重启扫描 幂等恢复 ==")
    sc = Scene("b-crash")
    name = "崩溃恢复 测试.mp4"
    install_stubs()
    try:
        start_listener(sc)
        CRASH["armed"] = True
        n_crash0 = len(ENGINE_CALLS)
        make_wav_mp4(os.path.join(sc.input_root, name), 2.0)
        run_id = wait_run_created(sc, 60)
        T.ok(run_id is not None, "崩溃前 AUTO run 已建")
        ok = wait_until(
            lambda: not CRASH["armed"] and not server._worker.get("running"),
            90)
        T.ok(ok, "强杀生效：worker 中途死亡（转写调用内 KeyboardInterrupt）")
        killed_recorded = any(
            isinstance(p, dict) and p.get("run_id") == run_id
            for p in server._worker.get("processed") or [])
        T.ok(not killed_recorded, "强杀不产生假终态记录（无 FAIL/PUBLISHED 记录）")
        T.ok(not vault_files(sc.vault), "强杀后 vault 零文件（无半成品发布）")
        stop_listener()

        # 重启（模拟进程重开：_worker_done 由 start 清空）
        start_listener(sc)
        res = wait_processed(run_id, ("PUBLISHED", "FAIL", "PUBLISH_BLOCKED"),
                             120)
        T.ok(res is not None and res.get("state") == "PUBLISHED",
             "重启扫描后同一 run 幂等重跑 → PUBLISHED")
        note = os.path.join(sc.vault, "崩溃恢复 测试.md")
        T.ok(os.path.isfile(note), "恢复后恰出 1 篇 md")
        snap = db_snapshot(sc.data_root)
        T.ok(snap["sources"] == 1 and snap["auto_runs"] == 1,
             "Lost/Duplicate=0：Source=1 且 AUTO Run=1")
        T.ok(snap["pub_published"] == 1, "publish PUBLISHED 恰 1（无重复发布）")
        T.ok(calls_since(n_crash0) == 2, "强杀 1 次 + 恢复 1 次，引擎共调 2 次")
        run_row = (snap["runs"] or [{}])[0]
        T.ok(all(run_row.get(k) for k in
                 ("raw_artifact_id", "initial_normalization_revision_id",
                  "initial_render_revision_id", "initial_publish_record_id")),
             "run 行四个产物 id 齐全")
        vfiles = vault_files(sc.vault)
        T.ok(vfiles == [note], "vault 恰 1 篇 md、无半成品: %r" % (vfiles,))

        # T3 正向：已发布笔记被用户删掉 + 重启 → 不重转、不静默重建
        print("  --  T3 正向：删已发布笔记 + 重启 = 不重转不重建")
        os.unlink(note)
        stop_listener()
        n_before = len(ENGINE_CALLS)
        start_listener(sc)
        time.sleep(12)  # 覆盖至少两个 worker 轮询周期（5s/轮）
        T.ok(os.path.isfile(note) is False,
             "已完成跳过：删笔记+重启不静默重建（笔记仍缺）")
        T.ok(calls_since(n_before) == 0, "已完成跳过：重启零重转（引擎 0 次新调用）")
        stop_listener()

        # T3-a 反向证伪第一层：双闸改坏 → 会重转，但提交链真值闸仍拒绝伪造
        # （publish_records 行 PUBLISHED 而盘上 final 缺失 → 拒绝 fabricate，
        #  宁 BLOCK 不静默重建）——第三道独立防线。
        print("  牙    T3-a：改坏双闸（磁盘 receipt 扫描 + DB 成功旧账）")
        orig_scan = server._scan_disk_states
        orig_dbsucc = server._db_success_run_ids_ro
        server._scan_disk_states = lambda *a, **k: {}
        server._db_success_run_ids_ro = lambda *a, **k: set()
        try:
            start_listener(sc)
            ok = wait_until(lambda: calls_since(n_before) > 0, 90)
            T.ok(calls_since(n_before) > 0, "双闸改坏后确实发生重转（不再跳过）")
            res_t3a = wait_processed(run_id, ("PUBLISH_BLOCKED", "FAIL",
                                              "PUBLISHED"), 60)
            T.ok(
                (res_t3a or {}).get("state") == "PUBLISH_BLOCKED" and
                "refusing to fabricate" in str((res_t3a or {}).get("verdict") or ""),
                "提交链真值闸：PUBLISHED 行 final 缺失 → 拒绝伪造字节，不静默重建")
            T.ok(not os.path.isfile(note), "第三道防线下笔记仍不被静默重建")
        finally:
            stop_listener()

        # T3-b 反向证伪第二层：真值闸也改坏（PUBLISHED 行被重置回 PENDING）
        # → 静默重建发生 → 测红（证明第三道防线本身有牙）。
        print("  牙    T3-b：连提交链真值闸一起改坏（PUBLISHED 行重置 PENDING）")
        orig_commit = s4_pc.commit_publish
        n_t3b = len(ENGINE_CALLS)

        def _broken_commit(con, job_dir, publish_record_id, on_tmp_ready=None):
            con.execute(
                "UPDATE publish_records SET status='PENDING', published_hash=NULL"
                " WHERE publish_record_id=?", (publish_record_id,))
            con.commit()
            return orig_commit(con, job_dir, publish_record_id,
                               on_tmp_ready=on_tmp_ready)

        try:
            s4_pc.commit_publish = _broken_commit
            start_listener(sc)
            ok = wait_until(lambda: os.path.isfile(note) and
                            calls_since(n_t3b) > 0, 90)
            red = os.path.isfile(note)
            T.tooth("「已完成跳过」全三道防线有牙（全改坏后静默重建发生）", red)
        finally:
            s4_pc.commit_publish = orig_commit
            server._scan_disk_states = orig_scan
            server._db_success_run_ids_ro = orig_dbsucc
            stop_listener()
    finally:
        CRASH["armed"] = False
        restore_stubs()

    print("== 场景 B1：发布中断 → 重启不重转、不重复、无半成品 ==")
    sc1 = Scene("b1-publish")
    name1 = "发布中断 测试.mp4"
    install_stubs()
    b1_state = {"calls": 0}
    orig_pub = s4_publish.initial_publish

    def _crash_publish(*a, **k):
        if b1_state["calls"] == 0:
            b1_state["calls"] += 1
            raise OSError("模拟发布中断（权限/占用）")
        return orig_pub(*a, **k)

    try:
        s4_publish.initial_publish = _crash_publish
        start_listener(sc1)
        make_wav_mp4(os.path.join(sc1.input_root, name1), 2.0)
        run_id = wait_run_created(sc1, 60)
        T.ok(run_id is not None, "B1 run 已建")
        res = wait_processed(run_id, ("PUBLISH_BLOCKED", "FAIL", "PUBLISHED"),
                             120)
        T.ok(res is not None and res.get("state") == "PUBLISH_BLOCKED",
             "发布中断 → PUBLISH_BLOCKED（字节未动，初稿保留）")
        T.ok(not vault_files(sc1.vault), "发布中断后 vault 零文件（无半成品）")
        rendered = (res or {}).get("rendered_path")
        T.ok(rendered and os.path.isfile(rendered),
             "成稿仍保留在数据目录（可显式恢复）")
        n_calls = len(ENGINE_CALLS)
        stop_listener()
        start_listener(sc1)
        time.sleep(12)  # 覆盖至少两个 worker 轮询周期
        T.ok(calls_since(n_calls) == 0,
             "重启扫描不重转（norm COMPLETED 已是跳过口径，不重烧 GPU）")
        snap = db_snapshot(sc1.data_root)
        T.ok(snap["sources"] == 1 and snap["auto_runs"] == 1,
             "重启不重复建 Source/Run")
        T.ok(snap["pub_published"] == 0, "重启不静默补发布（留显式恢复路径）")
        stop_listener()
    finally:
        s4_publish.initial_publish = orig_pub
        restore_stubs()


def _worker_cycled() -> bool:
    """worker 至少跑完一轮轮询（running 且无 current 即一轮已收口）。"""
    return bool(server._worker.get("running")) and \
        not server._worker.get("current")


# ---- 场景 C：周期 reconcile 幂等 + T2 ------------------------------------
def scenario_c() -> None:
    print("== 场景 C：reconcile 幂等（Lost/Duplicate=0）+ T2 ==")
    sc = Scene("c-reconcile")
    name = "对账 幂等.mp4"
    make_wav_mp4(os.path.join(sc.input_root, name), 1.0)
    s2_instance.startup(sc.data_root, sc.input_root)
    try:
        out1 = initial_reconcile(sc.input_root, sc.data_root, PROFILE_HASH)
        out2 = initial_reconcile(sc.input_root, sc.data_root, PROFILE_HASH)
        T.ok(all(i.get("deliver_error") is None for i in out1 + out2),
             "两轮 reconcile 投递零错误")
        snap = db_snapshot(sc.data_root)
        T.ok(snap["sources"] == 1, "reconcile ×2 → Source=1")
        T.ok(snap["auto_runs"] == 1, "reconcile ×2 → AUTO Run=1（幂等）")

        # T2 反向证伪：get_or_create_auto_run 改坏成「每次都建新 run」
        print("  牙    T2：改坏 get_or_create_auto_run（每次都新建）")
        dup_n = {"n": 0}
        orig_goc = s2_runs.get_or_create_auto_run

        def _dup_run(source_id, asr_profile_hash, data_root, _con=None):
            dup_n["n"] += 1
            h = "%s#dup%d" % (asr_profile_hash, dup_n["n"])
            rid = "run_dup%04d" % dup_n["n"]
            con = _con if _con is not None else s2_store.open_db(data_root)
            try:
                now = s2_store.utc_now_iso()
                con.execute(
                    "INSERT INTO processing_runs (run_id, job_id, source_id,"
                    " creation_mode, auto_run_identity, asr_profile_hash,"
                    " status, retry_count, created_at, updated_at,"
                    " completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, None, source_id, "AUTO", source_id + "|" + h, h,
                     "QUEUED", 0, now, now, None))
                if _con is None:
                    con.commit()
            finally:
                if _con is None:
                    con.close()
            return {"run_id": rid}

        try:
            s2_runs.get_or_create_auto_run = _dup_run
            initial_reconcile(sc.input_root, sc.data_root, PROFILE_HASH)
            snap = db_snapshot(sc.data_root)
            red = snap["auto_runs"] > 1
            T.tooth("重启扫描误重复建 run 有牙（改坏后 AUTO Run>1）", red)
        finally:
            s2_runs.get_or_create_auto_run = orig_goc
    finally:
        try:
            s2_instance.release(sc.data_root)
        except Exception:
            pass


# ---- 场景 D：半截源不发布 + T4 -------------------------------------------
def scenario_d() -> None:
    print("== 场景 D：半截源不发布（快照复核）+ T4 ==")
    sc = Scene("d-stale")
    name = "半截源 防护.mp4"
    src = os.path.join(sc.input_root, name)
    make_wav_mp4(src, 1.0)
    s2_instance.startup(sc.data_root, sc.input_root)
    install_stubs()
    orig_stale = server._stale_source_reason
    n_d0 = len(ENGINE_CALLS)
    try:
        out = s2_discover(src, sc.data_root, PROFILE_HASH)
        run_id = out.get("run_id")
        T.ok(run_id is not None, "直投 discover 建 run")
        # 入队后源文件又变大（半截被追加完/被替换）
        with open(src, "ab") as fh:
            fh.write(b"\x00" * 256)
        res = server._process_one_run(sc.data_root, sc.input_root, sc.vault,
                                      run_id, PROFILE_HASH)
        T.ok((res or {}).get("state") == "FAIL" and
             "还在变" in str((res or {}).get("verdict") or ""),
             "半截源 → FAIL 人话（%r）" % ((res or {}).get("verdict"),))
        T.ok(calls_since(n_d0) == 0, "半截源零转写（送引擎前拦下）")
        T.ok(not vault_files(sc.vault), "半截源零发布")
        T.ok(os.path.isfile(src), "源文件保留")

        # T4 反向证伪：快照复核改坏 → 半截源照样发布
        print("  牙    T4：改坏 _stale_source_reason（恒 None）")
        server._stale_source_reason = lambda *a, **k: None
        res2 = server._process_one_run(sc.data_root, sc.input_root, sc.vault,
                                       run_id, PROFILE_HASH)
        red = (res2 or {}).get("state") == "PUBLISHED" and \
            bool(vault_files(sc.vault))
        T.tooth("半截源复核门有牙（改坏后半截照发）", red)
    finally:
        server._stale_source_reason = orig_stale
        restore_stubs()
        try:
            s2_instance.release(sc.data_root)
        except Exception:
            pass


# ---- 主流程 --------------------------------------------------------------
def main() -> int:
    print("tmp 根：%s" % (TMP_ROOT,))
    if not T.ok(setup_ffmpeg_env(), "本机 ffmpeg 可用（真抽 wav 链路）"):
        print("FAIL 需要 ffmpeg 才能跑 Stage4 端到端自测", file=sys.stderr)
        return 1
    setup_fake_model_manifest()
    T.ok(os.path.isfile(os.environ["V2O_MODEL_MANIFEST"]),
         "tmp 模型清单就位（真 SHA-256 校验链）")
    try:
        scenario_a()
        scenario_b()
        scenario_c()
        scenario_d()
    finally:
        restore_stubs()
        try:
            shutil.rmtree(TMP_ROOT, ignore_errors=True)
        except Exception:
            pass
    print("")
    print("断言 %d 条，失败 %d；teeth %d 条，有牙 %d" %
          (T.total, len(T.failed), T.teeth_total, T.teeth_sharp))
    if T.failed:
        print("未过项：")
        for f in T.failed:
            print("  - %s" % (f,))
        return 1
    print("SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
