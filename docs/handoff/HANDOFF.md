# HANDOFF｜P1 收口＋冻结后接续（2026-09-15 晚）：P0 全清＋P1-1…P1-8 全 PASS；**新增 P1-9 在制品已落盘待走角色链**；Windows 迁移（另仓）暂缓，恢复先读我

> 旧版字段（governance-state / Evidence / Human Gate / Promotion / Dispatch ID）已废弃，不填。
> 本文件即恢复入口。「一/二/三」三节为本轮梳理版（2026-09-15 14:10 重写，旧版细节压缩进各链报告与 git 历史）；**Phase1 全过程记录**（用户四条反馈原文、九项 HD 决策、16 条真相、迁移决策、额度事件）与**迁移前项目交接原文**见本文件后段各节，逐字未动。

- Captured at（YYYY-MM-DD HH:MM）：**2026-09-15 22:10（TM 收口 + 冻结）**；本阶段 P1 全部收口并推送 `main`，随后**冻结版本**（tag `v1.0-mac`，落点 `5f06fdb`）；下一步＝**Windows 11 迁移**（另建独立仓库，方案已由 planner 产出：`docs/pm/WINDOWS-MIGRATION-PLAN.md`，**只出计划不施工**）
- **接续（编排者恢复工作，2026-09-15 晚）**：冻结后发现工作树留有**未提交的 P1-9 在制品**（4 文件），已按「续做半成品默认保留」逐块核对后**先 commit＋push 落盘**，再补走角色链。见 §一.4。Windows 迁移顺延到 P1-9 收口之后。
- **接续 2（编排者恢复工作，2026-09-16）**：实测 git 对账发现 HANDOFF 落后实际进度——WIN Stage 0（`ebabf91`）与 **Stage 1+2（`6e19fb5`，四角色链 PASS 已推 main）** 均已收口；工作树留 **Stage 3 在制品**（asr_backend 适配层＋stage1 改接＋platform_win.run_ffmpeg），已按「续做半成品默认保留」续链收口，见 §一.7。
- **接续 3（2026-09-16，用户叫停暂停）**：**Stage 3 已收口推 main（`d8f89d7`）**；**Stage 4 链已走完 builder→reviewer（含返工复核）→qa，差 supervisor 复检未收口**（见 §一.8，在制品已 commit 落盘）。Windows 远端空仓 **`wanghoufan/Video2Obsidian-Windows`**（PUBLIC）已建（用户拍板），交付时整目录推上去。恢复开发先读 §一.8 与 §二。
- PROJECT_PHASE：**DEVELOP**（Phase2 未关闭）
- PLAN_VERSION：`PRODUCT_PLAN_V1.3`（正文最新；文末「Readiness Score / 本轮真实验证记录」两段仍为 V1.2 旧文本，见 `docs/pm/PRODUCT_PLAN.md` 顶部收尾注记）
- PLAN_READINESS_SCORE：**未达 90**（planner 自评 89；Research Reviewer 独立 83；用户已知并决定开工）
- PLAN_GATE：**APPROVED**（用户明确进 DEVELOP；不是 Readiness 达标通过）
- DEV_BASELINE：`PRODUCT_PLAN_V1.3`（禁随意改 Plan；变更只走 Change C）
- CHANGE_REQUEST：**B**（用户 2026-09-15 授权「继续推进」「按分工表为准」「commit/push 默认 main 不再逐次问」）
- Stage ID（本阶段叫什么）：**DEVELOP-P1-9（接续，在制品已落盘）**：笔记库已有同名笔记→跳过转写（SKIPPED，whisper 零调用）；已 commit＋push `main`，**待补 code-reviewer／qa／supervisor 三角色链**
- 上一阶段（已收口）：**DEVELOP-P1-1 收口（P1 收官）**——监听漏发现修复（P1-1-FIX）＋真实规模/真机验证闭环，四角色链全 PASS，已推 `main` 并冻结（tag `v1.0-mac`）
---

## 一、当前的工作进展

### 1. 已收口链条速览（细节在 git log＋docs/review｜docs/qa 各报告，不在本文件展开）

| 链 | 内容 | commit（均已推 main） | supervisor |
|---|---|---|---|
| P0-1/2/3 | 诊断恢复闭环＋批量恢复＋四层状态/统一入口 | `c753b15` | 全 PASS（P0-3 链 1/2 后收口） |
| P1-2 | 任务身份与 API 契约加固（10 派） | `92fa77e` | PASS 0/2 |
| P1-3 | 进度与批量语义修正（11 派，含 P2-7 retry 带 data_root） | `fa6ba1b` | PASS 0/2 |
| P1-4 | 脱敏摘要复制（HD-2=A，5 派） | `e340396` | PASS 0/2 |
| P1-5 | 词库与候选易用性修整（5 派） | `7bc8cec` | PASS 0/2 |
| P1-7 | 产品改名「懒得笔记」（7 派＋supervisor） | `da0f572` | PASS 0/2（QA-P17-001 判 CLOSED） |
| P1-6 | 布局与历史分页（FR-10/11/12/15＋D-15，4 派） | `4d26865`＋收口 `f611824` | PASS 0/2（qa 的 HTTP 段环境限制由本窗口补位跑通） |
| P1-8 | 默认端口去硬编码 8765→**8899**＋`V2O_PORT` 覆盖（5 派，清历史 P2-4/P2-5） | `7b97aaa` | PASS 0/2（qa 的 HTTP／新牙口环境限制由本窗口补位跑通） |
| P1-1 | 真实规模/真机验证（9 条真实视频＋布局＋分页交互；4 派） | 证据见 §一.3 | qa 首轮 **FAIL**：独立复现 **BUG-P11-1**（监听漏发现，P1）→ 转入 P1-1-FIX |
| P1-1-FIX | 监听可靠性修复（cp 大文件漏发现；6 派含返工） | `ac3ecd8`（含 P1-1 全部文档） | PASS 0/2（reviewer 首轮**打回** → builder 返工 → 返工复核 **PASS**；qa 判 BUG **CLOSED**；supervisor **PASS** 放行） |

- 各链遗留 P3/P2 均记 backlog（见下「待排期」），无未收口 P0/P1。
- P1 顺序（TM 定）：P1-2✅ → P1-3✅ → P1-4✅ → P1-5✅ → P1-7✅ → P1-6✅（18:50） → P1-8✅（19:20） → **P1-1＋P1-1-FIX（2026-09-15 晚，P1 收官批）** → **冻结版本** → Windows 迁移（另仓）。

### 2. P1-6 链状态（**已收口**，证据在此备查）

- **范围**（Plan:223＋FR-10/11/12/15＋D-15＋HD-6/7/9）：①FR-12 完成列表分层＋cursor 历史（`/api/status` 增 `completed_limit` 默认 20／`completed_cursor`，返回 `completed_total/completed_page/next_cursor`，keyset 排序 `finished_at 回退 updated_at, run_id DESC` 决胜）②FR-15 隐藏已完成（localStorage 键 `v2o-hide-done-<sha256(data_root)前8位>`，纯视图过滤零删写可恢复）③FR-10 左列 tape 改顶部普通横条 `.flowbar`（~36px，不 sticky 不收起）④FR-11 两列 `minmax(0,1fr) 300px`、≤960px 单列。TM 记跳步原因：Plan 条目直接定义、HD 规格写死，跳 planner/product。
- **① builder 首版（PASS，opencode-go/deepseek-v4.1-flash）**：server.py 实测 **+178/−1**（仅 `_handle_status` 接 `_attach_completed_view`＋新增 cursor 编解码/视图 4 函数＋import base64/hmac；恢复/词库/诊断/发布/browse/start/stop/`_strip_paths` 全未碰——code-reviewer AST 逐函数 sha256 证实）；index.html **178/25**；两自测 +254/+135（contract 新增 part14 共 27 项 D-15 断言：77 条夹具 61 成功展开无重复遗漏、页大小 20·20·20·1、并列决胜；frontend 新增 S12＋L1-L5 布局与 FR-9 零回退断言）；三套自测 **512/167/58 rc=0**；反向证伪 7 条全有牙。
- **② code-reviewer 首轮（PASS，无 P0/P1；P3×5）**（报告 `docs/review/P1-6-LAYOUT-HISTORY-CODE-REVIEW.md`）：自建 8-run 夹具（含 5 个并列时间戳）独立复算 keyset 翻页 17/17 无重复遗漏、`summary.done==completed_total`；变异 M1-M4 全有牙（签名短路/keyset `<=`/隐藏键原始路径/flowbar sticky）；**P3-1** cursor 校验和为无密钥 sha256 可伪造（防意外篡改够用，防伪造需 HMAC＋nonce，影响面仅分页窗口）；**P3-2** numstat 口径差（builder 毛计数 vs 实测，已由 neat-freak 加注裁定以实测为准）；**P3-3** 全完成＋隐藏时空表头观感；**P3-4** 展开后 poll 重置 cursor 多一次重复拉取（去重兜底）；**P3-5** 排序键字符串比较依赖时间戳格式同源（既有口径）。
- **③ qa 独立 QA（PASS，codex/gpt-5.6-luna，耗时 5m50s，2026-09-15 18:36）**：业务 BUG **0**；FR-10/11/12/15＋HD-6/7/9＋D-15 逐条过；自建非 77 条夹具复算 keyset 页 3/3/3/1 无重复遗漏、并列 `run_id DESC` 决胜、回退行、坏游标 5 类 400、跨目录重放如实记；自算签名**实证** reviewer 的 P3-1（无密钥 sha256 可伪造）；报告 `docs/qa/P1-6-LAYOUT-HISTORY-QA-2026-09-15.md`。未覆盖：contract HTTP 段（codex 沙箱禁 loopback bind）＋真机 UI。
- **④ supervisor 复检（PASS，放行推送；本链 0/2 无 blocking）**：三套自测 **512/167/58 rc0**；**HTTP 段补位**——本窗口可 bind，`contract:949` 真 loopback 27 条全 PASS＋独立探针 27 项 rc0 → qa 该项判为环境限制**已补位**；反向证伪 **4/4 有牙**、5 文件 sha256 还原逐字一致（**未用 `git checkout`**）；两账本 exit0＋负控 **11/11 有牙**；三处对账本链 0 不匹配（另见 2 行表换代前历史行差异，不阻塞）；8765 外部进程零扰动。复检节在 qa 报告 `:75`。
- **已知遗留（非阻塞）**：qa 未覆盖「真机 UI 目检」→ 由本窗口 computer-use 补或按用户手测；reviewer/qa 共记 P3×5（P3-1 若要升 HMAC 需用户点头）。
- **账本**：`DISPATCH-LOG` **86 行**（P1-6 四派＋P1-8 五派全落）；`TASK-MODEL-LOG` **55 行**（P1-6／P1-8 任务行均已落）；两道校验 exit 0 且负控有牙（P1-8 复检负控 15/15）。
- **工作树／推送（TM 2026-09-15 19:20 收口）**：P1-6（`4d26865`＋`f611824`）已 push；P1-8 收口提交（端口 8 文件＋`docs/review/P1-8-PORT-CODE-REVIEW.md`＋`docs/qa/P1-8-PORT-QA-2026-09-15.md`＋两账本＋本 HANDOFF）已 push `main`；工作树仅 `?? .codebuddy/`（会话产物，保留不提交）。
- **P1-8 收口要点**：默认端口 **8899**（`app/server.py:54-68`，非法值 rc2 人话）＋环境变量 **`V2O_PORT`** 覆盖；`app/start.sh:8` 端口**单点**（`PORT="${V2O_PORT:-8899}"`）并透传，URL 全用 `$PORT`（清掉历史 P2-4/P2-5「改一处忘一处」）；文档中英＋测试同步；`contract:952-975` 新增钉值断言＋4 条 start.sh 牙口（**双向**：改 server.py 或 start.sh 任一侧都红）。8 文件 **+69/−22**。
- **暂停收尾（neat-freak 2026-09-15 本轮，PASS）**：P1-7 review 加行号勘误注记（:370→:364）、P1-6 review 加 numstat 口径注记；删仓根 `.DS_Store`；`__pycache__` 全仓零命中；业务文件零触碰。
- **环境事件（如实记）**：auto mode 安全分类器 11:50–13:00 限流（glm 端点 429，16:55 重置）拦 Bash 命令，用户拍板在 settings 配 `subagents.agents.autoModeClassifier.model=hy3`（x0.00 credits 档）恢复；hy3 对长复合命令仍偶发判不动 → 对策＝简单命令/任务书落文件让 codex 自读/文件编辑通道写账本，连续失败挂定时重试。
- **环境事件 2（2026-09-15 18:35–18:45，TM）**：hy3 恶化——对 `codex exec` 派工命令**持续**返回 `Max turns (1) exceeded`（同轮连拦 5 次：长提示词／短提示词／`bash 脚本`／`cp` 全部被拦，仅 `codex --version` 等极短命令过），P1-6 派 qa 因此卡住。**用户拍板换分类器**，实走两步：① `hy3` → `glm-5.3-flash`（热加载生效但**同样** `Max turns (1) exceeded`，证明瓶颈是**分类器单轮预算**而非模型档位）② `glm-5.3-flash` → **`deepseek-v4.1-flash`**（**通过**，派工命令放行）。现网值＝`deepseek-v4.1-flash`；改动前原件备份 `/tmp/settings.json.bak-20260915`。**配置热加载生效（无需重启会话）**；不改默认权限模式（仍 `auto`）。
- **环境事件 3（端口，TM 18:30 实测 → 19:20 已解）**：**8765 被另一个无关项目占用**（`/Users/zzymima0000/Downloads/视频笔记 ob/葫芦军师/红利打新底仓计算器_V1.2正式长期版/server.py`，PID 6586，11:41 启动）。**已由 P1-8 解决**：本项目默认端口改 **8899**，可用 `V2O_PORT` 覆盖；**仍禁 bind/kill 8765 与 PID 6586**。真机目检走 8899（注意 localStorage 按 origin 隔离，旧 8765  origin 的隐藏/主题偏好不会自动带过来）。

### 3. P1-1 ＋ P1-1-FIX 链状态（**已收口**，P1 收官批）

- **P1-1（真实规模／真机验证，TM 本窗口执行）已完成**：真实 whisper 通路打通（9 条真实视频全成功：8 条 47–223MB 单条 86–214 秒＋**462MB 长视频 122 秒**；vault 出真实中文正文）；真机布局核验（`.flowbar` 高 39px／`position:static`、1280px 两列 `913px 300px`、960/900/720px 单列、无横向滚动、浅色）；完成列表交互真机（默认 20 条＋失败 16 可见 → 展开 20/40/60/61 无重复无遗漏 → 隐藏可逆）。**真机手段：本窗口 venv＋真实视频＋headless Chrome/CDP**（codex 沙箱禁 bind，真机类历来由本窗口补）。
- **过程中发现并修复 BUG-P11-1（监听漏发现，P1）**：`cp` 大文件偶发不被发现（`watcher.py` 首事件即投递＋0.25s 丢事件；`periodic_reconcile` 是死代码）。qa 独立复现（投递时 size=1024／最终 8192／无重投）→ **P1-1-FIX**：稳定判定改「静默 3.0s＋采样 2.0s×3 轮（最小年龄 7s）＋独占占用检测」＋**接入周期 reconcile（45s 兜底）**＋**半截不发布两道门**（源 size/mtime 与入库快照不一致则不转写／不建 job／不写 vault）；6 文件（含 `app/server.py` +71，reviewer 判必要不误伤）＋新增 36 断言。
- **四角色链**：builder 首版 → **code-reviewer 打回**（慢写误判致半截投递）→ builder 返工 → **reviewer 返工复核 PASS** → qa 返工复验（**BUG-P11-1 CLOSED**；任务级 FAIL 仅因沙箱禁 bind）→ **supervisor PASS**（三套 **571/167/58 rc=0**；BUG CLOSED 复核成立；边界实测 **6.5s 安全／7.0s 起投半截**；正常发布端到端不误伤）。
- **TM 补位证据**：`contract 571 rc=0`（本窗口）；e2e（实例 8892＋新 tmp）系统 `cp` **未 touch** → **5 秒自动发现 → 90 秒完成 → vault 出正常笔记（19,453B，`-r--r--r--`）**。
- **已知限制（supervisor 要求必须落字，已写入 README 中英「已知限制」节）**：拷贝/下载中若**长时间停顿（>约 7 秒）**再续写，可能先按当时内容出一份**不完整稿**，完整稿因 No-Clobber 被挡（实测停 40s：半截稿 402B 落 vault、完整稿 `PUBLISH_BLOCKED`）→ 处理办法：删除该 md 后重新放入视频。**这是 P1-1-FIX 的已知残留（原 P2-a／P2-b 合并）**，非静默。
- **backlog（挂账，非阻塞）**：① 上述「长停顿→半截稿」根治需结构级 Change B（发布前最终校验＋失败标记语义）；② **P2-c 幽灵行**（被门拦下的 run 在 `run_summary` 显示成「排队中」，撞用户历史投诉，supervisor 建议修）；③ **P3-e**（`shutdown` 谎报 `reconciler_stopped`）；④ **P3**（`contract` 的 M4 最小年龄门差分不可观测）；⑤ 项目**至今无依赖声明文件**（缺 `watchdog` 这类会在新机器踩坑）；⑥ 「源文件不在原位」专门文案未用真实形状夹具复验（P1-1 未覆盖项）。

### 4. P1-9 在制品（接续落盘，未走角色链）

- **来源与性质**：接续时发现工作树 4 个 `M` 未提交（冻结后遗留的在制品），逐块 diff 核对后确认是一套**完整且自洽**的功能（不是做歪的块），按「续做半成品默认保留」先落盘，不回滚。无 `docs/pm/` 计划文档（本链未走 planner，范围＝下面「做了什么」逐条）。
- **做了什么**（`DEVELOP-P1-9` 标记全文可搜）：① `server.py` 送引擎前加**第 0 道门**——目标笔记已存在则判 `SKIPPED`，**whisper 一次都不跑**、不建 raw/asr、不写 vault（既有笔记一字节不动，No-Clobber 不变），只留一条轻量 receipt（`_vault_note_already_there`，命名真源复用入库同一函数 `_app_resolve_canonical`，不另造规则）② 磁盘 `_scan_disk_states` 认 `SKIPPED`，**重启后仍显示「已跳过」，不回落「排队中」**③ 诊断新增 `SKIPPED` 类别与 `PUBLISH_TARGET_EXISTS` 根因（按状态码判定，不看文案），**不再落 UNKNOWN**，文案不再误导「检查笔记库权限」④ 队列/列表单列 `skipped` 计数（不计入 total，不算成功也不算失败）⑤ 前端 `SKIPPED` 态文案、队列显示、toast、详情区「库里已有 / 重试」入口。
- **改动量（实测 `git diff --numstat`）**：`app/server.py` **+171/−6**、`app/index.html` **+31/−9**、`tests/selftest_p1_2_contract.py` **+177/−0**、`tests/selftest_p1_2_frontend.py` **+50/−1**。
- **自测（本窗口 `.venv` 实测）**：`contract` **600 断言全 PASS**（P1-9 新增 16A/16B/16C/16D 共 29 条，含「引擎真零调用」与「摘掉检查→引擎真被调」双向有牙）／`frontend` **全 PASS**（新增 S13：已跳过不画失败红、不进批量重试、监听脱钩）／`v26_presets` **58 全 PASS**。
- **flaky 一条（挂账，不阻塞）**：首轮 `contract` 曾 **FAIL 1／600**＝`15k 收不掉时 is_running() 仍为 True（不谎报已停)`，**重跑 600/600 全过** → 判时序敏感 flaky（P1-9 未触碰 `watcher.py`，非本次引入）。与历史挂账 **P3-e**（`shutdown` 谎报 `reconciler_stopped`）同源，待一起根治：要么让 `stop()` 收不掉时如实留 `is_running()=True`，要么给该断言加稳定窗。**不自欺**：这条不是「重跑绿了就算过」，已单独挂账。
- **三角色链（已补走完，全 PASS）**：
  - **code-reviewer**（`opencode/muse-spark-1.3-contributor-free`，本窗口）：PASS，**0 P0 / 0 P1**；P2×1、P3×6；变异 **6/6 有牙**；独立复算自建夹具 **51/51**；命名真源与入库 `canonical_path_for` **9 种命名全等**（确认无第二套命名）；AST 证新增 4 函数、改动 7 函数全在范围，`_err_text`/`_strip_paths`/`_stale_source_reason`/两道发布门/状态桶逐字未动，`watcher.py`/`reconcile.py` 未入提交。报告 `docs/review/P1-9-SKIP-EXISTING-NOTE-CODE-REVIEW.md`。
  - **qa**（`codex/gpt-5.6-luna`，codex 直调，tokens 82,819）：PASS，**新增业务 BUG 0**；独立夹具过主路径与边界（子目录同名／未配 vault／大写扩展名／同名是目录／转写途中撞名走**真** `initial_publish` 状态码）；三套中 `contract` **rc=1**（沙箱禁 loopback bind，环境限制）→ **本窗口补位 600/600 rc=0**、`frontend` rc0 全 PASS、`presets` 58 rc0 全 PASS；未覆盖真实 whisper／真机 UI／HTTP 真服务。报告 `docs/qa/P1-9-SKIP-QA-2026-09-15.md`。
  - **supervisor**（`opencode-go/muse-spark-1.3-contributor`，本窗口）：**放行推 main，本链打回 0/2**；三套 600/173/58 rc0；自建夹具 15/15 跑两遍一致（含「删笔记后同一 run 重试真 PUBLISHED」＝门 0 不永久锁死）；证伪 5/5 有牙；账本正控 EXIT=0（TASK 58／DISPATCH 95 坏行 0）＋负控 **4/4 有牙**（用 /tmp 备份还原，**未用 `git checkout`**）；P2-1 与 P3×6 定级**全部同意、不升不降、不阻塞**。复检节 `docs/qa/P1-9-SKIP-QA-2026-09-15.md:64`。
- **挂账（本链新增，非阻塞）**：**P2-1 口径相反**——同一条 run，队列计 `failed`（`PUBLISH_BLOCKED`）、诊断面板显示 `SKIPPED`「不算失败」；supervisor 建议按**方案②**修（摘 `PUBLISH_TARGET_EXISTS` 出 `_NOTE_EXISTS_ROOTS`，一行），**是否修由用户定**。P3×6（reviewer/qa 各记一份，逐条对齐）：脱敏口径同 PUBLISHED 先例／「已跳过」与成功同色／门 0 早于源稳定门与半截限制叠加／`PENDING_PUBLISH` 等仍用旧「检查权限」文案／同名目标是目录／历史 flaky 与无依赖声明。
- **账本 schema 校验 EXIT=1 的 1 条**：`TASK-MODEL-LOG` 第 40 行 `role="迁移整理工"` 不属 9+1（迁移遗留），挂账不改历史行。
- **状态**：**P1-9 已收口**（commit＋push `main`），无未收口 P0/P1。

### 5. 收尾注记（neat-freak，2026-09-16；只加注，本节结论正文不动）

**一致项（6 条，实测基准＝当前工作树 HEAD `e38151a`）**

1. **改动量 numstat**：`app/server.py +171/−6`、`app/index.html +31/−9`、`tests/selftest_p1_2_contract.py +177/−0`、`tests/selftest_p1_2_frontend.py +50/−1` —— 与 `git show --numstat 3d501a5` 实测**逐项一致**（`docs/review/P1-9-SKIP-EXISTING-NOTE-CODE-REVIEW.md:19-25` 亦一致）。
2. **commit hash 语义**：`3d501a5`＝在制品落盘（5 文件：4 业务/测试 ＋ HANDOFF）、`e38151a`＝收口（5 文件：两份新报告 ＋ HANDOFF ＋ 两账本）、`378eac2`＝基线 —— 与 `git log`／`git show --name-status` 实测一致。
3. **三套自测（本窗口 `.venv` 本人跑）**：`contract 600/600 rc=0`、`frontend 173 条 PASS rc=0`、`presets 58 rc=0` —— 与 supervisor 节 `docs/qa/P1-9-SKIP-QA-2026-09-15.md:79-81` 一致；qa 报告 `:46` 的 contract `rc=1` 确为 codex 沙箱禁 loopback bind 的环境限制，补位结论成立。
4. **账本行数与卫生**：`TASK-MODEL-LOG` **58 行**一致；`TASK-MODEL-LOG.jsonl:40` 实测 `role="迁移整理工"`（确在第 40 行、确不属 9+1），与本节上方「账本 schema 校验 EXIT=1 的 1 条」一致。
5. **抽查引用行号 8 处全部命中**：`app/server.py:4837-4871`（门 0）、`:4917`（引擎调用在门后）、`:950`（`_scan_disk_states`）、`:1040`（`_app_resolve_canonical`）、`:122`（`FAIL_STATES`）、`:1726-1727`（诊断展示 SKIPPED）、`app/index.html:896-898`（「库里已有」文案）、`:464-468`（`stClass`）。（另抽查 `src/stage4/publish_commit.py:54-56`/`:118`、`src/stage4/conflict.py:129-130`、`src/stage3/render.py:58` 亦全命中。）
6. **复检节指向**：`docs/qa/P1-9-SKIP-QA-2026-09-15.md:64`——`:64` 为该节起始分隔线、标题在 `:66`，指向同一节，无歧义。

**差异项（1 条）**

1. **DISPATCH 行数差 1**：本节上方 supervisor 条目与 `docs/qa/P1-9-SKIP-QA-2026-09-15.md:134` 均记 **95 行**，实测 `docs/model/DISPATCH-LOG.jsonl` 为 **96 行**（基线 `378eac2`＝93，`e38151a` 新增 reviewer／qa／supervisor 三行 ＝ +3）。成因＝记账时刻口径（校验发生在最后一行落盘前），**非坏行**，正控「坏行 0」结论不受影响；不改正文，只在此登记。

**另：历史未决项已办（上一轮 neat 只记未办）**

- `docs/pm/STAGE5-PLAN.md` 与 `docs/qa/STAGE5-QA-REPORT.md` **已各在文件尾部追加「收尾注记」一节**，说明 A5「抖动文件仍 WAITING」在 P1-1-FIX（`ac3ecd8`）后已变为「首投 `PROMOTED`」，并附 `docs/review/P1-1-FIX-WATCHER-CODE-REVIEW.md:74/:139` 探针 4 证据；核对结论＝**与当前代码一致**（`src/stage5/watcher.py:74-86`、`:284-352`）。两文件正文与结论一字未动。

### 6. Windows 施工变更（用户 2026-09-15 晚拍板，**CHANGE_REQUEST=B**）

- **变更内容**：Windows 版**不再另建独立仓库**，改为在本仓建子目录 **`windows/`**，在里面完成**大部分开发**，最后整个目录拷过去即成新仓库（`Video2Obsidian-Windows`）的根内容。原 `docs/pm/WINDOWS-MIGRATION-PLAN.md`（planner 出的五阶段方案）**继续有效**，只是「建仓」这一步从「新建远端仓库」改成「本仓建子目录」。
- **分类理由（TM 判 B，不召 Sol Planner）**：功能范围、五阶段、验收标准**一字未变**，只改**施工落点与交付方式**；计划文档已由 planner 出过并经用户看过。不属 Change C（无产品/架构新增）。
- **隔离铁律（违反即打回）**：
  1. 所有 Windows 施工**只改 `windows/**`**；仓根 `app/`、`src/`、`tests/`（＝Mac 端）**零改动**（已冻结 tag `v1.0-mac`＋P1-9 收口，不得被带歪）。
  2. `windows/` 内是**完整可独立运行副本**（app/src/tests/README…），拷过去即可跑，不依赖父目录任何文件。
  3. Windows 侧自测放 `windows/tests/`；Mac 端三套自测（`tests/selftest_*.py`）**不得被引用或修改**。
  4. 复制源＝当前 `main` 的 HEAD（含 P1-9，属平台无关功能，Windows 版同样要带），来源 hash 记档；排除 `.venv`／`.git`／`data/`／`.codebuddy/`／`__pycache__`。
  5. 隐私扫描：复制与交付前各扫一次（无密钥／无真实绝对路径／无用户隐私）。
- **本仓能完成的（约 70–80%，TM 估）**：ASR 后端抽象与 faster-whisper/CT2 适配器接线、`fcntl`→`msvcrt` 单实例锁、卷/原子性探针替换、长路径与路径比较、Explorer reveal、进程/信号去 POSIX 化、`start.ps1`＋`start.bat`、UTF-8、依赖声明（锁版本＋哈希）、ffmpeg 固定与 manifest、离线门禁、文档与 README；静态验证（compileall、AST、纯 Python 层单测、Windows 风格路径桩测）。
- **必须留到 Windows 真机（本仓验不了，交付时列清单）**：CUDA/CT2 与真实转写质量/性能、NTFS 原子/硬链接/只读语义实测、Defender/UAC、Explorer 与 `obsidian://` 真机、PowerShell 实跑、长路径策略注册表检查、干净机复装。**这些不许推断为通过**，一律在交付提示词里列明待验。
- **交付**：完工后出一份给 Windows 端智能体的接续提示词（待适配项清单＋验收命令＋红线），落 `windows/docs/` 与本 HANDOFF。

### 7. WIN Stage 3 链状态（**已收口**，2026-09-16）

- **范围**（`docs/pm/WINDOWS-MIGRATION-PLAN.md` §4 Stage 3＋§1）：单一 `asr_backend`（faster-whisper/CT2）适配层＋四处绑定点接线（stage1/7/8/prompt_builder）＋manifest 占位与 freeze 工具＋ffmpeg 单点。**在制品接续**：接续时工作树已有一批未提交 Stage 3 在制品（asr_backend/manifest/freeze 工具/stage1 改接/platform_win.run_ffmpeg，compileall rc0），逐块核对＝方向正确，保留续链。
- **builder 首版（PASS，opencode-go/deepseek-v4.1-flash）**：stage7/transcribe.py、stage8/transcribe_chunks.py、stage7/prompt_builder.py 三处 mlx 绑定改接 `asr_backend.transcribe_file`/`load_tokenizer`（契约保留：zh／word OFF／nst 0.6／decode 透传／segments 单调守卫／prompt ≤200 口径 " "+去空格）；`run_chunks` 整 run `load_model` 一次逐块复用；`server.py` 预检 `_asr_engine_available`＋`PRECHECK_ASR_BACKEND_MISSING`；`start.sh` venv 预检换包；README×2 表述替换；新增 `tests/selftest_win_stage3.py`（全桩注入，61 断言 0 失败，9/9 证伪有牙）；六套自测 rc0（contract 600）。mlx 残留清扫（供应链防线如 BANNED markers/MLX_REVISION 黑名单有意保留）。
- **code-reviewer 首轮（PASS 0P0/0P1，opencode/muse-spark-1.3-contributor-free）**：十项核查全过；**P1-1**（stage8 `_cut_chunk_wav` 直调 subprocess 未走 ffmpeg 单点，属既有代码非回归）、P2×2（每 chunk 全模型 SHA-256 重算；config=None 每 chunk 重跑 GPU probe）、P3×3。报告 `docs/review/WIN-STAGE3-CODE-REVIEW.md`。
- **builder 返工（PASS）**：三项全落（`transcribe_chunks.py:133` run_ffmpeg timeout=600、`:383` run 级 manifest 校验一次、`:382/:393` run 级 resolve_runtime＋config 透传）；自测增 [11c]/[11d]（61→70 断言，证伪 9→11）；stage8 numstat 26/12→46/19。**返工复核（PASS）**：三项逐条证实、签名变化全仓 grep 零遗漏、净增量对账差 0、自跑 2 条证伪有牙（/tmp 备份还原，未用 git checkout）。
- **qa（codex/gpt-5.6-luna，任务级 FAIL＝环境限制）**：**业务 BUG 0**（QA-001..003 P3 挂账）；独立夹具 **16/16**；证伪 3/3 有牙；contract rc=1（codex 沙箱禁 loopback bind）→ **TM 补位 600/600 rc0**。报告 `docs/qa/WIN-STAGE3-QA-2026-09-16.md`。
- **supervisor 复检（PASS，放行，0/2）**：四套自测独立复跑 rc0（contract 600 双重证实）；自建夹具 35 断言两遍一致；证伪 2/2 有牙（shasum 一致还原）；三项关闭读码证实；账本逐行 json 坏行 0；红线扫描零命中。复检节在 qa 报告尾部。
- **挂账（非阻塞）**：P3×3（monotonic 守卫保险带恒真／OOM 后模型无显式释放与 work_dir 不清理／requirements 哈希占位待随模型冻结回填）＋README 面向 Windows 的表述待 Stage 4 统一改写＋**Stage 4 真机清单**（真实 CUDA/CT2 加载链、Defender/UAC、Explorer/Obsidian、PowerShell、干净机复装——不许推断为通过）。

### 8. WIN Stage 4 链状态（**差 supervisor 复检，未收口**；在制品已落盘）

- **交付物**：① `windows/tests/selftest_win_stage4.py`（807 行：桩引擎＋全链路真跑——真 watchdog/discover/ffmpeg/分块/Norm/Render/发布；65 断言 0 失败、4/4 反向证伪有牙；端到端/No-Clobber/崩溃恢复幂等 Source=1 Run=1 PUBLISHED=1/reconcile Lost=Duplicate=0/半截源门）② README 中英 Windows 化改写 ③ `windows/docs/WINDOWS-HANDOFF.md`（交付文档：**15 条真机待验清单零推断**＋验收命令＋红线）④ `git rm windows/app/start.sh`（Mac 前史遗留）。
- **builder（PASS，opencode-go/deepseek-v4.1-flash）** → **code-reviewer 首轮（PASS 0P0/0P1）**：P2×1（reapply 恢复链路建议 QA 补测）＋P3×3（ffmpeg 随包承诺未落地／requirements 哈希占位／sleep(12) 轮询窗口）＋第二实例验收命令问题。报告 `docs/review/WIN-STAGE4-CODE-REVIEW.md`。
- **builder 返工（PASS）**：验收顺序**对调**（关键语义：单实例锁在 POST /api/start 才获取，`server.py:5365`→`startup.py:172`；先跑第二实例 CLI 会假绿 exit 0）＋start.sh 删除＋自决连带修三处（contract 4 条 start.sh 断言改 5 条防回退 600→601、frontend 改读 start.ps1、`index.html:2395` 文案）。**返工复核（PASS）**：逐 hunk 最小、证伪 3 条有牙、contract 601 rc0。
- **qa（codex/gpt-5.6-luna，tokens 163,977；任务级 FAIL＝环境阻塞，业务 BUG 0）**：**reapply 恢复链路独立端到端 PASS**（PUBLISH_BLOCKED→`/api/reapply` 出稿 Raw 不变 whisper 0 次；重试被已完成跳过挡回——**行为差异实证，恢复发布走 reapply**）；主路径/No-Clobber/崩溃恢复/第二实例 exit3 与 0 两态全过；证伪 3/3 有牙（SHA-256 一致还原，TM 复核变异全清）；contract 沙箱禁 bind → **TM 补位 601/601 rc0**。报告 `docs/qa/WIN-STAGE4-QA-2026-09-16.md`。
- **状态**：**supervisor 复检未做**——恢复后第一件事就是补 supervisor → 放行后收口推 main → `windows/` 整目录（排除 `.venv`/`.git`/`data/`/`.codebuddy/`/`__pycache__`，隐私扫描）推 `Video2Obsidian-Windows`。
- **挂账（非阻塞）**：reviewer P3×3（ffmpeg 随包二进制打包阶段补齐／requirements 哈希随模型冻结回填／sleep(12) 测试基建）＋知晓级 2 条（contract「字面量恰一份」断言连注释计数／HANDOFF 示例 data-root 默认路径易漏换）＋qa 提醒 PUBLISH_BLOCKED 恢复走 reapply 而非重试（README/文案可再顺一句）。

## 二、下一步的任务

- **下一步（Next Single Action，按序）**：
  0. **（已完成）P1 全链收口＋冻结 `v1.0-mac`**；P1-9 补链收口（`e38151a`）。
  1. **（已完成）WIN Stage 0/1/2**（`ebabf91`/`6e19fb5`）；**Stage 3**（`d8f89d7`）收口推 main。
  2. **（当前项·差最后一步）WIN Stage 4**：builder→reviewer（含返工复核）→qa 已全过（业务 0 缺陷，contract TM 补位 601 rc0），**只差 supervisor 复检**（见 §一.8）→ 放行后收口 commit＋push `main` → `windows/` 整目录（排除 `.venv`/`.git`/`data/`/`.codebuddy/`/`__pycache__`，隐私扫描先扫一次）推 **`wanghoufan/Video2Obsidian-Windows`**（空仓已建，用户拍板）→ 首个远端 commit 建议打 tag `win-v1-rc`。
  3. **Windows 11 真机 15 项**（`windows/docs/WINDOWS-HANDOFF.md` §二）：真实 CUDA/CT2、PowerShell、NTFS 语义、断网、干净机复装等——须真机做完才能宣布 Windows 版可用，零推断。
  4. 收尾：experience-recorder 一次（neat-freak 已于 2026-09-16 本轮完成）。
- **人要拍什么板（只问大事）**：
  1. **词库三铁律机械保证**（仍挂，不阻塞任何 P1）：「长 wrong 排前」「正词含 wrong 即删条」代码无机械保证。选项：① 只补口径文档（TM 建议）；② 补代码保证（须同改 `_user_rules_revision` 规范化，否则同内容异序被打进死路——见 P1-5 复检节技术约束）；③ 补断言钉现状。
  2. 是否换主用模型：以分工表为准，用户给精确 ID 才改表。
- **待排期 backlog（非阻塞）**：P1-6 链 P3×5（见上②，P3-1 若要升级为 HMAC 需用户点头）；P1-5 链 `P1-5-P3-5-001/002`、`VOCAB-FOLD P3-1`、`loadVocab` 失败静默等；P1-4 链 13×P3（含真 16 条诊断墙钟未实测→并入 P1-1）；P1-3 链 `QA-P13-P2-4-REAPPLY-MUTEX`（修法：内部调用改 `internal=True` 再上锁）；P1-2 链 15×P3＋P0-3 遗留 2×P2；各链其余 P3 按 V1.3 处置表分流。**真实 whisper／真机 UI／批量发布未实测**（各报告「未覆盖项」如实标注，集中到 P1-1）。

## 三、注意事项及相关规矩（本项目专用）

- **读盘顺序（全体系唯一，别乱）**：AGENTS → `docs/roles/` → 根 `USER_MODEL_OVERRIDE.md`（冲突以表为准）→ 本 HANDOFF → 根 `经验一句话.md` → 任务目标放**最后**。
- **两阶段治理**：`PLAN / WAITING_HUMAN_APPROVAL / DEVELOP / PLAN_REOPEN_REQUIRED`；只有用户明确说「第二阶段，开发」才进 Phase2。
- **派工显式**：每派先贴「正在调用 XX｜主用精确ID＋Runtime」，收工贴「XX 回来了 PASS/FAIL＋实走主/备」；HANDOFF 与账本记同一行。
- **固定通道（真源＝根 `USER_MODEL_OVERRIDE.md`）**：builder=`opencode-go/deepseek-v4.1-flash`（本窗口）；supervisor=`opencode-go/muse-spark-1.3-contributor`（本窗口）；code-reviewer／experience-recorder／neat-freak=`opencode/muse-spark-1.3-contributor-free`（本窗口）；planner／senior-expert=`codex/gpt-5.6-sol`（codex 直调）；qa／product-reviewer=`codex/gpt-5.6-luna`（codex 直调）。主用不可用即停派找人，禁自切备用/降级。
- **分类器（本窗口环境，2026-09-16 现网值＝`deepseek-v4.1-flash`）**：auto mode 分类器配置在 settings `subagents.agents.autoModeClassifier.model`；演进史 hy3→glm-5.3-flash（同卡：单轮预算瓶颈）→**deepseek-v4.1-flash（可用）**；长复合命令仍可能被拦 → 任务书落文件传路径（Write/Edit 不走分类器）、连续失败挂定时重试，**不为此打扰用户、不切默认权限**（用户 2026-09-15 明确）。
- **PUBLISH_BLOCKED 恢复口径（Stage 4 qa 实证）**：恢复被拦发布走**重跑（reapply，whisper 0 次）**；「重试」会被已完成跳过口径挡回（202 排队但不恢复）——用户指引与文档按此口径。
- **推进纪律**：小问题不问直接推；P0/P1 尽量解、解不了挂账记报告。**额度纪律**：外部模型先小步试、及时收、烧了多少如实报；用户说停立即停。
- **升级**：同一 Task 被 supervisor 累计打回 2 次自动升 senior-expert（QA 挂不算），只升当次；换模型/换 Runtime 即开新链。
- **账本**：`TASK-MODEL-LOG.jsonl` 一行一任务；`DISPATCH-LOG.jsonl` 逐派一行（`used` 恒填主）。builder 写初版 → supervisor 校验 → TM 判结果落盘。
- **红线**：commit/push 默认走 `main` 不再逐次问（用户授权）；**不碰 secrets**；不改 V1.10/V2.0 封存；`docs/sop/` 仅模板示例；`.codebuddy/` 会话产物不提交不删除。
- **数据安全**：测试只用**外置 tmp＋合成数据**；真实视频目录与 Obsidian 库**禁写**；凡调 handler 的测试首行断言 `data_root` 在 tmp 下；只读真实库先拷 tmp、用完即删。
- **No-Clobber**：已发布笔记永不覆盖；缺失 vault 不重建；user-edited＝一切字节差异。
- **服务／环境（2026-09-15 P1-1 期间重建，**历史 HANDOFF 写的 `stage0bench venv` 已不存在**）**：本机现有 venv ＝仓库根 **`.venv`**（`uv venv --python 3.12` 建的 CPython 3.12.13，已被 `.gitignore` 忽略，且 `app/start.sh` 的查找顺序里就有它）＋ 依赖 **`mlx-whisper`＋`watchdog`**（后者 `src/stage5/watcher.py:33` 需要；**项目至今无依赖声明文件，属欠账**）。起服务：`.venv/bin/python app/server.py`（默认端口 **8899**，`V2O_PORT=<端口>` 可覆盖），或用 `app/start.sh` 一键起（自动透传＋开浏览器）。whisper 模型本地缓存：`~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo`（1.5G）。**改 `app/` 或 `src/` 后必须重启再验**；8765 属另一无关项目（PID 6586），**禁 bind、禁 kill**。
- **主题**：默认必须**浅色**（`data-theme="light"`）；自动化**禁点真机主题开关**，用「抽源码＋node 桩」验。
- **词库三铁律**：wrong ≥ 2 字；正确文本含 wrong 即删条；长 wrong 排前；上限 500（①③有机械保证，②③「删条/排前」暂无——见待拍板项）。
- **产品名**：**「懒得笔记」** 已全量落地（P1-7）；GitHub 仓库名与内部 `v2o-*` 标识不动。
- **收尾**：经验／neat-freak 每阶段只派一次。
- permission_request：无

## 恢复读盘（全体系唯一顺序，别乱）

1. AGENTS；2. 角色卡；3. 根 `USER_MODEL_OVERRIDE.md`；4. 本 HANDOFF；5. 根 `经验一句话.md`；6. 任务目标放最后。
冲突才扩大读。

---

## 附：迁移前项目交接原文（截至 2026-09-13 17:30；迁移时换新模板版，原件已从 git `d807483` 恢复并折叠于此，不另存文件）

> 下列内容为原文逐字，未改动。项目历史（八条迭代链、污染事故追记、服务/词库现状、注意事项）全在此节。

# HANDOFF｜开发暂停（2026-09-13 17:30）：七链全 PASS 全收口，已提交推送 main（91beaf8），恢复开发先读我

> V1 字段（governance-state / Evidence / Human Gate / Promotion / Dispatch ID）已废弃，不填。
> 本文件即恢复开发的唯一入口；下面「一、当前进展／二、下一步／三、注意事项」三节按恢复用结构编排，字段名仍按 HANDOFF 模板（AGENTS 要求），一一对应。

- Captured at（YYYY-MM-DD HH:MM）：2026-09-13 17:30（本机钟点；本文件旧条目标注钟点偏高，以实际为准）
- Stage ID（本阶段叫什么）：**暂停／收口完成**。今日八项迭代全收口：分段 para-v2.7＋批量重排＋词库 v2＋错词重跑＋三件套 UI＋词库折叠＋轮询修复＋错词重跑进度显示（异步 202＋轮询进度条＋主题浅色迁移）。

---

## 一、当前的工作进展

- **剩 P0（没完的才列，多一条都不行）**：
  - 无。无阻塞事项，无未开工开发项。
- **当前 Task（正干到哪）（累计打回 n/2）**：supervisor 累计 **0/2** 从未打回，senior-expert 从未启用。最后一条链「错词重跑进度显示」已四角色全链收口：builder 交付并两轮返工 → code-reviewer「返工复核二」**PASS** → qa 独立复验 **PASS** → supervisor 复检 **PASS**（账本 schema exit=0）。
- **今日八链状态**：reapply 收口／错词重跑／三件套 UI／词库折叠／轮询修复／para-v2.7 直做轮（本窗口直做）／**进度显示链**（返工二轮）／neat 收尾 —— **全 PASS**。
- **代码现状（已提交并推送）**：commit **91beaf8**（21 files，+2190/−186）已 push 到 `origin/main`；工作树干净、与远端 0/0 同步。`env.err`（0 字节）仍留仓根未处理。
  - 业务代码：`app/server.py`（候选接口 + apply + rerun_old + 进度任务态与线程保护）、`app/index.html`（进度条 + 轮询容错 + 主题一次性迁移 + 折叠 + 范围二选一 + 词库折叠过滤）、`src/stage9/formatter_v2.py`（v2.7）、`app/presets/vocab/vocab-programming.json`、`app/presets/vocab/vocab-crypto.json`、`tests/selftest_v26_presets.py`、`USER_MODEL_OVERRIDE.md`（镜像）。
  - 文档：`docs/handoff/HANDOFF.md`、`docs/model/TASK-MODEL-LOG.jsonl`、`经验一句话.md`；新增报告 `docs/review/` 5 套、`docs/qa/` 5 套（含 `RERUN-PROGRESS-REWORK-QA-REPORT.md`，末附 supervisor 复检节）。
- **服务现状**：`stage0bench venv py3.12.13` 跑 `app/server.py`，**PID 12429**，`127.0.0.1:8765` 监听中（16:58:06 启动，晚于 `server.py` 16:31 的改动，即**线上已是最新后端**）。主页 HTTP 200。
- **数据现状**：live 词库 **303 条**，rev `s9-corr-v2-user-977413c8`；待审候选**高 4 中 3** 在位；污染事故已回滚（见追记 18:00）。
- **执行链/Session**：builder 走 codex（`gpt-5.6-luna`；进度显示链为 codex 新链 workspace-write，tokens 约 86177）；code-reviewer 走本窗口 subagent；qa 走 codex 新链（`gpt-5.6-luna`）；supervisor 走 codebuddy（`deepseek-v4.1-flash`，`-y` 已带）。codex 旧双终端（term_8d84 / term_5944）暂留未关（同功能续用比新开便宜）。**git 已提交（91beaf8）并 push `origin/main`。**
- **未闭环评审意见**：无 P0/P1。继承 backlog：前序四链 **16×P3** ＋ 进度显示链 **6×P3**＝**22×P3**，全部非阻塞（在各评审原文）。污染事故已回滚；qa blindness（合成数据未覆盖真实旅程）已用真实候选端到端补过。
- **docs 落盘清单（本轮新增/改了哪几个）**：`app/server.py`、`app/index.html`、`src/stage9/formatter_v2.py`、`app/presets/vocab/` 两域 v2、`tests/selftest_v26_presets.py`、`USER_MODEL_OVERRIDE.md`、`docs/handoff/HANDOFF.md`、`docs/model/TASK-MODEL-LOG.jsonl`（39 行）、`经验一句话.md`、`docs/review/` 5 套、`docs/qa/` 5 套。

---

## 二、下一步的任务

- **下一步（Next Single Action，按序）**：
  1. **用户手测验收**：开 http://127.0.0.1:8765/ 亲手点一遍「错词重跑」（跑的是最新后端）。候选高 4 中 3、默认勾高中；**先生成／阅历两类勿勾**。点完给验收结论。
  2. ~~用户给分支名 → commit~~ **已完成**：用户指定推 `main`，commit **91beaf8** 已 push 到 `origin/main`。
  3. 验收通过即视为本阶段收工；无其它未开工开发项。
- **人要拍什么板（列出来问，不问不许开工）**：
  1. **验收结论**：错词重跑实点结果 OK / 不 OK（不 OK 则按现象开新链）。
  2. ~~分支名~~ **已办**：用户定 `main`，91beaf8 已推。
  3. 可选（非阻塞，用户可暂不定）：① 22×P3 backlog 是否排期修；② 账本 `rework` 口径统一（见下）；③ `env.err` 去向（见下）。
- **待排期 backlog（非阻塞，供恢复后挑活）**：
  - 22×P3：进度显示链 6 条（details 结构不齐／status 忽略 data_root／job_id 前端不用／无候选却画绿 100%／运行中仍可改下次参数／无超时与取消）＋ 前序四链 16 条。
  - 真实规模未验：真实 whisper 转写、61 篇量级端到端重跑的耗时与长视频阶段表现（各报告均已标注「未跑，不可推断为通过」）。
  - qa 报告 O-1 提的「词库 303 vs 307 对账」仍未与用户核。

---

## 三、注意事项及相关规矩（本项目专用）

- **读盘顺序（全体系唯一，别乱）**：AGENTS → 角色卡 → 真源 `USER_MODEL_OVERRIDE.md`（模板包；本地镜像可速览，**改表只改真源**）→ 本 HANDOFF → 根 `经验一句话.md` → 任务目标放**最后**。
- **派工显式**：每派必先贴「正在调用 XX｜主用精确ID＋Runtime／备用精确ID＋Runtime」，收工必贴「XX 回来了 PASS/FAIL＋实际走主还是备」；HANDOFF 执行链与账本记同一行。
- **固定通道**：builder=codex/`gpt-5.6-luna`；planner=codex/`gpt-5.6-sol`；code-reviewer=本窗口 subagent；qa=codex/`gpt-5.6-luna`；supervisor=codebuddy/`deepseek-v4.1-flash`（非交互 **必带 `-y`**）；senior-expert=codex/`gpt-5.6-sol`（只接升级任务）。
- **超限口径**：主备均不可用即**停派找用户**，不静默扣费、不自动进 GO（GO=MANUAL_ONLY，仅用户明确说「这次可用 GO」才单次启用）。
- **不可跳** code-reviewer + qa + supervisor；跳 planner/product 需记一句原因；**结论只落 `docs/review` / `docs/qa` 报告，HANDOFF 只记状态**。
- **升级**：同一 Task 被 supervisor 累计打回 2 次自动升 senior-expert（QA 挂不算），只升当次；换模型即开新链。
- **账本**：`docs/model/TASK-MODEL-LOG.jsonl`，一行一任务，schema 锁死枚举；builder 写初版 → supervisor 校验 → TM 判结果落盘。
  - **已知口径分歧（待用户统一）**：`AGENTS.md` 字面「rework=被 supervisor 打回次数」，本仓历史行按「reviewer 返工轮次」记；记账时写明用哪种。
- **模型表**：真源为 4 列旧版、本地镜像为 5 列新版，**角色→ID 映射一致无冲突**；根模型表非用户指令被改即上报（09-11 翻转过一次）。
- **红线**：**不 push**（commit 需用户明确给分支名）；**不碰 secrets**；不改 V1.10/V2.0 封存物。
- **数据安全**：测试只用**外置 tmp ＋ 合成数据**；用户真实目录与 Obsidian 库**禁写**（只读浏览例外）；**凡调 handler 的测试，首行必须断言 `data_root` 在 tmp 下**（18:00 污染事故补丁）。
- **No-Clobber**：已发布笔记**永不覆盖**（EXISTS/CONFLICT 只判不写）；缺失 vault 不重建；user-edited = 一切字节差异。
- **服务**：`stage0bench venv python` 跑 `app/server.py`，固定 **8765**（PORT 硬编码，只能覆盖端口、不能改业务文件）；**改 `app/` 或 `src/` 后必须重启服务再验**（懒加载教训 ×2）；当前 PID **12429**，监听状态重启即丢失。
- **主题（用户投诉项）**：默认必须**浅色**（`<html data-theme="light">` ＋ 一次性迁移清掉旧版机器写入的 `v2o-theme=dark`）；**自动化禁止点真机主题开关**，验证改用「抽源码 ＋ node 桩」；不同 origin/端口是各自独立的 localStorage 区。
- **词库三铁律**：wrong ≥ 2 字；正确文本含 wrong 即删条；长 wrong 排前。上限 500（现三域 369 ＋ 用户 0，手头候选待审）。
- **收尾**：经验 / neat-freak 每阶段**只派一次**。

---

## 收尾记一笔（neat-freak）

- 首轮（18:30）：4 份 docs 加注对齐；删 5 目录 2 文件；保留 2 备份目录。
- 二轮（17:05）：仅加注、未删任何文件；RERUN 两报告加收口注记 ＋ 补迭代映射。
- 三轮（**本轮，获用户授权清理**）：**删 11 个 `__pycache__` ＋ 3 个 `.DS_Store`**，复查命令输出为空（清零）；只改本 HANDOFF 一处追记，未删任何业务/文档文件，未碰 `008林粒粒AI编程/` 其余素材。未决中 `env.err`（仓根 0 字节、已被 git 跟踪）去向待用户定 —— **未动**。

---

## 追记

## 追记 2026-09-13 13:00（reapply 收口：61篇vault全量vocab+v2.7；教训：改src必重启；No-Clobber三确认；残留2备份+43 ob测试仅数据稿）

## 追记 2026-09-13 15:30（错词重跑链：候选接口+一键apply；返工P2标记/P1 codesummary/P0 vault透传；报告review/qa各一；线上demo验过）

## 追记 2026-09-13 16:30（三件套UI：待审折叠+rerun_old二选一互斥+预置查看停用+effective_revision；报告各一）

## 追记 2026-09-13 17:10（词库折叠+过滤+空态文案+首份真实候选高4中3；报告各一）

## 追记 2026-09-13 17:40（轮询弹开bug：refresh参数化；报告各一）

## 追记 2026-09-13 18:00（测试污染正式库事故：15:16擅自导入7条+标imported+104重衍生；vault零改动；已回滚303+去标记+重启；制度补丁+两链警告）

## 追记 2026-09-13 18:30（neat收尾：4docs加注对齐；删5目录2文件；保留2备份；未决git/哈希/账本v2.7轮/16P3/旧tmp）

## 追记 2026-09-13 17:05·进度显示链收口（异步202+轮询进度条+主题浅色一次性迁移M1-M4；返工二轮：code-reviewer「返工复核二」PASS→qa复验 QA-RR-01/02/03+P1-1 全CLOSED、新增BUG=0→supervisor复检PASS、账本schema exit0；账本补L39 rework=2；服务重启8765 PID12429；6条既有P3非阻塞）

- 修复点（真源码已抽查在位）：M1 主题一次性迁移 `index.html:331-334`＋静态首帧 `<html data-theme="light">`:2；M2 202 先建表 `:1296`；M3 构造与 start 同保护 `server.py:2301-2315`；M4 停表文案分流 `index.html:1244-1249`；R3 `server.py:4307-4313` 映射仅在有回调时求值。

## 追记 2026-09-13 17:05（neat对齐：RERUN两报告加收口注记＋HANDOFF补迭代映射；仅加注未删、未改结论正文）

- 迭代映射：分段para-v2.7＋批量重排＋词库v2＋错词重跑＋三件套UI＋折叠＋轮询修复＋错词重跑进度显示，均为 Stage0-12 之后、V1.8 冻结基线外的迭代，**不进任何 STAGE*-PLAN**；唯一落盘索引＝本 HANDOFF 追记链＋`docs/review|qa/` 各链报告。`docs/pm/STAGE0-12-PLAN.md` 全部未动（pm 下零先例，照本仓既有体例记在 HANDOFF）。
- 不改只记（历史快照）：两报告内记线上 PID 72407、`count=307/rev 6536029d` 等为当时取证数字，与现状（PID 12429；303/rev 977413c8）不同，按规矩不改报告正文。

## 追记 2026-09-13（neat对齐二：删11个__pycache__+3个.DS_Store；清理获用户授权，仅删清单内系统产物，只改本 HANDOFF 一处，其余不改只记）

- 清理（已删，全部在 `.gitignore` 内）：`__pycache__` **11 个** = `app/`×1 + `src/stage1|2|3|4|5|6|7|8|9|12/`×10；`.DS_Store` **3 个** = 仓根、`docs/`、`008林粒粒AI编程/`。
- 复查：`find . -path ./.git -prune -o \( -name "__pycache__" -o -name ".DS_Store" \) -print` → **输出为空**（清零）。`008林粒粒AI编程/` 其余素材零改动；未删任何 `.py/.json/.md/.html/.sh/.log`、未删 `docs/` 任何报告。
- 只列不删：`env.err`（仓根、0 字节、已被 git 跟踪，疑似 `2> env.err` 残留）——去向待定；`docs/qa/benchmark_stage0/results/*.log`（20 个）为 benchmark 证据，保留。

## 追记 2026-09-13 17:30（git 提交并推送：用户指定推 main；commit 91beaf8「控制台：错词重跑进度显示+主题浅色迁移+候选三件套UI+词库折叠+para-v2.7+词库v2」21 files +2190/−186；`git push origin main` 65cfe2c..91beaf8；提交前扫描无密钥/.env/隐私；工作树干净、与 origin/main 0/0；未动 env.err）

## 恢复读盘（全体系唯一顺序，别乱）

1. AGENTS；2. 角色卡；3. 真源 `USER_MODEL_OVERRIDE.md`（模板包；本地镜像可速览）；4. 本 HANDOFF；5. 根 `经验一句话.md`；6. 任务目标放最后。
冲突才扩大读。

---

## Phase1 用户反馈原文（2026-09-13，下一版硬输入）

> 用户原话，逐字保留。planner 的 PRODUCT_PLAN 必须正面回应这三条；Research Reviewer 要核。

现在使用过程中，这个界面还是比较混乱的，主要存在以下问题：

1. 内容过多且缺乏展示优化
(a) 列表过长：首先完成的内容列在这儿非常长，内容太多了。
(b) 缺乏折叠机制：不能把七八十条全部怼在这个界面上给用户。可以参考市面上成熟产品的做法，比如采用滚动展示最近的二三十条，之前已完成的内容折叠起来，用户想看可以打开，不想看也可以不看。

2. 监测状态显示不一致
点击"监测"和没有点"监测"时，下面的每一条状态为什么不一样？
(a) 未点监测时：显示一堆失败或者是红色的字。
(b) 点击监测后：依然是一堆东西在上面，之前完成的也在上面。
不管点没点，不应该都统一显示当前任务的状态（比如已完成哪些、哪些还在排队）吗？为什么两个状态会不一样呢？

3. 缺少清空列表功能
应该有清空已完成列表的功能。我看现在这个功能好像没有做，或者做好了也没有发挥作用。

建议参考一下市面上成熟产品是怎么做的。

### 事实核对（TM 只读代码，2026-09-13，供 planner 起点；细节以代码为准）

- **清空功能"已存在但与期望不符"（对第 3 条的更正）**：`app/index.html:211` 有按钮「清空本目录任务」→ 弹窗 `:273`「清空本目录任务（先看清代价）」，选项只有「只清失败」(`:278`) 与「全部清空」(`:279`)；后端有只读代价预览 `_clear_plan`（`app/server.py:1248`）与 `_handle_clear_post`。**没有"只清已完成"选项**，入口也不显眼 → 用户感知为"没做/没生效"。
- **列表全量渲染（对第 1 条）**：`app/index.html:1396` 取数必带 `limit=200`，77 条一次全铺进表格；无折叠、无分页、无"只看最近 N 条"。与用户诉求（最近二三十条 + 已完成折叠）直接冲突。
- **状态口径不统一（对第 2 条）**：状态文案由 `statusCN(r, cur)`（`app/index.html:355`）产出，依赖运行态（`worker.current` / running）。未监听与监听下同一任务可能呈现不同文案与颜色（含失败红字），缺一条统一的"任务状态机"口径。

- Phase1 状态：`PROJECT_PHASE=PLAN`、`PLAN_GATE=IN_PROGRESS`；planner(Sol) 第 1 轮已在跑（初稿 brief 未含本节反馈，需在下一轮修订中并入）。

### 追加反馈二（2026-09-13，布局；同样交 planner 并入 PRODUCT_PLAN）

> 用户原话，逐字保留。

还有当前这个布局，我觉得也非常不合理：

1. 左侧流程占地过大：
为什么左侧从上到下的这个流程占了那么大的空间？这很有必要吗？我觉得应该放到最顶上，做成薄薄的一横条就可以了。现在放到左上角，占了一大块空间，空间利用率不好。

2. "重跑"措辞位置不当：
"重跑"这个措辞放在了最下面，那不是用户要翻半天才能翻到最底下去吗？这样也不对呀。

所以，布局应该符合用户的操作逻辑，即从上到下、从左到右。尽量让用户在视线范围内，就能把这个功能全部进行操作。比如说最左边就。我觉得也需要让这个 planner 看怎么优化一下。

#### 事实核对（TM 只读代码，2026-09-13）

- **第 1 条属实**：主布局是三列栅格 `main{grid-template-columns:220px 1fr 300px}`（`app/index.html:33`）——左列 **220px** 是「流水线磁带」（`<section>` :176-181 / `.tape` CSS :37），**纵向铺满全高**；中列 1fr 是「处理任务」；右列 300px 是词库/待审等。用户诉求：把左侧纵向流程改成**顶部一条横向细条**，把 220px 让给内容。
- **第 2 条属实**：`app/index.html:702-708` 注释写明「重跑（应用新词库）收进详情『高级』」——即「应用新词库重跑」被藏在 **详情区 → `<details>高级`** 里，而详情区「选中任务详情」位于页面**最下方**（表格之后），用户须滚到底再展开才找得到。另有 `:253`「全部应用新词库重跑」（在词库区）、`:578`「重试全部失败（N个）」（表格上方）——**同族动作散落三处**，无统一入口。
- 用户诉求总纲：**布局按操作逻辑自上而下、自左而右；常用操作尽量在首屏视线内可达**。planner 需给出具体优化方案（含是否改栅格、折叠阈值、动作归并到哪）。

### Phase1 用户决策记录（2026-09-13）

- **HD-1 = A（用户答复「a」）**：下一版**先解决 16 个失败**（失败根因归类 + 批量诊断 + 差异化恢复为主线）；**22 条 P3 只做支撑主线的与低成本项**，不单独排期清 backlog。与 planner 建议一致，属用户已拍板。
- 待办：该决策需并入 `docs/pm/PRODUCT_PLAN.md` 的 `Human Decisions Needed` / P0-P1 优先级 / Readiness 自评（在 planner 下一轮修订时落）。
- 其余 4 个 HD（HD-2 诊断导出形式 / HD-3 自动恢复边界 / HD-4 是否支持其他笔记软件 / HD-5 真实验证样本边界）**用户尚未答复**，等 Research Reviewer 第 1 轮结论后一并提交。
- **HD-3 = A（用户「其他的按照建议来」）**：自动恢复只覆盖"规则确定、安全可恢复"的类别；未知/环境/媒体类失败留人工确认。
- **HD-4 = A（同上）**：v2 继续 Markdown + Obsidian，不加其他笔记软件专有适配。
- **HD-5 = A（同上）**：允许只读查看真实 16 条失败证据，并把少量代表性失败视频复制到外置 tmp 做真实 whisper 验证；**用户真实目录与 Obsidian 库仍零写**。
- **HD-2 = A（用户 2026-09-13：「那就按 A」）**：页面可看 + 一键复制**脱敏**摘要（真实绝对路径打码，如 `…/第七周/xxx.mp4`）；不做完整路径导出。
- **五项 HD 全部拍板完成**（HD-1=A / HD-2=A / HD-3=A / HD-4=A / HD-5=A），需在 planner 下一轮全量并入 PRODUCT_PLAN 与 Readiness 自评。
- **HD-6 = A（用户 2026-09-13 拍板）**：已完成列表默认显示**最近 20 条**，更早的默认收起，总数与展开入口常显。
- **HD-7 = A（同上）**：「清空已完成列表」= **只从当前列表归档/隐藏，可在历史恢复**；稿件、任务记录、中间产物一律不删。破坏性清理仍留"高级"入口并二次确认。
- **HD-8 = A（同上）**：任务状态**与监听开关脱钩**——同一快照在未监听/监听中/停止收尾三种系统态下，逐 run_id 的状态文字与颜色不变；"监听中"单独一处显示，worker 只细化当前 ACTIVE 阶段。
- **HD-9 = A（同上）**：顶部流程条为**普通横条**（向下滚动自然离开视野），不做 sticky、不做可收起（若日后实测需要再升级）。
- **九项 HD 全部拍板完毕**（HD-1..HD-9 均 = A），等 Research Reviewer 第 1 轮结论后，由 planner 在下一轮**全量并入** PRODUCT_PLAN 与 Readiness 自评。

### 追加反馈三（2026-09-13，复制失败原因导致页面被文字占满；同样交 planner 并入 PRODUCT_PLAN）

> 用户原话：为什么我点"查询失败原因"后，它就卡在这儿了？有一些失败的记录，我点"查询失败原因"，它就开始卡在这儿，也没办法缩小，或者我不想看它了也不行，我不知道怎么处理它。
> 用户截图：`/var/folders/mp/.../orca-paste-1789294139055-...png`（页面被一大段"已复制：V2O 失败原因清单（共 16 个）1. …"文字占满，无关闭入口）

#### 事实核对（TM 只读代码，2026-09-13）——**这是真 BUG，不是误操作**

- **根因**：`app/index.html:1009` `copyText(t)` 复制成功后调用 `say("已复制："+t,"ok")`；而 `say()`（`:987`）把这段字符串**整段塞进状态提示行 `#msg` 的 textContent**。用户复制的是「复制全部失败原因」的**16 条长文本**，于是提示行被撑成整屏文字。
- **为什么"关不掉、缩不小"**：`say()` 写入的 `#msg` **没有截断、没有关闭按钮、没有超时清除**（对比 `toast()` `:988` 有 `max-width:360px` + 3 秒自动消失 + 点击可关，但这里没用 toast）。
- **正确做法（下一版）**：复制成功的提示只能是一句短话（如「已复制 16 条失败原因」）；长文本不得进状态提示行。若要看长文本，应给**可关闭的面板/抽屉**。
- **用户自救**：按 `Cmd+R` 重新加载页面即可清除（该文字只存在页面内存中，未持久化）。

#### 副产品：16 个失败的真实根因证据（对 P0-1 与 Research Reviewer Required Fix #1 直接有用）

- 截图与接口数据显示：这 **16 条失败原因高度同质**，全部是「**源视频文件找不到了：<原文件名>**，下一步：检查视频是否被移动或删除，补回后点重试」，且**全部归属 `…/葫芦军师`**（该目录 成功 8 / 排队 16）。
- 含义：计划里"按 8 类根因归类"的假设**很可能是错的**——至少这 16 条是**同一类**（源文件缺失）。planner 的失败分类矩阵必须用这批真实证据校正，并回答"这类失败是否根本不该算『转写失败』，而是『源文件已不在』"。

### 追加反馈四（2026-09-13，产品命名；**不需 planner/reviewer 讨论，已由用户直接拍板**）

- **决策**：产品名从 `V2O 本机控制台`（V2O = Video2Obsidian 缩写，字母 O 易被误读成数字 0）改为 **「懒得笔记」**。
- **用户原话**：网页的名字应该改一下吧？什么叫"V20 本机控制台"？你们取个名字吧，好记一点。→ 用户自选 **懒得笔记**。
- **命名含义（TM 理解，供文案参考）**：懒得记笔记 → 工具替你记；有梗、好记、一看就懂。
- **落地范围（v2 待改，Phase1 禁改代码）**：`app/index.html` 的 `<title>`（现「V2O · 本机控制台」）与页眉 `<h1>`（现「V2O 本机控制台」）；`.tape::before` 的「V2O · 本机磁带」文案；`README.md` / `README.en.md` 标题；建议配副标题「懒得笔记 · 本地视频自动转文字」。
- **不在本次范围**：GitHub 仓库名（现 `wanghoufan/Video2Obsidian`）与技术标识符（`v2o-*` 的 localStorage 键、日志前缀、`v2o-console-data` 数据目录）——改这些会破坏兼容/数据路径，如需再单独议。
- 由 TM 直接办，**不派 planner / product-reviewer**（用户明确说"不用他们讨论"）。

### Phase1 收束（用户指令，2026-09-13）

- **用户原话**：可以了 到这一轮给我看一下 不要继续讨论了。
- **执行**：**停止 planner↔Research Reviewer 循环**，不再开新的评审轮；V1.3 修订轮（已在跑）让其写完即止（避免打断写坏文件），**不再派第 3 轮复审**。
- **Gate 状态如实记录**：**Readiness 未达 90**（planner 自评 V1.2=89/100；Reviewer 第 2 轮独立打分 **83/100**，结论 FAIL）。本次进入人工审阅属**用户主动提前看**，**不是 Gate 通过**，不得记为 APPROVED。
- **已完成轮次留痕**：V1.0(76) → V1.1(87, 12条全改) → V1.2(89, 真实16行矩阵+分类8→7+反馈A/B/C入计划) → V1.3(在跑) ；评审 2 轮：第1轮 FAIL(12条)、第2轮 FAIL(11/12到位+新增RF13~16, 独立83)。
- **未闭环项（留给开发阶段验，属用户已知）**：Reviewer RF13~16（替代路径核验/页面-DB provenance/双轴分类/反馈A与改名边界）在 V1.3 中处置中；旧恢复入口六项契约、真实性能实测、UI 原型本质需 Phase2 才能验。
- **下一步**：TM 出一份**人话版计划总览**交用户过目；用户拍板后才谈"第二阶段，开发"。

### 16 条"失败"真相（TM 独立核实，2026-09-13，**已闭环，非工具故障**）

- **结论**：页面那 16 条红色"失败"**不是转写失败**——是**用户自己把那批视频移出了监听目录**。
  - 登记路径：`/Users/zzymima0000/Downloads/需转录视频/葫芦军师/`（现在只剩 **8** 个文件 = 8 个成功任务）
  - 实际文件：`/Users/zzymima0000/Downloads/暂不转录视频/葫芦军师/`（**16/16 全部找到，文件大小逐条一致**）
- **核实方式（只读）**：只读 `data/state.db`（拷贝到 tmp 后查询，用完已删）取 `processing_runs`×`sources`，筛 `葫芦军师` 共 **24** 条 → 8 条路径存在、16 条不在；再按 basename 在 `Downloads`/`Volumes` 全盘查找 → **16/16 命中**「暂不转录视频」目录，`source_size` **16/16 一致**。
- **DB 侧真相**：这 24 条的 `processing_runs.status` **全部为 `QUEUED`**、`sources.status` 全部 `ACTIVE` —— **数据库从未记录过"失败"**；页面的红字是**运行时找不到源文件**导致的显示层映射。
- **产品含义（v2 要改）**：应把这类区别于"真失败"——标为「源文件不在原位（可能被你移走）」，**保留可见**（别静默消失），但**不计入"可自动重试失败"**、不进批量重试；并提示"若已移到别处，请重新指定目录"。
- **可立即自查的事实**：`Downloads/暂不转录视频/葫芦军师/` 在**监听目录之外**，所以工具看不到它们；把该文件夹移回 `需转录视频/` 下（或把监听目录改到 `Downloads`）即可被重新发现。
- **额度事件记录**：Phase1 循环消耗较大（planner 单轮 13.6 万→42.5 万 token，reviewer 单轮 10.9 万→54.6 万 token），用户反映额度被耗尽；TM 已**停止全部外派**，并中止 planner 第 5 轮（V1.3 正文已写、尾部 Readiness/未验证项段仍为 V1.2 旧文本，**未完成收尾**）。

### 用户对 Phase1 计划的评价（2026-09-13，重要信号，供下一轮规划）

- **用户原话**：就这样 说实话似乎改的不多 产品功能上也没有参考别的产品来完善优化。
- **用户决定**：Phase1 **就此收束**，不再推进、不再开评审轮、不进入开发（未说"第二阶段，开发"）。
- **TM 认账（不辩解）**：本版计划本质是「**治病**」而非「**长身体**」——
  - 范围被 HD-1=A 与用户四条反馈锁定为：分清失败类型 + 批量恢复闭环 + 界面收拾 + 改名 + 22 条 P3 取舍；
  - 计划里**大量是"看不见的工程活"**（双轴分类、dry-run/plan token、四层状态与 provenance、快照/指纹/并发安全、cursor 分页、脱敏），**用户可见的新功能确实少**；
  - Research Reviewer 虽引了 26 条外部来源（GitHub Actions / Todoist / Docker Desktop / HandBrake / qBittorrent / aria2 / yt-dlp / Android DownloadManager / Apple / MLX 等），但**只用于"验证设计对不对"，没有用于"找别人有什么功能可以抄"**——用户这条批评**成立**。
- **下一轮规划应做（用户若再提"第一阶段，计划"时）**：以「**功能对标 / feature gap**」为主题——对标同类本地转写与笔记工作流产品（如 MacWhisper、Aiko、whisper.cpp GUI、Obsidian 相关插件等），产出「**别人有、我们没有**」的功能清单，由用户挑要哪些；而不是再打磨现有链路的工程细节。
- **当前状态**：Phase1 停在 V1.3（正文已写、尾部 Readiness/未验证项为 V1.2 旧文本）；PROJECT_PHASE=PLAN、PLAN_GATE=IN_PROGRESS；**未提交、未推送**；无外派在跑。

---

## 收尾记一笔（neat-freak，2026-09-14本轮）

- 范围：只改对应 docs 原文加注＋本节＋清系统产物；未改业务代码，未 commit/push，未碰 secrets，未碰 `008林粒粒AI编程/`。
- 报告核对（结论/计数/引用行号 vs 当前工作树 `M app/index.html 181/31＋M app/server.py 28/3`，基于 b94845e 未提交）：
  - `docs/review/P0-3-STATE-ENTRY-REWORK2-REVIEW.md`（PASS）：`publishOnlyRetry:1098` 在约 1093-1137 内 ✓；三处接线（首屏 1821／行级 658／详情 752）✓；批量三入口全文 0 命中 ✓；`obhint:741` 文案与 P2-1 描述逐字一致 ✓；`btnCandidateApply` title 已改用户语言（:251）✓；`rerun_old` confirm「转写0次」在 1396 行前后 ✓；index `181/31` 与工作树一致 ✓。server 记 `+19/−3` 属返工前 carryover 口径，无新增后端改动断言成立。
  - `docs/review/P0-3-SEMANTICS-FIX-REVIEW.md`（PASS）：`_FAIL_SEMANTICS:1342` ✓；`_scan_disk_states:660`、docstring `:664-665`、失败映射分支 `:713-716` ✓；`display_persisted_mismatch` 唯一写点 `:1373` ✓。**不一致 1 处**：报告记 server `+19/−3`，工作树实测 `28/3`，差 9 行插入（疑 provenance 字段计数口径差）——已在该文件尾加注，正文未动，待 supervisor 裁定。
  - `docs/qa/P0-3-STATE-ENTRY-RETEST2-2026-09-14.md`（FAIL，`QA-P03-RR2-001` OPEN）：当时快照无误；该 BUG 已在语义修复链 CLOSED（`docs/qa/P0-3-SEMANTICS-FIX-QA-2026-09-14.md`）——已在该文件尾加注链路，正文未动。
  - `docs/qa/P0-3-SEMANTICS-FIX-QA-2026-09-14.md`（PASS，`QA-P03-RR2-001` CLOSED）：断言输出（`AUTO_PUBLISH/PUBLISH_ONLY/eligible=1`、真 mismatch 仍 `NEEDS_HUMAN`、哈希 `3a88…19b` 前后不变）均为当时取证值，未复跑；静态回归项与当前工作树一致（批量 0 命中、文案/三态/面板/窄屏均在位）。未改动该文件。
  - `docs/qa/P0-3-STATE-ENTRY-QA-2026-09-14.md` 尾部 supervisor 复检节（打回 1/2）：历史快照，只加注不改正文——当时 `+112/−11`、账本 46／派工 24，当前已为 `+209/−34`；当时行号 `:605/:270/:742` 为旧位置，旧三入口现 0 命中；P0-3 本链打回计数仍为 `1/2`（语义修复链未新增 supervisor 打回）。
- 清理：删仓根 `./.DS_Store`＋`docs/.DS_Store` 共 2 个；`__pycache__` 全仓本就为空；复查 `find -name __pycache__ -o -name .DS_Store` 输出为空。未删任何业务/文档文件；`env.err`、benchmark `results/*.log` 未动（按要求保留）。
- 未决（P0-3 未收口，差 supervisor 复检；不写收口/完成）：① supervisor 复检语义修复链（含 server `28/3` vs `+19/−3` 计数口径裁定）；② `TASK-MODEL-LOG`／`DISPATCH-LOG` 语义修复链四行（builder/reviewer/qa/语义builder）落盘与 `rework` 口径（仍 1/2）由 TM 定；③ `HANDOFF.md` 工作树 `M`（TM 本轮落盘改动）与四份新报告均未提交未推送，是否 commit/push 待用户给分支名；④ QA 真 mismatch 夹具曾用 `FAILED_RETRYABLE`（旧报告）vs 现实枚举 `*_FAILED` 家族，口径差已记不拦。

---

## 收尾记一笔（neat-freak，2026-09-15 P1 收官本轮）

- 范围：只做治理与加注（三份报告尾部加注 ＋ 本节）＋ 清系统 tmp 产物；**未改业务代码**（`app/`、`src/`、`tests/` 零改动）、**未改任何报告正文结论**、未 commit/push、未碰 secrets、未碰 `008林粒粒AI编程/`、未碰用户真实目录（`~/Downloads/需转录视频`、`~/Downloads/暂不转录视频`、`~/Documents/ob 仓库`）。
- **清理（系统 tmp，先列清单后删）**：用户点名的族 `p11-*｜p11_*｜p11fix_*｜p16_*｜p18_*｜sup_*｜ux2_*｜v2o-*` **147 项 / 5.61 GB**；同族扩展（同一批探针/夹具，仅分隔符差：`p11b-*`、`p11sup_*`/`p11sup-*`、`p11fix-*`/`p11fix2_*`、`p18sup|rev|rev2|-pyc`、`v2o_*`）**98 项 / 1.19 GB**。**合计 245 项 / 约 6.80 GB 已删**，复查 0 命中；`/tmp` 由约 9.6GB 降至 **2.8GB**。删除前已确认**无任何进程持有**（`lsof` 零命中；8765=PID 6586、8766=65597 仅监听，未触碰）。
- **保留（未动）**：`/tmp/settings.json.bak-20260915`（分类器配置备份）、`/tmp/win_plan_prompt.md`、`/tmp/settings.json` 现网配置。
- **只列不删（未在用户清单内，同属 tmp 但属更早批的残留，合计 136 项 / 0.07 GB）**：`p12_*|p13_*|p14_*|p15_*|p17_*|p15rev2*|p13rev*|p14rev*`（P1-2…P1-7 各链探针/夹具）＋ `p1-2*|p1_7*|p1-7*|p1.json` ＋ `supervisor2.log`。理由：用户清单只点名 `p16_*`／`p18_*` 两支更早批，未点名 p12–p15/p17 支，按「拿不准先列」处理——需要清的话下一轮说一声即可（体积可忽略）。
- **仓库卫生**：删仓根 `.DS_Store` ＋ `app/`、`src/stage1,2,3,4,5,6,7,8,9,12/` 共 **11 个 `__pycache__`**（全部在 `.gitignore` 内，由本轮服务/测试运行产生，mtime 21:47–22:02）；复查 `find`（排除 `.venv`）**输出为空**。`.venv/` 内 `__pycache__` **未删**（属已装依赖的正常运行产物，非仓内残留，删了无收益且可能影响用户在跑的环境）。
- **文档一致性加注（各在文件尾部追加一节，原文与结论一字未动）**：
  - `docs/qa/P1-1-SCALE-QA-2026-09-15.md` §收尾注记：**一致项**＝`:195` 自证「原 189 行 / 12,964 字节 / sha256 `d7b33a5d…ce624`」，`head -n 189` 实测**逐字一致**（含字节数）；**差异 5 处**＝① gate 落点 `:4714/:4916` 实为注释行，判定调用在 `:4716/:4918`（`initial_publish :4933` 一致）② §S7 账本 92→**93** 行、`TASK-MODEL-LOG` 55→**56** 行且**仍无独立 `P1-1` 行**（S10 条件 1 只部分办到）③ §S8/§S10 条件 2 已闭合（`README.md:110/:116`、`README.en.md:110/:116`、本文件 `:58`）④ §S9「工作树 8 个 `M`」已被 `ac3ecd8` 覆盖 ⑤ 首轮节 `:32`/`:151` 为历史快照（现 `watcher.py:84-87`、周期兜底已接线、契约 571/rc0）。
  - `docs/review/P1-1-FIX-WATCHER-CODE-REVIEW.md` §收尾注记：**一致项**＝返工节 `:170` 六文件 numstat 以 `git diff --numstat 7b97aaa ac3ecd8` 实测**逐项一致**（+1111/−30）；**差异 4 处**＝① 首轮节 `:45/:123/:132/:136/:148` 的 `watcher.py:278-291|:251-259|:284` 与 `_stability`/`_flush_one` 返工后**全仓零命中**（现 `:284 _flush_loop → :294 _flush_batch`；`reconcile.py` 漂到 `:221/:249/:257/:260`）② 半截边界 `:208` 的 6.5–7.5s 应以 qa §S2 的 **6.5s 安全 / 7.0s 起投** 为准 ③ `:242`/`:248` 的「vault 安全」**已被 §S4 真机证伪**，P2-a/P2-b 合并裁定见本节 `:58` ④ `:4`/`:170` 的「未提交／基线 `7b97aaa`」已随 `ac3ecd8` 提交推送。**未办**：`:74`/`:139`（P3-5）请 neat-freak 给 `STAGE5-PLAN.md:47`、`STAGE5-QA-REPORT.md:24/:52/:65` 加注 → 见下未决。
  - `docs/pm/WINDOWS-MIGRATION-PLAN.md` §收尾注记：**一致项**＝§1/§2/§3 全部行号引用（`asr.py:49-58`/`:178-190`、`transcribe.py:80-93`、`transcribe_chunks.py:149-167`/`:109-137`、`prompt_builder.py:26-39`、`watcher.py:71-90`、`instance.py:24`/`:79-108`、`volume_probe.py:10-27`、`reliability.py:132-174`、`server.py:5644-5683`/`:3042-3065`、`index.html:576`、`asr.py:111-156`、`ingest.py:77`）**逐项命中、引用文件均存在**；**差异 2 处**＝① 端口 `app/server.py:54-70` 实为 **`:54-68`**（与 `:50`/`:87` 一致）② §3 写 `stage12 status_cli/snapshot`，实际文件名是 **`status_snapshot.py`**（无 `snapshot.py`）。
- **未决（不写收口/完成）**：
  1. `docs/review/...WATCHER-CODE-REVIEW.md:74`/`:139` 的 **P3-5 请求未执行**：`docs/pm/STAGE5-PLAN.md:47` 与 `docs/qa/STAGE5-QA-REPORT.md:24/:52/:65` 的 A5「抖动文件仍 WAITING」口径在 `P1-1-FIX` 后已变为「首投 `PROMOTED`」（reviewer 探针 4 实测），至今**无注记**——本轮授权范围只含三份报告，故只记不办，待用户/TM 一句话即可补。
  2. `TASK-MODEL-LOG.jsonl` **缺独立 `P1-1`（首轮 qa）任务行**；`DISPATCH-LOG.jsonl` 已 93 行。
  3. 本轮 tmp 清理后，三份报告引用的 `/tmp/**` 取证路径**已不可复跑**（结论未变，仅取证可复现性下降）。
  4. 更早批 tmp 残留 136 项 / 0.07 GB 只列未删（见上）。
