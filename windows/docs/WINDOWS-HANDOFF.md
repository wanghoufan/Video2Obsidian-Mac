# WINDOWS-HANDOFF｜Windows 迁移 Stage 4（集成交付）交接说明

> 交接对象：Windows 端接续的智能体 / 用户。
> 基线：Stages 0–3 已收口（asr_backend 适配层、stage1/7/8/prompt_builder 接线、msvcrt 锁/卷探针/长路径、start.ps1/bat、ffmpeg 单点）；Stage 4 本轮新增**端到端集成自测、崩溃恢复语义验证、README Windows 化、本交接文档**。
> 铁律提醒：本仓 `windows/` 子目录隔离开发，仓根 `app/ src/ tests/` 零改动；以下所有「真机待验」项**一律未推断为通过**。

---

## 一、本轮（Stage 4）在 macOS 宿主上已完成并自测通过的内容

自测文件：`windows/tests/selftest_win_stage4.py`（rc=0；65 条断言 + 4 条反向证伪全部有牙）。
跑法：仓库根 `.venv/bin/python windows/tests/selftest_win_stage4.py`（Mac 宿主；Windows 真机上用 `.venv\Scripts\python.exe tests\selftest_win_stage4.py` 复跑）。

自测方法：转写引擎用桩（monkeypatch `asr_backend.transcribe_file/load_model/resolve_runtime/load_tokenizer`），模型供应链闸走真实现（tmp 伪造模型目录 + 真 SHA-256 清单），其余全链路真跑：真 watchdog 监听 + 四道投递门 → 真 discover/promote/AUTO run → 真 ffmpeg 抽 16k mono wav → stage7/stage8 分块 → stage3 Norm/Render + stage9 后处理 → stage4 发布（真 No-Clobber/提交链）。

已验证（桩引擎口径）：

1. **端到端**：短音频（stage7 单文件，恰 1 次引擎调用）与长音频 601s（stage8 分块 600/2，恰 2 次调用）各自恰好出 1 篇 md；命名 `09.xxx.mp4→09.xxx.md` 单扩展名；源文件保留；run 状态枚举不被污染（P0-4，DB 始终 QUEUED 族）；publish_records 全 PUBLISHED；vault 无半成品文件。
2. **No-Clobber**：目标笔记已存在 → SKIPPED（whisper 0 次）、既有笔记一字节不动；改坏前置门后，提交链仍不覆盖（结构性防线）。
3. **崩溃恢复（转写中途强杀）**：重启扫描后同一 run 幂等重跑，恰好 1 篇 md、Source=1、AUTO Run=1、publish PUBLISHED=1、无半成品。
4. **崩溃恢复（发布中断）**：PUBLISH_BLOCKED 后重启**不重转**（norm COMPLETED 已是跳过口径）、不重复建 Source/Run、成稿保留在数据目录、不静默补发布。
5. **reconcile 幂等**：同一文件两轮 initial_reconcile → Source=1、Run=1（Lost/Duplicate=0）。
6. **半截源不发布**：入队后源文件变大 → 送引擎前 FAIL（人话），零转写、零发布、源保留。
7. **「已完成跳过」三道防线**（磁盘 receipt 扫描 / DB 成功旧账 / 提交链真值闸）：逐层改坏逐层测红——尤其第三道（`commit_publish` 对「SQLite PUBLISHED 而 final 缺失」拒绝伪造字节）即使前两道全失守仍挡住静默重建。

---

## 二、真机待验清单（Windows 11 实机；**未验=未通过，不许打勾**）

以下每项都需要在真 Windows 11 机器上实测并留证据（命令、截图或日志）。Mac 宿主的自测**不能**替代其中任何一条。

1. 真实 CUDA / faster-whisper / CTranslate2 加载：`resolve_runtime` GPU 探测、`int8_float16` 档位、绝对路径加载、缺 DLL/驱动时的 BLOCK 文案。
2. 真实转写质量与性能：60s 中文合成样本 + 一段真实视频的 CER/关键术语命中；墙钟、峰值显存/内存记录。
3. fp16 / batch>1 的 A/B 通行证流程（默认锁死，无通行证必须拦）。
4. 显式 CPU int8 兜底路径（页面勾选后真实跑通）。
5. Defender / UAC：首启是否被拦、SmartScreen 提示、是否要求管理员（**不得要求管理员**）。
6. 资源管理器定位：`explorer.exe /select,<path>` 参数数组调用真实打开。
7. `obsidian://` 协议跳转真实可用。
8. PowerShell 实跑 `start.ps1` / 双击 `start.bat`（venv 创建、依赖锁装、端口占用提示、UTF-8）。
9. 长路径策略：未开注册表策略时 >260 字符路径被拦的人话报错；开启后放行（含中文、空格、`丨`、emoji、280+ 字符）。
10. 干净机复装：干净 Windows 用户账户从 README 复装到端到端出稿。
11. 模型冻结与哈希校验：`tools/freeze_model_manifest.py` 生成清单 → 篡改一个文件 → 校验必须 BLOCK。
12. NTFS 语义：卷探针九项实测（原子改名/独占建/硬链接/劝告锁）、发布 tmp→final 提交、444 只读位在 NTFS 的表现。
13. 断网启动与断网转写（HF_HUB_OFFLINE 双保险、缺模型清单 BLOCK）。
14. 单实例锁在 POST /api/start 生效（console 进程本身不做启动期单实例，`app\server.py` 全程无 exit-3 路径）：监听开始后用 `src\stage2\instance.py --data-root <同一数据目录>` 跑第二实例 → exit 3、stderr `SECOND_INSTANCE`、DB 不变；端口被占用不杀进程。
15. 真机跑全套自测：`.venv\Scripts\python.exe tests\selftest_win_stage4.py`（及 stage3/stage12/contract/frontend/presets）rc=0。

---

## 三、Windows 机器验收命令（干净机按序执行）

```powershell
# 0) 前置：官方 Python 3.12（py -3.12 可用）、NVIDIA 驱动（nvidia-smi 可见卡）
py -3.12 --version
nvidia-smi

# 1) venv + 锁版本依赖
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 模型冻结（联网机做好模型目录后拷来；revision 换成真实 commit）
.\.venv\Scripts\python.exe tools\freeze_model_manifest.py ^
    --model-dir D:\models\faster-whisper-large-v3-turbo ^
    --revision <commit> ^
    --source https://huggingface.co/Systran/faster-whisper-large-v3-turbo ^
    --license MIT --license-file LICENSE

# 3) 断网启动（拔网线或关 Wi-Fi 后）
.\start.ps1

# 4) 健康检查（另一窗口）
Invoke-WebRequest http://127.0.0.1:8899/ -UseBasicParsing   # 期待 200

# 5) 端到端合成样本：页面填 tmp 测试目录 + tmp 测试 vault，按「开始」起监听，
#   放入 60s 合成音频（ wav 换 .mp4 后缀即可），观察出稿与 No-Clobber。

# 6) 第二实例（必须在监听已开始后跑：单实例锁在 POST /api/start 才获取，
#    console 进程本身不做启动期单实例，app\server.py 无 exit-3 路径）。
#    用 stage2 单实例 CLI 撞同一 data_root 的锁（页面若改过数据目录，
#    --data-root 换成实际值；DB 不被触碰）：
.\.venv\Scripts\python.exe src\stage2\instance.py --data-root "$env:LOCALAPPDATA\Video2Obsidian\data"; echo "exit=$LASTEXITCODE"   # 期待 exit=3、stderr SECOND_INSTANCE

# 7) 全套自测
.\.venv\Scripts\python.exe tests\selftest_win_stage4.py    # rc=0
.\.venv\Scripts\python.exe tests\selftest_win_stage3.py    # rc=0
.\.venv\Scripts\python.exe tests\selftest_win_stage12.py   # rc=0
.\.venv\Scripts\python.exe tests\selftest_p1_2_contract.py # rc=0
.\.venv\Scripts\python.exe tests\selftest_p1_2_frontend.py # rc=0
.\.venv\Scripts\python.exe tests\selftest_v26_presets.py   # rc=0
```

---

## 四、红线（Windows 端接续者同样遵守）

1. **No-Clobber**：已存在笔记一个字节都不能覆盖；文件占用、崩溃、OOM、源文件移动时源文件必须保留。
2. **不静默联网**：离线门禁 fail-closed；缺模型/缺依赖明确报错，绝不偷偷下载。
3. **不静默降级 CPU**：GPU不可用只报错；CPU int8 仅显式选择才走。
4. **测试只用外置 tmp + 合成数据**（`%TEMP%\v2o-win-<随机>`）；调 handler 前先断言 data_root 在系统临时目录下；**真实目录与真实 Obsidian 库零写入**。
5. 不提交模型、ffmpeg 大文件、密钥、token、`.env`、真实路径/隐私。
6. 不自动改注册表、不自动加 Defender 排除、不要求管理员运行。
7. 未获用户明确指令不 push；commit 规则按仓库 AGENTS.md。

---

## 五、残留风险与移交备注

1. **PUBLISH_BLOCKED 的显式恢复 UX**：发布中断后重启不会自动补发布（by design，留显式恢复路径；本次实测「重试」对 norm 已 COMPLETED 的 run 会被跳过口径挡回，**恢复发布请走页面的「重跑」链路（reapply，whisper 0 次）**。该交互在真机待验清单之外，建议 QA 真机补一条：PUBLISH_BLOCKED → 重跑 → 出稿。
2. 长音频（>600s）走 stage8 分块路径，本次桩验证覆盖 601s/2 块；真实长视频（10min+）的分块合并质量与耗时需真机记录。
3. ffmpeg 三档解析（V2O_FFMPEG → 随包 → PATH）在真机以日志 `kind=` 为准核对用的是哪一档。
4. `windows/README*.md` 中 `docs/usage.md`、`docs/troubleshooting.md` 等 Mac 仓文档链接已移除；Windows 包内文档以本文件为唯一交接入口。
5. `app/start.sh` 已删（Mac 前史遗留，Stage 1+2 复核 P3-5 挂账本轮闭环；git 历史可恢复）。Windows 入口唯一为 `start.ps1`/`start.bat`；端口透传相关自测断言已迁到 `start.ps1`。
