# WIN-STAGE4-CODE-REVIEW｜Windows 迁移 Stage 4（集成交付）复核

- 复核人：code-reviewer（独立新鲜眼睛，非橡皮章）
- 日期：2026-09-16
- 范围：`git status` 全部未提交项（2 个 M + `windows/docs/`、`windows/tests/selftest_win_stage4.py` 等 `??`，全部在 `windows/**` 内）
- 本轮交付物：①`windows/tests/selftest_win_stage4.py`（端到端集成自测，新）②`windows/docs/WINDOWS-HANDOFF.md`（Windows 端交接与真机验收清单，新）③`windows/README.md`／`README.en.md` Windows 化改写
- 结论：**PASS**（附 P2×1、P3×4；无 P0/P1；不阻塞 Stage 4 收口，P2 为建议 QA 补验项、P3 为挂账/知晓级）

---

## 一、必做核查逐项证据

### 1. 隔离铁律 ✅

实测 `git status --short -- app src tests`（仓根）输出为空。全部改动均限于 `windows/**`（2 个 M 的 README + 新增 docs/tests）。

### 2. numstat 对账 ✅（差 0）

`git diff --numstat` 实测（M 文件仅 2 个，与交付清单逐项一致）：

| 文件 | +/− | 与清单 |
|---|---|---|
| windows/README.en.md | 49/44 | ✅ |
| windows/README.md | 47/42 | ✅ |

未跟踪新件：`windows/tests/selftest_win_stage4.py`（807 行）、`windows/docs/WINDOWS-HANDOFF.md`（108 行）——与清单一致。**除申报改动外零触碰**：`windows/app/server.py`、`windows/src/**` 全部与 HEAD 零差异（证伪还原后逐文件 `shasum -a 256` 对账，见 §4；`git diff --numstat` 无任何第三个 M 文件）。本轮无 Stage 3 式业务代码增量，净增量全部在自测与文档两件新文件 + README 改写内。

### 3. README 事实声明逐条对码 ✅（不是文案审，是对码）

| README 声明 | 代码真源 | 核对 |
|---|---|---|
| 输入格式 `.mp4 .mov .mkv .m4v .avi .webm` | `windows/src/stage5/watcher.py:60` `VIDEO_SUFFIXES` 逐项一致 | ✅ |
| 默认数据目录 `%LOCALAPPDATA%\Video2Obsidian\data` | `platform_win.py:55,278-290`（拿不到 LOCALAPPDATA 不回落、人话报错） | ✅ |
| 端口 8899、`V2O_PORT` 覆盖、被占不杀进程 | `start.ps1:3-13`（端口单点注释与实现一致） | ✅ |
| `start.bat` 薄包装 | 实读 7 行，仅转调 `start.ps1` 并回传 errorlevel | ✅ |
| ffmpeg 三档（V2O_FFMPEG→随包→PATH）+ 日志标注 | Stage 3 已核（platform_win `ffmpeg_resolve`），本轮 README 表述与实现一致 | ✅ |
| 「重启后已完成任务自动跳过、半截任务自动重转，不重复出稿」 | 本轮 selftest 场景 B/T3/D 实证（见 §4） | ✅ |
| Mac 仓文档链接（docs/usage.md 等）已移除 | grep 零残留；`WINDOWS-HANDOFF.md` 为唯一交接入口 | ✅ |
| README.en.md 与中文版关键数字/路径对齐 | 8899/V2O_PORT/LOCALAPPDATA/格式清单逐项一致 | ✅ |

附带：两份 README grep `$'\x1b'` 零命中（diff 输出里的色码为终端渲染残留，非文件内容）。

### 4. 自测有牙：独立复跑 + 自跑 2 条文件级反向证伪 ✅

**独立复跑**（仓库根 `.venv/bin/python windows/tests/selftest_win_stage4.py`，日志 `/tmp/review_stage4_selftest_run.log`）：**rc=0，断言 65 条失败 0；teeth 4 条有牙 4**——与 builder 自报逐字一致（65/0、4/4）。

**自跑文件级证伪 2 条**（改真实现 → 测红 → `/tmp/win_stage4_after/` 备份拷回还原 → 测绿；全程未用 `git checkout`）：

| # | 变异（文件级） | 结果 |
|---|---|---|
| F1 | `windows/app/server.py` `_stale_source_reason` 开头插 `return None`（半截源复核门整体失效） | **测红：rc=1，失败 4**——「半截源 → FAIL 人话」「半截源零转写」「半截源零发布」全红 + T4 牙变「无牙」（半截源真的照发进 vault）；且证实守卫卡在送引擎之前。→ `/tmp` 还原 |
| F2 | `windows/src/stage2/runs.py` `get_or_create_auto_run` 改为每次 mint 新 hash（`-dupN` 递增）新建 run（幂等闸失效） | **测红：rc=1，失败 9**——「reconcile ×2 → AUTO Run=1」「重启不重复建 Source/Run」「已完成跳过不静默重建/零重转」「重启不重转/不静默补发布」全红。→ `/tmp` 还原 |

**还原对账**：还原后 `shasum -a 256` 与开工前记录逐字节一致（server.py `04c04ec0…`、runs.py `3c937a1b…`），`diff -q` 静默，`git diff --numstat` 复原为仅 README 49/44 + 47/42。终验复跑：**rc=0，65/0、4/4（SELFTEST PASS）**——工作树确证回到交付态。

**自测代码本身审读要点**（不是只看跑绿）：

- 铁律首检在位：`TMP_ROOT` 先断言 `realpath` 在系统临时目录下（selftest:74-77），之后才 import/调 handler。
- 桩面最小：只桩转写引擎四函数 + `server._asr_engine_available`；模型供应链闸走真实现（tmp 伪造模型目录 + 真 SHA-256 清单 + `V2O_MODEL_MANIFEST` 指路，selftest:206-235）；ffmpeg 抽 wav 是真 ffmpeg（`V2O_FFMPEG` 指本机真机）。
- 牙的真伪核过：4 条牙全部是「改坏实现 → 正向断言变红」结构（T1/T2/T3-b/T4），非恒真断言；F1/F2 从真实现侧再证一遍牙有牙。
- T3 三道防线逐层测红的结构真实：①磁盘 receipt 扫描 ②DB 成功旧账 ③提交链真值闸（`publish_commit.py:505` 起，"refusing to fabricate bytes" 三处分支在位），T3-a 双闸改坏后③独立挡住伪造字节、T3-b 连③改坏才静默重建——防线分层与 HANDOFF §一.7 描述一致。
- 计时窗口：`time.sleep(12)` 覆盖 ≥2 个 worker 5s 轮询周期（B2/B1 两处），worker 轮询周期若将来改动需同步此窗口（见 P3-3）。

### 5. WINDOWS-HANDOFF.md 审读 ✅

- 「已完成」与「真机待验」严格分列，15 条真机项全部标注「未验=未通过，不许打勾」，无一被推断为通过——符合铁律。
- 验收命令（§三）与 README/自测口径一致（8899、exit=3、六套自测清单）。
- 红线 7 条与本仓 AGENTS/Stage 0-3 口径一致（No-Clobber、不静默联网、不静默降级 CPU、外置 tmp、不提交模型/密钥、不动注册表、不 push）。
- 残留风险 §五如实（PUBLISH_BLOCKED 恢复 UX、长音频分块质量、ffmpeg 档位日志、Mac 文档链接移除）。

### 6. 隐私/密钥扫描 ✅

新文件与 README diff grep `/Users/`、`C:\`、`D:\`、`api[_-]?key`、`secret`、`password`、`token`：仅命中示例路径 `D:\models\faster-whisper-large-v3-turbo`（教科书式示例，非本机真实路径）、红线文案里的「token」一词、`_StubTokenizer` 类名。零隐私泄漏。`__pycache__/` 被根 `.gitignore` 覆盖，pyc 不入库。

---

## 二、问题清单

### P2-1｜PUBLISH_BLOCKED 的「重跑（reapply）恢复」链路本轮未端到端验证

- **位置**：`windows/app/server.py` 重跑链路（reapply）；HANDOFF §五.1 自报「重试会被 norm COMPLETED 跳过口径挡回，恢复发布请走页面的重跑链路（whisper 0 次）」并建议 QA 补一条。
- **影响**：发布中断后的唯一恢复出口是 reapply，本轮 65 条断言未覆盖「PUBLISH_BLOCKED → 重跑 → 出稿」这一条（仅验证了「不静默补发布」）。若 reapply 对 PUBLISH_BLOCKED run 不生效，用户将无路可走（成稿困在数据目录）。
- **建议**：不阻塞收口（HANDOFF 已如实挂账）；QA 在宿主或真机补一条：制造 PUBLISH_BLOCKED → 页面重跑 → 出稿且 whisper 0 次。

### P3-1｜README「ffmpeg 随包」承诺在仓内无对应物（交付打包清单未落地）

- **位置**：`windows/README.md` 前置要求「交付包自带 `third_party\ffmpeg\bin\ffmpeg.exe`」；`windows/` 树内无 `third_party/` 目录。
- **影响**：代码三档解析（V2O_FFMPEG→随包→PATH）对「随包缺失」有 PATH 兜底，功能不断链；但打包阶段若忘记真的带上 ffmpeg.exe，README 与交付物不符，用户按 README 排障会走偏。
- **建议**：打包/交付阶段核对随包清单（ffmpeg 二进制 + 来源/版本/许可证记录），与模型冻结、requirements 哈希回填同批完成。

### P3-2｜requirements.txt 哈希仍为占位（Stage 3 遗留，顺延）

- **位置**：`windows/requirements.txt` `--hash=REPLACE_WITH_REAL_DIGEST_*` 注释未启用。
- **现状**：仍开（有意保留，文件头已明示）；随模型冻结一并回填，别漏。

### P3-3｜selftest 两处 `time.sleep(12)` 硬编码轮询窗口

- **位置**：`selftest_win_stage4.py:566,654`（覆盖 2 个 5s worker 轮询周期）。
- **影响**：仅测试基建；若 worker 轮询周期将来调大，该窗口可能不足导致偶发误红。知晓即可，随周期改动同步。

### 前次报告（WIN-STAGE3）条目现状（逐条）

- **Stage3-P1-1/P2-1/P2-2**（ffmpeg 单点/run 级校验/run 级 config）：**已闭（保持）**——本轮 `windows/src/**` 与 HEAD 零差异，返工成果未被触碰。
- **Stage3-P3-1**（monotonic 守卫真实路径不可达）：**仍开/知晓级**——本轮桩口径不变，不构成新风险。
- **Stage3-P3-2**（OOM 后模型无显式释放、chunk wav 不清理）：**仍开**——本轮未触碰该路径；HANDOFF 已列入真机观察项，Stage 4 挂账成立。
- **Stage3-P3-3**（requirements.txt 哈希占位）：**仍开**（即上文 P3-2，随模型冻结一并回填）。

---

## 三、实测证据汇总

- 隔离：`git status --short -- app src tests` 空。
- numstat：2 个 M 文件与清单差 0；除申报外零触碰（server.py/runs.py 证伪还原后与 HEAD 零差异，sha256 对账一致）。
- 独立复跑 selftest_win_stage4：rc=0，65 断言 0 失败、4/4 有牙（`/tmp/review_stage4_selftest_run.log`）。
- 文件级证伪 F1/F2：改坏→rc=1（失败 4/失败 9）→ `/tmp/win_stage4_after/` 还原→终验 rc=0（`/tmp/review_stage4_F1_red.log`、`/tmp/review_stage4_F2_red.log`、`/tmp/review_stage4_final_green.log`）。
- README 事实声明 8 项逐条对码成立；README/HANDOFF 无 ESC 残留、无隐私泄漏。
- 未 commit/push；测试仅外置 tmp + 合成数据；未联网；业务代码零改动（证伪已全部还原并复核 numstat）。

**结论：PASS**——端到端集成自测真实有效（独立复跑+双向证伪证实）、崩溃恢复语义与三道「已完成跳过」防线实证成立、README Windows 化与代码逐条对码一致、HANDOFF 把真机未验项如实隔离。P2-1 建议 QA 补 reapply 恢复链路验证；P3 全部为挂账/知晓级。

---

## 四、返工复核（2026-09-16 第二轮）

- 复核人：code-reviewer（同一链续任）
- 返工范围：①P2-1 建议已改写为 HANDOFF §三步骤 5/6 对调＋§二.14 预期同步 ②P3-1 `git rm windows/app/start.sh`（-43 行，已暂存 D）＋HANDOFF §五.5 记删 ③builder 超范围自决连带修三处（已报备）：contract 新 5 条防回退断言、frontend 改读 start.ps1、index.html 用户文案换 `.\start.ps1`、server.py:57 注释一词
- 结论：**PASS（返工真实、最小、无副作用）**；遗留见节末清单

### 1. P2-1（第二实例验收命令对调）✅（读码逐行实证）

builder 实证逐条复验：

| 申报 | 实码 | 核对 |
|---|---|---|
| `instance.py:43` EXIT_SECOND_INSTANCE=3 | `EXIT_SECOND_INSTANCE = 3`（:43） | ✅ |
| `:208-210` main 捕 SecondInstanceError return 3 | `except SecondInstanceError` → stderr `SECOND_INSTANCE …` → `return EXIT_SECOND_INSTANCE`（:208-210） | ✅ |
| `:175-178` acquire 先于 init_db | `acquire(data_abs)`（:175）→ `store.init_db(data_abs)`（:178），SecondInstanceError 抛出时 DB 零触碰 | ✅ |
| 锁路径 `<data_root>/data/.lock` | `store.py:28` `LOCK_RELPATH = os.path.join("data", ".lock")`＋`:104` join(abspath) | ✅ |
| server 侧锁在 POST /api/start 才获取 | `server.py:6638` 路由 → `_handle_start_post`（:5431）→ 线程 `_launch`（:5346）→ `run_startup`（:5365）→ `stage5/startup.py:172` `_instance.startup`（内含 acquire，SecondInstanceError 传播） | ✅ |
| console 进程无启动期单实例、`app\server.py` 无 exit-3 路径 | grep `windows/app/server.py`：`acquire/SECOND_INSTANCE/exit(3)` 全部零命中 | ✅ |

**对调必要性（专项审）**：若按原顺序（第二实例在监听开始前跑），server 进程尚未走到 `_handle_start_post`，锁无人持有 → `instance.py` 会**自己抢到锁并 exit 0**（还顺手 init_db 建骨架），验收必假绿。对调后「先按开始（server 持锁）→ 再跑 instance.py 撞锁 exit 3」语义正确；HANDOFF §三步骤 6 已明写陷阱（「必须在监听已开始后跑…console 进程本身不做启动期单实例」），§二.14 预期同步改写，两处口径一致。失败路径副作用核查：`acquire` 抛错在 `fh.write`（:101）之前，且 server 已先建锁文件，故第二实例**零写入、DB 不变**——与文档「DB 不被触碰」一致。步骤 6 对「页面改过数据目录须同步换 `--data-root`」也已注明。

### 2. P3-1（start.sh 删除＋超范围三处修复）✅（diff 逐 hunk＋实测）

**删除本体**：`git diff --cached --numstat` = `windows/app/start.sh 0/43`，暂存态 D；HANDOFF §五.5 已记「start.sh 已删…Windows 入口唯一为 start.ps1/start.bat」。

**超范围三处＋server.py 一词，diff 逐 hunk 均最小且全在 `windows/**` 内**：

| 文件 | numstat | 内容 |
|---|---|---|
| windows/app/index.html | 2/2 | 仅 :2395-2396 两行用户文案 `./app/start.sh`→`.\start.ps1`（仓根 Mac 版原件未动，合规） |
| windows/app/server.py | 1/1 | 仅 :57 注释「start.sh 透传」→「start.ps1 透传」，零代码变更 |
| windows/tests/selftest_p1_2_contract.py | 17/14 | :962-978 旧 4 条 start.sh 断言 → 新 5 条（①start.sh 已删防回退 ②ps1 else 兜底恰一处 ③默认值==server.py PORT ④默认端口字面量全文恰一份 ⑤URL 走 `$Port` 无硬编码）；基线实跑 **601 断言**（600→601 与申报一致） |
| windows/tests/selftest_p1_2_frontend.py | 3/3 | :1478 改读 `../start.ps1`，:1470/:1481 文案同步，其余零触碰 |

新断言的 regex 前提对 start.ps1 实文核过：`else { "8899" }`（:14）恰一处、`127.0.0.1:$Port`（:15）在位、非注释行无 `127.0.0.1:<数字>` 硬编码——五条断言全部可满足且非恒真（见下证伪）。

**抽验反向证伪 3 条（改坏→测红→`/tmp` 备份还原，全程未用 `git checkout`）**：

| # | 变异 | 结果 |
|---|---|---|
| M1 | 伪造重建 `windows/app/start.sh`（改坏实现方向） | **测红 rc=1**：`FAIL 8 start.sh 已删…`，1/601 → 删伪造件，`git status` 复原为仅暂存 D |
| M2 | start.ps1:14 默认 `8899`→`8898` | **测红 rc=1**：`FAIL 8 start.ps1 默认值＝server.py 默认端口（真源分叉即挂）` |
| M3 | start.ps1 尾追加第二份 `8899` 字面量 | **测红 rc=1**：`FAIL 8 start.ps1 默认端口字面量全文恰好一份（防再写死第二份）` |

还原对账：`shasum -a 256` 与开工前逐字一致（start.ps1 `75437d1c…`、contract `631729eb…`）；还原后 contract 终验 **rc=0，ALL PASS（601）**。

**全仓 grep `start\.sh` 活引用对账**：windows/ 树内仅剩两类豁免——contract:963-965 防回退断言与 HANDOFF:112 交接说明；`windows/README.md`/`README.en.md` 零引用。其余命中全在仓根 Mac 侧原件（`app/start.sh` 本尊仍在，仅删 windows 副本）与 docs/qa/review 历史文档，不属活引用。

### 3. 实测证据汇总

- `cd windows && ../.venv/bin/python -m compileall -q app src tests` → rc=0
- contract 基线/终验 rc=0（601 断言，`/tmp/rw_contract_baseline.log`、`/tmp/rw_contract_final_green.log`）
- frontend rc=0（`/tmp/rw_frontend_baseline.log`）
- selftest_win_stage4 终验 rc=0（65 断言 0 失败、teeth 4/4，`/tmp/rw_stage4_final.log`）
- 隔离：`git status --short -- app src tests`（仓根）输出空；numstat 与申报逐项一致（README 49/44、47/42 为首轮已核项未再触碰，新增 M 四文件 + 暂存 D 一件）
- 证伪产物全部 /tmp 还原并 sha256 对账；未 commit/push；未联网；未 bind 8765/8899

### 返工复核结论

**PASS**——两项返工均真实、最小、无副作用：P2-1 对调必要性读码成立（原顺序必假绿）、陷阱文档已讲清；P3-1 删除已暂存、防回退断言五条中抽验三条全部有牙、超范围连带修复全部落在 windows/** 内且逐 hunk 最小。

遗留清单（不阻塞）：

1. 首轮 **P2-1（reapply 恢复链路真机补验）维持原状**——本轮返工未触及该链路，QA 真机补验责任不变。
2. 首轮 P3-1（ffmpeg 随包清单）、P3-2（requirements 哈希占位）、P3-3（sleep(12) 轮询窗口）维持原状。
3. 新增知晓级（P3，不派单）：contract ④「字面量全文恰一份」对**注释**同样计数（与 P1-8 首轮 P3-6 同风格）——将来在 start.ps1 注释里写 8899 会误挂；届时把计数范围收窄到非注释行即可。
4. 知晓级：HANDOFF §三步骤 6 示例 `--data-root` 用默认 LOCALAPPDATA 路径，步骤 5 若页面用了自定义目录须同步换值（文档已注明，执行者易漏看，真机 QA 照单执行时留意）。

---

## 五、收尾注记（2026-09-16，neat-freak，文档对齐抽查；上文正文一字未动）

- 行号引用抽查 8 处全部命中：`watcher.py:60`、`platform_win.py:55,278-290`、`selftest_win_stage4.py:74-77/206-235/566,654`、`server.py:57/5346/5431/6638`、`index.html:2395-2396`、`instance.py:43/175/178/208-210`、`store.py:28/104`、`startup.py:172`、`start.ps1:14/15`、`contract:962-978`、`frontend:1470/1478/1481`。唯一边界欠准：§3 首轮表格引 `start.ps1:3-13`——端口单点注释确在 :3-13，但「被占不杀进程」实现在 :38-45、默认 `8899` 在 :14，语义仍成立，仅行号范围偏窄。
- numstat 对账（收尾时点实测，`git diff --numstat` / `--cached`）：README.en 49/44、README 47/42、index.html 2/2、server.py 1/1、contract 17/14、frontend 3/3、start.sh（暂存 D）0/43——与本报告首轮/返工两节引用逐项一致。Stage 3 报告（WIN-STAGE3-CODE-REVIEW/QA）的 5/5、5/5、20/15、5/5、68/0、61/48、23/8、15/11、46/19 与 HEAD 提交 d8f89d7 实测逐项一致。
- §4 的 server.py sha `04c04ec0…` 与 WIN-STAGE4-QA F1 的 `db59ecfd…` 不同系时点差异：前者为返工前（server.py :57 注释改动前），后者为 QA 时点（返工后），各自与其备份自洽，非矛盾。
- 全仓 `start.sh` 活引用复核：windows/ 树内仅剩 contract:963-965 防回退断言与 HANDOFF:112 交接说明两类有意豁免，与本报告 §2 判断一致；`windows/README*.md` 零引用。
